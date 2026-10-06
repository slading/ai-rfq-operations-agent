"""Fixtures for the persistence tests.

Every test runs against a database built by the **real migrations**, never by
``Base.metadata.create_all``. That is deliberate: a schema built from the models
can only ever agree with the models, so a test suite using it would silently
stop checking the thing that actually runs in production - the migrations.

The schema is migrated once per session into a template file, then copied per
test. Copying a small SQLite file is far cheaper than replaying 26 ``CREATE
TABLE`` statements for every test, and keeps each test fully isolated.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.orm import Session

from rfq_agent.persistence import Database
from tests.persistence.factories import Core, seed_core

#: Repository root - ``alembic.ini`` and ``migrations/`` live there.
REPO_ROOT = Path(__file__).resolve().parents[2]


def make_alembic_config(database_path: Path | str) -> Config:
    """Return an Alembic config migrating ``database_path``.

    The URL is set explicitly rather than inherited from the environment, so the
    tests never touch the developer's ``var/rfq_agent.db`` and cannot pass by
    accident when ``RFQ_DATABASE__URL`` is set to something convenient.
    """
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fully migrated SQLite file, built once per session."""
    path = tmp_path_factory.mktemp("template") / "schema.db"
    config = make_alembic_config(path)
    command.upgrade(config, "head")
    return path


@pytest.fixture
def database_path(tmp_path: Path, migrated_template: Path) -> Path:
    """A private copy of the migrated schema for one test."""
    target = tmp_path / "rfq.db"
    shutil.copyfile(migrated_template, target)
    return target


@pytest.fixture
def db(database_path: Path) -> Iterator[Database]:
    """A :class:`Database` bound to this test's freshly migrated file."""
    database = Database.create(f"sqlite:///{database_path}", echo=False)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    """An open session for direct inserts and queries."""
    with db.session_factory() as active:
        yield active


@pytest.fixture
def core(session: Session) -> Core:
    """Seed the reference graph: master data, one RFQ and one run.

    Every operational table points at customers, products, price entries,
    locations, an RFQ or a run, so anything a test wants to write needs these
    rows first. They are inserted in dependency order, in explicit batches.
    """
    return seed_core(session)


@pytest.fixture
def connection(db: Database) -> Iterator[Connection]:
    """A raw connection, for PRAGMA checks and hand-written SQL."""
    with db.engine.connect() as active:
        yield active


@pytest.fixture
def inspector(connection: Connection) -> Inspector:
    """A SQLAlchemy inspector over this test's database."""
    return inspect(connection)
