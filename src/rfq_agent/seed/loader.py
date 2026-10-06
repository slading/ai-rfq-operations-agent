"""Applying the demo dataset to a database: seed, reset, reseed.

Three operations, with deliberately different meanings:

``seed``
    Make the database match the dataset. A missing row is inserted, a row whose
    managed columns differ is corrected, and an identical row is left completely
    alone - so running it twice is a no-op, and running it after somebody edited
    a price in a SQL prompt puts the dataset back where it belongs. Rows that are
    not part of the dataset are never touched.

``reset``
    Delete exactly the dataset's rows, children before parents. Rows that are not
    part of the dataset are again not touched, which is why this deletes by
    primary key instead of emptying tables: a developer's own test customer
    survives a reset, and so does the audit trail that references nothing here.

``reset_and_seed``
    Reset, then seed. This is the local-development and test path: it produces a
    byte-for-byte reproducible dataset regardless of what the database contained.

None of these functions commit. The caller owns the transaction
(:meth:`~rfq_agent.persistence.engine.Database.session` or a test's explicit
``commit()``), which is what makes ``reset_and_seed`` atomic: if seeding fails
halfway, the rollback restores the previous dataset rather than leaving an empty
database behind.

One caveat worth stating plainly: ``reset`` will fail - correctly - if business
rows reference the seeded master data. A quotation line points at a product, a
price entry and a warehouse; deleting them would either cascade into the
quotation or rewrite history, and this layer has no business deciding which.
The refusal is reported as :class:`SeedResetBlockedError` so the operator knows
the database has outgrown the reset path, rather than seeing a foreign-key
traceback.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError

from rfq_agent.persistence.base import Base
from rfq_agent.seed.dataset import (
    DATASET_NAME,
    SEED_VERSION,
    levels,
    row_counts,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "SeedError",
    "SeedReport",
    "SeedResetBlockedError",
    "SeedSchemaError",
    "TableReport",
    "reset",
    "reset_and_seed",
    "seed",
]

#: Columns the database or the ORM maintains. They record *when* a row was
#: written, not what the dataset says, so a difference in them is not a
#: difference in the data and never triggers an update.
_GENERATED_COLUMNS: frozenset[str] = frozenset({"created_at", "updated_at"})


class SeedError(RuntimeError):
    """Base class for failures of the seed operations."""


class SeedSchemaError(SeedError):
    """The database has no schema to seed (the migration has not been run)."""


class SeedResetBlockedError(SeedError):
    """Other data still references the seeded rows, so they cannot be removed."""


@dataclass(frozen=True, slots=True)
class TableReport:
    """Per-table outcome of a seed run."""

    table: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        """Number of rows the dataset declares for this table."""
        return self.inserted + self.updated + self.unchanged


@dataclass(frozen=True, slots=True)
class SeedReport:
    """Outcome of one seed run, per table."""

    dataset: str
    version: str
    tables: tuple[TableReport, ...]
    deleted: int = 0

    @property
    def inserted(self) -> int:
        """Rows that were absent and have been written."""
        return sum(table.inserted for table in self.tables)

    @property
    def updated(self) -> int:
        """Rows that existed with different values and have been corrected."""
        return sum(table.updated for table in self.tables)

    @property
    def unchanged(self) -> int:
        """Rows that already matched the dataset exactly."""
        return sum(table.unchanged for table in self.tables)

    @property
    def total(self) -> int:
        """Total rows the dataset declares."""
        return sum(table.total for table in self.tables)

    def format(self) -> str:
        """Render the report as the multi-line text the CLI prints."""
        lines = [f"{self.dataset} {self.version}"]
        if self.deleted:
            lines.append(f"  removed {self.deleted} existing rows")
        lines.extend(
            f"  {table.table:<22} {table.inserted:>4} inserted, "
            f"{table.updated:>4} updated, {table.unchanged:>4} unchanged"
            for table in self.tables
        )
        lines.append(
            f"  {self.total} rows in {len(self.tables)} tables: "
            f"{self.inserted} inserted, {self.updated} updated, {self.unchanged} unchanged"
        )
        return "\n".join(lines)


def require_schema(session: Session) -> None:
    """Raise :class:`SeedSchemaError` unless every seeded table exists.

    Args:
        session: Session whose bound engine describes the target database.

    Raises:
        SeedSchemaError: If the database is missing one or more of the tables the
            dataset writes, which means the migration has not been applied.
    """
    inspector = inspect(session.get_bind())
    missing = sorted(table for table in row_counts() if not inspector.has_table(table))
    if missing:
        msg = (
            f"the database is missing {len(missing)} seed table(s): {', '.join(missing)}. "
            "Create the schema first: `alembic upgrade head` (or `make migrate`)."
        )
        raise SeedSchemaError(msg)


def seed(session: Session) -> SeedReport:
    """Insert or correct every dataset row so the database matches the dataset.

    Args:
        session: Open session. The caller commits; nothing here does.

    Returns:
        A per-table report of what was inserted, corrected and left alone.

    Raises:
        SeedSchemaError: If the schema is missing.
    """
    require_schema(session)
    outcomes: dict[str, Counter[str]] = {}

    for level in levels():
        for row in level:
            table = type(row).__tablename__
            outcomes.setdefault(table, Counter())[_apply(session, row)] += 1
        # Flushed per level: SQLAlchemy flushes in mapper-registration order, so
        # a parent and its child must not be pending in the same flush.
        session.flush()

    return SeedReport(
        dataset=DATASET_NAME,
        version=SEED_VERSION,
        tables=tuple(
            TableReport(
                table=table,
                inserted=counts["inserted"],
                updated=counts["updated"],
                unchanged=counts["unchanged"],
            )
            for table, counts in sorted(outcomes.items())
        ),
    )


def reset(session: Session) -> int:
    """Delete the dataset's rows, children before parents.

    Args:
        session: Open session. The caller commits; nothing here does.

    Returns:
        The number of rows deleted.

    Raises:
        SeedSchemaError: If the schema is missing.
        SeedResetBlockedError: If other rows still reference the seeded data, in
            which case nothing has been deleted from the caller's point of view
            once the transaction is rolled back.
    """
    require_schema(session)
    deleted = 0
    for level in reversed(levels()):
        for row in level:
            deleted += _delete_one(session, row)
    session.flush()
    return deleted


def reset_and_seed(session: Session) -> SeedReport:
    """Delete the dataset's rows and write them again from scratch.

    Args:
        session: Open session. The caller commits; nothing here does, which is
            what makes this atomic: a failure leaves the previous dataset.

    Returns:
        The seed report, with ``deleted`` set to the number of rows removed.

    Raises:
        SeedSchemaError: If the schema is missing.
        SeedResetBlockedError: If other rows still reference the seeded data.
    """
    deleted = reset(session)
    return replace(seed(session), deleted=deleted)


def _apply(session: Session, row: Base) -> str:
    """Insert, correct or leave alone one row.

    Args:
        session: Open session.
        row: A pending row from :func:`~rfq_agent.seed.dataset.levels`.

    Returns:
        ``"inserted"``, ``"updated"`` or ``"unchanged"``.
    """
    row_class = type(row)
    mapper = row_class.__mapper__
    key = tuple(getattr(row, column.key) for column in mapper.primary_key)
    existing = session.get(row_class, key[0] if len(key) == 1 else key)

    if existing is None:
        session.add(row)
        return "inserted"

    differences = [
        (column.key, getattr(row, column.key))
        for column in mapper.columns
        if column.key not in _GENERATED_COLUMNS
        and getattr(existing, column.key) != getattr(row, column.key)
    ]
    if not differences:
        return "unchanged"

    for attribute, value in differences:
        setattr(existing, attribute, value)
    return "updated"


def _delete_one(session: Session, row: Base) -> int:
    """Delete one dataset row by primary key.

    The delete goes through the ORM (``synchronize_session`` is left at its
    default) so that an object already loaded in this session is removed from the
    identity map as well. Without that, a ``reset`` followed by a ``seed`` in the
    same session would compare the new rows against stale objects.
    """
    row_class = type(row)
    conditions = [column == getattr(row, column.key) for column in row_class.__mapper__.primary_key]
    try:
        result = session.execute(delete(row_class).where(*conditions))
    except IntegrityError as error:
        msg = (
            f"cannot reset the {DATASET_NAME} dataset: rows in "
            f"{row_class.__tablename__} are still referenced by other data "
            "(a run, quotation or intake record). Reset a database that holds no "
            "business records, or delete the dependent rows first."
        )
        raise SeedResetBlockedError(msg) from error
    return result.rowcount or 0
