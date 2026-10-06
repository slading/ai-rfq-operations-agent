"""Quote arithmetic against the demo dataset, through the read boundary.

Every number here comes from the accepted deterministic selectors rather than
from a literal in a test: the price from Phase 1D's ``select_price`` over the
rows ``BusinessReader`` returns, the stock status from Phase 1E's
``evaluate_stock``, the delivery promise from Phase 1F's ``evaluate_delivery``,
and the discount from Phase 1G's ``select_discount``. Phase 1H only does the
arithmetic - so this module is where "the whole chain agrees" is asserted, on
the Northwind Components data a reviewer can check by hand.

It lives with the persistence tests because that seam is what it exercises.

The seeded gaps are stated rather than worked around. Every product in the demo
data has at least one price row, so "no price entry exists at all" cannot happen
here; what the dataset does produce is the two refusal flavours that matter:
an expired price (``PRD_0006``, discontinued) and a product priced only for
another customer (``PRD_0012``, whose sole entry is ``CUS_0004``'s contract).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryEvaluation, evaluate_delivery
from rfq_agent.domain.policy import DiscountReason, DiscountSelection, select_discount
from rfq_agent.domain.pricing import PriceSelection, select_price
from rfq_agent.domain.quote import (
    CALC_VERSION,
    QuoteCalculation,
    QuoteLineInput,
    QuoteStatus,
    calculate_quote,
)
from rfq_agent.domain.stock import StockEvaluation, StockStatus, evaluate_stock
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import QuoteLineRow, QuoteRow
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import reset_and_seed

#: The date every price in these tests was selected for.
AS_OF = date(2026, 10, 6)
#: The stamp the seeded stock positions carry, and a moment after it.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


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


def price_for(
    reader: BusinessReader,
    product_id: str,
    quantity: int,
    *,
    customer: str | None = None,
    tier: str | None = None,
) -> PriceSelection:
    """Select one price the way the core will: read the entries, then decide."""
    return select_price(
        reader.pricing.entries_for_products([product_id]),
        product_id=product_id,
        quantity=quantity,
        as_of=AS_OF,
        customer_id=customer,
        customer_tier=tier,
        currency="EUR",
    )


def stock_for(reader: BusinessReader, product_id: str, requested_qty: int) -> StockEvaluation:
    """Evaluate stock the way the core will, from the positions the boundary returns."""
    return evaluate_stock(
        reader.stock.levels_for_products([product_id]),
        product_id=product_id,
        requested_qty=requested_qty,
        as_of=STOCK_STAMP,
    )


def discount_for(
    reader: BusinessReader,
    *,
    customer: str | None,
    quantity: int,
    order_value: Decimal | None,
    tier: str | None = None,
) -> DiscountSelection:
    """Select the discount the way the core will, from the rules the boundary returns."""
    return select_discount(
        reader.discounts.rules(),
        as_of=AS_OF,
        quantity=quantity,
        customer_id=customer,
        customer_tier=tier,
        order_value=order_value,
    )


def line(
    selection: PriceSelection,
    *,
    sku: str,
    quantity: int,
    description: str = "Seeded catalogue item",
    stock_status: StockStatus = StockStatus.SUFFICIENT,
) -> QuoteLineInput:
    """A resolved line carrying the price Phase 1D selected for it."""
    return QuoteLineInput(
        product_id=selection.product_id,
        sku=sku,
        description=description,
        quantity=quantity,
        price=selection,
        stock_status=stock_status,
    )


def quote(
    lines: Sequence[QuoteLineInput],
    *,
    customer: str = "CUS_0001",
    discount: DiscountSelection | None = None,
    delivery: DeliveryEvaluation | None = None,
    number: str = "Q-2026-0001",
) -> QuoteCalculation:
    """Calculate a quote from facts that were selected, never hand-written."""
    return calculate_quote(
        lines,
        quote_id="QTE_0001",
        quote_number=number,
        run_id="RUN_0001",
        customer_id=customer,
        currency="EUR",
        pricing_as_of=AS_OF,
        discount=None if discount is None or not discount.applied else discount.discount,
        delivery=None if delivery is None else delivery.assessment,
    )


def quote_counts(session: Session) -> tuple[int, int]:
    """How many quotations and lines the database holds."""
    quotes = session.scalar(select(func.count()).select_from(QuoteRow))
    lines = session.scalar(select(func.count()).select_from(QuoteLineRow))
    return quotes or 0, lines or 0


class TestTheSeededQuote:
    """The numbers a reviewer can reproduce from the price book by hand."""

    def test_the_negotiated_price_is_extended_exactly(self, reader: BusinessReader) -> None:
        """``CUS_0001``'s contract price for ``PRD_0001``: 40 x 1150.0000."""
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")

        calculation = quote([line(price, sku="PMP-A-100", quantity=40)])

        (priced,) = calculation.quote.lines
        assert priced.price_entry_id == "PE_0021"
        assert priced.unit_price == Decimal("1150.0000")
        assert priced.line_extension == Decimal("46000.00")
        assert calculation.quote.subtotal == Decimal("46000.00")
        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == Decimal("46000.00")
        assert calculation.complete is True

    def test_the_customer_discount_is_the_selected_one_and_is_applied(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0001`` holds ``DSC_0003`` at 3% - delegated to the system."""
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
        subtotal = Decimal("46000.00")
        selection = discount_for(reader, customer="CUS_0001", quantity=40, order_value=subtotal)

        calculation = quote([line(price, sku="PMP-A-100", quantity=40)], discount=selection)

        assert selection.rule_id == "DSC_0003"
        assert selection.requires_approval is False
        assert calculation.quote.discount == selection.discount
        assert calculation.quote.discount_amount == Decimal("1380.00")
        assert calculation.quote.total == Decimal("44620.00")

    def test_the_standard_rules_apply_when_nobody_negotiated(self, reader: BusinessReader) -> None:
        """No customer: the 25-unit break, then the 5% rule that needs sign-off."""
        price = price_for(reader, "PRD_0001", 40)

        assert price.price is not None
        assert price.price.price_entry_id == "PE_0002"
        subtotal = Decimal("47406.80")
        assert Decimal(40) * price.price.unit_price == subtotal

        selection = discount_for(reader, customer=None, quantity=40, order_value=subtotal)
        calculation = quote([line(price, sku="PMP-A-100", quantity=40)], discount=selection)

        assert selection.rule_id == "DSC_0002"
        assert selection.requires_approval is True
        assert calculation.quote.discount_amount == Decimal("2370.34")
        assert calculation.quote.total == Decimal("45036.46")

    def test_a_small_order_earns_no_discount_and_the_quote_says_so(
        self, reader: BusinessReader
    ) -> None:
        """Neither standard rule applies below 5000 EUR, and nothing is invented."""
        price = price_for(reader, "PRD_0008", 10)
        selection = discount_for(reader, customer=None, quantity=10, order_value=Decimal("1784.00"))

        calculation = quote([line(price, sku="MANO-100", quantity=10)], discount=selection)

        assert selection.applied is False
        assert selection.reason is DiscountReason.BELOW_MIN_ORDER_VALUE
        assert calculation.quote.discount is None
        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == calculation.quote.subtotal == Decimal("1784.00")

    def test_a_multi_line_quote_adds_up(self, reader: BusinessReader) -> None:
        """A pump line and a valve line, one customer, one rule, one total."""
        pump = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
        valve = price_for(reader, "PRD_0007", 20, customer="CUS_0001")

        lines = [
            line(pump, sku="PMP-A-100", quantity=40),
            line(valve, sku="BF-50", quantity=20),
        ]
        subtotal = Decimal("48855.00")
        selection = discount_for(reader, customer="CUS_0001", quantity=60, order_value=subtotal)

        calculation = quote(lines, discount=selection)

        assert [item.line_extension for item in calculation.quote.lines] == [
            Decimal("46000.00"),
            Decimal("2855.00"),
        ]
        assert [item.price_entry_id for item in calculation.quote.lines] == ["PE_0021", "PE_0008"]
        assert calculation.quote.subtotal == subtotal
        assert calculation.quote.discount_amount == Decimal("1465.65")
        assert calculation.quote.total == Decimal("47389.35")

    def test_the_calculation_version_and_digest_are_stamped(self, reader: BusinessReader) -> None:
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")

        first = quote([line(price, sku="PMP-A-100", quantity=40)])
        second = quote([line(price, sku="PMP-A-100", quantity=40)])

        assert first.quote.calc_version == CALC_VERSION
        assert first.quote.inputs_sha256 is not None
        assert len(first.quote.inputs_sha256) == 64
        assert first.quote.inputs_sha256 == second.quote.inputs_sha256
        assert first.quote == second.quote

    def test_the_quote_is_a_draft_until_a_human_says_otherwise(
        self, reader: BusinessReader
    ) -> None:
        """Arithmetic produces money, not a decision: nothing here is sendable."""
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")

        calculation = quote([line(price, sku="PMP-A-100", quantity=40)])

        assert calculation.quote.status is QuoteStatus.DRAFT
        assert calculation.quote.is_sendable is False
        assert not hasattr(calculation.quote, "approved_at")


class TestTheSeededRefusals:
    """What the demo data cannot price, and what happens to it."""

    def test_the_expired_price_is_refused_and_not_totalled(self, reader: BusinessReader) -> None:
        """``PRD_0006``'s only price row closed on 2026-06-30."""
        price = price_for(reader, "PRD_0006", 40, customer="CUS_0001")

        assert price.status.value == "EXPIRED"
        calculation = quote([line(price, sku="INS-LS-300", quantity=40)])

        (refused,) = calculation.quote.lines
        assert refused.blocked is True
        assert refused.price_entry_id == "PRICE_MISSING"
        assert refused.unit_price == Decimal("0.0000")
        assert refused.line_extension == Decimal("0.00")
        assert refused.blocked_reason is not None
        assert refused.blocked_reason.startswith("EXPIRED/EXPIRED")
        assert calculation.quote.subtotal == Decimal("0.00")
        assert calculation.quote.total == Decimal("0.00")
        assert calculation.complete is False
        assert calculation.quote.is_sendable is False

    def test_a_refused_line_does_not_stop_the_rest(self, reader: BusinessReader) -> None:
        """The priced line is totalled; the refused one is visible and uncounted."""
        priced = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
        refused = price_for(reader, "PRD_0006", 40, customer="CUS_0001")
        selection = discount_for(
            reader, customer="CUS_0001", quantity=80, order_value=Decimal("46000.00")
        )

        calculation = quote(
            [
                line(priced, sku="PMP-A-100", quantity=40),
                line(refused, sku="INS-LS-300", quantity=40),
            ],
            discount=selection,
        )

        (refusal,) = calculation.refusals
        assert refusal.ordinal == 2
        assert refusal.status.value == "EXPIRED"
        assert calculation.quote.subtotal == Decimal("46000.00")
        assert [item.ordinal for item in calculation.quote.blocked_lines] == [2]
        assert len(calculation.quote.lines) == 2

    def test_the_refusal_keeps_the_pricing_sentence(self, reader: BusinessReader) -> None:
        price = price_for(reader, "PRD_0006", 40, customer="CUS_0001")

        calculation = quote([line(price, sku="INS-LS-300", quantity=40)])

        (refusal,) = calculation.refusals
        assert refusal.detail == price.detail
        assert "PE_0020 ended 2026-06-30" in refusal.detail
        assert refusal.reason is price.reason

    def test_the_two_ways_the_demo_data_cannot_price_a_line(self, reader: BusinessReader) -> None:
        """Sixteen products price for ``CUS_0001``; two do not, for different reasons."""
        priced, unusable = [], {}
        for index in range(1, 19):
            product_id = f"PRD_{index:04d}"
            selection = price_for(reader, product_id, 1, customer="CUS_0001")
            if selection.found:
                priced.append(product_id)
            else:
                unusable[product_id] = (selection.status, selection.reason)

        assert len(priced) == 16
        assert list(unusable) == ["PRD_0006", "PRD_0012"]
        assert unusable["PRD_0006"][0].value == "EXPIRED"
        assert unusable["PRD_0012"][0].value == "MISSING"
        assert unusable["PRD_0012"][1].value == "NO_MATCHING_SCOPE"

    def test_a_contract_only_product_is_quotable_for_its_own_customer(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0012`` has no list price: only ``CUS_0004``'s contract covers it."""
        here = price_for(reader, "PRD_0012", 5, customer="CUS_0004")
        elsewhere = price_for(reader, "PRD_0012", 5, customer="CUS_0001")

        assert here.found is True
        assert here.price is not None
        assert here.price.price_entry_id == "PE_0025"
        assert elsewhere.found is False

        calculation = quote(
            [line(here, sku="FLT-EM-100", quantity=5)],
            customer="CUS_0004",
            number="Q-2026-0002",
        )

        assert calculation.quote.subtotal == Decimal("3060.00")
        assert calculation.complete is True


class TestSeededFactsTravel:
    """Stock, delivery and delivery dates ride along without being re-decided."""

    def test_partial_stock_is_priced_and_not_blocked(self, reader: BusinessReader) -> None:
        """Ten available against fifty asked for: the money is still complete."""
        stock = stock_for(reader, "PRD_0005", 50)

        assert stock.status is StockStatus.PARTIAL
        price = price_for(reader, "PRD_0005", 50, customer="CUS_0001")
        calculation = quote(
            [line(price, sku="PMP-A-100-SS", quantity=50, stock_status=stock.status)]
        )

        (priced,) = calculation.quote.lines
        assert priced.stock_status is StockStatus.PARTIAL
        assert priced.blocked is False
        assert calculation.complete is True

    def test_the_delivery_promise_is_carried_onto_the_quote(self, reader: BusinessReader) -> None:
        """One quote, four selectors: price, stock, delivery and discount."""
        stock = stock_for(reader, "PRD_0001", 100)
        delivery = evaluate_delivery(
            stock,
            destination="Hamburg",
            destination_country="DE",
            as_of=NOW,
            services=reader.delivery.services(),
            warehouses=reader.stock.warehouses(),
            holidays=reader.delivery.holidays(),
        )
        price = price_for(reader, "PRD_0001", 100, customer="CUS_0001")
        selection = discount_for(
            reader, customer="CUS_0001", quantity=100, order_value=Decimal("115000.00")
        )

        calculation = quote(
            [line(price, sku="PMP-A-100", quantity=100, stock_status=stock.status)],
            discount=selection,
            delivery=delivery,
        )

        assert calculation.quote.delivery is not None
        assert calculation.quote.delivery.promise.carrier_service_code == "DHL-EXP"
        assert calculation.quote.delivery.promise.earliest_ship_date == date(2026, 10, 6)
        assert calculation.quote.delivery.promise.earliest_delivery_date == date(2026, 10, 7)
        assert calculation.quote.subtotal == Decimal("115000.00")
        assert calculation.quote.discount_amount == Decimal("3450.00")
        assert calculation.quote.total == Decimal("111550.00")

    def test_the_one_product_with_no_stock_and_no_price(self, reader: BusinessReader) -> None:
        """``PRD_0006`` is absent from the stock table and expired in the price book."""
        stock = stock_for(reader, "PRD_0006", 40)
        price = price_for(reader, "PRD_0006", 40, customer="CUS_0001")

        calculation = quote([line(price, sku="INS-LS-300", quantity=40, stock_status=stock.status)])

        (refused,) = calculation.quote.lines
        assert stock.status is StockStatus.NONE
        assert refused.stock_status is StockStatus.NONE
        assert refused.blocked is True
        assert refused.blocked_reason is not None
        assert refused.blocked_reason.startswith("EXPIRED")


class TestNothingIsWritten:
    """The calculator is a function, not a step in the pipeline."""

    def test_calculating_writes_nothing(self, reader: BusinessReader, seeded: Session) -> None:
        before = quote_counts(seeded)
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")

        quote([line(price, sku="PMP-A-100", quantity=40)])

        assert quote_counts(seeded) == before
        assert list(seeded.new) == []
        assert list(seeded.dirty) == []

    def test_reads_leave_the_dataset_alone(self, reader: BusinessReader) -> None:
        entries = reader.pricing.entries_for_products(["PRD_0001"])
        rules = reader.discounts.rules()

        price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
        selection = discount_for(
            reader, customer="CUS_0001", quantity=40, order_value=Decimal("46000.00")
        )
        quote([line(price, sku="PMP-A-100", quantity=40)], discount=selection)

        assert reader.pricing.entries_for_products(["PRD_0001"]) == entries
        assert reader.discounts.rules() == rules

    def test_the_line_order_does_not_change_the_seeded_money(self, reader: BusinessReader) -> None:
        pump = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
        valve = price_for(reader, "PRD_0007", 20, customer="CUS_0001")
        lines = [
            line(pump, sku="PMP-A-100", quantity=40),
            line(valve, sku="BF-50", quantity=20),
        ]

        forwards = quote(lines)
        backwards = quote(list(reversed(lines)))

        assert forwards.quote.subtotal == backwards.quote.subtotal
        assert forwards.quote.total == backwards.quote.total
        assert {item.sku for item in forwards.quote.lines} == {
            item.sku for item in backwards.quote.lines
        }
