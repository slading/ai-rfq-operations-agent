"""The SQLite connection policy: foreign keys, WAL, busy timeout, sessions.

SQLite disables ``PRAGMA foreign_keys`` on every new connection by default. A
schema full of ``FOREIGN KEY`` clauses and a connection that ignores them is the
worst of both worlds: referential integrity that looks present in the models,
the migrations and the reviews, and is silently absent at runtime. These tests
exist because that failure is invisible without them - and they check real
violations, not just the pragma value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rfq_agent.config import DatabaseSettings
from rfq_agent.persistence import (
    SQLITE_PRAGMAS,
    Database,
    build_engine,
    is_memory_sqlite,
    session_scope,
    sqlite_pragmas,
)
from rfq_agent.persistence.models import RfqAttachmentRow, RfqRow
from tests.persistence.factories import NOW, SHA256, rfq_row


def test_foreign_keys_are_enabled_on_every_pooled_connection(db: Database) -> None:
    """Each new connection gets the pragma - not just the first one."""
    with db.engine.connect() as first:
        assert first.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with db.engine.connect() as second:
        assert second.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_foreign_key_violation_is_rejected_on_insert(session: Session) -> None:
    """A child row pointing at a non-existent parent cannot be written."""
    session.add(
        RfqAttachmentRow(
            attachment_id="ATT_0001",
            rfq_id="RFQ_DOES_NOT_EXIST",
            filename="spec.pdf",
            content_type="application/pdf",
            byte_length=10,
            sha256=SHA256,
            parsed=False,
            text_preview=None,
        )
    )

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.commit()
    session.rollback()


def test_foreign_key_violation_is_rejected_on_update(session: Session) -> None:
    """Re-pointing an existing row at a non-existent parent is refused too."""
    session.add(rfq_row())
    session.commit()

    row = session.get(RfqRow, "RFQ_0001")
    assert row is not None
    row.status = row.status  # touch nothing; the check below is the real subject
    session.commit()

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.execute(
            text("UPDATE rfqs SET superseded_by_rfq_id = :other WHERE rfq_id = :rfq"),
            {"other": "RFQ_MISSING", "rfq": "RFQ_0001"},
        )
    session.rollback()


def test_journal_mode_is_wal(connection) -> None:
    """WAL lets the run worker write while the UI reads traces."""
    assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL


def test_busy_timeout_is_configured(connection) -> None:
    """Concurrent writers wait for the lock instead of failing instantly."""
    assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5_000


def test_pragmas_follow_configuration(tmp_path: Path) -> None:
    """The pragma tuple is derived from settings, so the env vars are not decorative."""
    custom = DatabaseSettings(busy_timeout_ms=1234, journal_mode="MEMORY", synchronous="OFF")
    pragmas = dict(sqlite_pragmas(custom))

    assert pragmas["busy_timeout"] == "1234"
    assert pragmas["journal_mode"] == "MEMORY"
    assert pragmas["synchronous"] == "OFF"

    engine = build_engine(f"sqlite:///{tmp_path / 'custom.db'}", database_settings=custom)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 1234
    finally:
        engine.dispose()


def test_the_listener_is_what_enforces_foreign_keys(tmp_path: Path) -> None:
    """Without the connect listener, SQLite would ignore the FK entirely.

    This is the negative control for the test above: it proves the enforcement
    comes from our connection policy rather than from the database file.
    """
    url = f"sqlite:///{tmp_path / 'no-pragmas.db'}"
    engine = build_engine(url, pragmas=())
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    finally:
        engine.dispose()


def test_foreign_keys_cannot_be_disabled_through_settings() -> None:
    """``RFQ_DATABASE__FOREIGN_KEYS=false`` fails at startup, not silently later."""
    with pytest.raises(ValidationError):
        DatabaseSettings(foreign_keys=False)  # type: ignore[arg-type]


def test_memory_database_uses_a_static_pool_and_skips_wal() -> None:
    """An in-memory database is shared by one connection and cannot use WAL."""
    database = Database.create("sqlite://")
    try:
        assert is_memory_sqlite(database.url)
        assert isinstance(database.engine.pool, StaticPool)
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() != "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        database.dispose()


def test_file_database_uses_a_real_pool(tmp_path: Path) -> None:
    """File-backed databases get a pool, so the worker and the UI share handles."""
    database = Database.create(f"sqlite:///{tmp_path / 'pooled.db'}")
    try:
        assert not isinstance(database.engine.pool, StaticPool)
        assert not is_memory_sqlite(database.url)
    finally:
        database.dispose()


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    """A fresh clone can run migrations before anything has created ``var/``."""
    target = tmp_path / "nested" / "deeper" / "rfq.db"
    database = Database.create(f"sqlite:///{target}")
    try:
        with database.engine.connect():
            pass
    finally:
        database.dispose()

    assert target.parent.is_dir()
    assert target.exists()


def test_sqlite_pragmas_constant_documents_the_defaults() -> None:
    """The published constant matches the default settings, field for field."""
    assert dict(SQLITE_PRAGMAS) == {
        "foreign_keys": "ON",
        "busy_timeout": "5000",
        "journal_mode": "WAL",
        "synchronous": "NORMAL",
    }


def test_engine_is_created_from_the_configured_url() -> None:
    """``Database.create`` with no URL uses ``DatabaseSettings``, i.e. the environment."""
    settings = DatabaseSettings(url="sqlite://", echo=False)
    database = Database.create(database_settings=settings)
    try:
        assert database.url.database is None
    finally:
        database.dispose()


def test_session_scope_commits_on_success(db: Database) -> None:
    """A completed unit of work is durable."""
    with session_scope(db.session_factory) as session:
        session.add(rfq_row())
        session.add(rfq_row(rfq_id="RFQ_0002"))

    with db.session_factory() as check:
        assert check.scalar(select(func.count()).select_from(RfqRow)) == 2


def _failing_unit_of_work(factory: sessionmaker[Session]) -> None:
    """Write a row, then fail: the caller asserts nothing survived."""
    with session_scope(factory) as session:
        session.add(rfq_row())
        session.flush()
        raise RuntimeError("boom")


def test_session_scope_rolls_back_on_error(db: Database) -> None:
    """A failed unit of work leaves no partial row behind."""
    with pytest.raises(RuntimeError, match="boom"):
        _failing_unit_of_work(db.session_factory)

    with db.session_factory() as check:
        assert check.scalar(select(func.count()).select_from(RfqRow)) == 0


def test_session_factory_uses_the_documented_settings(db: Database) -> None:
    """``autoflush=False`` and ``expire_on_commit=False``, as the engine module claims."""
    factory = db.session_factory

    assert factory.kw["autoflush"] is False
    assert factory.kw["expire_on_commit"] is False


def test_objects_stay_loaded_after_commit(db: Database) -> None:
    """A committed row keeps its attributes: the worker reads what it just wrote."""
    factory = db.session_factory
    with session_scope(factory) as session:
        session.add(rfq_row())
        session.commit()
        row = session.get(RfqRow, "RFQ_0001")
        assert row is not None
        assert sa_inspect(row).unloaded == set()


def test_now_helper_is_used_for_timestamps(session: Session) -> None:
    """Row defaults are Python-generated aware UTC, not SQLite's second-resolution clock."""
    session.add(rfq_row())
    session.commit()

    row = session.get(RfqRow, "RFQ_0001")
    assert row is not None
    assert row.created_at.tzinfo is not None
    assert row.created_at >= NOW
