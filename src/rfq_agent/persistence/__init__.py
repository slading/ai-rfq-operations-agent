"""Persistence layer: SQLite via SQLAlchemy 2.0, versioned by Alembic.

The public surface of this package is small on purpose:

* :class:`~rfq_agent.persistence.engine.Database` - engine plus session factory;
* :class:`~rfq_agent.persistence.base.Base` - declarative base and metadata;
* the ``*Row`` models, imported through
  :mod:`rfq_agent.persistence.models`.

Schema *creation* is the migration's job, not this package's: there is
deliberately no ``create_all()`` helper in production code, so no code path can
silently produce a schema that no migration describes. Tests build their
databases by running the real migrations.
"""

from __future__ import annotations

from rfq_agent.persistence.base import Base
from rfq_agent.persistence.engine import (
    SQLITE_PRAGMAS,
    Database,
    build_engine,
    build_session_factory,
    is_memory_sqlite,
    session_scope,
    sqlite_pragmas,
)

__all__ = [
    "SQLITE_PRAGMAS",
    "Base",
    "Database",
    "build_engine",
    "build_session_factory",
    "is_memory_sqlite",
    "session_scope",
    "sqlite_pragmas",
]
