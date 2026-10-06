"""Price selection against the demo dataset, read through the read boundary.

The rule itself has its own tests in ``tests/unit/test_pricing.py``, with exact
boundary cases built by hand. This module checks the same rule against the real
business data - the Northwind Components catalogue, its contract prices and its
deliberately awkward edges - and it lives next to the persistence tests because
that is the seam it exercises: database → read models → deterministic selection.

What is being pinned here is that the seeded data, the read boundary and the
pricing rule agree with each other. A contract price that the boundary returns
but the rule ignores, a quantity break that only works in a hand-built fixture,
an expired entry that quietly becomes a quoted price: all three would pass a
unit test and fail this one.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from rfq_agent.domain.pricing import (
    LIST_TIER,
    PriceEntry,
    PriceLookupReason,
    PriceLookupStatus,
    PriceSelection,
    select_price,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import PriceEntryRow
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import TIER_STANDARD, reset_and_seed

#: The date the demo data is priced on: inside every current price window.
AS_OF = date(2026, 10, 6)


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


def entries_for(reader: BusinessReader, product_id: str) -> tuple[PriceEntry, ...]:
    """Return the price entries the read boundary sees for one product."""
    return reader.pricing.entries_for_products([product_id])


def pick(
    entries: Sequence[PriceEntry],
    *,
    product_id: str,
    quantity: int = 10,
    customer_id: str | None = None,
    customer_tier: str | None = None,
    as_of: date = AS_OF,
) -> PriceSelection:
    """Ask for one price, with everything a test is not about defaulted."""
    return select_price(
        entries,
        product_id=product_id,
        quantity=quantity,
        as_of=as_of,
        customer_id=customer_id,
        customer_tier=customer_tier,
    )


def chosen(selection: PriceSelection) -> str | None:
    """The selected entry's id, or ``None`` when nothing was selected."""
    return None if selection.price is None else selection.price.price_entry_id


class TestSeededDataAgreesWithTheDomain:
    def test_the_seeded_list_prices_use_the_tier_the_domain_falls_back_to(
        self, seeded: Session
    ) -> None:
        """The fallback tier is a fact about the data, and the two must not drift."""
        rows = seeded.scalars(select(PriceEntryRow)).all()
        tiers = {row.customer_tier for row in rows if row.customer_tier is not None}

        assert tiers == {LIST_TIER}
        assert TIER_STANDARD == LIST_TIER

    def test_contract_prices_are_scoped_to_a_customer_and_not_a_tier(self, seeded: Session) -> None:
        """An entry is scoped to a customer or a tier, never both."""
        rows = seeded.scalars(select(PriceEntryRow)).all()
        contracts = [row for row in rows if row.customer_id is not None]

        assert contracts, "the demo data has no contract prices at all"
        assert all(row.customer_tier is None for row in contracts)

    def test_every_seeded_unit_price_crosses_as_decimal(
        self, reader: BusinessReader, seeded: Session
    ) -> None:
        prices = [row.unit_price for row in seeded.scalars(select(PriceEntryRow)).all()] + [
            entry.unit_price for entry in entries_for(reader, "PRD_0001")
        ]

        assert prices
        assert all(isinstance(price, Decimal) for price in prices)
        assert all(price.as_tuple().exponent == -4 for price in prices)


class TestContractPrices:
    def test_a_contract_customer_gets_its_negotiated_price(self, reader: BusinessReader) -> None:
        selection = pick(
            entries_for(reader, "PRD_0001"),
            product_id="PRD_0001",
            quantity=40,
            customer_id="CUS_0001",
            customer_tier="STANDARD",
        )

        assert chosen(selection) == "PE_0021"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("1150.0000")
        assert "customer price PE_0021" in selection.detail

    def test_a_customer_without_a_contract_gets_the_list_price(
        self, reader: BusinessReader
    ) -> None:
        selection = pick(entries_for(reader, "PRD_0001"), product_id="PRD_0001", quantity=10)

        assert chosen(selection) == "PE_0001"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("1234.5600")

    def test_another_customers_contract_price_is_not_offered(self, reader: BusinessReader) -> None:
        """``CUS_0001`` has a contract for ``PRD_0002``; nobody else inherits it."""
        selection = pick(
            entries_for(reader, "PRD_0002"),
            product_id="PRD_0002",
            quantity=10,
            customer_id="CUS_0003",
        )

        assert chosen(selection) == "PE_0003"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("1789.0000")

    def test_a_contract_break_beats_the_list_price_once_it_applies(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0002`` holds a 10-unit contract break on ``PRD_0009``."""
        entries = entries_for(reader, "PRD_0009")

        below = pick(entries, product_id="PRD_0009", quantity=9, customer_id="CUS_0002")
        at = pick(entries, product_id="PRD_0009", quantity=10, customer_id="CUS_0002")

        assert chosen(below) == "PE_0011"
        assert chosen(at) == "PE_0027"
        assert at.price is not None
        assert at.price.unit_price == Decimal("36.4000")

    def test_a_contract_only_product_needs_that_customer(self, reader: BusinessReader) -> None:
        """``PRD_0012`` is priced only for ``CUS_0004``: no list price exists."""
        entries = entries_for(reader, "PRD_0012")

        held = pick(entries, product_id="PRD_0012", quantity=1, customer_id="CUS_0004")
        other = pick(entries, product_id="PRD_0012", quantity=1, customer_id="CUS_0001")

        assert chosen(held) == "PE_0025"
        assert held.price is not None
        assert held.price.unit_price == Decimal("612.0000")

        assert other.status is PriceLookupStatus.MISSING
        assert other.reason is PriceLookupReason.NO_MATCHING_SCOPE
        assert other.price is None


class TestQuantityBreaks:
    @pytest.mark.parametrize(("quantity", "expected"), [(24, "PE_0001"), (25, "PE_0002")])
    def test_the_break_starts_exactly_at_its_threshold(
        self, reader: BusinessReader, quantity: int, expected: str
    ) -> None:
        selection = pick(entries_for(reader, "PRD_0001"), product_id="PRD_0001", quantity=quantity)

        assert chosen(selection) == expected

    def test_the_break_price_is_the_stored_one(self, reader: BusinessReader) -> None:
        selection = pick(entries_for(reader, "PRD_0001"), product_id="PRD_0001", quantity=25)

        assert selection.price is not None
        assert str(selection.price.unit_price) == "1185.1700"
        assert selection.price.min_qty == 25


class TestWindows:
    def test_the_discontinued_products_only_price_reports_expired(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0006`` was discontinued mid-2026; its price ended with it."""
        selection = pick(entries_for(reader, "PRD_0006"), product_id="PRD_0006", quantity=1)

        assert selection.status is PriceLookupStatus.EXPIRED
        assert selection.reason is PriceLookupReason.EXPIRED
        assert selection.price is None
        assert "PE_0020 ended 2026-06-30" in selection.detail

    def test_the_same_entry_is_used_while_its_window_is_open(self, reader: BusinessReader) -> None:
        """Nothing about the row changed: only the pricing date did."""
        selection = pick(
            entries_for(reader, "PRD_0006"),
            product_id="PRD_0006",
            quantity=1,
            as_of=date(2026, 3, 1),
        )

        assert chosen(selection) == "PE_0020"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("2140.0000")

    def test_prices_are_not_yet_effective_before_the_catalogue_year(
        self, reader: BusinessReader
    ) -> None:
        selection = pick(
            entries_for(reader, "PRD_0001"),
            product_id="PRD_0001",
            quantity=10,
            customer_id="CUS_0001",
            as_of=date(2025, 12, 15),
        )

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.NOT_YET_EFFECTIVE
        assert "2026-01-01" in selection.detail


class TestTheSeamWithProductResolution:
    def test_pricing_is_asked_about_one_already_resolved_product(
        self, reader: BusinessReader
    ) -> None:
        """``PMP-A-100`` names two products; pricing is told which one to price.

        The ambiguity is reported by the read boundary, resolved (or escalated) by
        the resolver, and only then handed here as a product id. Passing both
        products' entries at once changes nothing, which is the point.
        """
        ambiguous = reader.catalog.search("PMP-A-100")
        assert ambiguous.is_ambiguous is True

        both = (*entries_for(reader, "PRD_0001"), *entries_for(reader, "PRD_0002"))
        selection = pick(
            both,
            product_id="PRD_0002",
            quantity=10,
            customer_id="CUS_0001",
            customer_tier="STANDARD",
        )

        assert chosen(selection) == "PE_0022"
        assert selection.product_id == "PRD_0002"

    def test_the_same_question_has_the_same_answer(self, reader: BusinessReader) -> None:
        entries = entries_for(reader, "PRD_0001")
        arguments = {
            "product_id": "PRD_0001",
            "quantity": 40,
            "customer_id": "CUS_0001",
            "customer_tier": "STANDARD",
        }

        assert pick(entries, **arguments) == pick(entries, **arguments)
        assert pick(entries, **arguments) == pick(tuple(reversed(entries)), **arguments)
