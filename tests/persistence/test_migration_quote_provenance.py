"""The 0002 migration: D-1's nullable provenance and D-2's ledger table.

These tests build their own databases from an empty file, because what they are
about is the *transition* - ``0001``'s schema, then ``0002``'s, then back - and
the shared fixture is already at head.

Three claims are load-bearing. The rebuild preserves stored rows rather than
reinterpreting them. The invariant ``(price_status = 'FOUND') = (price_entry_id
IS NOT NULL)`` is enforced in the database, in both directions, while the foreign
key keeps rejecting an unknown price entry. And a downgrade that would lose a
refused line *refuses*, before it has changed anything, because this database
runs with non-transactional DDL: a half-applied rebuild would be worse than a
migration that declines.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.stock import StockStatus
from rfq_agent.persistence import Database
from tests.persistence.conftest import make_alembic_config
from tests.persistence.factories import Core, quote_line_row, quote_row, seed_core

#: The revision under test, and the one it is built on.
NEW = "0002"
OLD = "0001"

_LEDGER_TABLE = "quote_blocked_reasons"
_LEDGER_TRIGGERS = ("trg_quote_blocked_reasons_no_update", "trg_quote_blocked_reasons_no_delete")
#: The scratch table a rebuild copies through. It must not survive a refusal.
_REBUILD_TABLE = "quote_lines_rebuild"

#: Every column of the line table, in one place: the rebuild copies all of them.
_LINE_COLUMNS = (
    "line_id, quote_id, ordinal, product_id, sku, description, quantity, unit_price, "
    "price_entry_id, line_extension, currency, stock_status, price_status, blocked, "
    "blocked_reason, notes"
)
_INSERT_LINE = text(
    "INSERT INTO quote_lines (line_id, quote_id, ordinal, product_id, sku, description, "
    "quantity, unit_price, price_entry_id, line_extension, currency, stock_status, "
    "price_status, blocked, blocked_reason, notes) VALUES (:line_id, :quote_id, 2, "
    "'PRD_0001', 'PMP-A-100', 'Centrifugal pump', 1, :unit_price, :price_entry_id, "
    ":line_extension, "
    "'EUR', :stock_status, :price_status, :blocked, :blocked_reason, NULL)"
)


@pytest.fixture
def staged(tmp_path: Path) -> Iterator[tuple[Config, Database]]:
    """A database of this test's own, migrated to ``0001`` with the reference graph.

    Starting at ``0001`` is the point: the rows the upgrade has to carry forward
    are written by the *old* schema, so nothing about the new one can quietly
    make them insertable.
    """
    path = tmp_path / "staged.db"
    config = make_alembic_config(path)
    command.upgrade(config, OLD)
    database = Database.create(f"sqlite:///{path}")
    try:
        with Session(database.engine) as session:
            seed_core(session)
            session.add(quote_row(Core()))
            session.flush()
            session.add(quote_line_row(Core()))
            session.commit()
        yield config, database
    finally:
        database.dispose()


def _version(connection: Connection) -> str:
    """The revision the database says it is at."""
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _columns(connection: Connection, table: str) -> dict[str, dict[str, object]]:
    """The columns of ``table``, keyed by name."""
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _lines(connection: Connection) -> list[tuple[object, ...]]:
    """Every stored line, as raw values, in ordinal order."""
    rows = connection.execute(text(f"SELECT {_LINE_COLUMNS} FROM quote_lines ORDER BY ordinal"))  # noqa: S608
    return [tuple(row) for row in rows.all()]


def _names(connection: Connection, kind: str) -> set[str]:
    """The names of every object of ``kind`` in the database."""
    rows = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type = :kind AND name NOT LIKE 'sqlite_%'"),
        {"kind": kind},
    )
    return {name for row in rows for name in row}


def _store_a_refused_line(database: Database, **overrides: object) -> None:
    """Append a line with no price entry - writable only at the new revision.

    The statement is SQL rather than the ORM, so what is being tested is the
    database's rule and not the mapper's opinion of it.
    """
    values: dict[str, object] = {
        "line_id": "QLI_9001",
        "quote_id": Core().quote_id,
        "unit_price": "0.0000",
        "line_extension": "0.00",
        "price_entry_id": None,
        "price_status": PriceLookupStatus.EXPIRED.value,
        "stock_status": StockStatus.NONE.value,
        "blocked": 1,
        "blocked_reason": "refused",
    }
    values.update(overrides)
    with database.engine.begin() as connection:
        connection.execute(_INSERT_LINE, values)


# ---------------------------------------------------------------------------
# The rebuild carries the old rows forward
# ---------------------------------------------------------------------------


def test_an_existing_quote_can_be_represented_before_and_after(
    staged: tuple[Config, Database],
) -> None:
    """A ``FOUND`` line is the shape both revisions agree on, and it does not move."""
    config, database = staged
    with database.engine.connect() as connection:
        before = _lines(connection)
        assert before[0][8] == "PE_0001"

    command.upgrade(config, NEW)

    with database.engine.connect() as connection:
        after = _lines(connection)
        assert _version(connection) == NEW
        assert _columns(connection, "quote_lines")["price_entry_id"]["nullable"] is True
    assert after == before


def test_upgrade_downgrade_upgrade_is_repeatable(staged: tuple[Config, Database]) -> None:
    """Running the pair twice leaves the same schema and the same rows."""
    config, database = staged
    with database.engine.connect() as connection:
        original = _lines(connection)

    for _ in range(2):
        command.upgrade(config, NEW)
        command.downgrade(config, OLD)
        with database.engine.connect() as connection:
            assert _version(connection) == OLD
            assert _columns(connection, "quote_lines")["price_entry_id"]["nullable"] is False
            assert _lines(connection) == original

    command.upgrade(config, NEW)
    with database.engine.connect() as connection:
        assert _version(connection) == NEW
        assert _lines(connection) == original


def test_the_rebuild_leaves_no_scratch_table_behind(staged: tuple[Config, Database]) -> None:
    """The table a rebuild copies through is an implementation detail, not schema."""
    config, database = staged
    command.upgrade(config, NEW)
    command.downgrade(config, OLD)
    command.upgrade(config, NEW)

    with database.engine.connect() as connection:
        assert _REBUILD_TABLE not in _names(connection, "table")


# ---------------------------------------------------------------------------
# The invariant, in both directions, plus the foreign key
# ---------------------------------------------------------------------------


def test_a_refused_line_can_be_stored_without_a_price_entry(
    staged: tuple[Config, Database],
) -> None:
    """The whole point of D-1: a line with no usable price is storable, as ``NULL``."""
    config, database = staged
    command.upgrade(config, NEW)

    _store_a_refused_line(database)

    with database.engine.connect() as connection:
        stored = _lines(connection)
    assert [(row[0], row[8]) for row in stored] == [("QLI_0001", "PE_0001"), ("QLI_9001", None)]


def test_a_found_line_without_a_price_entry_is_refused(staged: tuple[Config, Database]) -> None:
    """The other direction: ``FOUND`` means there is a price row, and it is named."""
    config, database = staged
    command.upgrade(config, NEW)

    with pytest.raises(IntegrityError, match="ck_quote_lines_price_provenance_pairing"):
        _store_a_refused_line(
            database,
            price_entry_id=None,
            price_status=PriceLookupStatus.FOUND.value,
            stock_status=StockStatus.SUFFICIENT.value,
            blocked=0,
            blocked_reason=None,
        )


def test_a_refused_line_that_names_a_price_entry_is_refused(
    staged: tuple[Config, Database],
) -> None:
    """And a refusal may not point at the price it says it could not use."""
    config, database = staged
    command.upgrade(config, NEW)

    with pytest.raises(IntegrityError, match="ck_quote_lines_price_provenance_pairing"):
        _store_a_refused_line(database, price_entry_id="PE_0001")


def test_the_foreign_key_still_rejects_an_unknown_price_entry(
    staged: tuple[Config, Database],
) -> None:
    """D-1 makes the reference optional; it does not make it unchecked."""
    config, database = staged
    command.upgrade(config, NEW)

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        _store_a_refused_line(
            database,
            price_entry_id="PE_9999",
            price_status=PriceLookupStatus.FOUND.value,
            stock_status=StockStatus.SUFFICIENT.value,
            blocked=0,
            blocked_reason=None,
        )


# ---------------------------------------------------------------------------
# Refusals, and what they leave behind
# ---------------------------------------------------------------------------


def test_the_upgrade_refuses_a_line_that_contradicts_the_new_rule(
    staged: tuple[Config, Database],
) -> None:
    """A row the new rule cannot describe stops the upgrade before any DDL runs.

    ``0001`` permits a refusal that still names a price entry, so a database
    could hold one. Refusing leaves it exactly where it was - still at ``0001``,
    still readable - which is the only safe option on a database whose DDL does
    not roll back.
    """
    config, database = staged
    with database.engine.connect() as connection:
        original = _lines(connection)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE quote_lines SET price_status = 'EXPIRED', blocked = 1, "
                "blocked_reason = 'price expired' WHERE line_id = 'QLI_0001'"
            )
        )

    with pytest.raises(RuntimeError, match="cannot upgrade to revision 0002"):
        command.upgrade(config, NEW)

    with database.engine.connect() as connection:
        assert _version(connection) == OLD
        assert _REBUILD_TABLE not in _names(connection, "table")
        assert _columns(connection, "quote_lines")["price_entry_id"]["nullable"] is False
        assert _lines(connection) == [
            (*original[0][:8], "PE_0001", *original[0][9:12], "EXPIRED", 1, "price expired", None)
        ]


def test_the_downgrade_refuses_a_refused_line_and_changes_nothing(
    staged: tuple[Config, Database],
) -> None:
    """``0001`` cannot store a ``NULL`` provenance, so the downgrade declines."""
    config, database = staged
    command.upgrade(config, NEW)
    _store_a_refused_line(database)
    with database.engine.connect() as connection:
        stored = _lines(connection)

    with pytest.raises(RuntimeError, match="cannot downgrade to revision 0001"):
        command.downgrade(config, OLD)

    with database.engine.connect() as connection:
        assert _version(connection) == NEW
        assert _REBUILD_TABLE not in _names(connection, "table")
        assert _lines(connection) == stored
        assert _columns(connection, "quote_lines")["price_entry_id"]["nullable"] is True


def test_the_downgrade_succeeds_once_the_refused_line_is_gone(
    staged: tuple[Config, Database],
) -> None:
    """The refusal is a condition, not a dead end."""
    config, database = staged
    command.upgrade(config, NEW)
    _store_a_refused_line(database)
    with pytest.raises(RuntimeError, match="cannot downgrade to revision 0001"):
        command.downgrade(config, OLD)

    with database.engine.begin() as connection:
        connection.execute(text("DELETE FROM quote_lines WHERE price_entry_id IS NULL"))

    command.downgrade(config, OLD)

    with database.engine.connect() as connection:
        assert _version(connection) == OLD
        assert _columns(connection, "quote_lines")["price_entry_id"]["nullable"] is False
    with pytest.raises(IntegrityError, match="NOT NULL"):
        _store_a_refused_line(database)


# ---------------------------------------------------------------------------
# D-2: the ledger table arrives and leaves with its revision
# ---------------------------------------------------------------------------


def test_the_ledger_table_and_its_guards_appear_with_the_revision(
    staged: tuple[Config, Database],
) -> None:
    """One table, one index and two triggers - guarded by the database, not by habit."""
    config, database = staged
    command.upgrade(config, NEW)

    with database.engine.connect() as connection:
        assert _LEDGER_TABLE in _names(connection, "table")
        assert set(_LEDGER_TRIGGERS) <= _names(connection, "trigger")
        indexes = {index["name"] for index in inspect(connection).get_indexes(_LEDGER_TABLE)}

    assert indexes == {"ix_quote_blocked_reasons_run_id"}


def test_the_ledger_table_leaves_with_the_revision(staged: tuple[Config, Database]) -> None:
    """Downgrading removes what the revision added - including its triggers."""
    config, database = staged
    command.upgrade(config, NEW)
    command.downgrade(config, OLD)

    with database.engine.connect() as connection:
        assert _LEDGER_TABLE not in _names(connection, "table")
        assert set(_LEDGER_TRIGGERS) & _names(connection, "trigger") == set()
