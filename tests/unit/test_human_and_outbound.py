"""Tests for human-action and outbound schemas (architecture §3.6, §8.3).

The property under test: nothing reaches a customer without an approval action,
and a failed canary scan cannot be talked past.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rfq_agent.domain.human import (
    HUMAN_APPROVAL_ACTIONS,
    REASON_REQUIRED_ACTIONS,
    HumanAction,
    HumanActionKind,
)
from rfq_agent.domain.outbound import (
    CanaryFinding,
    CanaryFindingKind,
    CanaryScanResult,
    OutboundChannel,
    OutboundStatus,
    RenderedOutbound,
    TemplateSlot,
)
from rfq_agent.domain.workflow import ReasonCode
from tests.conftest import utc


def make_action(**overrides: object) -> HumanAction:
    payload: dict[str, object] = {
        "run_id": "RUN-0001",
        "rfq_id": "RFQ-0001",
        "actor": "operator-1",
        "action": HumanActionKind.APPROVE,
        "occurred_at": utc(),
        "idempotency_key": "a" * 40,
    }
    payload.update(overrides)
    return HumanAction.model_validate(payload)


class TestHumanAction:
    def test_approve_is_the_only_outbound_unlock(self) -> None:
        assert frozenset({HumanActionKind.APPROVE}) == HUMAN_APPROVAL_ACTIONS
        assert make_action().unlocks_outbound is True
        assert make_action(action=HumanActionKind.RERUN).unlocks_outbound is False

    def test_reject_requires_a_reason_code(self) -> None:
        with pytest.raises(ValidationError, match="reason_code is required"):
            make_action(action=HumanActionKind.REJECT)

    def test_reject_with_a_reason_is_valid(self) -> None:
        action = make_action(
            action=HumanActionKind.REJECT,
            reason_code=ReasonCode.HUMAN_REJECTED,
            note="customer asked us to hold",
        )
        assert action.reason_code is ReasonCode.HUMAN_REJECTED

    def test_edit_requires_a_before_and_after_diff(self) -> None:
        with pytest.raises(ValidationError, match="before state"):
            make_action(action=HumanActionKind.EDIT)
        with pytest.raises(ValidationError, match="after state"):
            make_action(action=HumanActionKind.EDIT, before={"quantity": 40})

    def test_valid_edit_carries_its_diff(self) -> None:
        action = make_action(
            action=HumanActionKind.EDIT,
            before={"quantity": 40},
            after={"quantity": 50},
        )
        assert action.after == {"quantity": 50}

    def test_only_edit_carries_a_diff(self) -> None:
        with pytest.raises(ValidationError, match="only EDIT actions"):
            make_action(action=HumanActionKind.APPROVE, before={"x": 1})

    def test_add_note_requires_a_note(self) -> None:
        with pytest.raises(ValidationError, match="requires a note"):
            make_action(action=HumanActionKind.ADD_NOTE)

    def test_idempotency_key_must_be_long_enough(self) -> None:
        with pytest.raises(ValidationError):
            make_action(idempotency_key="short")

    def test_reason_required_set(self) -> None:
        assert frozenset({HumanActionKind.REJECT}) == REASON_REQUIRED_ACTIONS


def make_scan(**overrides: object) -> CanaryScanResult:
    payload: dict[str, object] = {"passed": True, "scanned_at": utc()}
    payload.update(overrides)
    return CanaryScanResult.model_validate(payload)


def make_outbound(**overrides: object) -> RenderedOutbound:
    payload: dict[str, object] = {
        "quote_id": "QUOTE-0001",
        "customer_id": "CUST-0001",
        "template_id": "quote-response",
        "template_version": "1.0.0",
        "slots_used": (TemplateSlot.CUSTOMER_NAME, TemplateSlot.TOTAL),
        "rendered_text": "Dear customer, your quote Q-2026-000123 totals EUR 626.00.",
        "scan": make_scan(),
    }
    payload.update(overrides)
    return RenderedOutbound.model_validate(payload)


class TestCanaryScan:
    def test_passed_scan_has_no_findings(self) -> None:
        assert make_scan().passed is True

    def test_passed_with_findings_is_inconsistent(self) -> None:
        with pytest.raises(ValidationError, match="passed must be false"):
            make_scan(
                passed=True,
                findings=(
                    CanaryFinding(
                        kind=CanaryFindingKind.CANARY_LEAK, detail="canary matched", offset=10
                    ),
                ),
            )

    def test_failed_scan_requires_findings(self) -> None:
        with pytest.raises(ValidationError, match="at least one finding"):
            make_scan(passed=False)

    def test_failed_scan_with_findings_is_valid(self) -> None:
        scan = make_scan(
            passed=False,
            findings=(
                CanaryFinding(
                    kind=CanaryFindingKind.FOREIGN_CUSTOMER_REFERENCE,
                    detail="reference to a customer other than the bound one",
                ),
            ),
        )
        assert scan.findings[0].kind is CanaryFindingKind.FOREIGN_CUSTOMER_REFERENCE


class TestRenderedOutbound:
    def test_draft_outbound_is_not_sent(self) -> None:
        outbound = make_outbound()
        assert outbound.status is OutboundStatus.DRAFTED
        assert outbound.sent_at is None
        assert outbound.channel is OutboundChannel.SIMULATED

    def test_failed_scan_must_leave_the_outbound_blocked(self) -> None:
        blocked_scan = make_scan(
            passed=False,
            findings=(CanaryFinding(kind=CanaryFindingKind.CANARY_LEAK, detail="leak"),),
        )
        with pytest.raises(ValidationError, match="must leave the outbound BLOCKED"):
            make_outbound(scan=blocked_scan)

    def test_blocked_outbound_with_failed_scan_is_valid(self) -> None:
        blocked_scan = make_scan(
            passed=False,
            findings=(CanaryFinding(kind=CanaryFindingKind.CANARY_LEAK, detail="leak"),),
        )
        outbound = make_outbound(scan=blocked_scan, status=OutboundStatus.BLOCKED)
        assert outbound.status is OutboundStatus.BLOCKED

    def test_sent_requires_a_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="requires sent_at"):
            make_outbound(status=OutboundStatus.SENT)

    def test_timestamp_requires_sent_status(self) -> None:
        with pytest.raises(ValidationError, match="sent_at requires status SENT"):
            make_outbound(sent_at=utc())

    def test_only_the_simulated_channel_exists_in_v1(self) -> None:
        assert list(OutboundChannel) == [OutboundChannel.SIMULATED]

    def test_operator_note_is_an_explicit_slot(self) -> None:
        assert TemplateSlot.OPERATOR_NOTE.value == "operator_note"
        assert TemplateSlot.TOTAL.value == "total"

    def test_empty_rendered_text_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_outbound(rendered_text="  ")
