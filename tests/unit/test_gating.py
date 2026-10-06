"""Tests for the deterministic quote gate (architecture §4.2 Group B).

The gate is a pure function, so its behaviour is fully enumerable. The case that
matters most for the portfolio claim: a clean quote is *not* cleared for
autonomous send in V1 - it is marked as requiring human approval.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from rfq_agent.domain.delivery import DeliveryFeasibility, DeliveryPromise
from rfq_agent.domain.gating import PolicyGateInput, evaluate_quote_gate
from rfq_agent.domain.policy import (
    BlockedReason,
    BlockedReasonCode,
    PolicyDecision,
    PolicyFlag,
)
from tests.conftest import make_quote


def make_delivery(**overrides: object) -> DeliveryPromise:
    payload: dict[str, object] = {
        "destination": "Warsaw",
        "earliest_delivery_date": date(2026, 10, 9),
        "requested_date": date(2026, 10, 9),
        "feasibility": DeliveryFeasibility.FEASIBLE,
        "rationale": "25 on hand in WAW, 1 transit day",
    }
    payload.update(overrides)
    return DeliveryPromise.model_validate(payload)


def make_gate(**overrides: object) -> PolicyGateInput:
    payload: dict[str, object] = {"quote": make_quote()}
    payload.update(overrides)
    return PolicyGateInput.model_validate(payload)


class TestCleanQuote:
    def test_clean_quote_is_allowed_but_needs_a_human(self) -> None:
        decision = evaluate_quote_gate(make_gate())
        assert decision.allowed is True
        assert decision.requires_human_approval is True
        assert decision.reason_codes == ()

    def test_policy_never_authorises_an_autonomous_send_in_v1(self) -> None:
        # Even with the (unused in V1) auto-approve flag off, "allowed" only
        # means "no business blocker"; routing to a human is the workflow's job.
        decision = evaluate_quote_gate(make_gate(require_human_approval=False))
        assert decision.allowed is True
        assert decision.requires_human_approval is False


class TestBlockingReasons:
    def test_a_blocking_reason_refuses_the_quote(self) -> None:
        gate = make_gate(
            blocked_reasons=(
                BlockedReason(
                    code=BlockedReasonCode.PRICE_MISSING,
                    message="no price book entry for Y-500 at this tier",
                    line_ordinal=2,
                ),
            )
        )
        decision = evaluate_quote_gate(gate)
        assert decision.allowed is False
        assert decision.reason_codes == (BlockedReasonCode.PRICE_MISSING,)
        assert "no price book entry" in decision.explanation

    def test_credit_hold_is_added_automatically(self) -> None:
        decision = evaluate_quote_gate(make_gate(customer_on_credit_hold=True))
        assert BlockedReasonCode.CREDIT_HOLD in decision.reason_codes

    def test_infeasible_delivery_blocks(self) -> None:
        delivery = make_delivery(
            requested_date=date(2026, 10, 9),
            earliest_delivery_date=date(2026, 10, 16),
            feasibility=DeliveryFeasibility.INFEASIBLE,
            rationale="earliest delivery 2026-10-16 via WAW-DHL",
        )
        decision = evaluate_quote_gate(make_gate(delivery=delivery))
        assert decision.allowed is False
        assert BlockedReasonCode.DELIVERY_INFEASIBLE in decision.reason_codes

    def test_unknown_delivery_blocks_rather_than_assuming(self) -> None:
        delivery = make_delivery(
            requested_date=None,
            earliest_delivery_date=None,
            feasibility=DeliveryFeasibility.UNKNOWN,
            rationale="no carrier service for this destination",
        )
        assert evaluate_quote_gate(make_gate(delivery=delivery)).allowed is False

    def test_non_blocking_flags_do_not_refuse_the_quote(self) -> None:
        gate = make_gate(flags=(PolicyFlag.PRICE_STALE, PolicyFlag.DATE_INFERRED))
        decision = evaluate_quote_gate(gate)
        assert decision.allowed is True
        assert decision.requires_human_approval is True

    def test_duplicate_reason_codes_are_rejected(self) -> None:
        reason = BlockedReason(code=BlockedReasonCode.MISSING_QTY, message="quantity not stated")
        with pytest.raises(ValidationError, match="duplicate codes"):
            make_gate(blocked_reasons=(reason, reason))

    def test_duplicate_flags_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="flags must not contain duplicates"):
            make_gate(flags=(PolicyFlag.PRICE_STALE, PolicyFlag.PRICE_STALE))


class TestDecisionSchema:
    def test_refusal_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="at least one reason_code"):
            PolicyDecision(allowed=False)

    def test_approval_must_not_carry_reasons(self) -> None:
        with pytest.raises(ValidationError, match="must be empty when allowed"):
            PolicyDecision(allowed=True, reason_codes=(BlockedReasonCode.PRICE_MISSING,))

    def test_requires_human_approval_only_on_a_clean_quote(self) -> None:
        with pytest.raises(ValidationError, match="otherwise-clean quote"):
            PolicyDecision(
                allowed=False,
                reason_codes=(BlockedReasonCode.PRICE_MISSING,),
                requires_human_approval=True,
            )
