"""SQLAlchemy DBAPI close faults are detected without logging exception data."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from sqlalchemy import event

from rfq_agent.persistence.engine import Database, build_engine, build_session_factory

_MARKER = "dbapi-close-sensitive-marker"


class _CloseFault:
    """DBAPI connection proxy whose actual close operation raises."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __getattr__(self, name: str):
        return getattr(self.connection, name)

    def close(self) -> None:
        raise sqlite3.OperationalError(_MARKER)


def test_real_dbapi_close_fault_is_sanitized_and_detected(tmp_path, caplog) -> None:
    """Disposal actually calls DBAPI close, but neither logs nor suppresses it."""
    engine = build_engine(f"sqlite:///{tmp_path / 'close-fault.db'}")
    raw_connections: list[sqlite3.Connection] = []

    def creator() -> _CloseFault:
        connection = sqlite3.connect(tmp_path / "close-fault.db")
        raw_connections.append(connection)
        return _CloseFault(connection)

    engine.pool._creator = creator
    database = Database(engine, build_session_factory(engine))
    engine.connect().close()

    with pytest.raises(RuntimeError, match="database connection cleanup failed") as raised:
        database.dispose()

    assert _MARKER not in str(raised.value)
    assert raised.value.__cause__ is None
    assert _MARKER not in caplog.text
    assert _MARKER not in caplog.records.__repr__()
    for connection in raw_connections:
        connection.close()


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("level", [logging.DEBUG, logging.CRITICAL])
def test_pool_logging_retains_namespace_and_clears_payloads(
    tmp_path, caplog, memory, level
) -> None:
    """Sanitize before handlers run, while retaining levels and safe messages."""
    database = Database.create(
        "sqlite:///:memory:" if memory else f"sqlite:///{tmp_path / 'log.db'}"
    )
    pool = database.engine.pool
    assert pool.logger.name == (
        "sqlalchemy.pool.impl.StaticPool" if memory else "sqlalchemy.pool.impl.QueuePool"
    )
    with caplog.at_level(level, logger="sqlalchemy"):
        caplog.handler.setLevel(logging.DEBUG)
        try:
            raise RuntimeError(_MARKER)
        except RuntimeError:
            pool.logger.error(
                _MARKER,
                object(),
                exc_info=True,
                stack_info=True,
                extra={"sensitive": _MARKER},
            )
        pool.logger.debug(_MARKER)
        pool.logger.info("Pool recreating")
        logging.getLogger("unrelated.application").warning("unrelated diagnostic")
    assert _MARKER not in caplog.text
    assert "unrelated diagnostic" in caplog.text
    for record in caplog.records:
        if record.name.startswith("sqlalchemy.pool."):
            assert not record.args
            assert record.exc_info is None
            assert record.exc_text is None
            assert record.stack_info is None
            assert not hasattr(record, "sensitive")
    if level == logging.DEBUG:
        assert "Pool recreating" in caplog.text
        assert "database pool operation failed; details withheld" in caplog.text
    with pytest.raises(RuntimeError, match="database connection cleanup failed"):
        database.dispose()
    database.dispose()


@pytest.mark.parametrize("memory", [False, True])
def test_real_reset_failure_invalidates_and_closes_connection(tmp_path, caplog, memory) -> None:
    """A swallowed rollback fault invalidates once and survives pool recreation."""
    database = Database.create(
        "sqlite:///:memory:" if memory else f"sqlite:///{tmp_path / 'reset.db'}"
    )
    raw_connections = []
    closed = []
    invalidated = []

    class FaultConnection(sqlite3.Connection):
        armed = False

        def rollback(self):
            if self.armed:
                raise RuntimeError(_MARKER)
            return super().rollback()

        def close(self):
            super().close()
            closed.append(self)

    @event.listens_for(database.engine, "do_connect")
    def connect(_dialect, _record, cargs, cparams):
        connection = sqlite3.connect(*cargs, **cparams, factory=FaultConnection)
        raw_connections.append(connection)
        return connection

    @event.listens_for(database.engine, "invalidate")
    def invalidated_connection(connection, _record, _error):
        invalidated.append(connection)

    handle = database.engine.connect()
    raw_connections[0].armed = True
    handle.close()  # default reset invokes DBAPI rollback and then invalidation
    assert invalidated == raw_connections == closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        raw_connections[0].execute("SELECT 1")
    with pytest.raises(RuntimeError, match="database connection cleanup failed"):
        database.dispose()
    assert _MARKER not in caplog.text
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    database.dispose()
    assert raw_connections == closed


@pytest.mark.parametrize("memory", [False, True])
def test_reset_event_failure_is_detected_without_replacing_sqlalchemy_cleanup(
    tmp_path, caplog, memory
) -> None:
    """Reset listener exceptions use the same contained SQLAlchemy error path."""
    database = Database.create(
        "sqlite:///:memory:" if memory else f"sqlite:///{tmp_path / 'event.db'}"
    )

    @event.listens_for(database.engine, "reset")
    def fail_reset(_connection, _record, _state):
        raise RuntimeError(_MARKER)

    database.engine.connect().close()
    with pytest.raises(RuntimeError, match="database connection cleanup failed"):
        database.dispose()
    assert _MARKER not in caplog.text
