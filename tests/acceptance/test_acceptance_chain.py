"""The acceptance slice end to end: composed in-process, then checked in the database.

The assertions here are about rows, not about the report: a DRAFT with the
expected money and no approval, a blocked DRAFT with its persisted evidence, a
claim refusal that stored no quotation, and a replay that stored nothing new.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from rfq_agent.acceptance import (
    AcceptanceOutcome,
    AcceptanceReason,
    accept_rfq,
)
from rfq_agent.domain.policy import BlockedReasonCode
from rfq_agent.domain.quote import QuoteStatus
from rfq_agent.domain.resolution import CustomerMatchStatus, ResolutionMatchStatus
from rfq_agent.domain.workflow import RunState
from rfq_agent.persistence.models import (
    HumanActionRow,
    IntakeEventRow,
    OutboundMessageRow,
    PriceEntryRow,
    QuoteBlockedReasonRow,
    QuoteLineRow,
    QuoteRow,
    RfqRow,
    RunEventRow,
    RunRow,
)
from tests.acceptance.conftest import EXAMPLES, load_example, make_declaration


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _quote(session, quote_id: str) -> QuoteRow:
    session.expire_all()
    row = session.get(QuoteRow, quote_id)
    assert row is not None
    return row


def test_case_a_clean_rfq_persists_a_draft_at_the_expected_total(seeded, session) -> None:
    report = accept_rfq(make_declaration(), seeded)

    assert report.outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert report.reason is None
    assert report.created is True
    assert report.customer_status is CustomerMatchStatus.EXACT
    assert report.line_statuses == (ResolutionMatchStatus.RESOLVED,)
    assert report.quote_id == "QTE_TEST_0001"
    assert report.quote_number == "Q-2026-9001"
    assert report.quote_status is QuoteStatus.DRAFT
    assert report.total == Decimal("28750.00")
    assert report.currency == "EUR"
    assert report.blocked_codes == ()
    assert report.eligible_for_review is True
    assert report.approval_required is True

    quote = _quote(session, "QTE_TEST_0001")
    assert quote.run_id == "RUN_TEST_0001"
    assert quote.rfq_id == "RFQ_TEST_0001"
    assert quote.revision == 1
    assert quote.status is QuoteStatus.DRAFT
    assert quote.total == Decimal("28750.00")
    assert quote.subtotal == Decimal("28750.00")
    assert quote.discount_amount == Decimal("0.00")
    assert quote.approved_at is None
    assert quote.policy_allowed is None
    assert quote.policy_reason_codes_json is None
    assert len(quote.inputs_sha256) == 64

    line = session.scalar(select(QuoteLineRow).where(QuoteLineRow.quote_id == "QTE_TEST_0001"))
    assert line is not None
    assert line.line_id == "QTE_TEST_0001-L01"
    assert line.ordinal == 1
    assert line.product_id == "PRD_0001"
    assert line.quantity == 25
    assert line.unit_price == Decimal("1150.0000")
    assert line.price_entry_id == "PE_0021"
    assert line.line_extension == Decimal("28750.00")
    assert line.blocked is False

    assert _count(session, QuoteBlockedReasonRow) == 0
    assert _count(session, HumanActionRow) == 0
    assert _count(session, OutboundMessageRow) == 0
    assert _count(session, RunEventRow) == 0
    assert _count(session, IntakeEventRow) == 1

    run = session.get(RunRow, "RUN_TEST_0001")
    assert run is not None
    assert run.state is RunState.RECEIVED
    assert run.outcome is None
    assert session.get(RfqRow, "RFQ_TEST_0001").status.value == "OPEN"


def test_case_b_stock_shortage_persists_a_blocked_draft(seeded, session) -> None:
    report = accept_rfq(make_declaration(number=2, quantity=200), seeded)

    assert report.outcome is AcceptanceOutcome.DRAFT_BLOCKED
    assert report.total == Decimal("230000.00")
    assert report.blocked_codes == ("STOCK_INSUFFICIENT",)
    assert report.eligible_for_review is False

    quote = _quote(session, "QTE_TEST_0002")
    assert quote.status is QuoteStatus.DRAFT
    assert quote.total == Decimal("230000.00")

    reason = session.scalar(
        select(QuoteBlockedReasonRow).where(QuoteBlockedReasonRow.quote_id == "QTE_TEST_0002")
    )
    assert reason is not None
    assert reason.seq == 1
    assert reason.code is BlockedReasonCode.STOCK_INSUFFICIENT
    assert reason.line_ordinal == 1
    assert reason.resolvable_by_human is True
    assert "PARTIAL" in reason.message

    line = session.scalar(select(QuoteLineRow).where(QuoteLineRow.quote_id == "QTE_TEST_0002"))
    assert line.quantity == 200
    assert line.stock_status.value == "PARTIAL"
    assert line.blocked is False


def test_case_c_tampered_claim_is_refused_and_no_quote_is_stored(seeded, session) -> None:
    report = accept_rfq(make_declaration(number=3, claim_quantity=26), seeded)

    assert report.outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE
    assert report.reason is AcceptanceReason.CLAIM_VERIFICATION_REFUSED
    assert set(report.details) == {"EVIDENCE_NOT_VERBATIM", "QUANTITY_UNSUPPORTED"}
    assert report.quote_id is None
    assert report.total is None

    assert _count(session, QuoteRow) == 0
    assert _count(session, QuoteLineRow) == 0
    assert _count(session, QuoteBlockedReasonRow) == 0

    # The request did arrive, and intake history says so; only the quotation
    # is absent. Refusals are reported, not persisted - there is no state for one.
    assert session.get(RfqRow, "RFQ_TEST_0003") is not None
    assert session.get(RunRow, "RUN_TEST_0003") is not None
    assert _count(session, IntakeEventRow) == 1


def test_case_d_identical_replay_reports_a_duplicate_and_writes_nothing(seeded, session) -> None:
    first = accept_rfq(make_declaration(), seeded)
    second = accept_rfq(make_declaration(), seeded)

    assert first.outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert second.outcome is AcceptanceOutcome.DUPLICATE
    assert second.created is False
    assert second.total == Decimal("28750.00")
    assert second.quote_id == "QTE_TEST_0001"

    session.expire_all()
    assert _count(session, RfqRow) == 1
    assert _count(session, RunRow) == 1
    assert _count(session, IntakeEventRow) == 1
    assert _count(session, QuoteRow) == 1
    assert _count(session, QuoteLineRow) == 1
    assert _count(session, QuoteBlockedReasonRow) == 0


def test_an_identical_replay_of_a_refusal_stays_a_refusal(seeded, session) -> None:
    declaration = make_declaration(number=3, claim_quantity=26)
    first = accept_rfq(declaration, seeded)
    second = accept_rfq(declaration, seeded)

    assert first.outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE
    assert second.outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE
    assert second.created is False
    assert _count(session, QuoteRow) == 0
    assert _count(session, IntakeEventRow) == 1


def test_different_content_under_a_stored_identity_is_a_conflict(seeded, session) -> None:
    assert accept_rfq(make_declaration(), seeded).outcome is (
        AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    )

    changed = make_declaration(body="Hello,\nplease quote 26 pcs of 100-ABC.")
    report = accept_rfq(changed, seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    assert report.reason is AcceptanceReason.IDENTITY_CONFLICT
    assert _count(session, QuoteRow) == 1
    stored = session.get(RfqRow, "RFQ_TEST_0001")
    session.refresh(stored)
    assert "26 pcs" not in stored.body_text


def test_reusing_an_idempotency_key_for_another_rfq_is_a_conflict(seeded) -> None:
    first = accept_rfq(make_declaration(), seeded)
    assert first.outcome is AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW

    second = accept_rfq(
        make_declaration(
            number=2,
            quantity=200,
            idempotency_key=make_declaration().intake.idempotency_key,
        ),
        seeded,
    )

    assert second.outcome is AcceptanceOutcome.CONFLICT
    assert second.reason is AcceptanceReason.IDENTITY_CONFLICT


def test_a_changed_price_is_a_conflict_instead_of_a_second_revision(seeded, session) -> None:
    assert accept_rfq(make_declaration(), seeded).outcome is (
        AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    )
    with seeded.session() as work:
        work.execute(
            update(PriceEntryRow)
            .where(PriceEntryRow.product_id == "PRD_0001")
            .values(unit_price=Decimal("1200.0000"))
        )

    report = accept_rfq(make_declaration(), seeded)

    assert report.outcome is AcceptanceOutcome.CONFLICT
    assert report.reason is AcceptanceReason.STORED_STATE_CONFLICT
    assert _quote(session, "QTE_TEST_0001").total == Decimal("28750.00")
    assert _count(session, QuoteRow) == 1


def test_an_unproven_sender_requires_a_human_and_stores_no_quote(seeded, session) -> None:
    report = accept_rfq(make_declaration(sender_email="unproven@example.invalid"), seeded)

    assert report.outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE
    assert report.reason is AcceptanceReason.CUSTOMER_NOT_EXACT
    assert report.customer_status is CustomerMatchStatus.UNBOUND
    assert report.quote_id is None
    assert _count(session, QuoteRow) == 0


def test_the_line_identifiers_must_pair_with_the_claims() -> None:
    declaration = make_declaration()
    payload = declaration.model_dump(mode="json")
    payload["line_item_ids"] = ["LI_TEST_0001", "LI_TEST_0002"]

    with pytest.raises(ValueError, match="pair one-to-one"):
        type(declaration).model_validate(payload)


def test_the_worked_examples_parse_as_declarations() -> None:
    for name in ("case-a-clean.json", "case-b-stock.json", "case-c-tampered.json"):
        declaration = load_example(name)
        assert declaration.intake.source_channel.value == "EMAIL"
        assert (EXAMPLES / name).is_file()
