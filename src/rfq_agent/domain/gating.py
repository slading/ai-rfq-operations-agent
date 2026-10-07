"""The deterministic quote gate (architecture §4.2 Group B).

:func:`evaluate_quote_gate` answers one question: *is this quote eligible to
proceed to a future human review step?* It is a pure function over typed inputs,
and the model is never consulted.

The answer is deliberately narrow. It is **not** approval, not sendability and
not the workflow's ``READY`` state: a quote can be eligible for review and never
leave the building. Nor does the gate decide whether the quote may be sent
autonomously - that is the workflow's job, and in V1 the answer is always no,
because ``workflow.require_human_approval`` is true and there is no auto-send
code path at all.

Phase 1K adds the part that makes the decision evidence: the gate checks the
facts it was handed against each other and against the quote contract, refuses to
certify anything but complete, un-contradicted evidence (see
:class:`~rfq_agent.domain.policy.PolicyEvidenceStatus`), and returns a
:class:`~rfq_agent.domain.policy.PolicyGateDecision` carrying its identity, its
evidence fingerprint and its verdict. A missing, ambiguous or contradictory fact
fails closed: the quote is not eligible, and the decision says which kind of
defect stopped it.

:func:`project_blocked_ledger` (Phase 1I) is the other half of the same idea: it
turns the facts a calculation produced into the contract's own ledger entries, so
"why can this quote not go out" is answered from recorded facts rather than
inferred. It maps exactly five facts - an un-priced line, a stock status the
contract calls blocking, a blocking delivery position, a selected rule that
exceeds the delegated limit, and a customer on credit hold - onto the existing
:class:`~rfq_agent.domain.policy.BlockedReasonCode` members. It invents no
semantics, decides nothing, and executes no gate.

This lives in its own module rather than in :mod:`rfq_agent.domain.policy`
because it consumes the quote schema; keeping the dependency one-way avoids a
circular import between policy and quote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Self

from pydantic import model_validator

from rfq_agent.domain.delivery import DeliveryAssessment, DeliveryPromise
from rfq_agent.domain.ids import RunId
from rfq_agent.domain.policy import (
    GATE_VERSION,
    BlockedReason,
    BlockedReasonCode,
    DiscountApplication,
    PolicyEvidenceStatus,
    PolicyFlag,
    PolicyGateDecision,
    QuoteBlockedLedger,
)
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import (
    TERMINAL_QUOTE_STATUSES,
    Quote,
    QuoteCalculation,
    QuoteLineRefusal,
)
from rfq_agent.domain.stock import BLOCKING_STOCK_STATUSES, StockStatus
from rfq_agent.domain.values import DomainModel, Json, canonical_json, sha256_text

__all__ = [
    "PolicyGateInput",
    "evaluate_quote_gate",
    "project_blocked_ledger",
]


class PolicyGateInput(DomainModel):
    """Typed inputs to the policy gate.

    Deliberately small and boring: everything needed to decide, nothing that
    could be argued with.
    """

    quote: Quote
    blocked_reasons: tuple[BlockedReason, ...] = ()
    flags: tuple[PolicyFlag, ...] = ()
    delivery: DeliveryPromise | None = None
    #: The customer's credit-hold fact, ``True`` when the account is on hold.
    #: ``None`` means it was not established, which is deliberately distinct from
    #: ``False``: "no hold" has to be stated, and an unstated fact fails the
    #: decision closed (Phase 1K) rather than reading as clean.
    customer_on_credit_hold: bool | None = None
    #: From configuration; ``True`` throughout V1.
    require_human_approval: bool = True

    @model_validator(mode="after")
    def _check_no_duplicate_entries(self) -> Self:
        """Flags and reason codes are sets in spirit; duplicates are a bug."""
        if len(set(self.flags)) != len(self.flags):
            msg = "flags must not contain duplicates"
            raise ValueError(msg)
        codes = [reason.code for reason in self.blocked_reasons]
        if len(set(codes)) != len(codes):
            msg = "blocked_reasons must not contain duplicate codes"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# The gate (Phases 1I and 1K)
# ---------------------------------------------------------------------------
#
# The reporting order, the message limits and the gate's own wording are shared
# by the decision and by the projection. They are defined once, here, so the same
# condition reads identically whoever reports it.

#: The order every entry is reported in: the declaration order of the contract's
#: own :class:`BlockedReasonCode`, which runs from the concrete data problems to
#: the commercial ones. The caller's order never matters.
_CODE_ORDER: Mapping[BlockedReasonCode, int] = {
    code: index for index, code in enumerate(BlockedReasonCode)
}

#: The ledger message limit, taken from :class:`BlockedReason`.
_MAX_BLOCKED_MESSAGE = 300

#: The decision explanation limit, taken from :class:`PolicyDecision`.
_MAX_EXPLANATION = 400

#: The gate's own words for a credit hold, reused rather than reworded, so the
#: same condition reads identically whoever reports it.
_CREDIT_HOLD_MESSAGE = "customer account is on credit hold"

#: How many offending lines a multi-line message names before it stops listing.
_MAX_NAMED_LINES = 6


def _fit(text: str, limit: int) -> str:
    """Keep a generated message inside a contract limit, without splitting a word."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _reason_order(reason: BlockedReason) -> tuple[int, int]:
    """Sort key: the code's contract order, then the offending line ordinal."""
    return (_CODE_ORDER[reason.code], reason.line_ordinal or 0)


def evaluate_quote_gate(gate: PolicyGateInput) -> PolicyGateDecision:
    """Decide whether a quote is eligible for a future human review step.

    Phase 1K. Still a pure function - no clock, no database, no model - and the
    answer is still narrow: eligibility means "a human may now look at this",
    never "approved", "sendable" or workflow ``READY``.

    The rules, in order:

    * **R1 - the asserted codes.** ``reason_codes`` is the codes the supplied
      facts assert, in the contract's declaration order, each with the sentence to
      report: the codes the ledger reports (the eight upstream failures cannot be
      witnessed by the quote, so they are taken on the ledger's word, as accepted
      in Phase 1I), the caller's own facts - a credit hold, a blocking delivery
      promise - and the quote-proven facts of R2. Where the ledger reported a
      code its own message is kept; where the gate asserts a fact for itself the
      wording is the gate's own and is never dressed up as a ledger entry.
    * **R2 - facts missing from the ledger.** Every code the quote contract
      itself proves (an un-priced line, a blocking stock status, a blocking
      delivery promise, a rule that needs sign-off) has to appear in
      ``blocked_reasons``. A fact the ledger omits is ``INCOMPLETE`` evidence,
      *and* the fact is asserted - the quote is the artefact being decided about,
      so its own condition is a fact rather than a claim - with gate-generated
      wording (see ``_MISSING_FACT_MESSAGES``), never as a quoted ledger message.
      The projection is not re-run here: ``project_blocked_ledger`` stays the one
      producer of entries.
    * **R3 - facts the ledger claims that the quote denies.** A ledger entry the
      quote's own facts cannot produce (a ``STOCK_INSUFFICIENT`` on a quote whose
      every line has stock, say) is ``CONTRADICTORY``: the gate will not certify
      a state the contract contradicts, and it will not launder the denied claim
      into ``reason_codes`` either - the contradiction is named in the
      explanation, the disputed code is not asserted as a fact.
    * **R4 - the credit-hold fact.** A hold the caller states is a code; a ledger
      claim of one that the fact denies is ``CONTRADICTORY``; a fact that was
      never established is ``INCOMPLETE``.
    * **R5 - two delivery assessments.** ``gate.delivery`` and the quote's own
      assessment must agree about :meth:`DeliveryPromise.is_blocking`; if they do
      not, nothing here can arbitrate and the evidence is ``CONTRADICTORY``.
    * **R6 - one condition, two channels.** A condition reported as both a
      blocking reason and a non-blocking flag is a contradiction: the accepted
      contract puts each condition in exactly one of the two.
    * **R7 - states this rule set cannot certify.** A quote that is already
      terminal is ``UNSUPPORTED``; a blocked line that no blocking code accounts
      for is ``INCOMPLETE``.
    * **R8 - the approval policy.** ``require_human_approval`` other than ``True``
      is ``UNSUPPORTED``: V1 has no auto-approve path, so the gate cannot certify
      eligibility under a policy it does not implement. ``allowed`` keeps its
      accepted values here; only the eligibility verdict changes.

    ``eligible_for_human_review`` is true only when the evidence is ``COMPLETE``,
    no code was asserted and the V1 policy holds. Each check only ever removes
    eligibility, and no check can add a business code: the gate reports the codes
    the inputs assert, never ones it invented.

    Args:
        gate: The quote, the ledger entries, the flags and the caller's facts.

    Returns:
        A :class:`~rfq_agent.domain.policy.PolicyGateDecision` carrying the
        verdict, the evidence status, the codes the inputs asserted and a
        fingerprint of exactly the facts decided over. Identical inputs always
        produce an identical decision, whatever order the ledger entries arrive
        in.
    """
    quote = gate.quote
    from_ledger = {reason.code for reason in gate.blocked_reasons}
    proven = _quote_proven_codes(quote)
    asserted = {
        **_asserted_messages(gate),
        # R2: a fact the quote proves is asserted whether or not the ledger
        # reports it. The quote is the artefact being decided about, so its own
        # condition is not a claim - the omitting ledger is the defect.
        **_missing_fact_messages(proven - from_ledger),
    }
    denied = _denied_codes(gate, from_ledger=from_ledger)
    established = {code: sentence for code, sentence in asserted.items() if code not in denied}
    codes = tuple(sorted(established, key=_CODE_ORDER.__getitem__))
    defects = _evidence_defects(
        gate,
        from_ledger=from_ledger,
        proven=proven,
        denied=denied,
        codes=codes,
    )

    explanation = "; ".join(established[code] for code in codes)
    if not codes:
        explanation = (
            "no blocking conditions; awaiting human approval (V1 policy)"
            if gate.require_human_approval
            else "all policy checks passed"
        )
    if defects:
        explanation = "; ".join([explanation, *(defect.sentence for defect in defects)])

    require_human_approval = not codes and gate.require_human_approval
    evidence_status = _most_severe(defects)
    return PolicyGateDecision(
        quote_id=quote.quote_id,
        run_id=quote.run_id,
        allowed=not codes,
        reason_codes=codes,
        explanation=_fit(explanation, _MAX_EXPLANATION),
        requires_human_approval=require_human_approval,
        eligible_for_human_review=(
            evidence_status is PolicyEvidenceStatus.COMPLETE and require_human_approval
        ),
        evidence_status=evidence_status,
        evidence_sha256=_evidence_fingerprint(gate),
        quote_inputs_sha256=quote.inputs_sha256,
    )


def _asserted_messages(gate: PolicyGateInput) -> dict[BlockedReasonCode, str]:
    """R1: the codes the gate input asserts, each with the sentence to report.

    The ledger's own message wins wherever the ledger reported the code - the
    same condition must read identically whoever names it. The two codes a
    caller's fact can assert on its own (a credit hold, a blocking delivery
    promise) carry the gate's accepted wording when the ledger is silent.
    """
    messages = {reason.code: reason.message for reason in gate.blocked_reasons}
    if gate.customer_on_credit_hold is True:
        messages.setdefault(BlockedReasonCode.CREDIT_HOLD, _CREDIT_HOLD_MESSAGE)
    delivery = gate.delivery
    if delivery is not None and delivery.is_blocking():
        messages.setdefault(
            BlockedReasonCode.DELIVERY_INFEASIBLE,
            _fit(f"requested delivery cannot be met: {delivery.rationale}", _MAX_BLOCKED_MESSAGE),
        )
    return messages


#: The codes the accepted quote contract can prove on its own: the projection's
#: four quote-side facts. The credit hold is the fifth, and it is a caller fact
#: rather than a property of the quote, so it is checked separately (R4).
_QUOTE_PROVABLE_CODES = frozenset(
    {
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
        BlockedReasonCode.DELIVERY_INFEASIBLE,
        BlockedReasonCode.DISCOUNT_OVER_POLICY,
    }
)

#: How severe each evidence status is; a decision reports the most severe defect
#: it found. ``COMPLETE`` is the absence of one.
_EVIDENCE_SEVERITY: Mapping[PolicyEvidenceStatus, int] = {
    PolicyEvidenceStatus.CONTRADICTORY: 3,
    PolicyEvidenceStatus.UNSUPPORTED: 2,
    PolicyEvidenceStatus.INCOMPLETE: 1,
    PolicyEvidenceStatus.COMPLETE: 0,
}


@dataclass(frozen=True, slots=True)
class _Defect:
    """One evidence defect: how severe it is, and the sentence that names it."""

    status: PolicyEvidenceStatus
    sentence: str


def _denied_codes(
    gate: PolicyGateInput, *, from_ledger: set[BlockedReasonCode]
) -> frozenset[BlockedReasonCode]:
    """Ledger claims the quote's or the caller's own facts deny (R3, R4).

    A denied claim is evidence *about the ledger*, not a business fact: it is
    reported as a contradiction and deliberately not carried into
    ``reason_codes``. A credit hold the caller neither states nor denies is left
    alone - unwitnessed evidence is incomplete (R4), not contradicted.
    """
    denied = set(from_ledger & _QUOTE_PROVABLE_CODES) - _witnessed_codes(gate)
    if BlockedReasonCode.CREDIT_HOLD in from_ledger and gate.customer_on_credit_hold is False:
        denied.add(BlockedReasonCode.CREDIT_HOLD)
    return frozenset(denied)


def _quote_proven_codes(quote: Quote) -> frozenset[BlockedReasonCode]:
    """The blocking facts the quote contract itself proves (R2).

    Read exactly as the projection reads them - the quote's own line statuses,
    its delivery assessment and its selected rule - from the contracts that
    already call those conditions blocking. This is a *check* on the ledger,
    never a second producer of entries: the gate asserts the code, and the
    entry's wording stays the gate's own (see :func:`_missing_fact_messages`).
    """
    codes = set()
    if any(line.price_status is not PriceLookupStatus.FOUND for line in quote.lines):
        codes.add(BlockedReasonCode.PRICE_MISSING)
    if any(line.stock_status in BLOCKING_STOCK_STATUSES for line in quote.lines):
        codes.add(BlockedReasonCode.STOCK_INSUFFICIENT)
    if quote.delivery is not None and quote.delivery.promise.is_blocking():
        codes.add(BlockedReasonCode.DELIVERY_INFEASIBLE)
    if quote.discount is not None and quote.discount.requires_approval:
        codes.add(BlockedReasonCode.DISCOUNT_OVER_POLICY)
    return frozenset(codes)


def _witnessed_codes(gate: PolicyGateInput) -> frozenset[BlockedReasonCode]:
    """Every code the supplied facts witness, quote and caller together (R3).

    A ledger claim is denied only when nothing here supports it: the quote's own
    facts, a blocking delivery promise the caller attached to the gate, or a
    credit hold the caller states. The two caller facts are witnesses but not a
    second evidence channel for R2: they are not properties of the quote, so a
    ledger that omits them is not incomplete - the fact itself already asserts
    its code.
    """
    codes = set(_quote_proven_codes(gate.quote))
    if gate.delivery is not None and gate.delivery.is_blocking():
        codes.add(BlockedReasonCode.DELIVERY_INFEASIBLE)
    if gate.customer_on_credit_hold is True:
        codes.add(BlockedReasonCode.CREDIT_HOLD)
    return frozenset(codes)


#: The wording the gate uses for a fact the quote proves that the ledger does not
#: report. It deliberately describes the *quote's* condition rather than copying
#: or re-deriving the projection's sentence: there is no ledger entry to quote
#: here, and the sentence must read as the gate's own derivation, never as an
#: original ledger message that was in fact never supplied.
_MISSING_FACT_MESSAGES: Mapping[BlockedReasonCode, str] = {
    BlockedReasonCode.PRICE_MISSING: (
        "the quote has a line whose price lookup did not return FOUND"
    ),
    BlockedReasonCode.STOCK_INSUFFICIENT: (
        "the quote has a line whose stock status the contract calls blocking"
    ),
    BlockedReasonCode.DELIVERY_INFEASIBLE: ("the quote's delivery assessment is blocking"),
    BlockedReasonCode.DISCOUNT_OVER_POLICY: (
        "the applied discount rule exceeds the delegated limit"
    ),
}


def _missing_fact_messages(missing: set[BlockedReasonCode]) -> dict[BlockedReasonCode, str]:
    """Codes the quote proves but the ledger does not report, with gate wording."""
    return {code: _MISSING_FACT_MESSAGES[code] for code in missing}


def _evidence_defects(
    gate: PolicyGateInput,
    *,
    from_ledger: set[BlockedReasonCode],
    proven: frozenset[BlockedReasonCode],
    denied: frozenset[BlockedReasonCode],
    codes: tuple[BlockedReasonCode, ...],
) -> list[_Defect]:
    """Every reason this evidence cannot carry the decision, in a fixed order.

    The order is R2, R3, R4, R5, R6, R7, R8 - the same order the rules are
    documented in - so the same facts always produce the same sentences.
    """
    defects: list[_Defect] = []
    quote = gate.quote

    # R2 - a fact the quote proves that the ledger does not report.
    missing = sorted(proven - from_ledger, key=_CODE_ORDER.__getitem__)
    if missing:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.INCOMPLETE,
                "incomplete evidence: the quote proves "
                f"{_names(missing)}, which the ledger does not report",
            )
        )

    # R3 - a ledger claim that no supplied fact witnesses.
    unproven = sorted(denied & _QUOTE_PROVABLE_CODES, key=_CODE_ORDER.__getitem__)
    if unproven:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.CONTRADICTORY,
                "contradiction: the ledger reports "
                f"{_names(unproven)}, which no supplied fact supports",
            )
        )

    # R4 - the credit-hold fact.
    if gate.customer_on_credit_hold is None:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.INCOMPLETE,
                "incomplete evidence: the customer's credit-hold status was not established",
            )
        )
    elif gate.customer_on_credit_hold is False and BlockedReasonCode.CREDIT_HOLD in from_ledger:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.CONTRADICTORY,
                "contradiction: the ledger reports CREDIT_HOLD but the credit-hold fact "
                "says the customer is not on hold",
            )
        )

    # R5 - two assessments of one delivery promise.
    if _deliveries_disagree(gate):
        defects.append(
            _Defect(
                PolicyEvidenceStatus.CONTRADICTORY,
                "contradiction: the delivery promise says "
                f"{gate.delivery.feasibility} while the quote's assessment says "
                f"{quote.delivery.promise.feasibility}",
            )
        )

    # R6 - one condition reported in both channels.
    duplicated = sorted({flag.value for flag in gate.flags} & {code.value for code in codes})
    if duplicated:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.CONTRADICTORY,
                "contradiction: "
                f"{', '.join(duplicated)} is reported as both a blocking reason and a flag",
            )
        )

    # R7 - states this rule set cannot certify.
    if quote.status in TERMINAL_QUOTE_STATUSES:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.UNSUPPORTED,
                f"unsupported state: the quote is {quote.status}, so it is not awaiting "
                "a review decision",
            )
        )
    unexplained = _unexplained_blocked_lines(quote)
    if unexplained:
        named = ", ".join(str(ordinal) for ordinal in unexplained[:_MAX_NAMED_LINES])
        more = len(unexplained) - _MAX_NAMED_LINES
        defects.append(
            _Defect(
                PolicyEvidenceStatus.INCOMPLETE,
                f"incomplete evidence: line(s) {named}{' and more' if more > 0 else ''} "
                "are blocked and no blocking reason accounts for them",
            )
        )

    # R8 - the approval policy.
    if gate.require_human_approval is not True:
        defects.append(
            _Defect(
                PolicyEvidenceStatus.UNSUPPORTED,
                "unsupported state: require_human_approval is not true, so this gate "
                "cannot certify a V1 human-review decision",
            )
        )
    return defects


def _names(codes: Sequence[BlockedReasonCode]) -> str:
    """Join codes for a human sentence, in the contract's own order."""
    return ", ".join(code.value for code in codes)


def _most_severe(defects: Sequence[_Defect]) -> PolicyEvidenceStatus:
    """The most severe defect, or ``COMPLETE`` when there is none."""
    if not defects:
        return PolicyEvidenceStatus.COMPLETE
    return max(defects, key=lambda defect: _EVIDENCE_SEVERITY[defect.status]).status


def _deliveries_disagree(gate: PolicyGateInput) -> bool:
    """R5: two assessments of the same promise that do not agree, where both exist."""
    quote_delivery = gate.quote.delivery
    if gate.delivery is None or quote_delivery is None:
        return False
    return gate.delivery.is_blocking() != quote_delivery.promise.is_blocking()


def _unexplained_blocked_lines(quote: Quote) -> list[int]:
    """R7: blocked lines that no blocking code accounts for, in ordinal order.

    A line is explained when its price is not ``FOUND`` (``PRICE_MISSING``) or its
    stock is ``NONE`` (``STOCK_INSUFFICIENT``) - the two shapes the accepted
    contract gives a blocked line. Anything else is a block with no recorded
    cause, which is evidence the ledger cannot have covered.
    """
    return sorted(
        line.ordinal
        for line in quote.lines
        if line.blocked
        and line.price_status is PriceLookupStatus.FOUND
        and line.stock_status is not StockStatus.NONE
    )


def _evidence_fingerprint(gate: PolicyGateInput) -> str:
    """Fingerprint of exactly the facts this decision was made over (Phase 1K).

    The whole typed input, with two normalisations and no others: ``created_at``
    is excluded (a wall-clock artefact rather than a fact - the quote's own input
    fingerprint makes the same exclusion), and the two ordered collections are
    normalised - the ledger entries into the contract's reporting order and the
    flags sorted by value - so two callers who state the same facts in a
    different order get the same fingerprint.
    """
    quote = gate.quote
    payload: Json = {
        "gate_version": GATE_VERSION,
        "quote": quote.model_dump(mode="json", exclude={"created_at"}),
        "blocked_reasons": [
            reason.model_dump(mode="json")
            for reason in sorted(gate.blocked_reasons, key=_reason_order)
        ],
        "flags": sorted(flag.value for flag in gate.flags),
        "delivery": gate.delivery.model_dump(mode="json") if gate.delivery is not None else None,
        "customer_on_credit_hold": gate.customer_on_credit_hold,
        "require_human_approval": gate.require_human_approval,
    }
    return sha256_text(canonical_json(payload))


# ---------------------------------------------------------------------------
# The blocking ledger (Phase 1I)
# ---------------------------------------------------------------------------
#
# Five facts map onto five codes that already exist. Nothing here decides
# whether those conditions *should* block - the contract already says so:
# `BLOCKING_STOCK_STATUSES` is documented as "statuses that force human review
# before a quote may be sent", `DeliveryPromise.is_blocking()` is documented as
# requiring human attention, and a rule that `requires_approval` is by
# definition over the delegated limit. This code only names them.


def project_blocked_ledger(
    calculation: QuoteCalculation,
    *,
    run_id: RunId,
    customer_on_credit_hold: bool | None = None,
) -> QuoteBlockedLedger:
    """Project the facts a calculation produced onto the contract's ledger.

    The five mappings, each taken from a contract that already exists:

    * a line whose price lookup did not return ``FOUND`` - the calculation's own
      refusals, every one of which is validated to name a really-blocked line -
      ⇒ :attr:`~BlockedReasonCode.PRICE_MISSING`;
    * a line whose stock status is in
      :data:`~rfq_agent.domain.stock.BLOCKING_STOCK_STATUSES` (``PARTIAL``,
      ``NONE``, ``UNKNOWN``: "statuses that force human review before a quote may
      be sent") ⇒ :attr:`~BlockedReasonCode.STOCK_INSUFFICIENT`;
    * a delivery position whose :meth:`DeliveryPromise.is_blocking` is true -
      ``INFEASIBLE`` or ``UNKNOWN``, never "assume it is fine" ⇒
      :attr:`~BlockedReasonCode.DELIVERY_INFEASIBLE`;
    * a selected discount whose ``requires_approval`` is true ⇒
      :attr:`~BlockedReasonCode.DISCOUNT_OVER_POLICY`;
    * the credit-hold fact the caller supplies ⇒
      :attr:`~BlockedReasonCode.CREDIT_HOLD`.

    Nothing else becomes a reason. Facts the calculation did not establish
    (stock nobody checked, a delivery nobody priced, a price that was never
    looked up) are not converted into other codes: an unknown stock status is
    ``STOCK_INSUFFICIENT`` **because** the contract's own blocking set says so,
    and an unknown delivery is ``DELIVERY_INFEASIBLE`` for the same reason - not
    because this function guessed what it meant.

    The contract's gate accepts at most one reason per code (its input refuses
    duplicate codes outright), so several lines with the same problem become
    **one** entry: it carries the lowest offending line ordinal and names the
    lines in its message, while the per-line detail stays where the calculation
    put it - on the blocked line and in the refusal entry. The ledger is
    therefore usable as ``PolicyGateInput.blocked_reasons`` unchanged.

    Nothing is approved, rejected or transitioned here: a clean quote produces
    an empty ledger and nothing more, and a blocked one produces reasons an
    operator can act on. No flags are emitted: which facts raise which
    non-blocking flag is not fixed by any accepted contract, and inventing that
    would be new policy semantics.

    Args:
        calculation: The arithmetic outcome. Its quote carries the lines' stock
            statuses and - when the caller attached them - the delivery
            assessment and the discount that was applied; its refusals are the
            lines that were not totalled.
        run_id: The run the ledger belongs to.
        customer_on_credit_hold: The customer's credit-hold fact, supplied by the
            caller from the customer record; ``True`` when the account is on
            hold, ``False`` when it is not and ``None`` when it was not
            established. Never looked up or assumed here. Only ``True`` becomes
            an entry - an absent fact is silence, and it is the Phase 1K gate
            that fails a decision closed on it.

    Returns:
        A :class:`QuoteBlockedLedger` whose ``reasons`` are ordered by the
        contract's own code order, with ``line_ordinal`` set for the line-level
        ones. Identical facts always produce an identical ledger, whatever order
        the lines or refusals arrive in.
    """
    quote = calculation.quote
    reasons: list[BlockedReason] = [
        *_price_reasons(calculation.refusals),
        *_stock_reasons(quote),
        *_delivery_reasons(quote.delivery),
        *_discount_reasons(quote.discount),
        *_credit_hold_reasons(customer_on_credit_hold),
    ]
    reasons.sort(key=_reason_order)
    return QuoteBlockedLedger(
        run_id=run_id,
        quote_id=quote.quote_id,
        reasons=tuple(reasons),
    )


def _price_reasons(refusals: Sequence[QuoteLineRefusal]) -> list[BlockedReason]:
    """One ``PRICE_MISSING`` entry covering every line that was not totalled.

    The message keeps the pricing lookup's own sentence, so the operator reads
    the same explanation the calculation recorded.
    """
    if not refusals:
        return []
    ordered = sorted(refusals, key=lambda refusal: refusal.ordinal)
    if len(ordered) == 1:
        only = ordered[0]
        message = (
            f"line {only.ordinal} {only.product_id} has no usable price "
            f"({only.status}/{only.reason}): {only.detail}"
        )
    else:
        named = "; ".join(
            f"line {refusal.ordinal} {refusal.product_id} ({refusal.status}/{refusal.reason})"
            for refusal in ordered[:_MAX_NAMED_LINES]
        )
        more = len(ordered) - _MAX_NAMED_LINES
        message = f"{len(ordered)} lines have no usable price: {named}" + (
            f"; and {more} more" if more > 0 else ""
        )
    return [
        BlockedReason(
            code=BlockedReasonCode.PRICE_MISSING,
            message=_fit(message, _MAX_BLOCKED_MESSAGE),
            line_ordinal=ordered[0].ordinal,
        )
    ]


def _stock_reasons(quote: Quote) -> list[BlockedReason]:
    """One ``STOCK_INSUFFICIENT`` entry covering every line needing stock review."""
    offenders = sorted(
        (line for line in quote.lines if line.stock_status in BLOCKING_STOCK_STATUSES),
        key=lambda line: line.ordinal,
    )
    if not offenders:
        return []
    if len(offenders) == 1:
        only = offenders[0]
        message = (
            f"line {only.ordinal} {only.product_id}: stock is {only.stock_status}, "
            "which must be reviewed before the quote may be sent"
        )
    else:
        named = ", ".join(
            f"line {line.ordinal} {line.product_id} ({line.stock_status})"
            for line in offenders[:_MAX_NAMED_LINES]
        )
        more = len(offenders) - _MAX_NAMED_LINES
        message = f"{len(offenders)} lines need stock review: {named}" + (
            f"; and {more} more" if more > 0 else ""
        )
    return [
        BlockedReason(
            code=BlockedReasonCode.STOCK_INSUFFICIENT,
            message=_fit(message, _MAX_BLOCKED_MESSAGE),
            line_ordinal=offenders[0].ordinal,
        )
    ]


def _delivery_reasons(assessment: DeliveryAssessment | None) -> list[BlockedReason]:
    """One ``DELIVERY_INFEASIBLE`` entry, in the gate's own wording."""
    if assessment is None or not assessment.promise.is_blocking():
        return []
    return [
        BlockedReason(
            code=BlockedReasonCode.DELIVERY_INFEASIBLE,
            message=_fit(
                f"requested delivery cannot be met: {assessment.promise.rationale}",
                _MAX_BLOCKED_MESSAGE,
            ),
        )
    ]


def _discount_reasons(discount: DiscountApplication | None) -> list[BlockedReason]:
    """One ``DISCOUNT_OVER_POLICY`` entry when the applied rule needs sign-off."""
    if discount is None or not discount.requires_approval:
        return []
    return [
        BlockedReason(
            code=BlockedReasonCode.DISCOUNT_OVER_POLICY,
            message=(
                f"discount rule {discount.rule_id} at {discount.percent}% exceeds the "
                "delegated limit and needs human sign-off"
            ),
        )
    ]


def _credit_hold_reasons(customer_on_credit_hold: bool | None) -> list[BlockedReason]:
    """One ``CREDIT_HOLD`` entry, from the fact the caller supplied.

    Only ``True`` is a hold. ``False`` is the customer's account not being held,
    and ``None`` is a fact nobody established - neither is evidence of a hold, and
    neither is turned into an entry. The projection reports facts; the Phase 1K
    gate is where an unestablished fact stops a decision.
    """
    if customer_on_credit_hold is not True:
        return []
    return [
        BlockedReason(
            code=BlockedReasonCode.CREDIT_HOLD,
            message=_CREDIT_HOLD_MESSAGE,
        )
    ]
