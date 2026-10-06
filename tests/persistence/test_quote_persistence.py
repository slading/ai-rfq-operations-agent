"""Storing a quote and its blocking ledger, over the seeded dataset (Phase 1J').

This is the module the phase exists for. Everything upstream already produced the
facts - the price was selected, the stock evaluated, the promise made, the
discount chosen, the money calculated and the reasons projected - and this layer
stores them. So the assertions here are mostly *comparisons*: what the database
holds against what the domain object said, field by field, scale by scale.

Two refusals are part of the contract rather than exceptions to it. A line with
no usable price is stored blocked, with no price entry and no money, because
``PRICE_MISSING`` is a domain sentinel and ``price_entry_id`` is a nullable
foreign key: the translation happens at this boundary and nowhere else. And a
retry of the same write stores nothing at all - the claim is taken and released
inside the same transaction, so a crash mid-write leaves the retry free rather
than half-applied.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryEvaluation, evaluate_delivery
from rfq_agent.domain.gating import project_blocked_ledger
from rfq_agent.domain.policy import (
    BlockedReasonCode,
    DiscountSelection,
    QuoteBlockedLedger,
    select_discount,
)
from rfq_agent.domain.pricing import PriceSelection, select_price
from rfq_agent.domain.quote import MISSING_PRICE_ENTRY_ID, QuoteLineInput, calculate_quote
from rfq_agent.domain.stock import StockStatus, evaluate_stock
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    IdempotencyClaimRow,
    QuoteBlockedReasonRow,
    QuoteLineRow,
    QuoteRow,
)
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import (
    CLAIM_SCOPE,
    QuoteWriter,
    SqlIdempotencyStore,
    quote_line_id,
)
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

#: The date every price question is asked for, and the stamp the stock carries.
PRICING_AS_OF = date(2026, 10, 6)
STOCK_AS_OF = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: The instant the tests pretend the write happened at.
WRITTEN_AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: One run per quotation, because ``(run_id, revision)`` is unique.
RUN_ID = "RUN_0001"
OTHER_RUN_ID = "RUN_0002"
THIRD_RUN_ID = "RUN_0003"


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """A clock that always says the same thing, so stored stamps are checkable."""

    instant: datetime = WRITTEN_AT

    def now(self) -> datetime:
        """Return the frozen instant."""
        return self.instant


@pytest.fixture
def seeded(session: Session) -> Session:
    """The demo dataset, plus the runs a quotation has to belong to."""
    reset_and_seed(session)
    session.add_all(
        [
            rfq_row("RFQ_0001"),
            rfq_row("RFQ_0002"),
            run_row(RUN_ID, "RFQ_0001"),
            run_row(OTHER_RUN_ID, "RFQ_0002"),
            run_row(THIRD_RUN_ID, "RFQ_0001", attempt_no=2),
        ]
    )
    session.commit()
    return session


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


@pytest.fixture
def writer(db: Database) -> QuoteWriter:
    """A writer stamping stored rows from a frozen clock."""
    return QuoteWriter(db, clock=FrozenClock())


def price_for(
    reader: BusinessReader,
    product_id: str,
    quantity: int,
    *,
    customer: str | None = "CUS_0001",
) -> PriceSelection:
    """Select one price the way the core will: read the entries, then decide."""
    return select_price(
        reader.pricing.entries_for_products([product_id]),
        product_id=product_id,
        quantity=quantity,
        as_of=PRICING_AS_OF,
        customer_id=customer,
        currency="EUR",
    )


def stock_for(reader: BusinessReader, product_id: str, requested_qty: int):
    """Evaluate stock the way the core will, from the positions the boundary returns."""
    return evaluate_stock(
        reader.stock.levels_for_products([product_id]),
        product_id=product_id,
        requested_qty=requested_qty,
        as_of=STOCK_AS_OF,
    )


def delivery_for(
    reader: BusinessReader,
    stock: object,
    *,
    destination: str = "Hamburg",
    destination_country: str | None = "DE",
    requested_date: date | None = None,
) -> DeliveryEvaluation:
    """Ask the delivery question with the facts the read boundary holds."""
    return evaluate_delivery(
        stock,  # type: ignore[arg-type]
        destination=destination,
        destination_country=destination_country,
        as_of=WRITTEN_AT,
        requested_date=requested_date,
        services=reader.delivery.services(),
        warehouses=reader.stock.warehouses(),
        holidays=reader.delivery.holidays(),
    )


def discount_for(
    reader: BusinessReader, *, customer: str, quantity: int, order_value: Decimal
) -> DiscountSelection:
    """Select the discount the way the core will, from the rules the boundary returns."""
    return select_discount(
        reader.discounts.rules(),
        as_of=PRICING_AS_OF,
        quantity=quantity,
        customer_id=customer,
        order_value=order_value,
    )


def line(
    selection: PriceSelection,
    *,
    sku: str,
    quantity: int,
    stock_status: StockStatus,
    description: str = "Seeded catalogue item",
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


def quote(lines: Sequence[QuoteLineInput], **overrides: object):
    """Calculate a quote from facts that were selected, never hand-written."""
    values: dict[str, object] = {
        "quote_id": "QTE_0001",
        "quote_number": "Q-2026-0001",
        "run_id": RUN_ID,
        "customer_id": "CUS_0001",
        "currency": "EUR",
        "pricing_as_of": PRICING_AS_OF,
    }
    values.update(overrides)
    return calculate_quote(lines, **values)  # type: ignore[arg-type]


def a_complete_quote(reader: BusinessReader, **overrides: object):
    """The seeded happy path: 40 pumps at the customer's contract price, 3% off."""
    priced = price_for(reader, "PRD_0001", 40)
    lines = [
        line(
            priced,
            sku="PMP-A-100",
            quantity=40,
            stock_status=stock_for(reader, "PRD_0001", 40).status,
        )
    ]
    provisional = quote(lines, **overrides)
    selection = discount_for(
        reader, customer="CUS_0001", quantity=40, order_value=provisional.quote.subtotal
    )
    return quote(lines, discount=selection.discount if selection.applied else None, **overrides)


def a_quote_with_a_refused_line(reader: BusinessReader, **overrides: object):
    """A product whose only price entry ended before the pricing date."""
    refused = price_for(reader, "PRD_0006", 1)
    lines = [
        line(
            refused,
            sku="PMP-D-300",
            quantity=1,
            stock_status=stock_for(reader, "PRD_0006", 1).status,
        )
    ]
    return quote(lines, quote_id="QTE_0002", quote_number="Q-2026-0002", **overrides)


def a_mixed_quote(reader: BusinessReader, **overrides: object):
    """One line that priced and one that could not: the case the phase is about."""
    refused = price_for(reader, "PRD_0006", 1)
    priced = price_for(reader, "PRD_0001", 40)
    lines = [
        line(
            refused,
            sku="PMP-D-300",
            quantity=1,
            stock_status=stock_for(reader, "PRD_0006", 1).status,
        ),
        line(
            priced,
            sku="PMP-A-100",
            quantity=40,
            stock_status=stock_for(reader, "PRD_0001", 40).status,
        ),
    ]
    provisional = quote(lines, quote_id="QTE_0003", quote_number="Q-2026-0003", **overrides)
    selection = discount_for(
        reader,
        customer="CUS_0001",
        quantity=40,
        order_value=provisional.quote.subtotal,
    )
    delivery = delivery_for(
        reader, stock_for(reader, "PRD_0001", 40), requested_date=date(2026, 10, 8)
    )
    return quote(
        lines,
        quote_id="QTE_0003",
        quote_number="Q-2026-0003",
        discount=selection.discount if selection.applied else None,
        delivery=delivery.assessment,
        **overrides,
    )


def project(calculation, *, run_id: str = RUN_ID, credit_hold: bool = False) -> QuoteBlockedLedger:
    """Project the calculation the way the caller will."""
    return project_blocked_ledger(calculation, run_id=run_id, customer_on_credit_hold=credit_hold)


def stored_lines(session: Session, quote_id: str = "QTE_0001") -> list[QuoteLineRow]:
    """Every stored line of one quote, in ordinal order."""
    statement = (
        select(QuoteLineRow).where(QuoteLineRow.quote_id == quote_id).order_by(QuoteLineRow.ordinal)
    )
    return list(session.scalars(statement))


def stored_ledger(session: Session, quote_id: str = "QTE_0001") -> list[QuoteBlockedReasonRow]:
    """Every stored ledger reason of one quote, in the order it was written."""
    statement = (
        select(QuoteBlockedReasonRow)
        .where(QuoteBlockedReasonRow.quote_id == quote_id)
        .order_by(QuoteBlockedReasonRow.seq)
    )
    return list(session.scalars(statement))


def row_counts(session: Session) -> dict[str, int]:
    """How many rows each table this phase writes holds."""
    tables = {
        "quotes": QuoteRow,
        "lines": QuoteLineRow,
        "ledger": QuoteBlockedReasonRow,
        "claims": IdempotencyClaimRow,
    }
    return {
        name: session.scalar(select(func.count()).select_from(model)) or 0
        for name, model in tables.items()
    }


# ---------------------------------------------------------------------------
# The ledger evidence
# ---------------------------------------------------------------------------


def test_the_ledger_is_stored_in_the_order_it_was_projected(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Sequence is the projection's order, and the flags travel with the evidence."""
    calculation = a_quote_with_a_refused_line(reader)
    ledger = project(calculation, credit_hold=True)
    assert [reason.code for reason in ledger.reasons] == [
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
        BlockedReasonCode.CREDIT_HOLD,
    ]

    writer.persist(calculation, ledger)

    stored = stored_ledger(seeded, "QTE_0002")
    assert [(row.seq, row.code, row.message) for row in stored] == [
        (seq, reason.code, reason.message) for seq, reason in enumerate(ledger.reasons, start=1)
    ]
    assert {row.run_id for row in stored} == {RUN_ID}
    assert {row.created_at for row in stored} == {WRITTEN_AT}
    assert {tuple(row.flags_json) for row in stored} == {tuple(flag.value for flag in ledger.flags)}


def test_a_reason_keeps_the_line_it_is_about(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Line-level reasons carry their ordinal; quote-level ones carry ``NULL``."""
    calculation = a_quote_with_a_refused_line(reader)
    ledger = project(calculation, credit_hold=True)

    writer.persist(calculation, ledger)

    by_code = {row.code: row for row in stored_ledger(seeded, "QTE_0002")}
    assert by_code[BlockedReasonCode.PRICE_MISSING].line_ordinal == 1
    assert by_code[BlockedReasonCode.STOCK_INSUFFICIENT].line_ordinal == 1
    assert by_code[BlockedReasonCode.CREDIT_HOLD].line_ordinal is None
    assert all(row.resolvable_by_human for row in by_code.values())


def test_a_clean_quote_stores_no_ledger_at_all(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """``project`` on a complete quote is empty, and an empty ledger writes nothing."""
    calculation = a_complete_quote(reader)
    ledger = project(calculation)
    assert ledger.reasons == ()

    result = writer.persist(calculation, ledger)

    assert stored_ledger(seeded) == []
    assert result.ledger_seqs == ()


def test_a_reason_that_names_a_line_ordinal_of_zero_is_refused(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """``ck_..._line_ordinal_positive``: an ordinal is a position, and starts at one."""
    calculation = a_quote_with_a_refused_line(reader)
    ledger = project(calculation)
    damaged = ledger.model_copy(
        update={"reasons": (ledger.reasons[0].model_copy(update={"line_ordinal": 0}),)}
    )

    with pytest.raises(IntegrityError, match="ck_quote_blocked_reasons_line_ordinal_positive"):
        writer.persist(calculation, damaged)

    seeded.rollback()
    assert row_counts(seeded) == {"quotes": 0, "lines": 0, "ledger": 0, "claims": 0}


# ---------------------------------------------------------------------------
# The quotation header
# ---------------------------------------------------------------------------


def test_the_header_is_stored_exactly_as_calculated(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Every column is the value the calculation produced - none is recomputed here."""
    calculation = a_complete_quote(reader)
    quote_object = calculation.quote

    result = writer.persist(calculation, project(calculation))

    stored = seeded.get(QuoteRow, "QTE_0001")
    assert stored is not None
    assert stored.quote_id == quote_object.quote_id == "QTE_0001"
    assert stored.quote_number == quote_object.quote_number == "Q-2026-0001"
    assert stored.customer_id == quote_object.customer_id == "CUS_0001"
    assert stored.currency == quote_object.currency == "EUR"
    assert stored.status is quote_object.status
    assert stored.revision == 1
    assert stored.subtotal == quote_object.subtotal == Decimal("46000.00")
    assert stored.discount_amount == quote_object.discount_amount == Decimal("1380.00")
    assert stored.total == quote_object.total == Decimal("44620.00")
    assert stored.pricing_as_of == PRICING_AS_OF
    assert stored.calc_version == quote_object.calc_version
    assert stored.inputs_sha256 == quote_object.inputs_sha256
    assert result.quote_id == "QTE_0001"
    assert result.duplicate is False


def test_the_rfq_is_derived_from_the_run(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """``quotes.rfq_id`` is read from ``runs``: the quote never states it itself."""
    calculation = a_complete_quote(reader, quote_id="QTE_0004", quote_number="Q-2026-0004")

    writer.persist(calculation, project(calculation))

    stored = seeded.get(QuoteRow, "QTE_0004")
    assert stored is not None
    assert stored.rfq_id == "RFQ_0001"


def test_the_money_keeps_the_scale_the_schema_promises(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """``UNIT_PRICE`` is 14,4 and ``MONEY`` is 14,2, and neither is a float."""
    calculation = a_complete_quote(reader)

    writer.persist(calculation, project(calculation))

    stored = stored_lines(seeded)[0]
    header = seeded.get(QuoteRow, "QTE_0001")
    assert header is not None
    assert str(stored.unit_price) == "1150.0000"
    assert str(stored.line_extension) == "46000.00"
    assert str(header.subtotal) == "46000.00"
    assert str(header.total) == "44620.00"
    assert str(header.discount_amount) == "1380.00"
    assert str(header.discount_percent) == "3.00"
    assert isinstance(stored.unit_price, Decimal)


def test_the_discount_keeps_its_provenance(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Rule, scope and percentage are stored, not just the amount they produced."""
    calculation = a_complete_quote(reader)
    discount = calculation.quote.discount
    assert discount is not None
    assert (discount.rule_id, discount.scope.value, str(discount.percent)) == (
        "DSC_0003",
        "CUSTOMER",
        "3.00",
    )

    writer.persist(calculation, project(calculation))

    stored = seeded.get(QuoteRow, "QTE_0001")
    assert stored is not None
    assert stored.discount_rule_id == "DSC_0003"
    assert stored.discount_scope is discount.scope
    assert str(stored.discount_percent) == "3.00"


def test_no_gate_outcome_is_ever_stored(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The gate did not run, so its columns stay empty - projection is not a decision."""
    calculation = a_quote_with_a_refused_line(reader)

    writer.persist(calculation, project(calculation, credit_hold=True))

    stored = seeded.get(QuoteRow, "QTE_0002")
    assert stored is not None
    assert stored.policy_allowed is None
    assert stored.policy_reason_codes_json is None
    assert stored.approved_at is None


def test_the_delivery_promise_keeps_its_facts(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Feasibility, destination, lane, carrier, transit and dates, as promised."""
    calculation = a_mixed_quote(reader)
    assessment = calculation.quote.delivery
    assert assessment is not None
    promise = assessment.promise
    assert promise.feasibility.value == "FEASIBLE"

    writer.persist(calculation, project(calculation))

    stored = seeded.get(QuoteRow, "QTE_0003")
    assert stored is not None
    assert stored.delivery_feasibility is promise.feasibility
    assert stored.delivery_destination == promise.destination
    assert stored.origin_location == promise.origin_location
    assert stored.carrier_service_code == promise.carrier_service_code
    assert stored.transit_days_min == promise.transit_days
    assert stored.transit_days_max is None
    assert stored.earliest_ship_date == promise.earliest_ship_date
    assert stored.earliest_delivery_date == promise.earliest_delivery_date
    assert stored.requested_date == date(2026, 10, 8)
    assert stored.split_shipment_proposed == assessment.split_shipment_proposed


def test_an_unknown_promise_stores_no_dates(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """A destination this data has no calendar for yields no dates, and none appear."""
    priced = price_for(reader, "PRD_0001", 40)
    delivery = delivery_for(
        reader, stock_for(reader, "PRD_0001", 100), destination="Milan", destination_country="IT"
    )
    calculation = quote(
        [line(priced, sku="PMP-A-100", quantity=40, stock_status=StockStatus.SUFFICIENT)],
        quote_id="QTE_0005",
        quote_number="Q-2026-0005",
        delivery=delivery.assessment,
    )
    assert delivery.assessment.promise.feasibility.value == "UNKNOWN"

    writer.persist(calculation, project(calculation))

    stored = seeded.get(QuoteRow, "QTE_0005")
    assert stored is not None
    assert stored.delivery_feasibility.value == "UNKNOWN"
    assert stored.earliest_ship_date is None
    assert stored.earliest_delivery_date is None
    assert stored.carrier_service_code is None
    assert stored.transit_days_min is None


# ---------------------------------------------------------------------------
# The lines
# ---------------------------------------------------------------------------


def test_a_priced_line_keeps_its_price_entry(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Provenance is the point: the stored row names the price it was built from."""
    calculation = a_complete_quote(reader)
    domain_line = calculation.quote.lines[0]
    assert domain_line.price_entry_id == "PE_0021"

    writer.persist(calculation, project(calculation))

    stored = stored_lines(seeded)[0]
    assert stored.price_entry_id == domain_line.price_entry_id
    assert stored.price_status is domain_line.price_status
    assert stored.unit_price == domain_line.unit_price
    assert stored.line_extension == domain_line.line_extension
    assert stored.blocked is domain_line.blocked is False
    assert stored.blocked_reason is None
    assert stored.notes is None
    assert stored.ordinal == 1


def test_a_refused_line_is_stored_blocked_with_no_price_entry(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """D-1's case: the sentinel becomes ``NULL`` and the line says why it is blocked."""
    calculation = a_quote_with_a_refused_line(reader)
    domain_line = calculation.quote.lines[0]

    writer.persist(calculation, project(calculation))

    stored = stored_lines(seeded, "QTE_0002")[0]
    assert stored.price_entry_id is None
    assert stored.price_status is domain_line.price_status
    assert stored.price_status.value == "EXPIRED"
    assert stored.blocked is True
    assert stored.blocked_reason == domain_line.blocked_reason
    assert stored.blocked_reason is not None
    assert stored.stock_status.value == "NONE"
    assert str(stored.unit_price) == "0.0000"
    assert str(stored.line_extension) == "0.00"


def test_the_sentinel_is_translated_only_at_the_boundary(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The domain keeps saying ``PRICE_MISSING``; the database says nothing at all."""
    calculation = a_quote_with_a_refused_line(reader)
    domain_line = calculation.quote.lines[0]
    assert domain_line.price_entry_id == MISSING_PRICE_ENTRY_ID

    writer.persist(calculation, project(calculation))

    assert domain_line.price_entry_id == MISSING_PRICE_ENTRY_ID
    assert stored_lines(seeded, "QTE_0002")[0].price_entry_id is None


def test_the_lines_are_stored_in_ordinal_order_with_deterministic_ids(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Two lines, two generated ids, in the order the calculation gave them."""
    calculation = a_mixed_quote(reader)
    assert [line_.ordinal for line_ in calculation.quote.lines] == [1, 2]

    result = writer.persist(calculation, project(calculation))

    stored = stored_lines(seeded, "QTE_0003")
    assert [row.ordinal for row in stored] == [1, 2]
    assert [row.line_id for row in stored] == ["QTE_0003-L01", "QTE_0003-L02"]
    assert result.line_ids == ("QTE_0003-L01", "QTE_0003-L02")
    assert stored[0].price_entry_id is None
    assert stored[1].price_entry_id == "PE_0021"


@pytest.mark.parametrize(
    ("quote_id", "ordinal", "expected"),
    [
        ("QTE_0001", 1, "QTE_0001-L01"),
        ("QTE_0001", 12, "QTE_0001-L12"),
        ("Q" * 64, 1, None),
    ],
    ids=["first", "twelfth", "too-long-for-the-column"],
)
def test_a_line_id_is_a_pure_bounded_function(
    quote_id: str, ordinal: int, expected: str | None
) -> None:
    """Deterministic, unique per ordinal, and never longer than the column."""
    first = quote_line_id(quote_id, ordinal)
    assert first == quote_line_id(quote_id, ordinal)
    assert first != quote_line_id(quote_id, ordinal + 1)
    assert len(first) <= 64
    if expected is not None:
        assert first == expected
    else:
        assert first.startswith("L")


def test_a_mixed_quote_stores_both_kinds_of_line(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The stored quote is internally consistent: one blocked line, one priced one."""
    calculation = a_mixed_quote(reader)
    assert calculation.complete is False
    assert calculation.quote.subtotal == Decimal("46000.00")

    writer.persist(calculation, project(calculation))

    stored = stored_lines(seeded, "QTE_0003")
    assert [row.blocked for row in stored] == [True, False]
    header = seeded.get(QuoteRow, "QTE_0003")
    assert header is not None
    assert header.subtotal == calculation.quote.subtotal
    assert header.total == calculation.quote.total


# ---------------------------------------------------------------------------
# Retries, refusals and the one transaction
# ---------------------------------------------------------------------------


def test_a_duplicate_attempt_writes_nothing_new(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The same write twice is one write: no second quote, no doubled lines."""
    calculation = a_mixed_quote(reader)
    ledger = project(calculation)
    first = writer.persist(calculation, ledger)
    before = row_counts(seeded)

    second = writer.persist(calculation, ledger)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.line_ids == first.line_ids
    assert second.ledger_seqs == first.ledger_seqs
    assert row_counts(seeded) == before


def test_a_duplicate_attempt_is_not_a_new_revision(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Nothing about revision creation is invented: the one row stays at revision 1."""
    calculation = a_complete_quote(reader)
    writer.persist(calculation, project(calculation))
    writer.persist(calculation, project(calculation))

    quotes = seeded.scalars(select(QuoteRow)).all()
    assert len(quotes) == 1
    assert quotes[0].revision == 1
    assert len(stored_lines(seeded)) == 1


def test_a_retry_with_a_different_clock_is_still_the_same_write(
    reader: BusinessReader, db: Database
) -> None:
    """The identity of a write is its content, not the instant it happened."""
    calculation = a_complete_quote(reader)
    ledger = project(calculation)
    QuoteWriter(db, clock=FrozenClock(WRITTEN_AT)).persist(calculation, ledger)

    later = FrozenClock(WRITTEN_AT.replace(hour=17))
    second = QuoteWriter(db, clock=later).persist(calculation, ledger)

    assert second.duplicate is True


def test_a_different_calculation_for_the_same_run_is_refused(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """A second quote for one run would need a revision number, which nothing here invents."""
    calculation = a_complete_quote(reader)
    writer.persist(calculation, project(calculation))
    priced = price_for(reader, "PRD_0001", 41)
    different = quote(
        [line(priced, sku="PMP-A-100", quantity=41, stock_status=StockStatus.SUFFICIENT)],
        quote_id="QTE_0006",
        quote_number="Q-2026-0006",
    )
    before = row_counts(seeded)

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        writer.persist(different, project(different))

    seeded.rollback()
    assert row_counts(seeded) == before


def test_a_line_the_database_refuses_rolls_back_the_whole_write(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """A blocked line with no reason is not storable, and takes the header down with it.

    The damaged line is built with ``model_copy``, which skips validation on
    purpose: this is about what the *database* does with a row the writer would
    otherwise happily hand it, not about the domain's own guards.
    """
    calculation = a_complete_quote(reader)
    damaged_line = calculation.quote.lines[0].model_copy(
        update={"blocked": True, "blocked_reason": None}
    )
    damaged = calculation.model_copy(
        update={"quote": calculation.quote.model_copy(update={"lines": (damaged_line,)})}
    )

    with pytest.raises(IntegrityError, match="ck_quote_lines_blocked_requires_reason"):
        writer.persist(damaged, project(calculation))

    seeded.rollback()
    assert row_counts(seeded) == {"quotes": 0, "lines": 0, "ledger": 0, "claims": 0}


def test_a_ledger_the_database_refuses_rolls_back_the_whole_write(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The ledger is not a second write: if it cannot be stored, the quote is not either.

    One code may appear once per quote (``uq_quote_blocked_reasons_quote_id_code``),
    which the projection already guarantees - so this is the failure the schema
    has to catch, not one the writer is expected to produce.
    """
    calculation = a_quote_with_a_refused_line(reader)
    ledger = project(calculation)
    repeated = ledger.model_copy(update={"reasons": (ledger.reasons[0], ledger.reasons[0])})

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        writer.persist(calculation, repeated)

    seeded.rollback()
    assert row_counts(seeded) == {"quotes": 0, "lines": 0, "ledger": 0, "claims": 0}


def test_a_failed_write_leaves_the_retry_free(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """The claim is taken and released in one transaction, so a crash is not a lock."""
    calculation = a_quote_with_a_refused_line(reader)
    ledger = project(calculation)
    damaged = ledger.model_copy(update={"reasons": (ledger.reasons[0], ledger.reasons[0])})
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        writer.persist(calculation, damaged)

    result = writer.persist(calculation, ledger)

    assert result.duplicate is False
    assert len(stored_ledger(seeded, "QTE_0002")) == len(ledger.reasons)


def test_a_ledger_for_another_quote_is_refused_before_anything_is_written(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Evidence attributed to a different quotation is a false record, not a row."""
    calculation = a_complete_quote(reader)
    mismatched = project(calculation, run_id=OTHER_RUN_ID)

    with pytest.raises(ValueError, match="ledger is for run"):
        writer.persist(calculation, mismatched)

    seeded.rollback()
    assert row_counts(seeded) == {"quotes": 0, "lines": 0, "ledger": 0, "claims": 0}


def test_a_found_line_that_carries_the_sentinel_is_refused(
    reader: BusinessReader, writer: QuoteWriter
) -> None:
    """``FOUND`` with the sentinel contradicts itself, and is caught here, not in SQL."""
    calculation = a_complete_quote(reader)
    lying = calculation.quote.lines[0].model_copy(update={"price_entry_id": MISSING_PRICE_ENTRY_ID})
    broken = calculation.model_copy(
        update={"quote": calculation.quote.model_copy(update={"lines": (lying,)})}
    )

    with pytest.raises(ValueError, match="is FOUND but carries no price entry id"):
        writer.persist(broken, project(calculation))


def test_the_inputs_are_not_mutated(reader: BusinessReader, writer: QuoteWriter) -> None:
    """The writer reads its arguments and does not rewrite them."""
    calculation = a_mixed_quote(reader)
    ledger = project(calculation)
    before = (calculation.model_dump(mode="json"), ledger.model_dump(mode="json"))

    writer.persist(calculation, ledger)

    assert (calculation.model_dump(mode="json"), ledger.model_dump(mode="json")) == before


def test_the_claim_store_can_release_a_claim(session: Session) -> None:
    """``release`` is part of the port, and it works on this database."""
    store = SqlIdempotencyStore(session, clock=FrozenClock())
    assert store.claim("key-00000001", scope=CLAIM_SCOPE) is True
    assert store.claim("key-00000001", scope=CLAIM_SCOPE) is False

    store.release("key-00000001", scope=CLAIM_SCOPE)

    assert store.is_claimed("key-00000001", scope=CLAIM_SCOPE) is False
    assert store.claim("key-00000001", scope=CLAIM_SCOPE) is True


# ---------------------------------------------------------------------------
# The whole pipeline, once
# ---------------------------------------------------------------------------


def test_the_seeded_pipeline_persists_what_it_computed(
    reader: BusinessReader, writer: QuoteWriter, seeded: Session
) -> None:
    """Selected, calculated, projected, stored - and read back equal to all of it."""
    calculation = a_mixed_quote(reader)
    ledger = project(calculation, credit_hold=True)
    quote_object = calculation.quote

    result = writer.persist(calculation, ledger)

    seeded.expire_all()
    stored_quote = seeded.get(QuoteRow, "QTE_0003")
    stored_line_rows = stored_lines(seeded, "QTE_0003")
    stored_reasons = stored_ledger(seeded, "QTE_0003")

    assert result.duplicate is False
    assert stored_quote is not None
    assert stored_quote.rfq_id == "RFQ_0001"
    assert stored_quote.subtotal == quote_object.subtotal
    assert stored_quote.discount_amount == quote_object.discount_amount
    assert stored_quote.total == quote_object.total
    assert stored_quote.inputs_sha256 == quote_object.inputs_sha256
    assert stored_quote.calc_version == quote_object.calc_version

    assert len(stored_line_rows) == len(quote_object.lines)
    for stored_line, domain_line in zip(stored_line_rows, quote_object.lines, strict=True):
        assert stored_line.ordinal == domain_line.ordinal
        assert stored_line.sku == domain_line.sku
        assert stored_line.quantity == domain_line.quantity
        assert stored_line.unit_price == domain_line.unit_price
        assert stored_line.line_extension == domain_line.line_extension
        assert stored_line.price_status is domain_line.price_status
        assert stored_line.stock_status is domain_line.stock_status
        assert stored_line.blocked is domain_line.blocked
        assert stored_line.blocked_reason == domain_line.blocked_reason
        expected_entry = (
            None
            if domain_line.price_entry_id == MISSING_PRICE_ENTRY_ID
            else domain_line.price_entry_id
        )
        assert stored_line.price_entry_id == expected_entry

    assert [(row.code, row.message) for row in stored_reasons] == [
        (reason.code, reason.message) for reason in ledger.reasons
    ]
