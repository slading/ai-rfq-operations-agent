"""The dataset written to a real, migrated database.

Every test here runs against a database built by the real Alembic migration
(``tests/seed/conftest.py`` imports that machinery from the persistence suite),
because the claims being made are about what the database ends up holding: that
every reference is real, that a second run changes nothing, that a reset and a
reseed reproduce the same rows, and that values keep their type and scale on the
way in and out.

The one test that builds its own database is the "empty database plus seed" case,
which is deliberately the documented bootstrap path rather than a fixture.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from sqlalchemy import select, text

from rfq_agent.domain.pricing import PriceEntry
from rfq_agent.domain.stock import StockLevel
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    CustomerRow,
    DiscountRuleRow,
    PriceBookRow,
    PriceEntryRow,
    ProductAliasRow,
    StockLevelRow,
)
from rfq_agent.seed import (
    DATASET_NAME,
    SEED_VERSION,
    STOCK_AS_OF,
    SeedResetBlockedError,
    SeedSchemaError,
    reset,
    reset_and_seed,
    row_counts,
)
from rfq_agent.seed import seed as seed_dataset
from rfq_agent.seed.dataset import levels
from tests.persistence.conftest import make_alembic_config
from tests.persistence.factories import Core, quote_line_row, quote_row, rfq_row, run_row

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: The model class for each table the dataset writes, taken from the dataset
#: itself so a new table cannot be added without the dump below covering it.
MODELS: dict[str, type] = {
    type(row).__tablename__: type(row) for level in levels() for row in level
}

_GENERATED_COLUMNS = frozenset({"created_at", "updated_at"})


def _model(table: str) -> type:
    """The row class for ``table``."""
    return MODELS[table]


def _count(session: Session, table: str) -> int:
    """Number of rows in ``table``."""
    return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608


def _dump(session: Session) -> dict[str, list[tuple[object, ...]]]:
    """Every managed column of every row, per table, in primary-key order.

    Write timestamps are excluded: they record when the rows were written, not
    what the dataset says, and a reset genuinely does write new rows.
    """
    dump: dict[str, list[tuple[object, ...]]] = {}
    for table, model in sorted(MODELS.items()):
        columns = [
            column for column in model.__mapper__.columns if column.key not in _GENERATED_COLUMNS
        ]
        statement = text(
            f"SELECT {', '.join(column.key for column in columns)} FROM {table} "  # noqa: S608
            f"ORDER BY {', '.join(column.key for column in model.__mapper__.primary_key)}"
        )
        dump[table] = [tuple(row) for row in session.execute(statement)]
    return dump


def _insert_extra_customer(session: Session) -> None:
    """Add a customer that is not part of the dataset."""
    session.add(
        CustomerRow(
            customer_id="CUS_9900",
            legal_name="Test Buyer AG",
            display_name="Test Buyer",
            country_code="DE",
            default_currency="EUR",
            payment_terms_days=30,
            credit_limit=Decimal("1000.00"),
            credit_hold=False,
            active=True,
        )
    )
    session.commit()


def _insert_quotation(session: Session) -> None:
    """Add the business data that makes a reset impossible.

    The seeded dataset already contains ``CUS_0001``, ``PRD_0001``, ``PE_0001``
    and ``WAW``, so a quotation built from the fixture identifiers points at the
    *seeded* rows - which is exactly the situation these tests need.
    """
    core = Core()
    session.add(rfq_row())
    session.flush()
    session.add(run_row())
    session.flush()
    session.add(quote_row(core))
    session.flush()
    session.add(quote_line_row(core))
    session.commit()


@pytest.fixture
def seeded(session: Session) -> Session:
    """A session whose database already holds the committed dataset."""
    seed_dataset(session)
    session.commit()
    return session


class TestSeeding:
    def test_seeding_an_empty_database_writes_the_whole_dataset(self, seeded: Session) -> None:
        for table, expected in row_counts().items():
            assert _count(seeded, table) == expected, table

    def test_the_report_describes_what_was_written(self, session: Session) -> None:
        report = seed_dataset(session)
        session.commit()

        assert report.dataset == DATASET_NAME
        assert report.version == SEED_VERSION
        assert {table.table for table in report.tables} == set(row_counts())
        assert report.inserted == report.total
        assert report.updated == 0
        assert report.unchanged == 0
        assert str(report.total) in report.format()

    def test_every_foreign_key_is_valid(self, seeded: Session) -> None:
        """SQLite's own checker, over the whole database.

        This is the strongest available statement that the seeded graph is
        closed: no alias, price entry, stock row or carrier points at something
        that was never written.
        """
        assert seeded.execute(text("PRAGMA foreign_key_check")).fetchall() == []

    def test_aliases_resolve_to_existing_rows(self, seeded: Session) -> None:
        for table, target, key in (
            ("product_aliases", "products", "product_id"),
            ("customer_aliases", "customers", "customer_id"),
        ):
            unjoined = seeded.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} AS a "  # noqa: S608
                    f"LEFT JOIN {target} AS t ON t.{key} = a.{key} WHERE t.{key} IS NULL"
                )
            ).scalar_one()
            assert unjoined == 0, table
            assert _count(seeded, table) == row_counts()[table]

    def test_price_entries_reference_existing_products_books_and_customers(
        self, seeded: Session
    ) -> None:
        orphans = seeded.execute(
            text(
                "SELECT COUNT(*) FROM price_entries AS e "
                "LEFT JOIN products AS p ON p.product_id = e.product_id "
                "LEFT JOIN price_books AS b ON b.price_book_code = e.price_book_code "
                "LEFT JOIN customers AS c ON c.customer_id = e.customer_id "
                "WHERE p.product_id IS NULL OR b.price_book_code IS NULL "
                "OR (e.customer_id IS NOT NULL AND c.customer_id IS NULL)"
            )
        ).scalar_one()
        assert orphans == 0
        assert _count(seeded, "price_entries") == row_counts()["price_entries"]

    def test_stock_references_existing_products_and_warehouses(self, seeded: Session) -> None:
        orphans = seeded.execute(
            text(
                "SELECT COUNT(*) FROM stock_levels AS s "
                "LEFT JOIN products AS p ON p.product_id = s.product_id "
                "LEFT JOIN warehouses AS w ON w.location_code = s.location_code "
                "WHERE p.product_id IS NULL OR w.location_code IS NULL"
            )
        ).scalar_one()
        assert orphans == 0
        assert _count(seeded, "stock_levels") == row_counts()["stock_levels"]

    def test_carrier_services_reference_existing_warehouses(self, seeded: Session) -> None:
        orphans = seeded.execute(
            text(
                "SELECT COUNT(*) FROM carrier_services AS s "
                "LEFT JOIN warehouses AS w ON w.location_code = s.origin_location "
                "WHERE w.location_code IS NULL"
            )
        ).scalar_one()
        assert orphans == 0

    def test_aliases_are_findable_by_their_normalised_form(self, seeded: Session) -> None:
        """The lookup the resolver will perform, done at the data level."""
        found = seeded.execute(
            text(
                "SELECT customer_id FROM customer_aliases "
                "WHERE normalized = 'altbau rheinland gmbh'"
            )
        ).scalar_one()
        assert found == "CUS_0008"

        by_sku = seeded.execute(
            text("SELECT product_id FROM product_aliases WHERE normalized = 'bf-50'")
        ).scalar_one()
        assert by_sku == "PRD_0007"


class TestValueFidelity:
    def test_money_keeps_its_scale_and_type(self, seeded: Session) -> None:
        entry = seeded.get(PriceEntryRow, "PE_0001")
        assert entry is not None
        assert isinstance(entry.unit_price, Decimal)
        assert not isinstance(entry.unit_price, float)
        assert str(entry.unit_price) == "1234.5600"
        assert entry.unit_price.as_tuple().exponent == -4
        assert entry.min_qty == 1

    def test_money_at_the_two_decimal_scale_stays_at_two(self, seeded: Session) -> None:
        customer = seeded.get(CustomerRow, "CUS_0001")
        assert customer is not None
        assert isinstance(customer.credit_limit, Decimal)
        assert str(customer.credit_limit) == "50000.00"

    def test_percentages_keep_two_decimals(self, seeded: Session) -> None:
        rule = seeded.get(DiscountRuleRow, "DSC_0004")
        assert rule is not None
        assert isinstance(rule.percent, Decimal)
        assert str(rule.percent) == "4.50"

    def test_dates_read_back_as_dates_not_datetimes(self, seeded: Session) -> None:
        book = seeded.get(PriceBookRow, "BK-EU-2026")
        assert book is not None
        assert book.effective_from == date(2026, 1, 1)
        assert not isinstance(book.effective_from, datetime)
        assert book.effective_to is None

    def test_a_stock_snapshot_reads_back_as_an_aware_instant(self, seeded: Session) -> None:
        level = seeded.get(StockLevelRow, {"location_code": "WAW", "product_id": "PRD_0001"})
        assert level is not None
        assert isinstance(level.as_of, datetime)
        assert level.as_of.tzinfo is not None
        assert level.as_of == STOCK_AS_OF

    def test_an_absent_value_stays_absent(self, seeded: Session) -> None:
        """A nullable field the dataset leaves empty must stay empty."""
        customer = seeded.get(CustomerRow, "CUS_0002")
        assert customer is not None
        assert customer.notes is None

    def test_enums_round_trip_as_members(self, seeded: Session) -> None:
        alias = seeded.get(ProductAliasRow, {"product_id": "PRD_0011", "normalized": "vlv-bl-20"})
        assert alias is not None
        assert alias.kind.value == "SKU"

        rule = seeded.get(DiscountRuleRow, "DSC_0003")
        assert rule is not None
        assert rule.scope.value == "CUSTOMER"
        assert rule.scope_ref == "CUS_0001"

    def test_every_seeded_row_can_be_loaded_as_a_domain_object(self, seeded: Session) -> None:
        """Read from the database and validate against the domain.

        This is the check the later phases depend on: whatever the repositories
        do, the *rows* have to be loadable as domain objects, which means the
        identifiers satisfy their contracts and the values obey the invariants
        the domain enforces (a scoped price, inbound stock with an ETA).
        """
        entries = seeded.scalars(select(PriceEntryRow)).all()
        assert entries
        for entry in entries:
            PriceEntry.model_validate(
                {
                    "price_entry_id": entry.price_entry_id,
                    "product_id": entry.product_id,
                    "price_book_code": entry.price_book_code,
                    "customer_id": entry.customer_id,
                    "customer_tier": entry.customer_tier,
                    "min_qty": entry.min_qty,
                    "unit_price": entry.unit_price,
                    "currency": entry.currency,
                    "effective_from": entry.effective_from,
                    "effective_to": entry.effective_to,
                }
            )

        levels_written = seeded.scalars(select(StockLevelRow)).all()
        assert levels_written
        for level in levels_written:
            StockLevel.model_validate(
                {
                    "product_id": level.product_id,
                    "location": level.location_code,
                    "on_hand_qty": level.on_hand_qty,
                    "reserved_qty": level.reserved_qty,
                    "inbound_qty": level.inbound_qty,
                    "inbound_eta": level.inbound_eta,
                    "as_of": level.as_of,
                }
            )


class TestIdempotence:
    def test_seeding_twice_changes_nothing(self, seeded: Session) -> None:
        before = _dump(seeded)
        report = seed_dataset(seeded)
        seeded.commit()

        assert report.inserted == 0
        assert report.updated == 0
        assert report.unchanged == report.total == sum(row_counts().values())
        assert _dump(seeded) == before

    def test_a_hand_edited_row_is_corrected(self, seeded: Session) -> None:
        """The dataset is the source of truth, not merely an initial insert."""
        before = _dump(seeded)
        seeded.execute(
            text("UPDATE customers SET display_name = 'Oops' WHERE customer_id = 'CUS_0001'")
        )
        seeded.commit()

        report = seed_dataset(seeded)
        seeded.commit()

        assert (report.inserted, report.updated) == (0, 1)
        customer = seeded.get(CustomerRow, "CUS_0001")
        assert customer is not None
        assert customer.display_name == "Nordwind"
        assert _dump(seeded) == before

    def test_rows_outside_the_dataset_are_left_alone(self, seeded: Session) -> None:
        _insert_extra_customer(seeded)

        report = seed_dataset(seeded)
        seeded.commit()

        assert report.inserted == 0
        assert report.updated == 0
        assert report.unchanged == report.total
        assert _count(seeded, "customers") == row_counts()["customers"] + 1


class TestReset:
    def test_reset_removes_every_dataset_row(self, seeded: Session) -> None:
        deleted = reset(seeded)
        seeded.commit()

        assert deleted == sum(row_counts().values())
        for table in row_counts():
            assert _count(seeded, table) == 0, table

    def test_reset_removes_only_dataset_rows(self, seeded: Session) -> None:
        _insert_extra_customer(seeded)

        reset(seeded)
        seeded.commit()

        remaining = seeded.execute(text("SELECT customer_id FROM customers")).scalars().all()
        assert remaining == ["CUS_9900"]

    def test_reset_then_reseed_reproduces_the_same_dataset(self, seeded: Session) -> None:
        before = _dump(seeded)

        report = reset_and_seed(seeded)
        seeded.commit()

        assert report.deleted == sum(row_counts().values())
        assert report.inserted == report.total
        assert _dump(seeded) == before

    def test_reset_is_refused_while_business_rows_reference_the_data(self, seeded: Session) -> None:
        """A database that has produced a quote is no longer resettable.

        The refusal is the point: deleting a product a quotation line points at
        would either cascade into the quotation or rewrite history, and neither
        is a decision this layer gets to make.
        """
        before = _dump(seeded)
        _insert_quotation(seeded)

        with pytest.raises(SeedResetBlockedError, match="still referenced"):
            reset(seeded)
        seeded.rollback()

        assert _dump(seeded) == before

    def test_a_failed_reset_and_reseed_leaves_the_previous_dataset(self, seeded: Session) -> None:
        """Atomicity is the reason the loader never commits.

        Half a dataset - rows deleted, nothing written back - would be worse than
        either state, so the failure has to roll back to the old one.
        """
        before = _dump(seeded)
        _insert_quotation(seeded)

        with pytest.raises(SeedResetBlockedError):
            reset_and_seed(seeded)
        seeded.rollback()

        assert _dump(seeded) == before


class TestBootstrap:
    def test_seeding_without_a_schema_says_what_to_do(self, session: Session) -> None:
        """A clear instruction beats an OperationalError about a missing table."""
        session.execute(text("DROP TABLE holidays"))
        session.commit()

        with pytest.raises(SeedSchemaError, match="alembic upgrade head"):
            seed_dataset(session)

    def test_migrating_an_empty_database_then_seeding_works(self, tmp_path: Path) -> None:
        """The documented bootstrap path, end to end.

        Deliberately not using the shared migrated fixture: this test exists to
        prove that an *empty* database plus the migration plus the seed produces
        the full dataset, which is what a fresh clone does.
        """
        path = tmp_path / "fresh.db"
        command.upgrade(make_alembic_config(path), "head")

        database = Database.create(f"sqlite:///{path}")
        try:
            with database.session() as session:
                report = reset_and_seed(session)
        finally:
            database.dispose()

        assert report.inserted == sum(row_counts().values())
        assert report.deleted == 0

        reopened = Database.create(f"sqlite:///{path}")
        try:
            with reopened.session() as session:
                assert _count(session, "products") == row_counts()["products"]
                assert session.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        finally:
            reopened.dispose()
