"""Migration tests: the schema is built by Alembic, and stays in sync with it.

The most valuable test here is
:func:`test_no_drift_between_models_and_migrations`: it compares every table,
column, index, unique and foreign-key constraint in the migrated database
against the SQLAlchemy metadata using Alembic's own autogenerate comparison. A
model edited without a matching revision - the single most common way a schema
rots - fails this test immediately.

Tests that start from an *empty* database build their own engine, because the
shared ``db`` fixture is already migrated. Tests that need an existing schema use
the ``session``/``connection`` fixtures.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Connection, Engine, create_engine, inspect, text

from rfq_agent.persistence import build_engine
from rfq_agent.persistence import models as _models  # noqa: F401  (registers metadata)
from rfq_agent.persistence.base import Base
from tests.persistence.conftest import make_alembic_config

#: Tables Alembic owns and the models do not declare.
_INTERNAL_TABLES = frozenset({"alembic_version"})


@pytest.fixture
def empty_path(tmp_path: Path) -> Path:
    """Path to a database file that does not exist yet."""
    return tmp_path / "empty.db"


@pytest.fixture
def empty_config(empty_path: Path) -> Config:
    """Alembic config pointing at that file."""
    return make_alembic_config(empty_path)


@pytest.fixture
def empty_engine(empty_path: Path) -> Iterator[Engine]:
    """An engine for the file being migrated, with the project's pragmas."""
    engine = build_engine(f"sqlite:///{empty_path}")
    try:
        yield engine
    finally:
        engine.dispose()


def _schema_objects(path: Path) -> list[str]:
    """Every named object in a SQLite file: tables, indexes and triggers."""
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT type, name, tbl_name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ).fetchall()
        return [f"{row[0]}:{row[1]}:{row[2]}" for row in rows]
    finally:
        engine.dispose()


def test_upgrade_from_empty_database_creates_every_model_table(
    empty_config: Config, empty_engine: Engine
) -> None:
    """Migrating an empty file produces exactly the tables the models declare."""
    command.upgrade(empty_config, "head")

    with empty_engine.connect() as connection:
        migrated = set(inspect(connection).get_table_names()) - _INTERNAL_TABLES

    assert migrated == set(Base.metadata.tables)
    assert len(migrated) == 26


def test_no_drift_between_models_and_migrations(connection: Connection) -> None:
    """The migrated schema and the SQLAlchemy metadata describe the same schema."""
    context = MigrationContext.configure(connection)
    differences = compare_metadata(context, Base.metadata)

    assert differences == [], f"models and migrations have drifted: {differences}"


def test_check_constraints_match_the_models_exactly(connection: Connection) -> None:
    """The built schema has exactly the ``CHECK`` constraints the models declare.

    Autogenerate does not compare ``CHECK`` constraints, so the drift test above
    would not notice one missing - or duplicated, which is what happened when
    Alembic rendered an ``Enum`` that creates its own constraint *and* an explicit
    constraint for the same list. Names and counts are both compared here.
    """
    ddl_by_table = {
        name: sql or ""
        for name, sql in connection.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        )
    }

    checked_any = False
    for table in Base.metadata.tables.values():
        expected = sorted(
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        )
        if not expected:
            continue
        checked_any = True
        found = sorted(re.findall(r"CONSTRAINT (\w+) CHECK", ddl_by_table[table.name]))
        assert found == expected, f"CHECK constraints differ on {table.name}"

    assert checked_any, "the models declare no CHECK constraints - wrong metadata?"


def test_downgrade_to_base_removes_everything(empty_config: Config, empty_engine: Engine) -> None:
    """``downgrade base`` is a clean inverse: no tables, no triggers, re-upgradable."""
    command.upgrade(empty_config, "head")
    command.downgrade(empty_config, "base")

    with empty_engine.connect() as connection:
        remaining = set(inspect(connection).get_table_names()) - _INTERNAL_TABLES
        triggers = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).fetchall()

    assert remaining == set()
    assert triggers == []

    command.upgrade(empty_config, "head")
    with empty_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) - _INTERNAL_TABLES == set(
            Base.metadata.tables
        )


def test_upgrade_is_idempotent(database_path: Path) -> None:
    """Running ``upgrade head`` against an up-to-date database changes nothing."""
    config = make_alembic_config(database_path)
    command.upgrade(config, "head")
    before = database_path.read_bytes()

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert database_path.read_bytes() == before


def test_head_revision_is_single_and_linear(empty_config: Config) -> None:
    """One head, one revision, no branch: a forked history is a merge waiting to happen."""
    script = ScriptDirectory.from_config(empty_config)

    heads = script.get_heads()
    assert heads == ["0002"]

    revisions = list(script.walk_revisions())
    assert [revision.revision for revision in revisions] == ["0002", "0001"]
    assert revisions[0].down_revision == "0001"
    assert revisions[-1].down_revision is None


def test_offline_mode_emits_sql_without_connecting(empty_config: Config, empty_path: Path) -> None:
    """``alembic upgrade --sql`` works: no connection, no driver, still correct DDL."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(empty_config, "head", sql=True)

    sql = buffer.getvalue()
    assert "CREATE TABLE rfqs" in sql
    assert "CREATE TRIGGER trg_run_events_no_update" in sql
    assert "ck_runs_terminal_requires_outcome" in sql
    # The whole point of offline mode: no database was created.
    assert not empty_path.exists()


def test_version_table_records_the_applied_revision(
    empty_config: Config, empty_engine: Engine
) -> None:
    """``alembic_version`` is the record of what was applied, not a guess."""
    command.upgrade(empty_config, "head")

    with empty_engine.connect() as connection:
        applied = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert applied == "0002"


def test_shared_test_schema_matches_a_freshly_migrated_one(
    migrated_template: Path, empty_path: Path
) -> None:
    """The shared fixture really is a fully migrated database.

    Every other persistence test runs against a copy of the session template. If
    that template were built differently from a fresh migration, all of them would
    be testing the wrong schema.
    """
    command.upgrade(make_alembic_config(empty_path), "head")

    assert _schema_objects(migrated_template) == _schema_objects(empty_path)
