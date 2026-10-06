"""Alembic environment for the RFQ agent schema.

Design notes:

* **One source of truth for the target.** ``target_metadata`` is
  :attr:`rfq_agent.persistence.base.Base.metadata`, populated by importing
  :mod:`rfq_agent.persistence.models`. Autogenerate therefore compares against
  the real models rather than a copy.
* **The URL comes from application configuration** unless a caller overrides it
  (``sqlalchemy.url`` in the ini, or ``config.set_main_option``), so
  ``RFQ_DATABASE__URL`` and ``.env`` behave exactly as they do at runtime.
* **``render_as_batch=True``.** SQLite cannot ``ALTER`` most things; batch mode
  makes future migrations rewrite the table instead of failing.
* **Migrations are self-contained.** They must not import the models they were
  generated from - an old revision has to keep working after the models move on.
  The one exception is this file, which is not a revision.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from rfq_agent.config import Settings
from rfq_agent.persistence import models  # noqa: F401  (registers every table)
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.engine import build_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: What autogenerate compares the database against.
target_metadata = Base.metadata


def database_url() -> str:
    """Return the URL to migrate: explicit ini value first, else settings."""
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return override
    return Settings.load().database.url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a real connection."""
    engine = build_engine(database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
