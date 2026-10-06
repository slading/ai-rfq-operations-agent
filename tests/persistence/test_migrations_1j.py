"""The 1J' migration: D-1's nullability change and D-2's ledger table.

These tests build their own database from an empty file, because what they check
is the *transition* between revisions - ``0001``'s schema, then ``0002``'s, then
back - and the shared ``db`` fixture is already at head.

Three properties carry the phase. The refused line's provenance becomes ``NULL``
without the foreign key being weakened; the pairing invariant holds in both
directions; and a downgrade that would lose a refused line refuses *before* it
changes anything, because this database runs with non-transactional DDL and a
half-applied rebuild would be worse than a refusal.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.stock import StockStatus
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import QuoteBlockedReasonRow, QuoteLineRow
from rfq_agent.seed import reset_and_seed
from tests.persistence.conftest import make_alembic_config
from tests.persistence.factories import NOW, Core, quote_line_row, quote_row, rfq_row, run_row

#: The revision this module is about, and the one before it.
HEAD = "0002"
PREVIOUS = "0001"

_LEDGER_TABLE = "quote_blocked_reasons"
_LEDGER_TRIGGERS = (
    "trg_quote_blocked_reasons_no_update",
    "trg_quote_blocked_reasons_no_delete",
)
#: The scratch table a rebuild copies into. It must never survive a run.
_REBUILD_TABLE = "quote_lines_rebuild"

_SQL = text
_LINES = _SQL(
    "SELECT line_id, quote_id, ordinal, product_id, sku, description, quantity, unit_price, "
    "price_entry_id, line_extension, currency, stock_status, price_status, blocked, "
    "blocked_reason, notes FROM quote_lines ORDER BY ordinal"
)


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[tuple[Config, Database]]:
    """A database of this test's own, at head, with its Alembic config."""
    path = tmp_path / "fresh.db"
    config = make_alembic_config(path)
    command.upgrade(config, HEAD)
    database = Database.create(f"sqlite:///{path}")
    try:
        yield config, database
    finally:
        database.dispose()


def _add_and_commit(session: Session, row: object) -> None:
    """Add one row and commit - one statement, which is what ``pytest.raises`` wants."""
    session.add(row)
    session.commit()


def _stored_lines(session: Session) -> list[tuple[object, ...]]:
    """Read the lines back as raw column values, for byte-level comparison."""
    return [tuple(row) for row in session.connection().execute(_LINES).all()]


def _store_a_priced_line(session: Session) -> None:
    """Store one ``FOUND`` line, the way ``0001`` could: it must survive untouched."""
    session.add(rfq_row())
    session.add(run_row())
    session.flush()
    session.add(quote_row(Core()))
    session.flush()
    session.add(quote_line_row(Core()))
    session.commit()


def _store_a_refused_line(session: Session, *, line_id: str = "QLI_9001", ordinal: int = 2) -> None:
    """Append a line that has no usable price - the case D-1 exists for.

    The line keeps a real quote and a real product; only its provenance is
    absent, which ``0001`` could not represent at all.
    """
    session.add(
        quote_line_row(
            Core(),
            line_id=line_id,
            ordinal=ordinal,
            quantity=1,
            unit_price=Decimal("0.0000"),
            price_entry_id=None,
            line_extension=Decimal("0.00"),
            price_status=PriceLookupStatus.EXPIRED,
            blocked=True,
            blocked_reason="EXPIRED: the only price entry ended before the pricing date",
        )
    )
    session.commit()


def _store_a_ledger_row(session: Session, **overrides: object) -> None:
    """Store one ledger reason through the model, so the column types are honest."""
    values: dict[str, object] = {
        "quote_id": Core().quote_id,
        "seq": 1,
        "run_id": Core().run_id,
        "code": "PRICE_MISSING",
        "message": "line 1 has no price",
        "line_ordinal": 2,
        "resolvable_by_human": True,
        "flags_json": ["PARTIAL_STOCK"],
        "created_at": NOW,
    }
    values.update(overrides)
    session.add(QuoteBlockedReasonRow(**values))  # type: ignore[arg-type]
    session.commit()


def _money_of(session: Session, line_id: str) -> tuple[str, str]:
    """The unit price and extension of one line at their stored scale."""
    line = session.get(QuoteLineRow, line_id)
    assert line is not None
    return str(line.unit_price), str(line.line_extension)


_INSERT_CODE = _SQL(
    "INSERT INTO quote_blocked_reasons "
    "(quote_id, seq, run_id, code, message, resolvable_by_human, flags_json, created_at) "
    "VALUES (:quote_id, :seq, :run_id, :code, 'm', 1, '[]', :created_at)"
)


def _code_parameters(code: str, *, seq: int = 1) -> dict[str, object]:
    """Parameters for a hand-written ledger insert, with one variable in play."""
    core = Core()
    return {
        "quote_id": core.quote_id,
        "seq": seq,
        "run_id": core.run_id,
        "code": code,
        "created_at": NOW.strftime("%Y-%m-%d %H:%M:%S.%f"),
    }


def _columns(database: Database, table: str) -> dict[str, dict[str, object]]:
    """The migrated columns of ``table``, keyed by name."""
    with database.engine.connect() as connection:
        return {column["name"]: column for column in inspect(connection).get_columns(table)}


# ---------------------------------------------------------------------------
# D-1: nullable provenance, unchanged foreign key
# ---------------------------------------------------------------------------


def test_upgrade_makes_the_price_reference_nullable(fresh: tuple[Config, Database]) -> None:
    """The one schema fact that blocked Phase 1J, checked from the database."""
    _, database = fresh
    assert _columns(database, "quote_lines")["price_entry_id"]["nullable"] is True


def test_stored_lines_survive_the_upgrade_unchanged(fresh: tuple[Config, Database]) -> None:
    """A rebuild copies rows; it must not re-scale money or reorder anything.

    The comparison is at byte level - raw column values, not ORM attributes -
    because ``Numeric(14, 2)`` silently re-quantises on the way in, so the stored
    text is the only honest witness that the value did not move.
    """
    config, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        before = _stored_lines(session)
        money_before = _money_of(session, "QLI_0001")

    command.downgrade(config, PREVIOUS)
    command.upgrade(config, HEAD)

    with Session(database.engine) as session:
        after = _stored_lines(session)
        money_after = _money_of(session, "QLI_0001")
    assert after == before
    assert after[0][8] == "PE_0001"
    # The two scales the schema promises, read through the ORM on both sides.
    assert money_after == money_before == ("1234.5600", "49382.40")


def test_the_foreign_key_still_refuses_an_unknown_price_entry(
    fresh: tuple[Config, Database],
) -> None:
    """D-1 relaxes nullability, not referential integrity: a *named* entry is real."""
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        session.add(rfq_row())
        session.add(run_row())
        session.flush()
        session.add(quote_row(Core()))
        session.flush()
        unknown = quote_line_row(Core(), line_id="QLI_9002", price_entry_id="PE_DOES_NOT_EXIST")
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            _add_and_commit(session, unknown)


@pytest.mark.parametrize(
    ("price_status", "price_entry_id"),
    [
        (PriceLookupStatus.EXPIRED, "PE_0001"),
        (PriceLookupStatus.MISSING, "PE_0001"),
        (PriceLookupStatus.AMBIGUOUS, "PE_0001"),
        (PriceLookupStatus.FOUND, None),
    ],
    ids=[
        "expired-with-provenance",
        "missing-with-provenance",
        "ambiguous-with-provenance",
        "found-without",
    ],
)
def test_the_pairing_invariant_holds_in_both_directions(
    fresh: tuple[Config, Database], price_status: PriceLookupStatus, price_entry_id: str | None
) -> None:
    """``(price_status = 'FOUND') = (price_entry_id IS NOT NULL)``, enforced below code.

    Only the database's own refusal is asserted, so this test also fails if the
    migration and the model ever disagree about the constraint's name.
    """
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        session.add(rfq_row())
        session.add(run_row())
        session.flush()
        session.add(quote_row(Core()))
        session.flush()
        inconsistent = quote_line_row(
            Core(),
            line_id="QLI_9003",
            price_status=price_status,
            price_entry_id=price_entry_id,
            blocked=price_status is not PriceLookupStatus.FOUND,
            blocked_reason=None if price_status is PriceLookupStatus.FOUND else "refused",
        )
        with pytest.raises(IntegrityError, match="ck_quote_lines_price_provenance_pairing"):
            _add_and_commit(session, inconsistent)


def test_a_refused_line_is_storable_without_weakening_the_foreign_key(
    fresh: tuple[Config, Database],
) -> None:
    """The whole point of D-1: evidence of a refusal, in a schema that keeps its FKs."""
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        _store_a_refused_line(session)
        lines = [(row[0], row[8]) for row in _stored_lines(session)]
        foreign_keys = [
            fk
            for fk in inspect(session.connection()).get_foreign_keys("quote_lines")
            if fk["constrained_columns"] == ["price_entry_id"]
        ]
    assert lines == [("QLI_0001", "PE_0001"), ("QLI_9001", None)]
    assert [fk["referred_table"] for fk in foreign_keys] == ["price_entries"]


# ---------------------------------------------------------------------------
# D-2: the ledger table
# ---------------------------------------------------------------------------


def test_the_ledger_table_and_its_guards_exist(fresh: tuple[Config, Database]) -> None:
    """One table, one index, two triggers - and the shape the model declares."""
    _, database = fresh
    columns = _columns(database, _LEDGER_TABLE)
    assert list(columns) == [
        "quote_id",
        "seq",
        "run_id",
        "code",
        "message",
        "line_ordinal",
        "resolvable_by_human",
        "flags_json",
        "created_at",
    ]
    assert [column["name"] for column in columns.values() if column["nullable"] is False] == [
        "quote_id",
        "seq",
        "run_id",
        "code",
        "message",
        "resolvable_by_human",
        "flags_json",
        "created_at",
    ]
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        primary_key = inspector.get_pk_constraint(_LEDGER_TABLE)
        indexes = inspector.get_indexes(_LEDGER_TABLE)
        triggers = (
            connection.execute(
                _SQL("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
            )
            .scalars()
            .all()
        )
    assert primary_key["constrained_columns"] == ["quote_id", "seq"]
    assert [index["name"] for index in indexes] == ["ix_quote_blocked_reasons_run_id"]
    assert set(_LEDGER_TRIGGERS).issubset(set(triggers))


def test_the_ledger_is_append_only_in_the_database(fresh: tuple[Config, Database]) -> None:
    """The evidence cannot be rewritten by anything that holds a connection."""
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        _store_a_ledger_row(session)

        with pytest.raises(IntegrityError, match="append-only") as caught:
            session.connection().execute(
                _SQL("UPDATE quote_blocked_reasons SET message = 'rewritten' WHERE seq = 1")
            )
        assert "UPDATE" in str(caught.value)
        session.rollback()

        with pytest.raises(IntegrityError, match="append-only") as caught:
            session.connection().execute(_SQL("DELETE FROM quote_blocked_reasons WHERE seq = 1"))
        assert "DELETE" in str(caught.value)
        session.rollback()

        stored = (
            session.connection()
            .execute(_SQL("SELECT message FROM quote_blocked_reasons"))
            .scalar_one()
        )
    assert stored == "line 1 has no price"


def test_a_ledger_row_must_belong_to_a_quote_and_a_run(fresh: tuple[Config, Database]) -> None:
    """Two real foreign keys, so ledger evidence cannot be invented out of nowhere."""
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            _store_a_ledger_row(session, quote_id="QTE_DOES_NOT_EXIST")


def test_the_ledger_code_column_is_constrained(fresh: tuple[Config, Database]) -> None:
    """The codes are the accepted thirteen, not free text - enforced in the schema.

    The ``INSERT`` is written in SQL on purpose. The ORM's enum type validates
    values in Python before they reach the driver, so a bad code through the model
    would prove only that the model is picky; what D-2 promises is a *database*
    that refuses anything else, including a hand-written statement.
    """
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        session.connection().execute(_INSERT_CODE, _code_parameters("PRICE_MISSING"))
        session.commit()
        stored = (
            session.connection()
            .execute(_SQL("SELECT code FROM quote_blocked_reasons"))
            .scalar_one()
        )
        assert stored == "PRICE_MISSING"
        with pytest.raises(IntegrityError, match="CHECK constraint failed"):
            session.connection().execute(_INSERT_CODE, _code_parameters("NOT_A_CODE", seq=2))
        session.rollback()
        remaining = (
            session.connection()
            .execute(_SQL("SELECT COUNT(*) FROM quote_blocked_reasons"))
            .scalar_one()
        )
    assert remaining == 1


def test_the_same_code_cannot_be_recorded_twice_for_one_quote(
    fresh: tuple[Config, Database],
) -> None:
    """The projector emits one reason per code; the schema makes that structural."""
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        _store_a_ledger_row(session, seq=1)
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            _store_a_ledger_row(session, seq=2, message="the same reason again")


# ---------------------------------------------------------------------------
# The refusal that keeps the database whole
# ---------------------------------------------------------------------------


def test_a_downgrade_that_would_lose_a_refused_line_refuses_intact(
    fresh: tuple[Config, Database],
) -> None:
    """The refusal is safe, and it happens before anything is dropped.

    ``0001`` cannot represent a line with no price entry, so the downgrade must
    fail - but it must fail *without* leaving the schema half-rebuilt, which is
    what this database's non-transactional DDL would otherwise allow. The
    revision, the lines and the ledger must all be exactly where they were.
    """
    config, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        _store_a_refused_line(session)
        _store_a_ledger_row(session)

    with pytest.raises(RuntimeError, match="cannot downgrade to revision 0001"):
        command.downgrade(config, PREVIOUS)

    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.execute(_SQL("SELECT version_num FROM alembic_version")).scalar_one()
        lines = connection.execute(_SQL("SELECT COUNT(*) FROM quote_lines")).scalar_one()
        ledger = connection.execute(_SQL("SELECT COUNT(*) FROM quote_blocked_reasons")).scalar_one()
    assert _REBUILD_TABLE not in tables
    assert _LEDGER_TABLE in tables
    assert (revision, lines, ledger) == (HEAD, 2, 1)


def test_a_refused_downgrade_is_repairable_and_then_succeeds(
    fresh: tuple[Config, Database],
) -> None:
    """A refusal is not a dead end: fix the data, downgrade again, no cleanup needed.

    The scratch table an interrupted rebuild leaves behind is why this is a test
    rather than an assumption - statements issued after a failing copy are not
    reliably committed on this connection, so the repair belongs in the migration
    rather than in the operator's runbook.
    """
    config, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        _store_a_refused_line(session)

    with pytest.raises(RuntimeError, match="cannot downgrade to revision 0001"):
        command.downgrade(config, PREVIOUS)

    with Session(database.engine) as session:
        session.connection().execute(_SQL("DELETE FROM quote_lines WHERE price_entry_id IS NULL"))
        session.commit()

    command.downgrade(config, PREVIOUS)
    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.execute(_SQL("SELECT version_num FROM alembic_version")).scalar_one()
    assert _columns(database, "quote_lines")["price_entry_id"]["nullable"] is False
    assert _LEDGER_TABLE not in tables
    assert revision == PREVIOUS


def test_downgrade_restores_the_revision_it_came_from(fresh: tuple[Config, Database]) -> None:
    """Down and up again: the schema returns, and the same rows come back."""
    config, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        before = _stored_lines(session)

    command.downgrade(config, PREVIOUS)
    command.upgrade(config, HEAD)

    with database.engine.connect() as connection:
        triggers = connection.execute(
            _SQL(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE '%blocked_reasons%'"
            )
        ).scalar_one()
        revision = connection.execute(_SQL("SELECT version_num FROM alembic_version")).scalar_one()
    with Session(database.engine) as session:
        after = _stored_lines(session)
    assert _columns(database, "quote_lines")["price_entry_id"]["nullable"] is True
    assert triggers == len(_LEDGER_TRIGGERS)
    assert revision == HEAD
    assert after == before


def test_downgrade_to_base_removes_the_ledger_and_its_triggers(tmp_path: Path) -> None:
    """Nothing this revision added may outlive it - including its triggers."""
    path = tmp_path / "base.db"
    config = make_alembic_config(path)
    command.upgrade(config, HEAD)
    command.downgrade(config, "base")

    database = Database.create(f"sqlite:///{path}")
    try:
        with database.engine.connect() as connection:
            remaining = (
                connection.execute(
                    _SQL(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger') "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
                .scalars()
                .all()
            )
    finally:
        database.dispose()
    assert [name for name in remaining if name != "alembic_version"] == []


# ---------------------------------------------------------------------------
# The model and the migration agree
# ---------------------------------------------------------------------------


def test_the_migrated_ledger_has_exactly_the_columns_the_model_declares(
    fresh: tuple[Config, Database],
) -> None:
    """A rebuild is where a table drifts from its model, so the ledger is compared directly."""
    _, database = fresh
    declared = set(QuoteBlockedReasonRow.__table__.columns.keys())
    assert set(_columns(database, _LEDGER_TABLE)) == declared


def test_a_clean_line_still_round_trips_after_the_upgrade(fresh: tuple[Config, Database]) -> None:
    """The upgrade must not have broken the ordinary case it was written around.

    The row is read through the ORM, so ``0002``'s definition has to accept
    exactly what the model and the 1J' write path produce.
    """
    _, database = fresh
    with Session(database.engine) as session:
        reset_and_seed(session)
        _store_a_priced_line(session)
        line = session.get(QuoteLineRow, "QLI_0001")
    assert line is not None
    assert line.price_status is PriceLookupStatus.FOUND
    assert line.stock_status is StockStatus.SUFFICIENT
    assert line.currency == "EUR"
    assert str(line.unit_price) == "1234.5600"
    assert line.price_entry_id == "PE_0001"
    assert line.blocked is False
