"""The blocking ledger over the demo dataset, through the accepted pipeline.

Phase 1I joins two things that were already accepted: the facts Phase 1H
produced (which lines priced, what the stock position is, whether the customer
is on credit hold) and the representation Phase 0 defined for a blocked quote.
Nothing here hand-writes the facts. The price comes from ``select_price`` over
the rows ``BusinessReader`` returns, the stock status from ``evaluate_stock``,
the delivery promise from ``evaluate_delivery``, the discount from
``select_discount``, and the credit hold from the customer record - which is why
a reviewer can reproduce every ledger below from the Northwind Components
catalogue by hand.

It lives with the persistence tests because that seam is what it exercises.

Two seeded properties are load-bearing here and are asserted rather than
assumed: exactly one customer (``CUS_0007``) is on credit hold, and the demo
data contains no un-priced *combination* that hides a stock problem - so the
"all five reasons at once" case can be built from real records.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryEvaluation, evaluate_delivery
from rfq_agent.domain.gating import project_blocked_ledger
from rfq_agent.domain.policy import (
    BlockedReasonCode,
    DiscountSelection,
    PolicyFlag,
    QuoteBlockedLedger,
    select_discount,
)
from rfq_agent.domain.pricing import PriceSelection, select_price
from rfq_agent.domain.quote import QuoteCalculation, QuoteLineInput, calculate_quote
from rfq_agent.domain.stock import (
    BLOCKING_STOCK_STATUSES,
    StockEvaluation,
    StockStatus,
    evaluate_stock,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import CustomerRow, QuoteLineRow, QuoteRow
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import reset_and_seed

#: The date every price and delivery question in these tests is asked for.
AS_OF = date(2026, 10, 6)
#: The stamp the seeded stock positions carry, and a moment on the same day.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: When the enquiry arrived - before the DHL express cut-off, so it ships today.
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: The run every ledger in this module belongs to.
RUN_ID = "RUN_0001"


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


# ---------------------------------------------------------------------------
# The pipeline's own steps, in one place
# ---------------------------------------------------------------------------


def price_for(
    reader: BusinessReader,
    product_id: str,
    quantity: int,
    *,
    customer: str | None = None,
) -> PriceSelection:
    """Select one price the way the core will: read the entries, then decide."""
    return select_price(
        reader.pricing.entries_for_products([product_id]),
        product_id=product_id,
        quantity=quantity,
        as_of=AS_OF,
        customer_id=customer,
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


def delivery_for(
    reader: BusinessReader,
    stock: StockEvaluation,
    *,
    destination: str = "Hamburg",
    destination_country: str | None = "DE",
    requested_date: date | None = None,
) -> DeliveryEvaluation:
    """Ask the delivery question with the facts the read boundary holds."""
    return evaluate_delivery(
        stock,
        destination=destination,
        destination_country=destination_country,
        as_of=NOW,
        requested_date=requested_date,
        services=reader.delivery.services(),
        warehouses=reader.stock.warehouses(),
        holidays=reader.delivery.holidays(),
    )


def discount_for(
    reader: BusinessReader,
    *,
    customer: str | None,
    quantity: int,
    order_value: Decimal | None,
) -> DiscountSelection:
    """Select the discount the way the core will, from the rules the boundary returns."""
    return select_discount(
        reader.discounts.rules(),
        as_of=AS_OF,
        quantity=quantity,
        customer_id=customer,
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
        run_id=RUN_ID,
        customer_id=customer,
        currency="EUR",
        pricing_as_of=AS_OF,
        discount=None if discount is None or not discount.applied else discount.discount,
        delivery=None if delivery is None else delivery.assessment,
    )


def stored_quotes(session: Session) -> tuple[int, int]:
    """How many quotations and lines the database holds - still zero, this phase."""
    quotes = session.scalar(select(func.count()).select_from(QuoteRow)) or 0
    lines = session.scalar(select(func.count()).select_from(QuoteLineRow)) or 0
    return quotes, lines


def project(
    calculation: QuoteCalculation,
    *,
    customer_on_credit_hold: bool = False,
) -> QuoteBlockedLedger:
    """Project the calculation the way the caller will."""
    return project_blocked_ledger(
        calculation,
        run_id=RUN_ID,
        customer_on_credit_hold=customer_on_credit_hold,
    )


def codes(ledger: QuoteBlockedLedger) -> list[BlockedReasonCode]:
    """The ledger's codes, in the order it reports them."""
    return [reason.code for reason in ledger.reasons]


def a_clean_quote(reader: BusinessReader) -> QuoteCalculation:
    """``PRD_0001`` x40 for ``CUS_0001``: priced, in stock, delegated discount."""
    price = price_for(reader, "PRD_0001", 40, customer="CUS_0001")
    stock = stock_for(reader, "PRD_0001", 40)
    selection = discount_for(
        reader, customer="CUS_0001", quantity=40, order_value=Decimal("46000.00")
    )
    return quote(
        [line(price, sku="PMP-A-100", quantity=40, stock_status=stock.status)],
        discount=selection,
    )


def a_fully_blocked_quote(reader: BusinessReader) -> QuoteCalculation:
    """``CUS_0007``'s enquiry: one discontinued pump, one good one, and a promise."""
    refused_price = price_for(reader, "PRD_0006", 1)
    refused_stock = stock_for(reader, "PRD_0006", 1)
    priced = price_for(reader, "PRD_0001", 40)
    priced_stock = stock_for(reader, "PRD_0001", 40)
    lines = [
        line(refused_price, sku="PMP-D-300", quantity=1, stock_status=refused_stock.status),
        line(priced, sku="PMP-A-100", quantity=40, stock_status=priced_stock.status),
    ]
    # The order value the discount rules are asked about is this quote's own
    # subtotal, taken from a first pass - not a number written into the test.
    provisional = quote(lines, customer="CUS_0007")
    selection = discount_for(
        reader,
        customer="CUS_0007",
        quantity=40,
        order_value=provisional.quote.subtotal,
    )
    delivery = delivery_for(reader, stock_for(reader, "PRD_0001", 100), destination_country="IT")
    return quote(lines, customer="CUS_0007", discount=selection, delivery=delivery)


# ---------------------------------------------------------------------------
# The credit-hold fact, as the dataset holds it
# ---------------------------------------------------------------------------


class TestTheSeededCreditHold:
    """The one fact the caller supplies is a stored field, read not guessed."""

    def test_exactly_one_seeded_customer_is_on_credit_hold(self, seeded: Session) -> None:
        """``CUS_0007`` is the seeded account, and it is the only one."""
        held = seeded.scalars(
            select(CustomerRow.customer_id)
            .where(CustomerRow.credit_hold.is_(True))
            .order_by(CustomerRow.customer_id)
        ).all()

        assert list(held) == ["CUS_0007"]

    def test_the_seam_reports_the_hold_as_a_plain_fact(self, reader: BusinessReader) -> None:
        """No interpretation happens on the way out of the database."""
        record = reader.customers.get("CUS_0007")

        assert record is not None
        assert record.credit_hold is True
        assert record.active is True
        assert record.country_code == "SE"

    def test_the_reader_reports_no_hold_for_the_key_account(self, reader: BusinessReader) -> None:
        """The same field, the other value - not an absence to be inferred around."""
        record = reader.customers.get("CUS_0001")

        assert record is not None
        assert record.credit_hold is False

    def test_the_hold_reaches_the_ledger_for_the_held_customer(
        self, reader: BusinessReader
    ) -> None:
        """A clean quote for ``CUS_0007`` is blocked by the account's standing alone."""
        record = reader.customers.get("CUS_0007")
        calculation = a_clean_quote(reader)

        ledger = project(calculation, customer_on_credit_hold=record.credit_hold)

        assert codes(ledger) == [BlockedReasonCode.CREDIT_HOLD]
        assert ledger.reasons[0].line_ordinal is None

    def test_the_same_quote_is_clean_for_a_customer_without_a_hold(
        self, reader: BusinessReader
    ) -> None:
        """Nothing else about the quote changed, so nothing else is reported."""
        record = reader.customers.get("CUS_0001")
        calculation = a_clean_quote(reader)

        ledger = project(calculation, customer_on_credit_hold=record.credit_hold)

        assert ledger.reasons == ()
        assert ledger.blocked is False

    def test_an_inactive_account_is_not_projected_as_a_credit_hold(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0008`` is deactivated; no accepted contract turns that into a reason."""
        record = reader.customers.get("CUS_0008")
        assert record is not None
        assert record.active is False
        assert record.credit_hold is False

        ledger = project(a_clean_quote(reader), customer_on_credit_hold=record.credit_hold)

        assert ledger.reasons == ()


# ---------------------------------------------------------------------------
# Clean, and priced-with-a-caveat
# ---------------------------------------------------------------------------


class TestTheCleanSeededQuote:
    """The baseline the rest of the module deviates from."""

    def test_the_clean_quote_has_nothing_to_explain(self, reader: BusinessReader) -> None:
        """Priced, in stock, delegated discount - an empty ledger and a total."""
        calculation = a_clean_quote(reader)

        ledger = project(calculation)

        assert calculation.complete is True
        assert calculation.quote.total == Decimal("44620.00")
        assert ledger.reasons == ()
        assert ledger.hard_blocked is False

    def test_the_clean_ledger_still_names_customer_and_quote(self, reader: BusinessReader) -> None:
        """An empty ledger is still evidence about a particular quotation."""
        ledger = project(a_clean_quote(reader))

        assert ledger.quote_id == "QTE_0001"
        assert ledger.run_id == RUN_ID

    def test_a_partially_available_pump_is_still_totalled_and_still_blocked(
        self, reader: BusinessReader
    ) -> None:
        """Phase 1H prices ``PARTIAL`` stock; Phase 1I reports it as a reason."""
        price = price_for(reader, "PRD_0005", 50)
        stock = stock_for(reader, "PRD_0005", 50)
        assert stock.status is StockStatus.PARTIAL
        assert stock.available_qty == 10

        calculation = quote([line(price, sku="PMP-C-250", quantity=50, stock_status=stock.status)])
        ledger = project(calculation)

        assert calculation.complete is True
        assert codes(ledger) == [BlockedReasonCode.STOCK_INSUFFICIENT]
        assert ledger.reasons[0].line_ordinal == 1
        assert "PARTIAL" in ledger.reasons[0].message

    def test_a_position_that_is_only_inbound_is_reported_not_waited_for(
        self, reader: BusinessReader
    ) -> None:
        """120 units arrive on 2026-10-13; the ledger does not treat them as stock."""
        stock = stock_for(reader, "PRD_0011", 800)
        assert stock.status is StockStatus.PARTIAL
        assert stock.inbound_qty == 120
        assert stock.earliest_inbound_eta == date(2026, 10, 13)

        price = price_for(reader, "PRD_0011", 800)
        ledger = project(
            quote([line(price, sku="VLV-BL-020", quantity=800, stock_status=stock.status)])
        )

        assert codes(ledger) == [BlockedReasonCode.STOCK_INSUFFICIENT]
        assert "PARTIAL" in ledger.reasons[0].message

    def test_the_same_pump_within_its_available_quantity_is_not_reported(
        self, reader: BusinessReader
    ) -> None:
        """700 of the 750 on the shelf is a promise the data supports."""
        stock = stock_for(reader, "PRD_0011", 700)
        assert stock.status is StockStatus.SUFFICIENT

        price = price_for(reader, "PRD_0011", 700)
        ledger = project(
            quote([line(price, sku="VLV-BL-020", quantity=700, stock_status=stock.status)])
        )

        assert ledger.reasons == ()


# ---------------------------------------------------------------------------
# Prices: the two ways the demo data cannot price a line
# ---------------------------------------------------------------------------


class TestTheSeededPriceRefusals:
    """The refusals Phase 1H recorded, reported as the code the contract names."""

    def test_the_discontinued_pump_is_blocked_for_its_expired_price(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0006``'s only entry ended 2026-06-30, and it is also discontinued."""
        product = reader.catalog.get("PRD_0006")
        assert product is not None
        assert product.active is False

        price = price_for(reader, "PRD_0006", 1)
        assert price.status.value == "EXPIRED"
        stock = stock_for(reader, "PRD_0006", 1)
        calculation = quote([line(price, sku="PMP-D-300", quantity=1, stock_status=stock.status)])
        refusal = calculation.refusals[0]

        ledger = project(calculation)

        assert refusal.detail in ledger.reasons[0].message
        assert ledger.reasons[0].code is BlockedReasonCode.PRICE_MISSING
        assert ledger.reasons[0].line_ordinal == 1

    def test_a_product_priced_only_for_another_customer_is_blocked_for_its_scope(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0012``'s sole entry serves ``CUS_0004``, so nobody else may quote it."""
        price = price_for(reader, "PRD_0012", 5, customer="CUS_0001")
        assert price.status.value == "MISSING"

        stock = stock_for(reader, "PRD_0012", 5)
        ledger = project(
            quote([line(price, sku="VLV-AC-063", quantity=5, stock_status=stock.status)])
        )

        assert codes(ledger) == [BlockedReasonCode.PRICE_MISSING]

    def test_the_same_product_is_quotable_for_the_customer_holding_the_contract(
        self, reader: BusinessReader
    ) -> None:
        """The scope, not the product, was the problem."""
        price = price_for(reader, "PRD_0012", 5, customer="CUS_0004")
        assert price.status.value == "FOUND"

        stock = stock_for(reader, "PRD_0012", 5)
        ledger = project(
            quote(
                [line(price, sku="VLV-AC-063", quantity=5, stock_status=stock.status)],
                customer="CUS_0004",
            )
        )

        assert ledger.reasons == ()

    def test_a_discontinued_product_is_not_reported_as_discontinued(
        self, reader: BusinessReader
    ) -> None:
        """There is no code for "withdrawn"; the facts that exist carry the block."""
        price = price_for(reader, "PRD_0006", 1)
        stock = stock_for(reader, "PRD_0006", 1)

        ledger = project(
            quote([line(price, sku="PMP-D-300", quantity=1, stock_status=stock.status)])
        )

        assert PolicyFlag.DISCONTINUED_PRODUCT not in ledger.flags
        assert ledger.flags == ()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestTheSeededDeliveryReasons:
    """Promises the demo data can and cannot make, reported from the promise itself."""

    def test_a_requested_date_the_carrier_cannot_meet_is_a_reason(
        self, reader: BusinessReader
    ) -> None:
        """Asking for 2026-10-06 when the earliest delivery is 2026-10-07."""
        stock = stock_for(reader, "PRD_0001", 100)
        delivery = delivery_for(reader, stock, requested_date=date(2026, 10, 6))
        assert delivery.assessment.promise.feasibility.value == "INFEASIBLE"

        price = price_for(reader, "PRD_0001", 100, customer="CUS_0001")
        ledger = project(
            quote(
                [line(price, sku="PMP-A-100", quantity=100, stock_status=stock.status)],
                delivery=delivery,
            )
        )

        assert codes(ledger) == [BlockedReasonCode.DELIVERY_INFEASIBLE]
        assert delivery.assessment.promise.rationale in ledger.reasons[0].message
        assert ledger.reasons[0].line_ordinal is None

    def test_a_destination_without_a_calendar_is_reported_not_promised(
        self, reader: BusinessReader
    ) -> None:
        """``Milan`` has no seeded holiday calendar, so no date is guessed."""
        stock = stock_for(reader, "PRD_0001", 100)
        delivery = delivery_for(
            reader,
            stock,
            destination="Milan",
            destination_country="IT",
            requested_date=date(2026, 10, 20),
        )
        assert delivery.assessment.promise.feasibility.value == "UNKNOWN"

        price = price_for(reader, "PRD_0001", 100, customer="CUS_0001")
        ledger = project(
            quote(
                [line(price, sku="PMP-A-100", quantity=100, stock_status=stock.status)],
                delivery=delivery,
            )
        )

        assert codes(ledger) == [BlockedReasonCode.DELIVERY_INFEASIBLE]
        assert ledger.reasons[0].message.endswith(delivery.assessment.promise.rationale)

    def test_the_express_lane_meets_the_date_the_customer_asked_for(
        self, reader: BusinessReader
    ) -> None:
        """The same shipment one day later is feasible, and therefore silent."""
        stock = stock_for(reader, "PRD_0001", 100)
        delivery = delivery_for(reader, stock, requested_date=date(2026, 10, 7))

        price = price_for(reader, "PRD_0001", 100, customer="CUS_0001")
        ledger = project(
            quote(
                [line(price, sku="PMP-A-100", quantity=100, stock_status=stock.status)],
                delivery=delivery,
            )
        )

        assert ledger.reasons == ()

    def test_a_quote_without_a_requested_date_is_not_a_delivery_failure(
        self, reader: BusinessReader
    ) -> None:
        """``NOT_REQUESTED`` records what the enquiry did not say."""
        stock = stock_for(reader, "PRD_0001", 100)
        delivery = delivery_for(reader, stock, requested_date=None)
        assert delivery.assessment.promise.feasibility.value == "NOT_REQUESTED"

        price = price_for(reader, "PRD_0001", 100, customer="CUS_0001")
        ledger = project(
            quote(
                [line(price, sku="PMP-A-100", quantity=100, stock_status=stock.status)],
                delivery=delivery,
            )
        )

        assert ledger.reasons == ()


# ---------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------


class TestTheSeededDiscountReasons:
    """``requires_approval`` is reported from the selected rule and nowhere else."""

    def test_the_customers_own_rule_is_delegated_and_silent(self, reader: BusinessReader) -> None:
        """``CUS_0001`` holds ``DSC_0003`` at 3% - inside the delegated limit."""
        selection = discount_for(
            reader, customer="CUS_0001", quantity=40, order_value=Decimal("46000.00")
        )

        ledger = project(a_clean_quote(reader))

        assert selection.rule_id == "DSC_0003"
        assert selection.requires_approval is False
        assert ledger.reasons == ()

    def test_a_customer_rule_outside_the_limit_is_a_reason(self, reader: BusinessReader) -> None:
        """``CUS_0004`` holds ``DSC_0004`` at 4.5%, which needs sign-off."""
        price = price_for(reader, "PRD_0001", 5, customer="CUS_0004")
        stock = stock_for(reader, "PRD_0001", 5)
        selection = discount_for(
            reader, customer="CUS_0004", quantity=5, order_value=Decimal("5600.00")
        )
        assert selection.rule_id == "DSC_0004"
        assert selection.requires_approval is True

        calculation = quote(
            [line(price, sku="PMP-A-100", quantity=5, stock_status=stock.status)],
            customer="CUS_0004",
            discount=selection,
        )
        ledger = project(calculation)

        assert codes(ledger) == [BlockedReasonCode.DISCOUNT_OVER_POLICY]
        assert "DSC_0004" in ledger.reasons[0].message

    def test_the_global_rule_above_the_floor_is_a_reason(self, reader: BusinessReader) -> None:
        """Nobody negotiated, the order is large enough, and the rule needs approval."""
        price = price_for(reader, "PRD_0001", 40)
        stock = stock_for(reader, "PRD_0001", 40)
        selection = discount_for(
            reader, customer=None, quantity=40, order_value=Decimal("47406.80")
        )
        assert selection.rule_id == "DSC_0002"
        assert selection.requires_approval is True

        calculation = quote(
            [line(price, sku="PMP-A-100", quantity=40, stock_status=stock.status)],
            customer="CUS_0002",
            discount=selection,
        )
        ledger = project(calculation)

        assert codes(ledger) == [BlockedReasonCode.DISCOUNT_OVER_POLICY]
        assert "DSC_0002" in ledger.reasons[0].message

    def test_an_order_below_every_floor_carries_no_discount_and_no_reason(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0008`` x10 is 1784.00 - under the 5000.00 floor."""
        price = price_for(reader, "PRD_0008", 10, customer="CUS_0002")
        stock = stock_for(reader, "PRD_0008", 10)
        selection = discount_for(
            reader, customer="CUS_0002", quantity=10, order_value=Decimal("1784.00")
        )
        assert selection.applied is False

        calculation = quote(
            [line(price, sku="VLV-BF-080", quantity=10, stock_status=stock.status)],
            customer="CUS_0002",
            discount=selection,
        )
        ledger = project(calculation)

        assert calculation.quote.discount_amount == Decimal("0.00")
        assert ledger.reasons == ()

    def test_an_expired_customer_rule_does_not_silence_the_global_one(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0006``'s 6% rule ended 2026-06-30, so ``DSC_0002`` applies instead."""
        selection = discount_for(
            reader, customer="CUS_0006", quantity=40, order_value=Decimal("47406.80")
        )

        assert selection.applied is True
        assert selection.rule_id == "DSC_0002"
        assert selection.requires_approval is True

    def test_discount_approval_is_reported_without_being_decided(
        self, reader: BusinessReader
    ) -> None:
        """The quote keeps its discount and its total; only the reason is added."""
        price = price_for(reader, "PRD_0001", 40, customer="CUS_0002")
        stock = stock_for(reader, "PRD_0001", 40)
        selection = discount_for(
            reader, customer="CUS_0002", quantity=40, order_value=Decimal("47406.80")
        )
        calculation = quote(
            [line(price, sku="PMP-A-100", quantity=40, stock_status=stock.status)],
            customer="CUS_0002",
            discount=selection,
        )

        ledger = project(calculation)

        assert calculation.quote.discount_amount == Decimal("2370.34")
        assert calculation.quote.total == Decimal("45036.46")
        assert calculation.quote.status.value == "DRAFT"
        assert codes(ledger) == [BlockedReasonCode.DISCOUNT_OVER_POLICY]


# ---------------------------------------------------------------------------
# Everything the demo data can produce at once
# ---------------------------------------------------------------------------


class TestTheSeededBlockingLedger:
    """The whole ledger, on records a reviewer can look up."""

    def test_the_demo_data_can_block_one_quote_every_way_at_once(
        self, reader: BusinessReader
    ) -> None:
        """Expired price, no stock, no calendar, an approval rule and a credit hold."""
        ledger = project(a_fully_blocked_quote(reader), customer_on_credit_hold=True)

        assert ledger.reasons
        assert codes(ledger) == [
            BlockedReasonCode.PRICE_MISSING,
            BlockedReasonCode.STOCK_INSUFFICIENT,
            BlockedReasonCode.DELIVERY_INFEASIBLE,
            BlockedReasonCode.DISCOUNT_OVER_POLICY,
            BlockedReasonCode.CREDIT_HOLD,
        ]
        assert ledger.blocked is True
        assert ledger.hard_blocked is True

    def test_every_reason_in_that_ledger_names_the_thing_it_is_about(
        self, reader: BusinessReader
    ) -> None:
        """An operator reading the ledger can find each fact in the data."""
        ledger = project(a_fully_blocked_quote(reader), customer_on_credit_hold=True)
        messages = {reason.code: reason.message for reason in ledger.reasons}

        assert "PRD_0006" in messages[BlockedReasonCode.PRICE_MISSING]
        assert "PRD_0006" in messages[BlockedReasonCode.STOCK_INSUFFICIENT]
        assert "DSC_0002" in messages[BlockedReasonCode.DISCOUNT_OVER_POLICY]
        assert "credit hold" in messages[BlockedReasonCode.CREDIT_HOLD]

    def test_the_ledger_is_identical_when_it_is_projected_twice(
        self, reader: BusinessReader
    ) -> None:
        """Determinism over real rows, not just over literals."""
        calculation = a_fully_blocked_quote(reader)

        first = project(calculation, customer_on_credit_hold=True)
        second = project(calculation, customer_on_credit_hold=True)

        assert first == second
        assert first.model_dump() == second.model_dump()

    def test_the_same_quote_projects_the_same_way_over_a_fresh_reader(
        self, seeded: Session, db: Database
    ) -> None:
        """The projection reads no session state, so a second reader changes nothing."""
        del seeded  # dependency only: both readers must see the seeded rows
        with db.session_factory() as first_session:
            first = project(a_fully_blocked_quote(BusinessReader.for_session(first_session)))
        with db.session_factory() as second_session:
            second = project(a_fully_blocked_quote(BusinessReader.for_session(second_session)))

        assert first == second

    def test_the_seeded_positions_show_all_three_stock_verdicts(
        self, reader: BusinessReader
    ) -> None:
        """``BLOCKING_STOCK_STATUSES`` is the contract the projection obeys."""
        statuses = {}
        for product_id, quantity in (("PRD_0001", 40), ("PRD_0005", 50), ("PRD_0006", 1)):
            statuses[product_id] = stock_for(reader, product_id, quantity).status

        assert statuses["PRD_0001"] is StockStatus.SUFFICIENT
        assert statuses["PRD_0005"] is StockStatus.PARTIAL
        assert statuses["PRD_0006"] is StockStatus.NONE
        assert StockStatus.SUFFICIENT not in BLOCKING_STOCK_STATUSES
        assert {StockStatus.PARTIAL, StockStatus.NONE} <= BLOCKING_STOCK_STATUSES

    def test_projection_writes_nothing_to_the_database(self, seeded: Session, db: Database) -> None:
        """No rows are created, changed or queued: this phase has no write path."""
        before = stored_quotes(seeded)

        with db.session_factory() as reading:
            reader = BusinessReader.for_session(reading)
            project(a_fully_blocked_quote(reader), customer_on_credit_hold=True)
            assert list(reading.new) == []
            assert list(reading.dirty) == []

        seeded.rollback()  # end this session's read transaction, so the counts cannot be stale
        assert stored_quotes(seeded) == before == (0, 0)

    def test_projection_leaves_a_usable_session_behind(self, reader: BusinessReader) -> None:
        """The reading side is not left in an unusable state by a pure function."""
        project(a_fully_blocked_quote(reader), customer_on_credit_hold=True)

        assert reader.customers.get("CUS_0001") is not None
        assert reader.discounts.rule("DSC_0002") is not None
