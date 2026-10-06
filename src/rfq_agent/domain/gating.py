"""The deterministic quote gate (architecture §4.2 Group B).

:func:`evaluate_quote_gate` answers one question: *does this quote have any
unresolved business problem?* It is a pure function over typed inputs, and the
model is never consulted.

It deliberately does **not** decide whether the quote may be sent autonomously.
That is the workflow's job, and in V1 the answer is always no, because
``workflow.require_human_approval`` is true and there is no auto-send code path
at all.

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
from typing import Self

from pydantic import model_validator

from rfq_agent.domain.delivery import DeliveryAssessment, DeliveryPromise
from rfq_agent.domain.ids import RunId
from rfq_agent.domain.policy import (
    BlockedReason,
    BlockedReasonCode,
    DiscountApplication,
    PolicyDecision,
    PolicyFlag,
    QuoteBlockedLedger,
)
from rfq_agent.domain.quote import Quote, QuoteCalculation, QuoteLineRefusal
from rfq_agent.domain.stock import BLOCKING_STOCK_STATUSES
from rfq_agent.domain.values import DomainModel

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
    customer_on_credit_hold: bool = False
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


def evaluate_quote_gate(gate: PolicyGateInput) -> PolicyDecision:
    """Evaluate blocking conditions for a quote. Pure function, no I/O."""
    codes: list[BlockedReasonCode] = []
    messages: list[str] = []
    seen: set[BlockedReasonCode] = set()

    def _add(code: BlockedReasonCode, message: str) -> None:
        if code not in seen:
            seen.add(code)
            codes.append(code)
            messages.append(message)

    for reason in gate.blocked_reasons:
        _add(reason.code, reason.message)
    if gate.customer_on_credit_hold:
        _add(BlockedReasonCode.CREDIT_HOLD, "customer account is on credit hold")
    if gate.delivery is not None and gate.delivery.is_blocking():
        _add(
            BlockedReasonCode.DELIVERY_INFEASIBLE,
            f"requested delivery cannot be met: {gate.delivery.rationale}",
        )

    if codes:
        return PolicyDecision(
            allowed=False,
            reason_codes=tuple(codes),
            explanation="; ".join(messages),
        )

    if gate.require_human_approval:
        return PolicyDecision(
            allowed=True,
            explanation="no blocking conditions; awaiting human approval (V1 policy)",
            requires_human_approval=True,
        )

    return PolicyDecision(allowed=True, explanation="all policy checks passed")


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

#: The order the ledger reports entries in: the declaration order of the
#: contract's own :class:`BlockedReasonCode`, which runs from the concrete data
#: problems to the commercial ones. The caller's order never matters.
_CODE_ORDER: Mapping[BlockedReasonCode, int] = {
    code: index for index, code in enumerate(BlockedReasonCode)
}

#: The ledger message limit, taken from :class:`BlockedReason`.
_MAX_BLOCKED_MESSAGE = 300

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


def project_blocked_ledger(
    calculation: QuoteCalculation,
    *,
    run_id: RunId,
    customer_on_credit_hold: bool = False,
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
            caller from the customer record. Never looked up or assumed here.

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


def _credit_hold_reasons(customer_on_credit_hold: bool) -> list[BlockedReason]:
    """One ``CREDIT_HOLD`` entry, from the fact the caller supplied."""
    if not customer_on_credit_hold:
        return []
    return [
        BlockedReason(
            code=BlockedReasonCode.CREDIT_HOLD,
            message=_CREDIT_HOLD_MESSAGE,
        )
    ]
