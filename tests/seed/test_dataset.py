"""The dataset as data, before a database is involved.

These tests pin exactly what the demo dataset contains: the counts, the shape of
the identifier space, the internal consistency of every reference, and the
awkward cases that exist on purpose (a discontinued product, an expired price, a
switched-off rule, a customer on credit hold, an ambiguous part number).

They fail when the data changes silently, which is the point of a versioned
dataset: changing a price is allowed, changing a price without bumping
:data:`~rfq_agent.seed.SEED_VERSION` and adjusting what depends on it is not.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter

from rfq_agent.domain.ids import CustomerId, PriceEntryId, ProductId
from rfq_agent.domain.pricing import PriceEntry
from rfq_agent.domain.stock import LocationCode, StockLevel
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.seed import (
    CALENDAR_YEAR,
    CURRENCY,
    SEED_VERSION,
    TIER_STANDARD,
    normalize_alias,
    row_counts,
    total_rows,
)
from rfq_agent.seed.dataset import levels
from rfq_agent.seed.loader import _GENERATED_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Mapping

#: What the dataset contains. These numbers change only with :data:`SEED_VERSION`
#: - a change here means the demo database gained or lost rows on purpose.
EXPECTED_COUNTS: Mapping[str, int] = {
    "carrier_services": 4,
    "customer_aliases": 18,
    "customers": 8,
    "discount_rules": 6,
    "holidays": 75,
    "price_books": 2,
    "price_entries": 27,
    "product_aliases": 16,
    "product_families": 3,
    "products": 18,
    "stock_levels": 27,
    "warehouses": 2,
}

#: Tables that hold text whose natural key is not the primary key alone.
PRODUCT_SKU_COLUMN = "sku"


def rows() -> list[Base]:
    """Every dataset row, freshly built."""
    return [row for level in levels() for row in level]


def by_table(table: str) -> list[Base]:
    """Every dataset row in one table, freshly built."""
    return [row for row in rows() if type(row).__tablename__ == table]


def primary_key(row: Base) -> tuple[object, ...]:
    """The primary key of ``row`` as a tuple."""
    return tuple(getattr(row, column.key) for column in type(row).__mapper__.primary_key)


def dump() -> list[tuple[str, tuple[object, ...], tuple[tuple[str, object], ...]]]:
    """A comparable snapshot of the dataset: table, key and every managed value."""
    snapshot: list[tuple[str, tuple[object, ...], tuple[tuple[str, object], ...]]] = []
    for row in rows():
        values = tuple(
            (column.key, getattr(row, column.key))
            for column in type(row).__mapper__.columns
            if column.key not in _GENERATED_COLUMNS
        )
        snapshot.append((type(row).__tablename__, primary_key(row), values))
    return sorted(snapshot, key=lambda entry: (entry[0], repr(entry[1])))


class TestShape:
    def test_dataset_version_is_declared(self) -> None:
        assert SEED_VERSION == "2026.10.1"

    @pytest.mark.parametrize(("table", "expected"), sorted(EXPECTED_COUNTS.items()))
    def test_expected_entity_counts(self, table: str, expected: int) -> None:
        assert row_counts()[table] == expected

    def test_no_table_outside_the_expected_set_is_written(self) -> None:
        assert set(row_counts()) == set(EXPECTED_COUNTS)

    def test_total_row_count(self) -> None:
        assert total_rows() == sum(EXPECTED_COUNTS.values()) == 206

    def test_the_briefing_numbers_are_met(self) -> None:
        """Three families, eighteen products, eight customers, two warehouses."""
        assert len(by_table("product_families")) == 3
        assert len(by_table("products")) == 18
        assert len(by_table("customers")) == 8
        assert len(by_table("warehouses")) == 2

    def test_the_products_are_evenly_spread_over_the_families(self) -> None:
        """Six per family, so no family is a rounding error in the demo."""
        families = [family.family_code for family in by_table("product_families")]  # type: ignore[attr-defined]
        per_family = {
            family: sum(
                1
                for product in by_table("products")
                if product.family_code == family  # type: ignore[attr-defined]
            )
            for family in families
        }
        assert per_family == {"FAM_PUMPS": 6, "FAM_VALVES": 6, "FAM_INSTR": 6}

    def test_every_row_carries_its_own_object(self) -> None:
        """Two builds share no instances.

        A row belongs to one session, so handing the same object out twice would
        make a second seeding run depend on what the first one did to it.
        """
        first, second = rows(), rows()
        assert len(first) == len(second)
        assert not {id(row) for row in first} & {id(row) for row in second}

    def test_rebuilding_the_dataset_gives_identical_values(self) -> None:
        """Determinism starts here: no clock, no randomness, no set iteration."""
        assert dump() == dump()


class TestConsistency:
    def test_primary_keys_are_unique_within_each_table(self) -> None:
        for table in sorted(EXPECTED_COUNTS):
            keys = [primary_key(row) for row in by_table(table)]
            duplicates = [key for key in set(keys) if keys.count(key) > 1]
            assert duplicates == [], f"{table} has duplicate keys: {duplicates}"

    def test_skus_and_natural_keys_are_unique(self) -> None:
        skus = [product.sku for product in by_table("products")]  # type: ignore[attr-defined]
        assert len(set(skus)) == len(skus)

        natural_keys = [
            (entry.price_book_code, entry.product_id, entry.min_qty, entry.effective_from)  # type: ignore[attr-defined]
            for entry in by_table("price_entries")
        ]
        assert len(set(natural_keys)) == len(natural_keys)

    def test_every_column_the_dataset_manages_is_stated_explicitly(self) -> None:
        """No value may come from a column default.

        The loader compares what is in the dataset against what is stored, so a
        field that only exists as a default would make that comparison a guess.
        """
        unstated: list[str] = []
        for row in rows():
            for column in type(row).__mapper__.columns:
                if column.key in _GENERATED_COLUMNS or column.nullable:
                    continue
                if getattr(row, column.key) is None:
                    unstated.append(f"{type(row).__tablename__}.{column.key}")
        assert unstated == []

    def test_identifiers_satisfy_the_domain_contracts(self) -> None:
        """The seed must use identifiers the domain would accept as its own."""
        customer_ids = TypeAdapter(CustomerId)
        product_ids = TypeAdapter(ProductId)
        price_entry_ids = TypeAdapter(PriceEntryId)
        locations = TypeAdapter(LocationCode)

        for customer in by_table("customers"):
            customer_ids.validate_python(customer.customer_id)  # type: ignore[attr-defined]
        for product in by_table("products"):
            product_ids.validate_python(product.product_id)  # type: ignore[attr-defined]
        for entry in by_table("price_entries"):
            price_entry_ids.validate_python(entry.price_entry_id)  # type: ignore[attr-defined]
        for warehouse in by_table("warehouses"):
            locations.validate_python(warehouse.location_code)  # type: ignore[attr-defined]
        for carrier in by_table("carrier_services"):
            locations.validate_python(carrier.origin_location)  # type: ignore[attr-defined]

    def test_every_currency_is_eur(self) -> None:
        for row in rows():
            mapper = type(row).__mapper__
            for column in ("currency", "default_currency"):
                if column in mapper.columns and getattr(row, column) != CURRENCY:
                    raise AssertionError(f"{type(row).__tablename__}.{column} is not {CURRENCY}")

    def test_every_product_belongs_to_a_declared_family(self) -> None:
        families = {family.family_code for family in by_table("product_families")}  # type: ignore[attr-defined]
        for product in by_table("products"):
            assert product.family_code in families  # type: ignore[attr-defined]

    def test_aliases_point_at_rows_in_the_dataset(self) -> None:
        """Referential integrity is checked here *before* a database is built.

        When this fails the failure is in the data, not in the loader, and that
        is worth being able to tell apart.
        """
        product_ids = {product.product_id for product in by_table("products")}  # type: ignore[attr-defined]
        customer_ids = {customer.customer_id for customer in by_table("customers")}  # type: ignore[attr-defined]

        for alias in by_table("product_aliases"):
            assert alias.product_id in product_ids  # type: ignore[attr-defined]
        for alias in by_table("customer_aliases"):
            assert alias.customer_id in customer_ids  # type: ignore[attr-defined]

    def test_every_alias_is_stored_in_normalised_form(self) -> None:
        for table in ("product_aliases", "customer_aliases"):
            for alias in by_table(table):
                assert alias.normalized == normalize_alias(alias.alias)  # type: ignore[attr-defined]
                assert normalize_alias(alias.normalized) == alias.normalized  # type: ignore[attr-defined]
                assert alias.kind in AliasKind  # type: ignore[attr-defined]

    def test_every_price_entry_is_internally_consistent(self) -> None:
        product_ids = {product.product_id for product in by_table("products")}  # type: ignore[attr-defined]
        book_codes = {book.price_book_code for book in by_table("price_books")}  # type: ignore[attr-defined]
        customer_ids = {customer.customer_id for customer in by_table("customers")}  # type: ignore[attr-defined]

        for entry in by_table("price_entries"):
            assert entry.product_id in product_ids  # type: ignore[attr-defined]
            assert entry.price_book_code in book_codes  # type: ignore[attr-defined]
            assert entry.effective_to is None or entry.effective_to >= entry.effective_from  # type: ignore[attr-defined]
            assert entry.min_qty >= 1  # type: ignore[attr-defined]

            scoped_to_customer = entry.customer_id is not None  # type: ignore[attr-defined]
            if scoped_to_customer:
                assert entry.customer_id in customer_ids  # type: ignore[attr-defined]
                assert entry.customer_tier is None  # type: ignore[attr-defined]
            else:
                assert entry.customer_tier == TIER_STANDARD  # type: ignore[attr-defined]

    def test_every_price_entry_validates_as_a_domain_object(self) -> None:
        """The domain is stricter than the table, and the dataset satisfies it.

        ``price_entries`` permits an entry scoped to neither a customer nor a
        tier; :class:`~rfq_agent.domain.pricing.PriceEntry` does not, because an
        unscoped price is a price nobody can defend. Every seeded entry can be
        loaded as a domain object without special-casing.
        """
        for entry in by_table("price_entries"):
            validated = PriceEntry.model_validate(
                {
                    "price_entry_id": entry.price_entry_id,  # type: ignore[attr-defined]
                    "product_id": entry.product_id,  # type: ignore[attr-defined]
                    "price_book_code": entry.price_book_code,  # type: ignore[attr-defined]
                    "customer_id": entry.customer_id,  # type: ignore[attr-defined]
                    "customer_tier": entry.customer_tier,  # type: ignore[attr-defined]
                    "min_qty": entry.min_qty,  # type: ignore[attr-defined]
                    "unit_price": entry.unit_price,  # type: ignore[attr-defined]
                    "currency": entry.currency,  # type: ignore[attr-defined]
                    "effective_from": entry.effective_from,  # type: ignore[attr-defined]
                    "effective_to": entry.effective_to,  # type: ignore[attr-defined]
                }
            )
            assert validated.currency == CURRENCY

    def test_money_carries_the_scale_its_column_declares(self) -> None:
        """Four decimals for unit prices, two for money, two for percentages."""
        for entry in by_table("price_entries"):
            price = Decimal(str(entry.unit_price))  # type: ignore[attr-defined]
            assert price.as_tuple().exponent == -4, entry.price_entry_id  # type: ignore[attr-defined]
        for customer in by_table("customers"):
            limit = Decimal(str(customer.credit_limit))  # type: ignore[attr-defined]
            assert limit.as_tuple().exponent == -2, customer.customer_id  # type: ignore[attr-defined]
        for rule in by_table("discount_rules"):
            percent = Decimal(str(rule.percent))  # type: ignore[attr-defined]
            assert percent.as_tuple().exponent == -2, rule.rule_id  # type: ignore[attr-defined]

    def test_every_stock_row_is_internally_consistent(self) -> None:
        product_ids = {product.product_id for product in by_table("products")}  # type: ignore[attr-defined]
        locations = {warehouse.location_code for warehouse in by_table("warehouses")}  # type: ignore[attr-defined]

        seen: set[tuple[str, str]] = set()
        for level in by_table("stock_levels"):
            assert level.product_id in product_ids  # type: ignore[attr-defined]
            assert level.location_code in locations  # type: ignore[attr-defined]
            assert level.reserved_qty <= level.on_hand_qty  # type: ignore[attr-defined]
            assert level.on_hand_qty >= 0  # type: ignore[attr-defined]
            assert level.reserved_qty >= 0  # type: ignore[attr-defined]
            if level.inbound_qty > 0:  # type: ignore[attr-defined]
                assert level.inbound_eta is not None, level.product_id  # type: ignore[attr-defined]
            assert (level.location_code, level.product_id) not in seen  # type: ignore[attr-defined]
            seen.add((level.location_code, level.product_id))  # type: ignore[attr-defined]

    def test_every_stock_row_validates_as_a_domain_object(self) -> None:
        """Including the domain's own rule that inbound stock needs an ETA."""
        for level in by_table("stock_levels"):
            validated = StockLevel.model_validate(
                {
                    "product_id": level.product_id,  # type: ignore[attr-defined]
                    "location": level.location_code,  # type: ignore[attr-defined]
                    "on_hand_qty": level.on_hand_qty,  # type: ignore[attr-defined]
                    "reserved_qty": level.reserved_qty,  # type: ignore[attr-defined]
                    "inbound_qty": level.inbound_qty,  # type: ignore[attr-defined]
                    "inbound_eta": level.inbound_eta,  # type: ignore[attr-defined]
                    "as_of": level.as_of,  # type: ignore[attr-defined]
                }
            )
            assert validated.available_qty == level.on_hand_qty - level.reserved_qty  # type: ignore[attr-defined]

    def test_every_carrier_ships_from_a_declared_warehouse(self) -> None:
        locations = {warehouse.location_code for warehouse in by_table("warehouses")}  # type: ignore[attr-defined]
        for carrier in by_table("carrier_services"):
            assert carrier.origin_location in locations  # type: ignore[attr-defined]
            assert carrier.transit_days_max >= carrier.transit_days_min  # type: ignore[attr-defined]
            assert 0 <= carrier.cutoff_hour_utc <= 23  # type: ignore[attr-defined]

    def test_discount_rules_are_internally_consistent(self) -> None:
        customer_ids = {customer.customer_id for customer in by_table("customers")}  # type: ignore[attr-defined]
        for rule in by_table("discount_rules"):
            assert Decimal("0") <= Decimal(str(rule.percent)) <= Decimal("100")  # type: ignore[attr-defined]
            assert rule.effective_to is None or rule.effective_to >= rule.effective_from  # type: ignore[attr-defined]
            if str(rule.scope) == "CUSTOMER":  # type: ignore[attr-defined]
                assert rule.scope_ref in customer_ids  # type: ignore[attr-defined]
            else:
                assert rule.scope_ref is None  # type: ignore[attr-defined]


class TestCalendar:
    def test_calendar_covers_every_country_in_the_dataset(self) -> None:
        """A delivery promise counted in working days needs the buyer's calendar.

        Origin countries come from the warehouses, destination countries from the
        customers; both are needed, so a missing country is a hole in the
        delivery calculation rather than a cosmetic gap.
        """
        covered = {holiday.country_code for holiday in by_table("holidays")}  # type: ignore[attr-defined]
        warehouse_countries = {warehouse.country_code for warehouse in by_table("warehouses")}  # type: ignore[attr-defined]
        customer_countries = {customer.country_code for customer in by_table("customers")}  # type: ignore[attr-defined]

        assert warehouse_countries <= covered
        assert customer_countries <= covered

    def test_holidays_are_dated_in_the_calendar_year_and_unique(self) -> None:
        seen: set[tuple[str, date]] = set()
        for holiday in by_table("holidays"):
            assert holiday.holiday_date.year == CALENDAR_YEAR  # type: ignore[attr-defined]
            key = (holiday.country_code, holiday.holiday_date)  # type: ignore[attr-defined]
            assert key not in seen
            seen.add(key)

    def test_the_movable_feasts_of_2026_are_right(self) -> None:
        """Easter Sunday 2026 is 5 April, so the derived Mondays follow.

        Pinned because a calendar that is wrong by a week silently moves every
        delivery date that crosses it, and nothing else in the suite would
        notice.
        """
        by_country_date = {
            (holiday.country_code, holiday.holiday_date): holiday.name  # type: ignore[attr-defined]
            for holiday in by_table("holidays")
        }
        easter_sunday = date(2026, 4, 5)
        assert by_country_date[("DE", date(2026, 4, 3))] == "Good Friday"
        assert by_country_date[("DE", date(2026, 4, 6))] == "Easter Monday"
        assert by_country_date[("DE", date(2026, 5, 14))] == "Ascension Day"
        assert by_country_date[("DE", date(2026, 5, 25))] == "Whit Monday"
        assert by_country_date[("PL", date(2026, 6, 4))] == "Corpus Christi"
        assert (easter_sunday.month, easter_sunday.day) == (4, 5)


class TestDeliberateCases:
    """The awkward data is present on purpose; these tests say so out loud."""

    def test_there_is_one_discontinued_product(self) -> None:
        inactive = [product.sku for product in by_table("products") if not product.active]  # type: ignore[attr-defined]
        assert inactive == ["PMP-D-300"]

    def test_the_discontinued_product_has_only_an_expired_price(self) -> None:
        """EXPIRED, not MISSING: the entry exists and its window has closed."""
        prices = [entry for entry in by_table("price_entries") if entry.product_id == "PRD_0006"]  # type: ignore[attr-defined]
        assert [entry.price_entry_id for entry in prices] == ["PE_0020"]  # type: ignore[attr-defined]
        assert prices[0].effective_to == date(2026, 6, 30)  # type: ignore[attr-defined]

    def test_one_product_has_no_list_price_at_all(self) -> None:
        """The actuator is contract-only, which is the PRICE MISSING case."""
        product_id = "PRD_0012"
        list_entries = [
            entry
            for entry in by_table("price_entries")
            if entry.product_id == product_id  # type: ignore[attr-defined]
        ]
        assert [entry.price_book_code for entry in list_entries] == ["BK-EU-CONTRACT-2026"]  # type: ignore[attr-defined]
        assert [entry.customer_id for entry in list_entries] == ["CUS_0004"]  # type: ignore[attr-defined]

    def test_one_customer_is_on_credit_hold_and_one_is_deactivated(self) -> None:
        on_hold = [
            customer.customer_id for customer in by_table("customers") if customer.credit_hold
        ]  # type: ignore[attr-defined]
        inactive = [
            customer.customer_id for customer in by_table("customers") if not customer.active
        ]  # type: ignore[attr-defined]
        assert on_hold == ["CUS_0007"]
        assert inactive == ["CUS_0008"]

    def test_there_is_one_switched_off_rule_and_one_expired_rule(self) -> None:
        inactive = [rule.rule_id for rule in by_table("discount_rules") if not rule.active]  # type: ignore[attr-defined]
        expired = [
            rule.rule_id
            for rule in by_table("discount_rules")
            if rule.active
            and rule.effective_to is not None
            and rule.effective_to < date(2026, 10, 1)  # type: ignore[attr-defined]
        ]
        assert inactive == ["DSC_0006"]
        assert expired == ["DSC_0005"]

    def test_there_is_a_stock_row_with_nothing_available_but_stock_inbound(self) -> None:
        zero_available = [
            level
            for level in by_table("stock_levels")
            if level.on_hand_qty - level.reserved_qty == 0  # type: ignore[attr-defined]
        ]
        assert [(level.location_code, level.product_id) for level in zero_available] == [
            (  # type: ignore[attr-defined]
                "BER",
                "PRD_0011",
            )
        ]
        assert zero_available[0].inbound_qty > 0  # type: ignore[attr-defined]

    def test_not_every_product_is_stocked_in_every_warehouse(self) -> None:
        """ "Not stocked here" is the absence of a row, not a zero quantity."""
        products = {product.product_id for product in by_table("products")}  # type: ignore[attr-defined]
        for location in ("WAW", "BER"):
            stocked = {
                level.product_id
                for level in by_table("stock_levels")
                if level.location_code == location  # type: ignore[attr-defined]
            }
            assert stocked < products, f"{location} stocks the entire catalogue"

    def test_one_part_number_is_deliberately_ambiguous(self) -> None:
        """``PMP-A-100`` is one pump's SKU and the other pump's alias.

        A customer writing "PMP-A-100" may mean either; there is no defensible
        automatic choice, so the data contains a real ambiguity for the resolver
        to report rather than an adversarial fixture to detect.
        """
        sku_keys = {
            normalize_alias(product.sku): product.product_id  # type: ignore[attr-defined]
            for product in by_table("products")
        }
        collisions = {
            (sku_keys[normalize_alias(alias.alias)], alias.product_id)  # type: ignore[attr-defined]
            for alias in by_table("product_aliases")
            if normalize_alias(alias.alias) in sku_keys  # type: ignore[attr-defined]
        }
        assert collisions == {("PRD_0001", "PRD_0002")}

    def test_the_discontinued_product_has_no_stock_row(self) -> None:
        stocked = {level.product_id for level in by_table("stock_levels")}  # type: ignore[attr-defined]
        assert "PRD_0006" not in stocked


class TestNormalisation:
    def test_casefolding_agrees_with_german_text(self) -> None:
        assert normalize_alias("STRASSE") == normalize_alias("Straße") == "strasse"

    def test_unicode_is_nfc_normalised(self) -> None:
        """``Böhm`` written with a combining diaeresis must match the precomposed one."""
        assert normalize_alias("Bo\u0308hm") == normalize_alias("Böhm") == "böhm"

    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        assert normalize_alias("  Compagnie \t Fluides \n") == "compagnie fluides"

    def test_punctuation_is_preserved(self) -> None:
        """Dropping punctuation is a matching decision, not a storage decision."""
        assert normalize_alias("PMP-A-100") == "pmp-a-100"
        assert normalize_alias("Nordwind Industrie GmbH") == "nordwind industrie gmbh"

    def test_normalisation_is_idempotent(self) -> None:
        for alias in by_table("customer_aliases"):
            once = normalize_alias(alias.alias)  # type: ignore[attr-defined]
            assert normalize_alias(once) == once

    def test_a_non_string_is_refused(self) -> None:
        with pytest.raises(TypeError, match="expected str"):
            normalize_alias(None)  # type: ignore[arg-type]

    def test_datetime_values_are_timezone_aware(self) -> None:
        """A naive timestamp in the dataset would be rejected only at write time."""
        for row in rows():
            mapper = type(row).__mapper__
            if "as_of" in mapper.columns:
                assert isinstance(row.as_of, datetime)  # type: ignore[attr-defined]
                assert row.as_of.tzinfo is not None  # type: ignore[attr-defined]
