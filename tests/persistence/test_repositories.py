"""The read boundary: what comes out of the database, and what cannot.

Every test here runs against a database built by the real migration and filled by
the real seed, and reads it through a **second session** - one that did no
writing - so the values under test really travel row → caller-visible object
instead of being handed back out of the identity map that wrote them.

Three things are being pinned at once:

* *fidelity*: money stays ``Decimal`` at its column's scale, enums arrive as
  members, dates as ``date`` and instants as aware UTC. A value that quietly
  changes type at this seam becomes a wrong quotation four phases later;
* *no leakage*: no public read method returns a SQLAlchemy row, a list or a
  dict - and the argument table below is checked for completeness, so a new read
  method cannot be added without being exercised here;
* *no decisions*: the deliberate ``PMP-A-100`` collision comes back as two
  matches with ``is_ambiguous`` set, a discontinued product is returned like any
  other, and a price entry whose window has closed is returned alongside the
  valid ones. The boundary reports; the resolver and the pricing rule decide.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Callable, Iterator
from dataclasses import fields
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from sqlalchemy import text

import rfq_agent.seed.normalize as seed_normalize
from rfq_agent.domain.policy import DiscountScope
from rfq_agent.domain.pricing import PriceEntry
from rfq_agent.domain.resolution import normalize_alias
from rfq_agent.domain.stock import StockLevel
from rfq_agent.persistence import Database, read_models
from rfq_agent.persistence import repositories as repositories_module
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.persistence.models import CustomerAliasRow, ProductAliasRow
from rfq_agent.persistence.read_models import (
    CarrierServiceRecord,
    CustomerRecord,
    DiscountRuleRecord,
    HolidayRecord,
    MatchSource,
    PriceBookRecord,
    ProductFamilyRecord,
    ProductRecord,
    WarehouseRecord,
)
from rfq_agent.persistence.repositories import (
    BusinessReader,
    CatalogRepository,
    CustomerRepository,
    DeliveryRepository,
    DiscountRepository,
    PricingRepository,
    StockRepository,
)
from rfq_agent.seed import STOCK_AS_OF, reset_and_seed
from tests.persistence.conftest import make_alembic_config

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Every repository class that makes up the read boundary.
REPOSITORIES = (
    CustomerRepository,
    CatalogRepository,
    PricingRepository,
    StockRepository,
    DeliveryRepository,
    DiscountRepository,
)

#: One read, called with the arguments that exercise it.
Call = Callable[[BusinessReader], object]

#: Every public read method, with arguments that exercise it.
#:
#: ``test_every_read_method_is_covered_below`` compares this table against the
#: classes, so a new read cannot be added without also being checked for row
#: leakage.
CALLS: dict[tuple[type, str], dict[str, object]] = {
    (CustomerRepository, "get"): {"customer_id": "CUS_0001"},
    (CustomerRepository, "search"): {"text": "Nordwind"},
    (CatalogRepository, "get"): {"product_id": "PRD_0001"},
    (CatalogRepository, "search"): {"text": "PMP-A-100"},
    (CatalogRepository, "family"): {"family_code": "FAM_PUMPS"},
    (CatalogRepository, "families"): {},
    (PricingRepository, "book"): {"price_book_code": "BK-EU-2026"},
    (PricingRepository, "books"): {},
    (PricingRepository, "entry"): {"price_entry_id": "PE_0001"},
    (PricingRepository, "entries_for_products"): {"product_ids": ("PRD_0001",)},
    (StockRepository, "warehouse"): {"location_code": "WAW"},
    (StockRepository, "warehouses"): {},
    (StockRepository, "level"): {"location_code": "WAW", "product_id": "PRD_0001"},
    (StockRepository, "levels_for_products"): {"product_ids": ("PRD_0001",)},
    (DeliveryRepository, "service"): {"service_code": "DHL-EXP"},
    (DeliveryRepository, "services"): {},
    (DeliveryRepository, "services_from"): {"location_code": "WAW"},
    (DeliveryRepository, "holidays"): {"country_codes": ("PL",)},
    (DiscountRepository, "rule"): {"rule_id": "DSC_0003"},
    (DiscountRepository, "rules"): {},
}


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def reading(seeded: Session, db: Database) -> Iterator[Session]:
    """An open session that has only ever read, over the seeded database.

    ``seeded`` is requested as a dependency, not as a value: the database has to
    be filled *before* the reading session opens, and nothing here needs the
    writing session itself.
    """
    del seeded  # dependency only
    with db.session_factory() as active:
        yield active


@pytest.fixture
def reader(reading: Session) -> BusinessReader:
    """The read boundary, over a session that did no writing."""
    return BusinessReader.for_session(reading)


def _walk(value: object) -> Iterator[object]:
    """Yield ``value`` and everything reachable inside it."""
    yield value
    if isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            yield from _walk(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from _walk(getattr(value, field.name))


def _module_tree(module: object) -> ast.Module:
    """Parse ``module``'s source, for the structural checks."""
    source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return ast.parse(source)


class TestMapping:
    """Row → caller-visible object, field by field."""

    def test_a_customer_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.customers.get("CUS_0001") == CustomerRecord(
            customer_id="CUS_0001",
            legal_name="Nordwind Industrie GmbH",
            display_name="Nordwind",
            country_code="DE",
            default_currency="EUR",
            payment_terms_days=30,
            credit_limit=Decimal("50000.00"),
            credit_hold=False,
            active=True,
            notes="Key account. Contract prices agreed for pumps and butterfly valves.",
        )

    def test_a_product_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.catalog.get("PRD_0001") == ProductRecord(
            product_id="PRD_0001",
            sku="PMP-A-100",
            family_code="FAM_PUMPS",
            name="Centrifugal pump PMP-A-100",
            description="Cast-iron centrifugal pump, 100 mm flange",
            uom="EA",
            active=True,
        )

    def test_a_family_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.catalog.family("FAM_PUMPS") == ProductFamilyRecord(
            family_code="FAM_PUMPS",
            name="Centrifugal pumps",
            description="Industrial centrifugal pumps",
            sort_order=1,
        )

    def test_a_price_book_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.pricing.book("BK-EU-2026") == PriceBookRecord(
            price_book_code="BK-EU-2026",
            name="EU list 2026",
            currency="EUR",
            customer_tier="STANDARD",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            active=True,
        )

    def test_a_warehouse_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.stock.warehouse("BER") == WarehouseRecord(
            location_code="BER",
            name="Berlin DC",
            city="Berlin",
            country_code="DE",
            active=True,
        )

    def test_a_carrier_service_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.delivery.service("DHL-EXP") == CarrierServiceRecord(
            service_code="DHL-EXP",
            carrier="DHL",
            name="Express 24",
            origin_location="WAW",
            transit_days_min=1,
            transit_days_max=2,
            cutoff_hour_utc=12,
            runs_on_weekends=False,
            active=True,
        )

    def test_a_holiday_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert HolidayRecord(
            country_code="PL",
            holiday_date=date(2026, 5, 1),
            name="Labour Day",
        ) in reader.delivery.holidays(("PL",))

    def test_a_discount_rule_maps_to_its_read_model(self, reader: BusinessReader) -> None:
        assert reader.discounts.rule("DSC_0003") == DiscountRuleRecord(
            rule_id="DSC_0003",
            scope=DiscountScope.CUSTOMER,
            scope_ref="CUS_0001",
            percent=Decimal("3.00"),
            min_qty=None,
            min_order_value=None,
            requires_approval=False,
            priority=30,
            active=True,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
        )

    def test_one_price_entry_maps_to_the_domain_object(self, reader: BusinessReader) -> None:
        """Prices cross as the domain's own value object, not a look-alike."""
        entry = reader.pricing.entry("PE_0001")
        assert entry == PriceEntry(
            price_entry_id="PE_0001",
            product_id="PRD_0001",
            price_book_code="BK-EU-2026",
            customer_id=None,
            customer_tier="STANDARD",
            min_qty=1,
            unit_price=Decimal("1234.5600"),
            currency="EUR",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
        assert type(entry).__module__.startswith("rfq_agent.domain")

    def test_one_stock_level_maps_to_the_domain_object(self, reader: BusinessReader) -> None:
        level = reader.stock.level("WAW", "PRD_0001")
        assert level == StockLevel(
            product_id="PRD_0001",
            location="WAW",
            on_hand_qty=120,
            reserved_qty=20,
            inbound_qty=0,
            inbound_eta=None,
            as_of=STOCK_AS_OF,
        )
        assert type(level).__module__.startswith("rfq_agent.domain")


class TestBoundary:
    """No row, list or dict reaches a caller."""

    def test_every_read_method_is_covered_below(self) -> None:
        declared = {
            (repository, name)
            for repository in REPOSITORIES
            for name in vars(repository)
            if not name.startswith("_")
        }
        assert declared == set(CALLS), "add the new read method to CALLS"

    @pytest.mark.parametrize(
        ("repository", "method"),
        sorted(CALLS, key=lambda pair: (pair[0].__name__, pair[1])),
        ids=lambda value: value if isinstance(value, str) else value.__name__,
    )
    def test_a_read_never_returns_a_row(
        self, reading: Session, repository: type, method: str
    ) -> None:
        instance = repository(reading)
        result = getattr(instance, method)(**CALLS[(repository, method)])

        leaked = [value for value in _walk(result) if isinstance(value, Base)]
        assert leaked == [], f"{repository.__name__}.{method} returned a row"

        containers = [value for value in _walk(result) if isinstance(value, (list, dict, set))]
        assert containers == [], f"{repository.__name__}.{method} returned a mutable container"

    def test_reads_stage_nothing(self, reading: Session) -> None:
        """A reader is not allowed to leave anything pending in its session."""
        for (repository, method), arguments in CALLS.items():
            getattr(repository(reading), method)(**arguments)

        assert list(reading.new) == []
        assert list(reading.dirty) == []

    def test_the_reader_exposes_no_session(self) -> None:
        assert {field.name for field in fields(BusinessReader)} == {
            "customers",
            "catalog",
            "pricing",
            "stock",
            "delivery",
            "discounts",
        }
        assert not set(dir(BusinessReader)) & {
            "session",
            "scalars",
            "execute",
            "add",
            "commit",
            "flush",
            "get_bind",
        }

    def test_read_models_import_no_database(self) -> None:
        """Structural, not behavioural: no session, no row type, no query."""
        imported = {
            alias.name
            for node in ast.walk(_module_tree(read_models))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not [name for name in imported if "sqlalchemy" in name]

    def test_the_boundary_imports_only_reads(self) -> None:
        """``select`` and ``Session`` are all the SQL surface this module needs."""
        sqlalchemy_imports = {
            alias.name
            for node in ast.walk(_module_tree(repositories_module))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (node.module or "").startswith("sqlalchemy")
            for alias in node.names
        }
        assert sqlalchemy_imports == {"select", "Session"}

        mutations = {
            node.attr
            for node in ast.walk(_module_tree(repositories_module))
            if isinstance(node, ast.Attribute)
        } & {"add", "add_all", "flush", "commit", "delete", "merge", "execute"}
        assert mutations == set()


class TestValueFidelity:
    """Enum, Decimal, date and aware-UTC conversion at the seam."""

    def test_money_crosses_as_decimal_at_the_column_scale(self, reader: BusinessReader) -> None:
        entry = reader.pricing.entry("PE_0001")
        assert entry is not None
        assert isinstance(entry.unit_price, Decimal)
        assert not isinstance(entry.unit_price, float)
        assert str(entry.unit_price) == "1234.5600"
        assert entry.unit_price.as_tuple().exponent == -4

    def test_two_decimal_money_stays_at_two(self, reader: BusinessReader) -> None:
        customer = reader.customers.get("CUS_0001")
        assert customer is not None
        assert isinstance(customer.credit_limit, Decimal)
        assert str(customer.credit_limit) == "50000.00"

    def test_a_null_credit_limit_is_absence_not_zero(self, seeded: Session) -> None:
        """``None`` means no limit recorded - a fact the caller must handle."""
        seeded.execute(
            text("UPDATE customers SET credit_limit = NULL WHERE customer_id = 'CUS_0002'")
        )
        seeded.commit()

        customer = CustomerRepository(seeded).get("CUS_0002")
        assert customer is not None
        assert customer.credit_limit is None

    def test_percentages_cross_as_decimal(self, reader: BusinessReader) -> None:
        rule = reader.discounts.rule("DSC_0004")
        assert rule is not None
        assert isinstance(rule.percent, Decimal)
        assert str(rule.percent) == "4.50"

    def test_a_minimum_order_value_crosses_as_decimal(self, reader: BusinessReader) -> None:
        rule = reader.discounts.rule("DSC_0001")
        assert rule is not None
        assert isinstance(rule.min_order_value, Decimal)
        assert str(rule.min_order_value) == "5000.00"

    def test_enums_cross_as_members_not_strings(self, reader: BusinessReader) -> None:
        rule = reader.discounts.rule("DSC_0003")
        assert rule is not None
        assert rule.scope is DiscountScope.CUSTOMER
        assert isinstance(rule.scope, DiscountScope)

        match = reader.catalog.search("100-ABC").matches[0]
        assert match.alias_kind is AliasKind.CUSTOMER_PART
        assert isinstance(match.alias_kind, AliasKind)

    def test_dates_cross_as_dates(self, reader: BusinessReader) -> None:
        entry = reader.pricing.entry("PE_0020")
        assert entry is not None
        assert entry.effective_from == date(2026, 1, 1)
        assert entry.effective_to == date(2026, 6, 30)
        assert not isinstance(entry.effective_to, datetime)

        national_holiday = next(
            record
            for record in reader.delivery.holidays(("DE",))
            if record.name == "German Unity Day"
        )
        assert national_holiday.holiday_date == date(2026, 10, 3)
        assert not isinstance(national_holiday.holiday_date, datetime)

    def test_a_date_range_crosses_unchanged(self, reader: BusinessReader) -> None:
        book = reader.pricing.book("BK-EU-CONTRACT-2026")
        assert book is not None
        assert book.customer_tier is None
        assert book.effective_from == date(2026, 1, 1)
        assert book.effective_to == date(2026, 12, 31)

    def test_instants_cross_as_aware_utc(self, reader: BusinessReader) -> None:
        level = reader.stock.level("WAW", "PRD_0002")
        assert level is not None
        assert level.as_of == STOCK_AS_OF
        assert level.as_of.tzinfo is not None
        assert level.as_of.utcoffset() == datetime(2026, 10, 1, 6, tzinfo=UTC).utcoffset()

    def test_a_stock_level_keeps_its_inbound_eta(self, reader: BusinessReader) -> None:
        level = reader.stock.level("BER", "PRD_0011")
        assert level is not None
        assert level.on_hand_qty == 0
        assert level.inbound_qty == 120
        assert level.inbound_eta == date(2026, 10, 13)
        # The domain's own property, not a field the reader invented.
        assert level.available_qty == 0


class TestCustomerLookup:
    def test_a_trading_name_alias_resolves(self, reader: BusinessReader) -> None:
        """The alias and the display name are both evidence for the same customer."""
        result = reader.customers.search("Nordwind")
        assert [(match.source, match.matched_text) for match in result.matches] == [
            (MatchSource.ALIAS, "Nordwind"),
            (MatchSource.NAME, "Nordwind"),
        ]
        assert result.matched_ids == ("CUS_0001",)
        assert result.is_ambiguous is False
        assert result.matches[0].alias_kind is AliasKind.NAME
        assert result.matches[1].alias_kind is None

    def test_an_email_domain_alias_resolves(self, reader: BusinessReader) -> None:
        result = reader.customers.search("vistula.pl")
        assert result.matched_ids == ("CUS_0002",)
        assert result.matches[0].alias_kind is AliasKind.EMAIL_DOMAIN
        assert result.found is True

    def test_a_legacy_legal_name_alias_resolves(self, reader: BusinessReader) -> None:
        """Mail still arrives under a name the company stopped using."""
        result = reader.customers.search("Altbau Rheinland GmbH")
        assert result.matched_ids == ("CUS_0008",)
        assert result.matches[0].alias_kind is AliasKind.NAME

    def test_lookup_normalises_case_and_whitespace(self, reader: BusinessReader) -> None:
        assert reader.customers.search("  NORDWIND  ").matched_ids == ("CUS_0001",)
        assert normalize_alias("  NORDWIND  ") == "nordwind"

    def test_a_stored_alias_is_already_normalised(self, reader: BusinessReader) -> None:
        """The rule is idempotent, which is what makes the indexed column work."""
        assert normalize_alias(normalize_alias("Vistula Machinery")) == normalize_alias(
            "Vistula Machinery"
        )
        assert reader.customers.search("Vistula").matched_ids == ("CUS_0002",)

    def test_a_full_legal_name_resolves_by_name(self, reader: BusinessReader) -> None:
        result = reader.customers.search("Vistula Machinery Sp. z o.o.")
        assert result.matched_ids == ("CUS_0002",)
        assert result.matches[0].source is MatchSource.NAME
        assert result.matches[0].alias_kind is None

    def test_two_kinds_of_evidence_for_one_customer_are_both_returned(
        self, seeded: Session, reading: Session
    ) -> None:
        """Naming a customer twice is evidence twice, and neither is preferred."""
        seeded.add(
            ProductAliasRow(
                product_id="PRD_0001",
                normalized=normalize_alias("Centrifugal pump PMP-A-100"),
                alias="Centrifugal pump PMP-A-100",
                kind=AliasKind.NAME,
            )
        )
        seeded.commit()

        result = CatalogRepository(reading).search("Centrifugal pump PMP-A-100")
        assert [(match.source, match.product.product_id) for match in result.matches] == [
            (MatchSource.ALIAS, "PRD_0001"),
            (MatchSource.NAME, "PRD_0001"),
        ]
        assert result.is_ambiguous is False
        assert result.matched_ids == ("PRD_0001",)

    def test_search_does_not_prefer_an_active_customer(self, reader: BusinessReader) -> None:
        """``active`` is a fact that travels; the boundary applies no filter."""
        assert reader.customers.search("Rheinland").matches[0].customer.active is False

    def test_an_unknown_name_finds_nothing(self, reader: BusinessReader) -> None:
        result = reader.customers.search("Sauron Logistics S.A.")
        assert result.found is False
        assert result.matches == ()
        assert result.matched_ids == ()
        assert result.is_ambiguous is False
        assert result.query == "Sauron Logistics S.A."


class TestCatalogLookup:
    def test_a_sku_resolves(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("PMP-B-150")
        assert result.matched_ids == ("PRD_0003",)
        assert result.matches[0].source is MatchSource.SKU
        assert result.matches[0].matched_text == "PMP-B-150"

    def test_a_customer_part_number_resolves_through_its_alias(
        self, reader: BusinessReader
    ) -> None:
        result = reader.catalog.search("100-ABC")
        assert result.matched_ids == ("PRD_0001",)
        assert result.matches[0].source is MatchSource.ALIAS
        assert result.matches[0].alias_kind is AliasKind.CUSTOMER_PART

    def test_a_legacy_sku_resolves_through_its_alias(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("BF-50")
        assert result.matched_ids == ("PRD_0007",)
        assert result.matches[0].alias_kind is AliasKind.SKU

    def test_a_colloquial_name_resolves(self, reader: BusinessReader) -> None:
        assert reader.catalog.search("Manometer 100").matched_ids == ("PRD_0014",)
        assert reader.catalog.search("boiler feed pump").matched_ids == ("PRD_0005",)

    def test_a_product_name_resolves_by_name(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("level switch INS-LS-300")
        assert result.matched_ids == ("PRD_0018",)
        assert result.matches[0].source is MatchSource.NAME

    def test_a_spaced_alias_resolves(self, reader: BusinessReader) -> None:
        """``PMP A 100`` is the same alias as ``PMP-A-100`` put differently."""
        assert reader.catalog.search("PMP A 100").matched_ids == ("PRD_0001",)

    def test_the_sku_question_can_be_asked_on_its_own(self, reader: BusinessReader) -> None:
        """Narrowing to SKU is how the collision is shown to be a catalogue alias."""
        result = reader.catalog.search("PMP-A-100", sources=frozenset({MatchSource.SKU}))
        assert result.matched_ids == ("PRD_0001",)
        assert result.is_ambiguous is False
        assert result.sources == frozenset({MatchSource.SKU})

    def test_the_alias_question_can_be_asked_on_its_own(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("PMP-A-100", sources=frozenset({MatchSource.ALIAS}))
        assert result.matched_ids == ("PRD_0002",)
        assert result.matches[0].alias_kind is AliasKind.NAME

    def test_a_discontinued_product_is_still_found(self, reader: BusinessReader) -> None:
        """It must resolve, so the quote is blocked by policy and not by ignorance."""
        result = reader.catalog.search("PMP-D-300")
        assert result.matched_ids == ("PRD_0006",)
        assert result.matches[0].product.active is False

    def test_families_are_returned_in_display_order(self, reader: BusinessReader) -> None:
        assert [family.family_code for family in reader.catalog.families()] == [
            "FAM_PUMPS",
            "FAM_VALVES",
            "FAM_INSTR",
        ]

    def test_an_unknown_sku_finds_nothing(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("NOPE-000")
        assert result.found is False
        assert result.matches == ()
        assert result.sources == frozenset(MatchSource)


class TestAmbiguity:
    def test_the_deliberate_product_ambiguity_is_returned_as_ambiguity(
        self, reader: BusinessReader
    ) -> None:
        """``PMP-A-100`` is one pump's SKU and the other pump's stored alias.

        Two matches, two products, and no attempt to choose: taking the first
        would put a confident, wrong part number on a quotation.
        """
        result = reader.catalog.search("PMP-A-100")

        assert result.is_ambiguous is True
        assert result.matched_ids == ("PRD_0001", "PRD_0002")
        assert [(match.source, match.matched_text) for match in result.matches] == [
            (MatchSource.SKU, "PMP-A-100"),
            (MatchSource.ALIAS, "PMP-A-100"),
        ]
        assert result.matches[1].alias_kind is AliasKind.NAME

    def test_the_ambiguity_survives_a_case_difference(self, reader: BusinessReader) -> None:
        assert reader.catalog.search("pmp-a-100").is_ambiguous is True

    def test_a_single_match_is_not_reported_as_ambiguous(self, reader: BusinessReader) -> None:
        result = reader.catalog.search("100-ABC-SS")
        assert result.matched_ids == ("PRD_0002",)
        assert result.is_ambiguous is False

    def test_a_customer_alias_can_be_ambiguous_too(self, seeded: Session, reading: Session) -> None:
        """The same rule holds on the customer side, not only for the demo case."""
        seeded.add(
            CustomerAliasRow(
                customer_id="CUS_0003",
                normalized=normalize_alias("Nordwind"),
                alias="Nordwind",
                kind=AliasKind.NAME,
            )
        )
        seeded.commit()

        result = CustomerRepository(reading).search("Nordwind")
        assert result.is_ambiguous is True
        assert result.matched_ids == ("CUS_0001", "CUS_0003")

    def test_search_results_report_the_query_they_answered(self, reader: BusinessReader) -> None:
        assert reader.catalog.search("PMP-A-100").query == "PMP-A-100"
        assert reader.customers.search("Nordwind").query == "Nordwind"


class TestAbsence:
    def test_unknown_identifiers_return_none(self, reader: BusinessReader) -> None:
        assert reader.customers.get("CUS_9999") is None
        assert reader.catalog.get("PRD_9999") is None
        assert reader.catalog.family("FAM_NOPE") is None
        assert reader.pricing.book("BK-NOPE") is None
        assert reader.pricing.entry("PE_9999") is None
        assert reader.stock.warehouse("XXX") is None
        assert reader.stock.level("WAW", "PRD_9999") is None
        assert reader.delivery.service("NOPE") is None
        assert reader.discounts.rule("DSC_9999") is None

    def test_a_product_not_stocked_anywhere_has_no_level(self, reader: BusinessReader) -> None:
        """``None`` distinguishes "not stocked here" from "nothing on the shelf"."""
        assert reader.stock.level("BER", "PRD_0005") is None
        assert reader.stock.level("WAW", "PRD_0006") is None

    def test_an_empty_query_matches_nothing(self, reader: BusinessReader) -> None:
        """A blank string is not a wildcard."""
        assert reader.catalog.search("").matches == ()
        assert reader.catalog.search("   ").matches == ()
        assert reader.customers.search("").matches == ()

    def test_collections_are_empty_on_an_unseeded_database(self, session: Session) -> None:
        """A migrated database with no data: empty tuples, never an error."""
        empty = BusinessReader.for_session(session)
        assert empty.catalog.families() == ()
        assert empty.pricing.books() == ()
        assert empty.discounts.rules() == ()
        assert empty.delivery.services() == ()
        assert empty.stock.warehouses() == ()
        assert empty.delivery.holidays() == ()

    def test_empty_input_sequences_return_empty_results(self, reader: BusinessReader) -> None:
        """An empty request must not degrade into "everything"."""
        assert reader.pricing.entries_for_products([]) == ()
        assert reader.stock.levels_for_products([]) == ()
        assert reader.delivery.holidays([]) == ()
        assert reader.catalog.search("x", sources=frozenset()).matches == ()

    def test_duplicate_ids_are_read_once(self, reader: BusinessReader) -> None:
        entries = reader.pricing.entries_for_products(["PRD_0001", "PRD_0001"])
        assert [entry.price_entry_id for entry in entries] == ["PE_0001", "PE_0021", "PE_0002"]


class TestDeterminism:
    @pytest.mark.parametrize(
        "call",
        [
            lambda reader: reader.customers.search("Nordwind"),
            lambda reader: reader.customers.search("Vistula"),
            lambda reader: reader.catalog.search("PMP-A-100"),
            lambda reader: reader.catalog.families(),
            lambda reader: reader.pricing.books(),
            lambda reader: reader.pricing.entries_for_products(["PRD_0001", "PRD_0005"]),
            lambda reader: reader.stock.warehouses(),
            lambda reader: reader.stock.levels_for_products(["PRD_0001", "PRD_0011"]),
            lambda reader: reader.delivery.services(),
            lambda reader: reader.delivery.services_from("WAW"),
            lambda reader: reader.delivery.holidays(),
            lambda reader: reader.discounts.rules(),
        ],
        ids=[
            "customer-search",
            "customer-search-second",
            "catalog-search",
            "families",
            "books",
            "price-entries",
            "warehouses",
            "stock-levels",
            "carrier-services",
            "services-from",
            "holidays",
            "discount-rules",
        ],
    )
    def test_repeated_reads_return_identical_results(
        self, reader: BusinessReader, call: Call
    ) -> None:
        first = call(reader)
        second = call(reader)
        assert first == second
        assert type(first) is type(second)

    def test_price_entries_are_ordered_by_the_documented_key(self, reader: BusinessReader) -> None:
        """Product, then quantity break, then window start, then entry id.

        Two entries tie on quantity and window - the list price and the customer
        contract - and the entry id settles it. That is a total reading order;
        which entry *applies* is the pricing rule's decision, and nothing here
        prefers the contract.
        """
        entries = reader.pricing.entries_for_products(["PRD_0001"])
        assert [entry.price_entry_id for entry in entries] == ["PE_0001", "PE_0021", "PE_0002"]
        assert [entry.customer_id for entry in entries] == [None, "CUS_0001", None]
        assert [entry.min_qty for entry in entries] == [1, 1, 25]

    def test_an_expired_entry_is_returned_like_any_other(self, reader: BusinessReader) -> None:
        """The window is a fact on the entry; judging it is pricing's job."""
        entries = reader.pricing.entries_for_products(["PRD_0006"])
        assert [entry.price_entry_id for entry in entries] == ["PE_0020"]
        assert entries[0].effective_to == date(2026, 6, 30)

    def test_stock_levels_are_ordered_by_product_then_location(
        self, reader: BusinessReader
    ) -> None:
        levels = reader.stock.levels_for_products(["PRD_0001"])
        assert [(level.product_id, level.location) for level in levels] == [
            ("PRD_0001", "BER"),
            ("PRD_0001", "WAW"),
        ]

    def test_holidays_are_ordered_by_country_then_date(self, reader: BusinessReader) -> None:
        calendar = reader.delivery.holidays(("DE", "PL"))
        assert [(record.country_code, record.holiday_date) for record in calendar] == sorted(
            (record.country_code, record.holiday_date) for record in calendar
        )
        assert calendar[0].country_code == "DE"

    def test_services_are_ordered_by_code(self, reader: BusinessReader) -> None:
        assert [service.service_code for service in reader.delivery.services()] == [
            "DHL-ECO",
            "DHL-EXP",
            "DPD-CLS",
            "GLS-EUR",
        ]
        assert [service.service_code for service in reader.delivery.services_from("BER")] == [
            "DPD-CLS",
            "GLS-EUR",
        ]

    def test_two_readers_over_the_same_database_agree(self, db: Database) -> None:
        """Determinism is not an accident of one session's identity map."""
        with db.session_factory() as first, db.session_factory() as second:
            left = BusinessReader.for_session(first)
            right = BusinessReader.for_session(second)
            assert left.catalog.search("PMP-A-100") == right.catalog.search("PMP-A-100")
            assert left.pricing.entries_for_products(
                ["PRD_0001"]
            ) == right.pricing.entries_for_products(["PRD_0001"])
            assert left.delivery.holidays() == right.delivery.holidays()


class TestFreshDatabase:
    def test_reads_work_on_a_freshly_migrated_and_seeded_database(self, tmp_path: Path) -> None:
        """The path a fresh clone takes, read through the boundary."""
        path = tmp_path / "fresh.db"
        command.upgrade(make_alembic_config(path), "head")

        database = Database.create(f"sqlite:///{path}")
        try:
            with database.session_factory() as writing:
                reset_and_seed(writing)
                writing.commit()
            with database.session_factory() as reading:
                reader = BusinessReader.for_session(reading)

                assert reader.customers.get("CUS_0001") is not None
                assert reader.customers.search("nordwind-industrie.de").matched_ids == ("CUS_0001",)
                assert reader.catalog.search("PMP-A-100").is_ambiguous is True
                assert reader.catalog.search("100-ABC").matched_ids == ("PRD_0001",)
                assert len(reader.pricing.entries_for_products(["PRD_0001"])) == 3
                assert reader.stock.level("WAW", "PRD_0001") is not None
                assert len(reader.delivery.holidays(("FR",))) == 11
                assert len(reader.discounts.rules()) == 6
        finally:
            database.dispose()


class TestStructuralInvariants:
    def test_there_is_exactly_one_normaliser(self) -> None:
        """The seed re-exports the domain's rule; it does not reimplement it."""
        assert seed_normalize.normalize_alias is normalize_alias

    def test_the_seed_module_holds_no_normalisation_logic(self) -> None:
        imported = {
            alias.name
            for node in ast.walk(_module_tree(seed_normalize))
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.module != "__future__"
            for alias in node.names
        }
        assert imported == {"normalize_alias"}

    def test_the_repositories_module_exports_its_surface(self) -> None:
        """Pinned, so the boundary neither grows nor leaks the normaliser."""
        assert repositories_module.__all__ == [
            "BusinessReader",
            "CatalogRepository",
            "CustomerRepository",
            "DeliveryRepository",
            "DiscountRepository",
            "PricingRepository",
            "StockRepository",
        ]
