"""The Phase 1K gate decision: one question, answered from evidence.

The gate asks whether a quote is *eligible for a future human review step* - and
that is all it says. These tests are the phase's contract, so they are written as
three groups:

* what the decision asserts (R1) - the accepted Phase 1I code set, in the
  contract's own order, with the ledger's own wording;
* how it fails closed (R2-R8) - every case where a fact is missing, denied,
  unsupported or contradictory ends in ``eligible_for_human_review is False``
  with the evidence status that names the kind of defect;
* what it never becomes - no approval, no sendability, no workflow state, and
  identical inputs always produce an identical decision.

The builders live here rather than in ``conftest`` because each one exists to
make exactly one rule fire; a shared builder would hide which fact did it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.delivery import (
    DeliveryAssessment,
    DeliveryFeasibility,
    DeliveryPromise,
)
from rfq_agent.domain.gating import (
    PolicyGateInput,
    evaluate_quote_gate,
    project_blocked_ledger,
)
from rfq_agent.domain.policy import (
    GATE_VERSION,
    BlockedReason,
    BlockedReasonCode,
    DiscountApplication,
    DiscountScope,
    PolicyEvidenceStatus,
    PolicyFlag,
    PolicyGateDecision,
)
from rfq_agent.domain.pricing import PriceEntry, select_price
from rfq_agent.domain.quote import (
    Quote,
    QuoteCalculation,
    QuoteLineInput,
    QuoteStatus,
    calculate_quote,
)
from rfq_agent.domain.stock import StockStatus
from tests.conftest import (
    make_clean_quote,
    make_quote,
    make_quote_line,
    make_refused_quote_line,
    utc,
)

AS_OF = date(2026, 10, 6)
RUN_ID = "RUN_0001"


# ---------------------------------------------------------------------------
# Builders: each one establishes exactly one fact, and nothing else
# ---------------------------------------------------------------------------


def clean_quote() -> Quote:
    """A quote whose lines priced, whose stock is stated, with no discount."""
    return make_clean_quote()


def refused_quote() -> Quote:
    """A quote with one line the calculator refused: it proves ``PRICE_MISSING``."""
    return make_quote(lines=(make_refused_quote_line(stock_status=StockStatus.SUFFICIENT),))


def blocking_stock_quote() -> Quote:
    """A quote with one line the contract calls blocking stock."""
    return make_quote(lines=(make_quote_line(1, stock_status=StockStatus.PARTIAL),))


def over_limit_quote() -> Quote:
    """A quote carrying a discount rule that exceeds the delegated limit."""
    line = clean_quote().lines[0]
    discount = DiscountApplication(
        rule_id="DSC_0002",
        scope=DiscountScope.GLOBAL,
        percent=Decimal("5.00"),
        requires_approval=True,
    )
    amount = (line.line_extension * discount.percent / Decimal("100")).quantize(Decimal("0.01"))
    return make_clean_quote(
        lines=(line,),
        discount=discount,
        discount_amount=amount,
        total=line.line_extension - amount,
    )


def blocking_delivery() -> DeliveryPromise:
    """A promise the contract calls blocking: INFEASIBLE."""
    return DeliveryPromise(
        destination="Hamburg",
        origin_location="WAW",
        carrier_service_code="DHL-EXP",
        transit_days=1,
        earliest_ship_date=AS_OF,
        earliest_delivery_date=date(2026, 10, 16),
        requested_date=date(2026, 10, 9),
        feasibility=DeliveryFeasibility.INFEASIBLE,
        rationale="earliest delivery 2026-10-16 via WAW DHL-EXP",
    )


def feasible_delivery() -> DeliveryPromise:
    """A promise the contract calls feasible: the request can be met."""
    return DeliveryPromise(
        destination="Hamburg",
        origin_location="WAW",
        carrier_service_code="DHL-EXP",
        transit_days=1,
        earliest_ship_date=AS_OF,
        earliest_delivery_date=date(2026, 10, 9),
        requested_date=date(2026, 10, 9),
        feasibility=DeliveryFeasibility.FEASIBLE,
        rationale="25 on hand in WAW, 1 transit day",
    )


def infeasible_delivery_quote() -> Quote:
    """A quote whose own delivery assessment the contract calls blocking."""
    return make_clean_quote(delivery=DeliveryAssessment(promise=blocking_delivery()))


def feasible_delivery_quote() -> Quote:
    """A quote whose own delivery assessment the contract calls feasible."""
    return make_clean_quote(delivery=DeliveryAssessment(promise=feasible_delivery()))


def reason(code: BlockedReasonCode, message: str = "reported by the ledger") -> BlockedReason:
    """One ledger entry, in the shape the projection produces."""
    return BlockedReason(code=code, message=message)


def gate_for(code: BlockedReasonCode) -> PolicyGateInput:
    """A gate input whose supplied facts assert exactly this code, and no other.

    Every code gets a corroborated shape, because a claim the quote denies is a
    contradiction rather than an asserted code (that direction is tested
    separately). Codes no fact can witness are taken on the ledger's word, which
    is the accepted Phase 1I behaviour.
    """
    match code:
        case BlockedReasonCode.PRICE_MISSING:
            return PolicyGateInput(
                quote=refused_quote(),
                blocked_reasons=(reason(code),),
                customer_on_credit_hold=False,
            )
        case BlockedReasonCode.STOCK_INSUFFICIENT:
            return PolicyGateInput(
                quote=blocking_stock_quote(),
                blocked_reasons=(reason(code),),
                customer_on_credit_hold=False,
            )
        case BlockedReasonCode.DELIVERY_INFEASIBLE:
            return PolicyGateInput(
                quote=infeasible_delivery_quote(),
                blocked_reasons=(reason(code),),
                customer_on_credit_hold=False,
            )
        case BlockedReasonCode.DISCOUNT_OVER_POLICY:
            return PolicyGateInput(
                quote=over_limit_quote(),
                blocked_reasons=(reason(code),),
                customer_on_credit_hold=False,
            )
        case BlockedReasonCode.CREDIT_HOLD:
            return PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=True)
        case _:
            return PolicyGateInput(
                quote=clean_quote(),
                blocked_reasons=(reason(code),),
                customer_on_credit_hold=False,
            )


def ledger_for_delivery_quote() -> tuple[QuoteCalculation, Quote, BlockedReason]:
    """A real calculation, projected the way the pipeline will, with its ledger."""
    entry = PriceEntry(
        price_entry_id="PE_0001",
        product_id="PRD_0001",
        price_book_code="BK-EU-2026",
        customer_tier="STANDARD",
        min_qty=1,
        unit_price=Decimal("12.5000"),
        currency="EUR",
        effective_from=date(2026, 1, 1),
    )
    selection = select_price([entry], product_id="PRD_0001", quantity=5, as_of=AS_OF)
    calculation = calculate_quote(
        [
            QuoteLineInput(
                product_id="PRD_0001",
                sku="SKU-0001",
                description="Line 1",
                quantity=5,
                price=selection,
                stock_status=StockStatus.SUFFICIENT,
            )
        ],
        quote_id="QTE_0001",
        quote_number="Q-2026-0001",
        run_id=RUN_ID,
        customer_id="CUS_0001",
        currency="EUR",
        pricing_as_of=AS_OF,
        delivery=DeliveryAssessment(promise=blocking_delivery()),
    )
    ledger = project_blocked_ledger(calculation, run_id=RUN_ID, customer_on_credit_hold=False)
    return calculation, calculation.quote, ledger.reasons[0]


# ---------------------------------------------------------------------------
# R1 - what the decision asserts
# ---------------------------------------------------------------------------


def test_a_clean_quote_is_eligible_for_review_and_nothing_more() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=False)
    )

    assert decision.eligible_for_human_review is True
    assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
    assert decision.allowed is True
    assert decision.reason_codes == ()
    assert decision.requires_human_approval is True
    assert decision.gate_version == GATE_VERSION
    assert decision.requires_human_approval is True


def test_the_decision_carries_the_quotes_identity() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=False)
    )

    assert decision.quote_id == "QUOTE-0001"
    assert decision.run_id == "RUN-0001"


@pytest.mark.parametrize("code", list(BlockedReasonCode))
def test_every_contract_code_stops_eligibility(code: BlockedReasonCode) -> None:
    """The accepted Phase 1I vocabulary is closed: every member fails closed."""
    decision = evaluate_quote_gate(gate_for(code))

    assert decision.eligible_for_human_review is False
    assert code in decision.reason_codes
    assert decision.evidence_status is not PolicyEvidenceStatus.CONTRADICTORY


def test_codes_are_reported_in_the_contracts_own_order() -> None:
    """Input order never matters; the vocabulary's declaration order does."""
    gate = PolicyGateInput(
        quote=clean_quote(),
        blocked_reasons=(
            reason(BlockedReasonCode.CREDIT_HOLD),
            reason(BlockedReasonCode.MISSING_QTY),
            reason(BlockedReasonCode.UNKNOWN_SKU),
        ),
        customer_on_credit_hold=True,
    )

    decision = evaluate_quote_gate(gate)

    assert decision.reason_codes == (
        BlockedReasonCode.UNKNOWN_SKU,
        BlockedReasonCode.MISSING_QTY,
        BlockedReasonCode.CREDIT_HOLD,
    )


def test_a_ledger_entry_keeps_its_own_wording() -> None:
    """The operator reads the message the projection produced, not a paraphrase."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(
                reason(
                    BlockedReasonCode.MISSING_QTY,
                    "line 1 PRD_0001: the request does not state a quantity",
                ),
            ),
            customer_on_credit_hold=False,
        )
    )

    assert "the request does not state a quantity" in decision.explanation


def test_the_credit_hold_fact_alone_asserts_the_code() -> None:
    """A fact the caller states needs no ledger entry to be reported."""
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=True)
    )

    assert decision.reason_codes == (BlockedReasonCode.CREDIT_HOLD,)
    assert decision.explanation == "customer account is on credit hold"


def test_the_same_condition_reads_identically_whoever_reports_it() -> None:
    """Ledger wording and gate wording agree when both name the same hold."""
    from_ledger = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(reason(BlockedReasonCode.CREDIT_HOLD, "account on hold"),),
            customer_on_credit_hold=True,
        )
    )
    from_gate = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=True)
    )

    assert from_ledger.explanation == "account on hold"
    assert from_gate.explanation == "customer account is on credit hold"


def test_a_projected_ledger_feeds_the_gate_unchanged() -> None:
    """The Phase 1I pipeline meets Phase 1K without translation."""
    calculation, quote, entry = ledger_for_delivery_quote()

    decision = evaluate_quote_gate(
        PolicyGateInput(quote=quote, blocked_reasons=(entry,), customer_on_credit_hold=False)
    )

    assert entry.code is BlockedReasonCode.DELIVERY_INFEASIBLE
    assert decision.reason_codes == (BlockedReasonCode.DELIVERY_INFEASIBLE,)
    assert decision.eligible_for_human_review is False
    assert calculation.complete is True


# ---------------------------------------------------------------------------
# R2 - a fact the quote proves that the ledger does not report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "quote"),
    [
        ("PRICE_MISSING", refused_quote()),
        ("DISCOUNT_OVER_POLICY", over_limit_quote()),
        ("DELIVERY_INFEASIBLE", infeasible_delivery_quote()),
    ],
)
def test_a_quote_proven_fact_missing_from_the_ledger_is_incomplete(name: str, quote: Quote) -> None:
    """The ledger is the projection's output; a gap in it is not a clean quote."""
    decision = evaluate_quote_gate(PolicyGateInput(quote=quote, customer_on_credit_hold=False))

    assert decision.eligible_for_human_review is False
    assert decision.evidence_status is PolicyEvidenceStatus.INCOMPLETE
    assert name in decision.explanation
    assert "the ledger does not report" in decision.explanation


def test_a_quote_proven_fact_is_asserted_even_when_the_ledger_omits_it() -> None:
    """The quote's own condition is a fact, so ``allowed`` cannot stay true."""
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=over_limit_quote(), customer_on_credit_hold=False)
    )

    assert decision.allowed is False
    assert decision.reason_codes == (BlockedReasonCode.DISCOUNT_OVER_POLICY,)


def test_blocking_stock_is_read_from_the_contracts_own_set() -> None:
    """``PARTIAL`` blocks because ``BLOCKING_STOCK_STATUSES`` says so."""
    partial = make_quote(lines=(make_quote_line(1, stock_status=StockStatus.PARTIAL),))

    decision = evaluate_quote_gate(PolicyGateInput(quote=partial, customer_on_credit_hold=False))

    assert decision.reason_codes == (BlockedReasonCode.STOCK_INSUFFICIENT,)
    assert decision.evidence_status is PolicyEvidenceStatus.INCOMPLETE


# ---------------------------------------------------------------------------
# R3 - a ledger claim no supplied fact witnesses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
        BlockedReasonCode.DELIVERY_INFEASIBLE,
        BlockedReasonCode.DISCOUNT_OVER_POLICY,
    ],
)
def test_a_claim_the_quote_denies_is_a_contradiction(code: BlockedReasonCode) -> None:
    """A quote whose facts deny the claim must not have it recorded as a fact."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(reason(code, "ledger says so"),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.eligible_for_human_review is False
    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert code not in decision.reason_codes
    assert code.value in decision.explanation
    assert "no supplied fact supports" in decision.explanation


def test_a_caller_supplied_promise_witnesses_a_delivery_claim() -> None:
    """The gate-level promise is a witness, so an unquoted claim is not denied."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(reason(BlockedReasonCode.DELIVERY_INFEASIBLE),),
            delivery=blocking_delivery(),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
    assert decision.reason_codes == (BlockedReasonCode.DELIVERY_INFEASIBLE,)
    assert decision.eligible_for_human_review is False


def test_an_upstream_code_is_taken_on_the_ledgers_word() -> None:
    """No fact in the gate input can witness an extraction failure; 1I said so."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(reason(BlockedReasonCode.INJECTION_SUSPECTED),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
    assert decision.reason_codes == (BlockedReasonCode.INJECTION_SUSPECTED,)


# ---------------------------------------------------------------------------
# R4 - the credit-hold fact
# ---------------------------------------------------------------------------


def test_an_unstated_credit_hold_fails_closed() -> None:
    """``None`` is not ``False``: "nobody checked" is not "the account is fine"."""
    decision = evaluate_quote_gate(PolicyGateInput(quote=clean_quote()))

    assert decision.eligible_for_human_review is False
    assert decision.evidence_status is PolicyEvidenceStatus.INCOMPLETE
    assert "credit-hold status was not established" in decision.explanation


def test_a_ledger_hold_the_fact_denies_is_a_contradiction() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            blocked_reasons=(reason(BlockedReasonCode.CREDIT_HOLD, "account on hold"),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert decision.reason_codes == ()
    assert "says the customer is not on hold" in decision.explanation


# ---------------------------------------------------------------------------
# R5 - two assessments of one delivery promise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "delivery"),
    [
        (infeasible_delivery_quote(), feasible_delivery()),
        (feasible_delivery_quote(), blocking_delivery()),
    ],
)
def test_two_delivery_assessments_must_agree(quote: Quote, delivery: DeliveryPromise) -> None:
    """Two assessments of one promise cannot both be right; the gate refuses."""
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=quote, delivery=delivery, customer_on_credit_hold=False)
    )

    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert decision.eligible_for_human_review is False
    assert "the delivery promise says" in decision.explanation


def test_a_delivery_claim_the_quote_denies_is_a_contradiction() -> None:
    """A blocking gate-level promise cannot be claimed against a feasible quote."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=feasible_delivery_quote(),
            blocked_reasons=(reason(BlockedReasonCode.DELIVERY_INFEASIBLE),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert BlockedReasonCode.DELIVERY_INFEASIBLE not in decision.reason_codes


def test_matching_assessments_are_not_a_contradiction() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=infeasible_delivery_quote(),
            delivery=blocking_delivery(),
            blocked_reasons=(reason(BlockedReasonCode.DELIVERY_INFEASIBLE),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
    assert decision.reason_codes == (BlockedReasonCode.DELIVERY_INFEASIBLE,)


# ---------------------------------------------------------------------------
# R6 - one condition, two channels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        PolicyFlag.STOCK_INSUFFICIENT,
        PolicyFlag.DELIVERY_INFEASIBLE,
        PolicyFlag.INJECTION_SUSPECTED,
    ],
)
def test_a_condition_reported_in_both_channels_is_a_contradiction(
    flag: PolicyFlag,
) -> None:
    """The contract classifies each condition as blocking or non-blocking."""
    code = BlockedReasonCode(flag.value)
    gate = gate_for(code).model_copy(update={"flags": (flag,)})

    decision = evaluate_quote_gate(gate)

    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert "both a blocking reason and a flag" in decision.explanation


def test_flags_for_other_conditions_are_not_a_contradiction() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            flags=(PolicyFlag.PRICE_STALE, PolicyFlag.DATE_INFERRED),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.COMPLETE
    assert decision.eligible_for_human_review is True


# ---------------------------------------------------------------------------
# R7 - states this rule set cannot certify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [QuoteStatus.SENT, QuoteStatus.REJECTED])
def test_a_terminal_quote_is_unsupported(status: QuoteStatus) -> None:
    """A quote that already left the review is not awaiting a review decision."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=make_clean_quote(status=status),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.UNSUPPORTED
    assert decision.eligible_for_human_review is False
    assert "not awaiting a review decision" in decision.explanation


def test_a_blocked_line_no_code_accounts_for_is_incomplete() -> None:
    """A block with no recorded cause is evidence the ledger cannot have covered."""
    flagged = make_quote_line(
        1,
        stock_status=StockStatus.SUFFICIENT,
        blocked=True,
        blocked_reason="operator flagged this line for review",
    )
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=make_quote(lines=(flagged,)),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.INCOMPLETE
    assert "no blocking reason accounts for them" in decision.explanation


# ---------------------------------------------------------------------------
# R8 - the approval policy
# ---------------------------------------------------------------------------


def test_a_policy_without_human_approval_is_unsupported() -> None:
    """V1 has no auto-approve path, so the gate cannot certify that state."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=clean_quote(),
            customer_on_credit_hold=False,
            require_human_approval=False,
        )
    )

    assert decision.allowed is True
    assert decision.requires_human_approval is False
    assert decision.eligible_for_human_review is False
    assert decision.evidence_status is PolicyEvidenceStatus.UNSUPPORTED


# ---------------------------------------------------------------------------
# Severity: the most severe defect is the one reported
# ---------------------------------------------------------------------------


def test_the_most_severe_defect_is_reported() -> None:
    """A contradiction outranks a missing fact, and both are in the explanation."""
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=refused_quote(),
            blocked_reasons=(reason(BlockedReasonCode.STOCK_INSUFFICIENT),),
            customer_on_credit_hold=False,
        )
    )

    assert decision.evidence_status is PolicyEvidenceStatus.CONTRADICTORY
    assert "the ledger does not report" in decision.explanation
    assert "no supplied fact supports" in decision.explanation


# ---------------------------------------------------------------------------
# Determinism: the same facts always produce the same decision
# ---------------------------------------------------------------------------


def test_the_same_input_always_produces_the_same_decision() -> None:
    gate = PolicyGateInput(
        quote=infeasible_delivery_quote(),
        blocked_reasons=(reason(BlockedReasonCode.DELIVERY_INFEASIBLE),),
        flags=(PolicyFlag.PRICE_STALE,),
        customer_on_credit_hold=True,
    )

    first = evaluate_quote_gate(gate)
    second = evaluate_quote_gate(gate)

    assert first == second
    assert first.model_dump() == second.model_dump()
    assert first.evidence_sha256 == second.evidence_sha256


def test_ledger_order_does_not_change_the_decision() -> None:
    """Two callers who state the same facts in a different order agree."""
    entries = (
        reason(BlockedReasonCode.MISSING_QTY, "quantity not stated"),
        reason(BlockedReasonCode.UNKNOWN_SKU, "sku not recognised"),
        reason(BlockedReasonCode.INJECTION_SUSPECTED, "prompt-shaped text"),
    )
    quote = clean_quote()

    forward = evaluate_quote_gate(
        PolicyGateInput(quote=quote, blocked_reasons=entries, customer_on_credit_hold=False)
    )
    reversed_order = evaluate_quote_gate(
        PolicyGateInput(
            quote=quote, blocked_reasons=tuple(reversed(entries)), customer_on_credit_hold=False
        )
    )

    assert forward == reversed_order
    assert forward.evidence_sha256 == reversed_order.evidence_sha256


def test_a_changed_fact_changes_the_fingerprint() -> None:
    """The fingerprint identifies the evidence, so different facts differ."""
    on_hold = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=True)
    )
    not_on_hold = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=False)
    )

    assert on_hold.evidence_sha256 != not_on_hold.evidence_sha256
    assert len(on_hold.evidence_sha256) == 64


def test_the_fingerprint_ignores_the_wall_clock() -> None:
    """``created_at`` is an artefact of storage, not a fact the gate decided on."""
    early = make_clean_quote(created_at=utc(hour=8))
    late = make_clean_quote(created_at=utc(hour=17))

    first = evaluate_quote_gate(PolicyGateInput(quote=early, customer_on_credit_hold=False))
    second = evaluate_quote_gate(PolicyGateInput(quote=late, customer_on_credit_hold=False))

    assert first.evidence_sha256 == second.evidence_sha256


def test_the_decision_carries_the_quotes_own_input_fingerprint() -> None:
    """The decision binds itself to the artefact it is about."""
    bare = evaluate_quote_gate(PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=False))
    assert bare.quote_inputs_sha256 is None

    _, quote, entry = ledger_for_delivery_quote()
    assert quote.inputs_sha256 is not None
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=quote, blocked_reasons=(entry,), customer_on_credit_hold=False)
    )
    assert decision.quote_inputs_sha256 == quote.inputs_sha256


def test_the_gate_does_not_mutate_its_inputs() -> None:
    """Nothing is promoted, relabelled or annotated on the way through."""
    quote = over_limit_quote()
    entries = (reason(BlockedReasonCode.DISCOUNT_OVER_POLICY),)
    gate = PolicyGateInput(quote=quote, blocked_reasons=entries, customer_on_credit_hold=True)
    before = (quote.model_dump(), [entry.model_dump() for entry in entries], gate.model_dump())

    evaluate_quote_gate(gate)

    after = (quote.model_dump(), [entry.model_dump() for entry in entries], gate.model_dump())
    assert before == after


def test_the_gate_leaves_the_quote_a_draft() -> None:
    """Eligibility is not a workflow state: no status is promoted here."""
    quote = clean_quote()

    decision = evaluate_quote_gate(PolicyGateInput(quote=quote, customer_on_credit_hold=False))

    assert decision.eligible_for_human_review is True
    assert quote.status is QuoteStatus.DRAFT
    assert quote.is_sendable is False


# ---------------------------------------------------------------------------
# The decision schema: the two fail-open shapes are unrepresentable
# ---------------------------------------------------------------------------


def decision_kwargs(**overrides: object) -> dict[str, object]:
    """Valid keyword arguments for a :class:`PolicyGateDecision`."""
    payload: dict[str, object] = {
        "quote_id": "QUOTE-0001",
        "run_id": RUN_ID,
        "allowed": True,
        "explanation": "clean",
        "requires_human_approval": True,
        "eligible_for_human_review": True,
        "evidence_status": PolicyEvidenceStatus.COMPLETE,
        "evidence_sha256": "a" * 64,
    }
    payload.update(overrides)
    return payload


def test_an_eligible_decision_requires_complete_evidence() -> None:
    with pytest.raises(ValidationError, match="only be eligible for human review"):
        PolicyGateDecision.model_validate(
            decision_kwargs(evidence_status=PolicyEvidenceStatus.INCOMPLETE)
        )


def test_an_eligible_decision_carries_no_codes() -> None:
    with pytest.raises(ValidationError, match="an eligible quote carries no blocking reason codes"):
        PolicyGateDecision.model_validate(
            decision_kwargs(
                allowed=False,
                requires_human_approval=False,
                reason_codes=(BlockedReasonCode.PRICE_MISSING,),
            )
        )


def test_an_eligible_decision_requires_the_v1_policy() -> None:
    with pytest.raises(ValidationError, match="requires the V1 policy"):
        PolicyGateDecision.model_validate(decision_kwargs(requires_human_approval=False))


def test_a_refused_decision_must_say_what_stopped_it() -> None:
    with pytest.raises(ValidationError, match="must name a blocking reason code"):
        PolicyGateDecision.model_validate(decision_kwargs(eligible_for_human_review=False))


def test_a_refused_decision_may_name_an_evidence_defect_instead_of_a_code() -> None:
    refused = PolicyGateDecision.model_validate(
        decision_kwargs(
            eligible_for_human_review=False,
            evidence_status=PolicyEvidenceStatus.CONTRADICTORY,
            explanation="contradiction: ...",
        )
    )

    assert refused.eligible_for_human_review is False
    assert refused.reason_codes == ()


def test_the_decision_is_frozen_and_closed() -> None:
    decision = PolicyGateDecision.model_validate(decision_kwargs())

    with pytest.raises(ValidationError):
        decision.eligible_for_human_review = False  # type: ignore[misc]

    with pytest.raises(ValidationError):
        PolicyGateDecision.model_validate({**decision_kwargs(), "approved_at": "2026-10-07"})


def test_the_decision_names_its_rule_set() -> None:
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), customer_on_credit_hold=False)
    )

    assert decision.gate_version == GATE_VERSION == "gate-v1"


def test_the_explanation_stays_inside_the_contract_limit() -> None:
    """Even with every code at once the sentence fits ``PolicyDecision``."""
    entries = tuple(reason(code, "x" * 299) for code in BlockedReasonCode)
    decision = evaluate_quote_gate(
        PolicyGateInput(quote=clean_quote(), blocked_reasons=entries, customer_on_credit_hold=False)
    )

    assert len(decision.explanation) <= 400
    assert decision.eligible_for_human_review is False


def test_the_decision_exposes_no_approval_or_send_surface() -> None:
    """The record is a decision about review, not an authorisation to act."""
    fields = set(PolicyGateDecision.model_fields)
    forbidden = {"status", "approved", "approved_at", "sendable", "sent_at", "sent"}

    assert forbidden & fields == set()
    for surface in ("transition", "send", "approve", "advance"):
        assert not hasattr(PolicyGateDecision, surface)
