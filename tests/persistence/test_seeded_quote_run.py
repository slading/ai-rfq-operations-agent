"""The deterministic quote run, end to end over the demo dataset.

Phase 1L's core is one composition: ``BusinessReader`` supplies the business
data, the accepted selectors decide the price, the stock, the delivery and the
discount, the calculator does the arithmetic, the projection writes the blocking
ledger and the Phase 1K gate answers the one question it answers. Nothing here
re-derives any of that. What these tests check is the seam: the run reaches the
verdicts the accepted functions would reach, equal requests over equal data
produce equal results down to the fingerprints, a request that cannot proceed
fails closed without inventing anything, and storing what a run produced changes
none of a quote's standing.

The verdicts themselves belong to their phase tests - ``test_seeded_quote.py``
for the money, ``test_seeded_delivery.py`` for the promise,
``test_seeded_blocking.py`` for the ledger and ``test_policy_gate_decision.py``
for the gate - and are not re-argued here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryFeasibility
from rfq_agent.domain.policy import (
    GATE_VERSION,
    BlockedReasonCode,
    PolicyEvidenceStatus,
    PolicyGateDecision,
    QuoteBlockedLedger,
)
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import QuoteCalculation, QuoteStatus
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    OutboundMessageRow,
    QuoteBlockedReasonRow,
    QuoteRow,
    RunEventRow,
    RunRow,
)
from rfq_agent.persistence.read_models import CustomerRecord
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import QuoteWriter
from rfq_agent.quoting import (
    DeliveryQuestion,
    DiscountQuestion,
    QuoteLineRequest,
    QuoteRequest,
    QuoteRunStatus,
    run_quote,
)
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

#: The date every price in these tests is selected for.
AS_OF = date(2026, 10, 6)
#: The stamp the seeded stock positions carry.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: When the enquiry arrived - before the DHL express cut-off, so it ships today.
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: The run every quote in this module belongs to, and its RFQ.
RUN_ID = "RUN_0001"
RFQ_ID = "RFQ_0001"


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def runs(seeded: Session) -> Session:
    """The seeded database plus the run a quote belongs to (and its RFQ)."""
    seeded.add(rfq_row(RFQ_ID))
    seeded.flush()
    seeded.add(run_row(RUN_ID, RFQ_ID))
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
    """The accepted write path, for the integration checks."""
    return QuoteWriter(db)


def clean_request(**overrides: object) -> QuoteRequest:
    """The seeded best case: forty pumps for Nordwind, delivered, discounted."""
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "quote_id": "QTE_0001",
        "quote_number": "Q-2026-0001",
        "customer_id": "CUS_0001",
        "lines": (
            QuoteLineRequest(
                product_id="PRD_0001",
                sku="PMP-A-100",
                description="Centrifugal pump PMP-A-100",
                quantity=40,
            ),
        ),
        "pricing_as_of": AS_OF,
        "stock_as_of": STOCK_STAMP,
        "delivery": DeliveryQuestion(
            destination="Hamburg",
            destination_country="DE",
            as_of=NOW,
            requested_date=date(2026, 10, 8),
        ),
        "discount": DiscountQuestion(order_value=Decimal("46000.00")),
    }
    values.update(overrides)
    return QuoteRequest(**values)  # type: ignore[arg-type]


def line_request(product_id: str, *, sku: str, quantity: int) -> QuoteLineRequest:
    """One request line for the seeded catalogue item named."""
    return QuoteLineRequest(
        product_id=product_id,
        sku=sku,
        description=f"Seeded catalogue item {sku}",
        quantity=quantity,
    )


# ---------------------------------------------------------------------------
# The run over the demo data
# ---------------------------------------------------------------------------


class TestTheSeededRun:
    """What one run decides, from data a reviewer can check by hand."""

    def test_a_seeded_request_is_priced_discounted_and_delivered(
        self, reader: BusinessReader
    ) -> None:
        """40 x 1,150.00 with Nordwind's 3% contract rule, arriving on the asked date."""
        result = run_quote(clean_request(), reader)

        assert result.status is QuoteRunStatus.COMPLETED
        quote = result.quote
        assert quote is not None
        assert quote.customer_id == "CUS_0001"
        assert quote.currency == "EUR"
        assert quote.status is QuoteStatus.DRAFT
        assert quote.subtotal == Decimal("46000.00")
        assert quote.discount is not None
        assert quote.discount.rule_id == "DSC_0003"
        assert quote.discount_amount == Decimal("1380.00")
        assert quote.total == Decimal("44620.00")
        assert quote.delivery is not None
        assert quote.delivery.promise.feasibility is DeliveryFeasibility.FEASIBLE
        assert quote.delivery.promise.requested_date == date(2026, 10, 8)
        assert quote.delivery.promise.earliest_delivery_date == date(2026, 10, 8)
        assert result.ledger is not None
        assert result.ledger.reasons == ()

    def test_the_gate_decides_over_the_runs_own_artefacts(self, reader: BusinessReader) -> None:
        """The decision is the accepted one, bound to the quote it decided about."""
        result = run_quote(clean_request(), reader)

        decision = result.decision
        quote = result.quote
        assert decision is not None
        assert quote is not None
        assert decision.allowed is True
        assert decision.reason_codes == ()
        assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
        assert decision.gate_version == GATE_VERSION
        assert decision.quote_id == "QTE_0001"
        assert decision.run_id == RUN_ID
        assert decision.quote_inputs_sha256 == quote.inputs_sha256
        assert decision.evidence_sha256
        # Eligible for a future human review - and still nothing but a draft,
        # with the V1 policy requiring the human approval the gate names.
        assert decision.eligible_for_human_review is True
        assert decision.requires_human_approval is True
        assert quote.status is QuoteStatus.DRAFT

    def test_the_run_hands_back_the_accepted_artefacts(self, reader: BusinessReader) -> None:
        """The composition wraps the phases' own value objects, not look-alikes."""
        result = run_quote(clean_request(), reader)

        assert isinstance(result.customer, CustomerRecord)
        assert result.customer.credit_hold is False
        assert type(result.calculation) is QuoteCalculation
        assert type(result.ledger) is QuoteBlockedLedger
        assert type(result.decision) is PolicyGateDecision

    def test_the_same_request_over_the_same_data_is_the_same_run(
        self, reader: BusinessReader
    ) -> None:
        """Determinism, given the business data: everything matches, fingerprints too."""
        first = run_quote(clean_request(), reader)
        second = run_quote(clean_request(), reader)

        assert first == second
        assert first.calculation == second.calculation
        assert first.decision == second.decision
        first_decision, second_decision = first.decision, second.decision
        assert first_decision is not None
        assert second_decision is not None
        assert first_decision.evidence_sha256 == second_decision.evidence_sha256
        assert first_decision.quote_inputs_sha256 == second_decision.quote_inputs_sha256

    def test_two_lines_are_priced_by_the_same_rules_without_a_question(
        self, reader: BusinessReader
    ) -> None:
        """No delivery or discount question: the lines still price, one by one."""
        request = clean_request(
            quote_id="QTE_0002",
            quote_number="Q-2026-0002",
            lines=(
                line_request("PRD_0001", sku="PMP-A-100", quantity=2),
                line_request("PRD_0003", sku="PMP-B-150", quantity=1),
            ),
            delivery=None,
            discount=None,
        )
        result = run_quote(request, reader)

        calculation = result.calculation
        assert calculation is not None
        quote = calculation.quote
        assert [line.ordinal for line in quote.lines] == [1, 2]
        assert [line.unit_price for line in quote.lines] == [
            Decimal("1150.0000"),
            Decimal("1480.0000"),
        ]
        assert quote.subtotal == Decimal("3780.00")
        assert quote.discount is None
        assert quote.delivery is None
        assert result.ledger is not None
        assert result.ledger.reasons == ()

    def test_no_delivery_question_means_no_promise_is_invented(
        self, reader: BusinessReader
    ) -> None:
        """Nobody asked when it can arrive, so no delivery fact is created."""
        result = run_quote(clean_request(delivery=None), reader)

        quote = result.quote
        assert quote is not None
        assert quote.delivery is None
        assert result.decision is not None
        assert result.decision.eligible_for_human_review is True


# ---------------------------------------------------------------------------
# The run fails closed
# ---------------------------------------------------------------------------


class TestTheRunFailsClosed:
    """Nothing is guessed, and a run that cannot decide says so instead."""

    def test_an_unresolved_customer_stops_the_run(self, reader: BusinessReader) -> None:
        """No customer record, no quote - and no decision to misread as approval."""
        request = clean_request(customer_id="CUS_9999")
        result = run_quote(request, reader)

        assert result.status is QuoteRunStatus.CUSTOMER_NOT_FOUND
        assert result.customer is None
        assert result.calculation is None
        assert result.ledger is None
        assert result.decision is None
        assert result.quote is None
        assert result.eligible_for_human_review is False
        assert "CUS_9999" in result.detail

    def test_an_unresolved_customer_leaves_the_database_alone(
        self, reader: BusinessReader, runs: Session
    ) -> None:
        """A refused run writes nothing, because nothing was decided to write."""
        run_quote(clean_request(customer_id="CUS_9999"), reader)

        assert runs.scalar(select(func.count()).select_from(QuoteRow)) == 0
        assert runs.scalar(select(func.count()).select_from(QuoteBlockedReasonRow)) == 0

    def test_a_credit_hold_is_a_blocking_fact_and_never_eligible(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0007`` is the demo data's credit hold (failure case F11)."""
        result = run_quote(
            clean_request(customer_id="CUS_0007", discount=None),
            reader,
        )

        assert result.customer is not None
        assert result.customer.credit_hold is True
        assert result.ledger is not None
        assert [reason.code for reason in result.ledger.reasons] == [BlockedReasonCode.CREDIT_HOLD]
        decision = result.decision
        assert decision is not None
        assert decision.allowed is False
        assert decision.eligible_for_human_review is False
        assert decision.reason_codes == (BlockedReasonCode.CREDIT_HOLD,)
        assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
        assert result.quote is not None
        assert result.quote.status is QuoteStatus.DRAFT

    def test_an_expired_price_refuses_the_line_and_blocks_the_quote(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0006`` is discontinued and its only price ended 2026-06-30."""
        request = clean_request(
            quote_id="QTE_0003",
            quote_number="Q-2026-0003",
            lines=(line_request("PRD_0006", sku="PMP-D-300", quantity=40),),
            delivery=None,
            discount=None,
        )
        result = run_quote(request, reader)

        calculation = result.calculation
        ledger = result.ledger
        decision = result.decision
        assert calculation is not None
        assert ledger is not None
        assert decision is not None
        (refusal,) = calculation.refusals
        assert refusal.status is PriceLookupStatus.EXPIRED
        (refused_line,) = calculation.quote.lines
        assert refused_line.blocked is True
        assert refused_line.price_status is PriceLookupStatus.EXPIRED
        assert refused_line.unit_price == Decimal("0")
        assert calculation.quote.subtotal == Decimal("0.00")
        assert calculation.complete is False
        assert [reason.code for reason in ledger.reasons] == [
            BlockedReasonCode.PRICE_MISSING,
            BlockedReasonCode.STOCK_INSUFFICIENT,
        ]
        assert ledger.reasons[0].line_ordinal == 1
        assert decision.eligible_for_human_review is False
        assert decision.reason_codes == (
            BlockedReasonCode.PRICE_MISSING,
            BlockedReasonCode.STOCK_INSUFFICIENT,
        )

    def test_a_price_that_belongs_to_another_customer_is_unavailable(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0012``'s only price entry is CUS_0004's contract, never Nordwind's."""
        request = clean_request(
            quote_id="QTE_0004",
            quote_number="Q-2026-0004",
            lines=(line_request("PRD_0012", sku="VLV-X-200", quantity=5),),
            delivery=None,
            discount=None,
        )
        result = run_quote(request, reader)

        calculation = result.calculation
        ledger = result.ledger
        assert calculation is not None
        assert ledger is not None
        (refusal,) = calculation.refusals
        assert refusal.reason.value == "NO_MATCHING_SCOPE"
        assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.PRICE_MISSING]

    def test_a_delivery_question_on_uncovered_stock_is_not_answered(
        self, reader: BusinessReader
    ) -> None:
        """Phase 1F promises only what stock covers; the short stock still blocks."""
        request = clean_request(
            quote_id="QTE_0005",
            quote_number="Q-2026-0005",
            lines=(line_request("PRD_0005", sku="PMP-C-050", quantity=50),),
            discount=None,
        )
        result = run_quote(request, reader)

        ledger = result.ledger
        decision = result.decision
        assert result.quote is not None
        assert result.quote.delivery is None
        assert ledger is not None
        assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.STOCK_INSUFFICIENT]
        assert decision is not None
        assert decision.eligible_for_human_review is False

    def test_asking_the_run_to_skip_the_human_gate_is_unsupported(
        self, reader: BusinessReader
    ) -> None:
        """R8: the gate cannot certify a state its rules do not implement."""
        result = run_quote(clean_request(require_human_approval=False), reader)

        decision = result.decision
        assert decision is not None
        assert decision.eligible_for_human_review is False
        assert decision.evidence_status is PolicyEvidenceStatus.UNSUPPORTED
        assert decision.allowed is True
        assert decision.requires_human_approval is False
        assert result.quote is not None
        assert result.quote.status is QuoteStatus.DRAFT


# ---------------------------------------------------------------------------
# Storing what a run decided changes nothing about it
# ---------------------------------------------------------------------------


class TestStoringTheRunsOutput:
    """The writer stores the artefacts; the verdict stays unstored and DRAFT."""

    def test_the_run_stores_a_draft_and_never_a_decision(
        self, reader: BusinessReader, runs: Session, writer: QuoteWriter
    ) -> None:
        """Eligibility is not an approval: the row stays DRAFT and the columns NULL."""
        result = run_quote(clean_request(), reader)
        assert result.calculation is not None
        assert result.ledger is not None

        stored = writer.persist(result.calculation, result.ledger)
        row = stored_header(runs)

        assert stored.duplicate is False
        assert row.status == QuoteStatus.DRAFT.value
        assert row.policy_allowed is None
        assert row.policy_reason_codes_json is None
        assert row.approved_at is None
        assert result.decision is not None
        assert result.decision.eligible_for_human_review is True

    def test_a_blocked_run_stores_its_ledger_and_still_no_decision(
        self, reader: BusinessReader, runs: Session, writer: QuoteWriter
    ) -> None:
        """The reason is stored; the verdict about it is not."""
        result = run_quote(clean_request(customer_id="CUS_0007", discount=None), reader)
        assert result.calculation is not None
        assert result.ledger is not None

        writer.persist(result.calculation, result.ledger)
        row = stored_header(runs)
        reasons = stored_reasons(runs)

        assert [reason.code for reason in reasons] == ["CREDIT_HOLD"]
        assert row.status == QuoteStatus.DRAFT.value
        assert row.policy_allowed is None
        assert row.policy_reason_codes_json is None
        assert row.approved_at is None
        assert result.decision is not None
        assert result.decision.eligible_for_human_review is False

    def test_storing_a_run_sends_nothing_and_transitions_nothing(
        self, reader: BusinessReader, runs: Session, writer: QuoteWriter
    ) -> None:
        """No outbound message, no run event and no workflow state change."""
        result = run_quote(clean_request(), reader)
        assert result.calculation is not None
        assert result.ledger is not None

        writer.persist(result.calculation, result.ledger)

        assert runs.scalar(select(func.count()).select_from(OutboundMessageRow)) == 0
        assert runs.scalar(select(func.count()).select_from(RunEventRow)) == 0
        runs.rollback()
        row = runs.get(RunRow, RUN_ID)
        assert row is not None
        assert row.state.value == "RECEIVED"


def stored_header(session: Session, quote_id: str = "QTE_0001") -> QuoteRow:
    """The stored quote header, read in a fresh transaction so nothing is cached."""
    session.rollback()
    row = session.get(QuoteRow, quote_id)
    assert row is not None
    return row


def stored_reasons(session: Session, quote_id: str = "QTE_0001") -> list[QuoteBlockedReasonRow]:
    """The stored ledger, in the order it was written."""
    session.rollback()
    return list(
        session.scalars(
            select(QuoteBlockedReasonRow)
            .where(QuoteBlockedReasonRow.quote_id == quote_id)
            .order_by(QuoteBlockedReasonRow.seq)
        )
    )
