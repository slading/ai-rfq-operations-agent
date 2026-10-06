"""The Phase 1J' write path, over the demo dataset.

Everything stored here was decided before it was stored. The price comes from
``select_price`` over the rows ``BusinessReader`` returns, the stock status from
``evaluate_stock``, the delivery promise from ``evaluate_delivery``, the discount
from ``select_discount``, the arithmetic from ``calculate_quote`` and the
evidence from ``project_blocked_ledger``. The writer's only job is to put those
accepted facts into rows - so a reviewer can reproduce every assertion below
from the Northwind Components catalogue by hand.

The tests therefore check *storage*: that the value read back is the value that
was computed, down to the money's scale and the price entry the price came from;
that a line with no usable price is storable (Phase 1J' decision D-1) without
ceasing to be a refusal; that the same write twice is one quote; and that a write
the database refuses leaves nothing behind, claim included.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
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
from rfq_agent.domain.quote import (
    MISSING_PRICE_ENTRY_ID,
    QuoteCalculation,
    QuoteLineInput,
    QuoteStatus,
    calculate_quote,
)
from rfq_agent.domain.stock import StockEvaluation, StockStatus, evaluate_stock
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    IdempotencyClaimRow,
    QuoteBlockedReasonRow,
    QuoteLineRow,
    QuoteRow,
    RunRow,
)
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import (
    CLAIM_SCOPE,
    QuoteWriter,
    QuoteWriteResult,
    quote_line_id,
)
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

#: The date every price question in these tests is asked for.
AS_OF = date(2026, 10, 6)
#: The stamp the seeded stock positions carry.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: When the enquiry arrived - before the DHL express cut-off, so it ships today.
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: The run every quote in this module belongs to, and its RFQ.
RUN_ID = "RUN_0001"
RFQ_ID = "RFQ_0001"
#: A second run, for the tests that need a quote the first one does not own.
OTHER_RUN_ID = "RUN_0002"


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def runs(seeded: Session) -> Session:
    """The seeded database plus the two runs a quote can belong to.

    The demo dataset is business data; a run is an operational record the agent
    creates, so the tests create the two runs they need - each with the RFQ it
    belongs to, because ``quotes.rfq_id`` is read from this table (and the
    foreign key is enforced).
    """
    seeded.add_all([rfq_row("RFQ_0001"), rfq_row("RFQ_0002")])
    seeded.flush()
    seeded.add_all([run_row("RUN_0001", "RFQ_0001"), run_row("RUN_0002", "RFQ_0002")])
    seeded.commit()
    return seeded


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


@pytest.fixture
def writer(db: Database) -> QuoteWriter:
    """The write path under test."""
    return QuoteWriter(db)


# ---------------------------------------------------------------------------
# The pipeline's own steps, in one place
# ---------------------------------------------------------------------------


def price_for(
    reader: BusinessReader,
    product_id: str,
    quantity: int,
    *,
    customer: str = "CUS_0001",
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
    quote_id: str = "QTE_0001",
    number: str = "Q-2026-0001",
    run_id: str = RUN_ID,
    customer: str = "CUS_0001",
    discount: DiscountSelection | None = None,
    delivery: DeliveryEvaluation | None = None,
) -> QuoteCalculation:
    """Calculate a quote from facts that were selected, never hand-written."""
    return calculate_quote(
        lines,
        quote_id=quote_id,
        quote_number=number,
        run_id=run_id,
        customer_id=customer,
        currency="EUR",
        pricing_as_of=AS_OF,
        discount=None if discount is None or not discount.applied else discount.discount,
        delivery=None if delivery is None else delivery.assessment,
    )


def project(
    calculation: QuoteCalculation,
    *,
    run_id: str = RUN_ID,
    customer_on_credit_hold: bool = False,
) -> QuoteBlockedLedger:
    """Project the calculation the way the caller will, before storing it."""
    return project_blocked_ledger(
        calculation,
        run_id=run_id,
        customer_on_credit_hold=customer_on_credit_hold,
    )


def a_priced_quote(reader: BusinessReader) -> QuoteCalculation:
    """One line, forty pumps, priced and discounted: the ordinary case."""
    price = price_for(reader, "PRD_0001", 40)
    return quote(
        [line(price, sku="PMP-A-100", quantity=40)],
        discount=discount_for(
            reader, customer="CUS_0001", quantity=40, order_value=Decimal("46000.00")
        ),
    )


def a_quote_with_a_refusal(reader: BusinessReader) -> QuoteCalculation:
    """One line whose price lookup failed: storable since D-1, still a refusal."""
    price = price_for(reader, "PRD_0006", 1)
    stock = stock_for(reader, "PRD_0006", 1)
    return quote([line(price, sku="PMP-D-300", quantity=1, stock_status=stock.status)])


# ---------------------------------------------------------------------------
# Reading back what was written
# ---------------------------------------------------------------------------


def header(session: Session, quote_id: str = "QTE_0001") -> QuoteRow:
    """The stored header, read in a fresh transaction so nothing is cached."""
    session.rollback()
    row = session.get(QuoteRow, quote_id)
    assert row is not None
    return row


def lines_of(session: Session, quote_id: str = "QTE_0001") -> list[QuoteLineRow]:
    """The stored lines, in ordinal order."""
    session.rollback()
    return list(
        session.scalars(
            select(QuoteLineRow)
            .where(QuoteLineRow.quote_id == quote_id)
            .order_by(QuoteLineRow.ordinal)
        )
    )


def reasons_of(session: Session, quote_id: str = "QTE_0001") -> list[QuoteBlockedReasonRow]:
    """The stored ledger, in the order it was written."""
    session.rollback()
    return list(
        session.scalars(
            select(QuoteBlockedReasonRow)
            .where(QuoteBlockedReasonRow.quote_id == quote_id)
            .order_by(QuoteBlockedReasonRow.seq)
        )
    )


def counts(session: Session) -> tuple[int, int, int, int]:
    """Quotes, lines, ledger rows and claims: the four things a write touches."""
    session.rollback()

    def count(model: type[object]) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    return (
        count(QuoteRow),
        count(QuoteLineRow),
        count(QuoteBlockedReasonRow),
        count(IdempotencyClaimRow),
    )


# ---------------------------------------------------------------------------
# The header, as calculated
# ---------------------------------------------------------------------------


def test_a_priced_quote_is_stored_header_and_all(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The header is the calculation's header: nothing is added, nothing recomputed."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.quote_id == "QTE_0001"
    assert row.quote_number == "Q-2026-0001"
    assert row.run_id == RUN_ID
    assert row.customer_id == "CUS_0001"
    assert row.currency == "EUR"
    assert row.pricing_as_of == AS_OF
    assert row.calc_version == calculation.quote.calc_version
    assert row.inputs_sha256 == calculation.quote.inputs_sha256
    assert row.status == QuoteStatus.DRAFT.value
    assert row.revision == 1


def test_the_stored_totals_are_the_ones_that_were_calculated(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Money is compared as decimals, at the scale the schema promises."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert str(row.subtotal) == "46000.00"
    assert str(row.discount_amount) == "1380.00"
    assert str(row.total) == "44620.00"
    assert row.subtotal == calculation.quote.subtotal
    assert row.discount_amount == calculation.quote.discount_amount
    assert row.total == calculation.quote.total


def test_the_rfq_comes_from_the_run_not_the_quote(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """``rfq_id`` is derived, read-only, and read from the run the quote names."""
    calculation = a_priced_quote(reader)
    stored_rfq = runs.scalar(select(RunRow.rfq_id).where(RunRow.run_id == RUN_ID))

    writer.persist(calculation, project(calculation))

    assert not hasattr(calculation.quote, "rfq_id")
    assert header(runs).rfq_id == stored_rfq == RFQ_ID


def test_the_discount_keeps_its_provenance(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Which rule applied, at which scope, at which percentage - all stored."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.discount_rule_id == "DSC_0003"
    assert row.discount_scope == "CUSTOMER"
    assert str(row.discount_percent) == "3.00"


def test_a_quote_without_a_discount_stores_no_rule(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Below the floor the selection is empty, and empty is stored as ``NULL``."""
    price = price_for(reader, "PRD_0008", 10, customer="CUS_0002")
    selection = discount_for(
        reader, customer="CUS_0002", quantity=10, order_value=Decimal("1784.00")
    )
    assert not selection.applied  # the only global rule wants 5000.00
    calculation = quote(
        [line(price, sku="VLV-GT-010", quantity=10)], customer="CUS_0002", discount=selection
    )
    assert calculation.quote.discount is None

    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.discount_rule_id is None
    assert row.discount_scope is None
    assert row.discount_percent is None
    assert str(row.discount_amount) == "0.00"
    assert row.total == calculation.quote.total
    assert row.total < Decimal("5000")


def test_the_gate_columns_are_left_unpopulated(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """No gate ran, so no gate outcome, no reason codes and no approval are stored."""
    calculation = quote([line(price_for(reader, "PRD_0001", 40), sku="PMP-A-100", quantity=40)])
    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.policy_allowed is None
    assert row.policy_reason_codes_json is None
    assert row.approved_at is None


# ---------------------------------------------------------------------------
# The lines, provenance included
# ---------------------------------------------------------------------------


def test_a_priced_line_keeps_the_price_entry_that_was_selected(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The row names the price row it came from, and carries its unit price."""
    price = price_for(reader, "PRD_0001", 40)
    calculation = quote([line(price, sku="PMP-A-100", quantity=40)])
    writer.persist(calculation, project(calculation))
    stored = lines_of(runs)

    assert price.price is not None
    assert [row.line_id for row in stored] == ["QTE_0001-L01"]
    assert stored[0].price_entry_id == price.price.price_entry_id == "PE_0021"
    assert stored[0].price_status == "FOUND"
    assert str(stored[0].unit_price) == "1150.0000"
    assert str(stored[0].line_extension) == "46000.00"
    assert stored[0].quantity == 40
    assert stored[0].currency == "EUR"
    assert stored[0].product_id == "PRD_0001"
    assert stored[0].sku == "PMP-A-100"
    assert stored[0].blocked is False
    assert stored[0].blocked_reason is None


def test_a_refused_line_is_stored_blocked_without_a_price_entry(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The D-1 case: representable, blocked, and carrying no price and no money."""
    calculation = a_quote_with_a_refusal(reader)
    writer.persist(calculation, project(calculation))
    stored = lines_of(runs)

    assert stored[0].price_entry_id is None
    assert stored[0].price_status == "EXPIRED"
    assert stored[0].blocked is True
    assert stored[0].blocked_reason
    assert stored[0].stock_status == StockStatus.NONE.value
    assert str(stored[0].unit_price) == "0.0000"
    assert str(stored[0].line_extension) == "0.00"
    assert str(header(runs).subtotal) == "0.00"
    assert str(header(runs).total) == "0.00"


def test_the_sentinel_is_translated_at_the_boundary_and_nowhere_else(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """In memory the refusal is ``PRICE_MISSING``; in the column it is ``NULL``."""
    calculation = a_quote_with_a_refusal(reader)
    assert calculation.quote.lines[0].price_entry_id == MISSING_PRICE_ENTRY_ID

    writer.persist(calculation, project(calculation))

    assert calculation.quote.lines[0].price_entry_id == MISSING_PRICE_ENTRY_ID
    assert lines_of(runs)[0].price_entry_id is None


def test_a_line_that_contradicts_itself_is_refused_before_it_is_stored(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """A ``FOUND`` line that names no price entry is a claim, not a row."""
    del runs  # dependency only: a quote cannot be stored without its run
    calculation = a_priced_quote(reader)
    lying = calculation.quote.lines[0].model_copy(update={"price_entry_id": MISSING_PRICE_ENTRY_ID})
    broken = calculation.model_copy(
        update={"quote": calculation.quote.model_copy(update={"lines": (lying,)})}
    )

    with pytest.raises(ValueError, match="FOUND but carries no price entry id"):
        writer.persist(broken, project(broken))


def test_the_lines_are_stored_in_ordinal_order_with_minted_ids(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Ordinal order on the way in, and an id per line that the quote determines."""
    refused = price_for(reader, "PRD_0006", 1)
    priced = price_for(reader, "PRD_0001", 40)
    calculation = quote(
        [
            line(refused, sku="PMP-D-300", quantity=1, stock_status=StockStatus.NONE),
            line(priced, sku="PMP-A-100", quantity=40),
        ]
    )
    result = writer.persist(calculation, project(calculation))
    stored = lines_of(runs)

    assert [row.ordinal for row in stored] == [1, 2]
    assert [row.line_id for row in stored] == ["QTE_0001-L01", "QTE_0001-L02"]
    assert result.line_ids == ("QTE_0001-L01", "QTE_0001-L02")
    assert [row.line_id for row in stored] == [
        quote_line_id("QTE_0001", line_.ordinal) for line_ in calculation.quote.lines
    ]


def test_the_write_result_reports_what_was_stored(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    del runs  # dependency only: a quote cannot be stored without its run
    """The result is a receipt: the ids written, in order, and the ledger's ordinals."""
    calculation = a_quote_with_a_refusal(reader)
    result = writer.persist(calculation, project(calculation))

    assert isinstance(result, QuoteWriteResult)
    assert result.quote_id == "QTE_0001"
    assert result.duplicate is False
    assert result.ledger_seqs == (1, 2)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_a_clean_quote_stores_no_ledger_at_all(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """A quote with nothing to report has no reasons, so it has no rows."""
    calculation = a_priced_quote(reader)
    ledger = project(calculation)

    assert ledger.reasons == ()
    assert writer.persist(calculation, ledger).ledger_seqs == ()
    assert reasons_of(runs) == []


def test_the_ledger_is_stored_in_the_order_it_was_projected(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The order the projector chose is evidence, so it is stored, not recomputed."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation, customer_on_credit_hold=True)
    writer.persist(calculation, ledger)
    stored = reasons_of(runs)

    assert [reason.code for reason in ledger.reasons] == [
        BlockedReasonCode.PRICE_MISSING.value,
        BlockedReasonCode.STOCK_INSUFFICIENT.value,
        BlockedReasonCode.CREDIT_HOLD.value,
    ]
    assert [row.seq for row in stored] == [1, 2, 3]
    assert [row.code for row in stored] == [reason.code.value for reason in ledger.reasons]
    assert [row.message for row in stored] == [reason.message for reason in ledger.reasons]


def test_a_reason_keeps_the_line_it_is_about(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Line reasons carry their ordinal; a quote-level reason carries ``NULL``."""
    calculation = a_quote_with_a_refusal(reader)
    writer.persist(calculation, project(calculation, customer_on_credit_hold=True))
    stored = reasons_of(runs)

    assert stored[0].line_ordinal == 1
    assert stored[1].line_ordinal == 1
    assert stored[2].line_ordinal is None


def test_a_reason_keeps_whether_a_human_can_resolve_it(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The projection's judgement travels with the reason, unchanged."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation, customer_on_credit_hold=True)
    writer.persist(calculation, ledger)
    stored = reasons_of(runs)

    assert [row.resolvable_by_human for row in stored] == [
        reason.resolvable_by_human for reason in ledger.reasons
    ]
    assert [row.run_id for row in stored] == [RUN_ID, RUN_ID, RUN_ID]


def test_the_ledger_stores_its_flags_on_every_reason(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Flags belong to the ledger, so every row carries them - even when empty."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation, customer_on_credit_hold=True)

    assert ledger.flags == ()
    writer.persist(calculation, ledger)

    assert [row.flags_json for row in reasons_of(runs)] == [[], [], []]


# ---------------------------------------------------------------------------
# Retry, idempotency, and what a second write means
# ---------------------------------------------------------------------------


def test_a_second_identical_write_stores_nothing_new(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """A retry is the same operation, and the same operation is written once."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation)
    first = writer.persist(calculation, ledger)
    before = counts(runs)

    second = writer.persist(calculation, ledger)

    assert second.duplicate is True
    assert second.line_ids == first.line_ids
    assert second.ledger_seqs == first.ledger_seqs
    assert counts(runs) == before == (1, 1, 2, 1)


def test_a_retry_is_not_a_new_revision(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The write path stores a calculation; it does not invent revision policy."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))
    writer.persist(calculation, project(calculation))

    assert [row.revision for row in runs.scalars(select(QuoteRow))] == [1]


def test_a_different_quote_for_the_same_run_is_refused(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """A second quotation for one run is a contradiction, not revision two."""
    first = a_priced_quote(reader)
    writer.persist(first, project(first))

    other = quote(
        [line(price_for(reader, "PRD_0001", 40), sku="PMP-A-100", quantity=40)],
        quote_id="QTE_0002",
        number="Q-2026-0002",
        customer="CUS_0002",
    )
    with pytest.raises(IntegrityError, match=r"quotes\.run_id"):
        writer.persist(other, project(other))

    assert counts(runs)[0] == 1
    assert runs.scalars(select(QuoteLineRow.line_id)).all() == ["QTE_0001-L01"]


def test_a_failed_write_leaves_the_retry_free(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The claim is taken and released with the rows, so a failure is not a lock."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation)
    damaged = ledger.model_copy(
        update={"reasons": (ledger.reasons[0].model_copy(update={"message": "x" * 301}),)}
    )

    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        writer.persist(calculation, damaged)

    assert counts(runs) == (0, 0, 0, 0)
    assert writer.persist(calculation, ledger).duplicate is False
    assert counts(runs) == (1, 1, 2, 1)


# ---------------------------------------------------------------------------
# What the database refuses
# ---------------------------------------------------------------------------


def test_a_line_the_database_refuses_leaves_no_quote_behind(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Line one is impossible, so the header is not stored either."""
    calculation = a_priced_quote(reader)
    impossible = calculation.quote.lines[0].model_copy(
        update={"stock_status": StockStatus.NONE, "blocked": False}
    )
    broken = calculation.model_copy(
        update={"quote": calculation.quote.model_copy(update={"lines": (impossible,)})}
    )

    with pytest.raises(IntegrityError, match="ck_quote_lines_no_stock_blocks_line"):
        writer.persist(broken, project(broken))

    assert counts(runs) == (0, 0, 0, 0)


def test_a_ledger_the_database_refuses_leaves_no_quote_behind(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Evidence is not a second write: if it cannot be stored, nothing is."""
    calculation = a_quote_with_a_refusal(reader)
    ledger = project(calculation)
    repeated = ledger.model_copy(update={"reasons": (ledger.reasons[0], ledger.reasons[0])})

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        writer.persist(calculation, repeated)

    assert counts(runs) == (0, 0, 0, 0)


def test_a_ledger_for_another_run_is_refused_before_anything_is_written(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Evidence attributed to the wrong run would be a false record."""
    calculation = a_quote_with_a_refusal(reader)

    with pytest.raises(ValueError, match="is for run"):
        writer.persist(calculation, project(calculation, run_id=OTHER_RUN_ID))

    assert counts(runs) == (0, 0, 0, 0)


def test_a_claim_is_stored_under_the_persistence_scope(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """One claim per written quote, in this scope, keyed by the operation."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))
    runs.rollback()

    claims = list(runs.scalars(select(IdempotencyClaimRow)))
    assert [claim.scope for claim in claims] == [CLAIM_SCOPE] == ["quote_persist"]
    assert claims[0].claim_key


# ---------------------------------------------------------------------------
# Delivery, and the one documented mapping
# ---------------------------------------------------------------------------


def test_a_feasible_promise_keeps_its_dates(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """The stored delivery block is the promise's own facts, not a summary of them."""
    stock = stock_for(reader, "PRD_0001", 40)
    delivery = delivery_for(reader, stock, requested_date=date(2026, 10, 8))
    calculation = quote(
        [line(price_for(reader, "PRD_0001", 40), sku="PMP-A-100", quantity=40)],
        delivery=delivery,
    )
    promise = calculation.quote.delivery
    assert promise is not None
    assert promise.promise is not None

    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.delivery_feasibility == promise.promise.feasibility.value == "FEASIBLE"
    assert row.delivery_destination == promise.promise.destination == "Hamburg"
    assert row.origin_location == promise.promise.origin_location
    assert row.carrier_service_code == promise.promise.carrier_service_code
    assert row.earliest_ship_date == promise.promise.earliest_ship_date
    assert row.earliest_delivery_date == promise.promise.earliest_delivery_date
    assert row.requested_date == promise.promise.requested_date == date(2026, 10, 8)
    assert row.split_shipment_proposed is promise.split_shipment_proposed
    assert row.transit_days_min == promise.promise.transit_days
    assert row.transit_days_max is None


def test_a_quote_without_a_delivery_question_stores_none(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Nobody asked when it can arrive, so the whole block stays empty."""
    calculation = a_priced_quote(reader)
    assert calculation.quote.delivery is None

    writer.persist(calculation, project(calculation))
    row = header(runs)

    assert row.delivery_feasibility is None
    assert row.delivery_destination is None
    assert row.origin_location is None
    assert row.carrier_service_code is None
    assert row.transit_days_min is None
    assert row.transit_days_max is None
    assert row.earliest_ship_date is None
    assert row.earliest_delivery_date is None
    assert row.requested_date is None
    assert row.split_shipment_proposed is False


def test_an_unknown_promise_stores_no_dates(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """``UNKNOWN`` is a fact with no date, and no date is stored for it."""
    stock = stock_for(reader, "PRD_0001", 40)
    delivery = delivery_for(
        reader, stock, destination="Milan", destination_country="IT", requested_date=None
    )
    calculation = quote(
        [line(price_for(reader, "PRD_0001", 40), sku="PMP-A-100", quantity=40)],
        delivery=delivery,
    )

    promise = calculation.quote.delivery
    assert promise is not None
    assert promise.promise is not None
    assert promise.promise.feasibility.value == "UNKNOWN"

    writer.persist(calculation, project(calculation))
    row = header(runs)

    # The route is known even when the date is not; the dates stay empty.
    assert row.delivery_feasibility == "UNKNOWN"
    assert row.origin_location == promise.promise.origin_location
    assert row.carrier_service_code == promise.promise.carrier_service_code
    assert row.earliest_ship_date is None
    assert row.earliest_delivery_date is None
    assert row.requested_date is None


# ---------------------------------------------------------------------------
# The whole pipeline, stored
# ---------------------------------------------------------------------------


def test_the_seeded_pipeline_persists_what_it_computed(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Selected, calculated, projected, stored - and read back equal to all of it."""
    refused = price_for(reader, "PRD_0006", 1)
    priced = price_for(reader, "PRD_0001", 40)
    calculation = quote(
        [
            line(refused, sku="PMP-D-300", quantity=1, stock_status=StockStatus.NONE),
            line(priced, sku="PMP-A-100", quantity=40),
        ],
        discount=discount_for(
            reader,
            customer="CUS_0007",
            quantity=40,
            order_value=Decimal("46000.00"),
        ),
    )
    ledger = project(calculation, customer_on_credit_hold=True)
    result = writer.persist(calculation, ledger)

    stored = reasons_of(runs)
    assert result.duplicate is False
    assert result.ledger_seqs == tuple(range(1, len(ledger.reasons) + 1))
    assert [row.code for row in stored] == [reason.code.value for reason in ledger.reasons]
    # The refused line, its stock, the approval-required rule CUS_0007 selects,
    # and the hold on the account - one row each, in the projector's order.
    assert sorted(row.code for row in stored) == [
        BlockedReasonCode.CREDIT_HOLD.value,
        BlockedReasonCode.DISCOUNT_OVER_POLICY.value,
        BlockedReasonCode.PRICE_MISSING.value,
        BlockedReasonCode.STOCK_INSUFFICIENT.value,
    ]
    assert [(row.line_id, row.ordinal, row.price_entry_id) for row in lines_of(runs)] == [
        ("QTE_0001-L01", 1, None),
        ("QTE_0001-L02", 2, priced.price.price_entry_id if priced.price else None),
    ]
    assert counts(runs) == (1, 2, 4, 1)


def test_the_stored_quote_matches_the_calculation_line_for_line(
    reader: BusinessReader, runs: Session, writer: QuoteWriter
) -> None:
    """Nothing is recalculated: every stored number equals the accepted one."""
    calculation = a_priced_quote(reader)
    writer.persist(calculation, project(calculation))

    for stored, accepted in zip(lines_of(runs), calculation.quote.lines, strict=True):
        assert stored.ordinal == accepted.ordinal
        assert stored.product_id == accepted.product_id
        assert stored.quantity == accepted.quantity
        assert stored.unit_price == accepted.unit_price
        assert stored.line_extension == accepted.line_extension
        assert stored.price_entry_id == accepted.price_entry_id
        assert stored.price_status == accepted.price_status.value
        assert stored.stock_status == accepted.stock_status.value
        assert stored.blocked is accepted.blocked
        assert stored.blocked_reason == accepted.blocked_reason
