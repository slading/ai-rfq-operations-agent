"""Engine, session factory and SQLite connection policy (architecture §5.1).

This module is the *only* place an engine is constructed. Everything that
matters about talking to SQLite is decided here:

* **Foreign keys are enforced.** SQLite disables ``PRAGMA foreign_keys`` by
  default, *per connection*. Without the ``connect`` event listener below, every
  ``FOREIGN KEY`` in the schema would be decorative - and the database-level
  guarantee that a quotation cannot reference a customer or product that does
  not exist would silently not hold.
* **WAL journal mode.** Readers never block the writer, which matters because
  one in-process worker writes runs while the HTMX UI reads traces.
* **``busy_timeout``.** Concurrent writers wait rather than failing instantly
  with ``database is locked``.
* **One session per unit of work.** :func:`session_scope` commits on success,
  rolls back on any exception and always closes, so a half-applied run cannot
  survive an error.

Timestamps are the one SQLite-specific subtlety handled elsewhere: see
:class:`~rfq_agent.persistence.types.UtcDateTime`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rfq_agent.config import DatabaseSettings, Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import URL

__all__ = [
    "SQLITE_PRAGMAS",
    "Database",
    "build_engine",
    "build_session_factory",
    "is_memory_sqlite",
    "session_scope",
    "sqlite_pragmas",
]


def sqlite_pragmas(
    database_settings: DatabaseSettings | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return the connection PRAGMAs implied by ``database_settings``.

    Derived from configuration rather than hardcoded, so ``RFQ_DATABASE__*``
    actually controls the connection behaviour it claims to control.
    """
    settings = database_settings or DatabaseSettings()
    return (
        # ``foreign_keys`` is typed ``Literal[True]`` in settings: there is no
        # supported configuration in which referential integrity is optional.
        ("foreign_keys", "ON" if settings.foreign_keys else "OFF"),
        ("busy_timeout", str(settings.busy_timeout_ms)),
        ("journal_mode", settings.journal_mode),
        ("synchronous", settings.synchronous),
    )


#: PRAGMAs applied to every SQLite connection under the default settings.
#:
#: ``journal_mode`` is database-scoped and persists once set, but it is
#: re-issued per connection anyway: it is idempotent, and re-issuing it is how a
#: database created by an older process gets moved to WAL.
SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = sqlite_pragmas()

#: PRAGMAs that a database may legitimately not support (in-memory databases
#: cannot use WAL: they are private to a single connection).
_MEMORY_UNSUPPORTED_PRAGMAS: frozenset[str] = frozenset({"journal_mode"})


def is_memory_sqlite(url: URL | str) -> bool:
    """Whether ``url`` points at an in-memory SQLite database."""
    parsed = make_url(url) if isinstance(url, str) else url
    if parsed.get_backend_name() != "sqlite":
        return False
    if parsed.database in (None, ""):
        return True
    if parsed.database == ":memory:":
        return True
    return "memory" in parsed.query.get("mode", "")


def _sqlite_file_path(url: URL) -> Path | None:
    """Return the on-disk path of a file-backed SQLite URL, if any."""
    if url.get_backend_name() != "sqlite" or is_memory_sqlite(url):
        return None
    database = url.database
    return Path(database) if database else None


def _apply_sqlite_pragmas(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
    pragmas: tuple[tuple[str, str], ...],
    is_memory: bool,
) -> None:
    # The signature is fixed by SQLAlchemy's ``connect`` event.
    """Apply ``pragmas`` to a freshly opened DBAPI connection."""
    cursor = dbapi_connection.cursor()
    try:
        for name, value in pragmas:
            if is_memory and name in _MEMORY_UNSUPPORTED_PRAGMAS:
                continue
            # Pragma names and values come from the frozen SQLITE_PRAGMAS tuple
            # and from validated settings - never from user input.
            cursor.execute(f"PRAGMA {name}={value}")
            if name == "journal_mode":
                # journal_mode returns a row; consume it so the connection is
                # not left with an unread result set.
                cursor.fetchone()
    finally:
        cursor.close()


def build_engine(
    url: str | URL | None = None,
    *,
    echo: bool = False,
    database_settings: DatabaseSettings | None = None,
    pragmas: tuple[tuple[str, str], ...] | None = None,
    create_parent_directory: bool = True,
) -> Engine:
    """Create an engine for ``url`` with the project's SQLite policy applied.

    Args:
        url: Database URL. Defaults to ``database_settings.url`` (which in turn
            defaults to the configured ``RFQ_DATABASE__URL``).
        echo: Log emitted SQL. Useful when reading a failing migration.
        database_settings: Settings supplying the default URL and pragma values.
        pragmas: Override the connection pragmas. Tests use this to prove the
            listener is what enforces foreign keys.
        create_parent_directory: Create the parent directory of a file-backed
            SQLite database, so a fresh clone can run migrations immediately.

    Returns:
        A configured :class:`~sqlalchemy.engine.Engine` with no connection open.
    """
    resolved = make_url(url) if url is not None else None
    if resolved is None:
        resolved = make_url((database_settings or DatabaseSettings()).url)

    is_memory = is_memory_sqlite(resolved)

    if create_parent_directory:
        file_path = _sqlite_file_path(resolved)
        if file_path is not None and file_path.parent != Path():
            file_path.parent.mkdir(parents=True, exist_ok=True)

    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {}
    if resolved.get_backend_name() == "sqlite":
        # One worker thread plus request handler threads share the pool.
        connect_args["check_same_thread"] = False
    if is_memory:
        # Every new connection to ":memory:" would otherwise get its own empty
        # database, which is never what the caller means.
        engine_kwargs["poolclass"] = StaticPool
        connect_args.pop("check_same_thread", None)

    engine = create_engine(
        resolved,
        echo=echo,
        future=True,
        connect_args=connect_args,
        **engine_kwargs,
    )

    if resolved.get_backend_name() == "sqlite":
        effective = pragmas if pragmas is not None else sqlite_pragmas(database_settings)
        event.listen(
            engine,
            "connect",
            lambda connection, record: _apply_sqlite_pragmas(
                connection, record, effective, is_memory
            ),
        )
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine``.

    ``autoflush=False`` keeps writes explicit (a repository decides when to
    flush) and ``expire_on_commit=False`` lets a caller keep reading an object
    after its transaction closes - both are appropriate for a request/worker
    process that finishes work immediately after committing.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Run one unit of work: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class Database:
    """An engine plus its session factory - the persistence entry point.

    Construct via :meth:`create` in application code, or directly with a
    temporary URL in tests.
    """

    engine: Engine
    session_factory: sessionmaker[Session]

    @classmethod
    def create(
        cls,
        url: str | URL | None = None,
        *,
        echo: bool | None = None,
        settings: Settings | None = None,
        database_settings: DatabaseSettings | None = None,
    ) -> Database:
        """Build a :class:`Database` for ``url`` (default: configured URL).

        ``settings`` supplies the whole configuration; ``database_settings``
        overrides just the persistence section. At least one should be passed in
        application code - the fallback below exists so a test can construct a
        throwaway database without loading the environment.
        """
        effective = database_settings
        if effective is None:
            effective = settings.database if settings is not None else DatabaseSettings()
        engine = build_engine(
            url,
            echo=echo if echo is not None else effective.echo,
            database_settings=effective,
        )
        return cls(engine=engine, session_factory=build_session_factory(engine))

    @property
    def url(self) -> URL:
        """The URL this database was created for."""
        return self.engine.url

    def dispose(self) -> None:
        """Close every pooled connection. Safe to call more than once."""
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session inside a committing unit of work."""
        with session_scope(self.session_factory) as session:
            yield session
