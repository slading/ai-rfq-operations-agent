"""Policy decisions and the quote-blocked ledger (architecture §4.2 Group B).

Two ideas live here:

1. **Policy is a function, not a prompt.** Whether a quote carries blocking
   conditions is a deterministic evaluation over typed inputs. The model is
   never asked.
2. **Blocking reasons are an explicit ledger.** Every reason a quote cannot be
   sent is a typed entry with a human-readable message, so the operator sees a
   list of concrete problems rather than a vague "needs review".

Note the deliberate separation: the policy *gate*, which evaluates a whole quote,
lives in :mod:`rfq_agent.domain.gating` (it needs the quote schema, and keeping
it there keeps the import graph acyclic). ``PolicyDecision.allowed`` means "no
unresolved business condition blocks this quote"; it never means "send it".

:class:`PolicyGateDecision` (Phase 1K) is that decision with its identity and its
evidence attached. It answers exactly one question - *is this quote eligible to
proceed to a future human review step?* - and refuses to answer ``yes`` on
anything but complete, un-contradicted evidence. It is not approval and not
sendability; a decision's :attr:`~PolicyGateDecision.eligible_for_human_review`
is the only field a consumer may act on.

:func:`select_discount` (Phase 1G) is the lookup the seeded rules describe -
"the lookup (highest priority, narrowest scope, active, in window)" - and the
only place a discount rule is chosen. It is a pure function over the read
boundary's rule rows: it names the one rule that applies, or why none does, and
it applies no percentage to any amount. Turning a rule into money is arithmetic,
and that belongs to the deterministic quote calculator.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import CustomerId, QuoteId, RunId
from rfq_agent.domain.pricing import Money
from rfq_agent.domain.values import DomainModel

if TYPE_CHECKING:
    # Named for typing only, exactly as Phase 1F does for the delivery facts:
    # the read boundary's own rows are what a caller passes in, and the domain
    # never imports the persistence layer at run time.
    from rfq_agent.persistence.read_models import DiscountRuleRecord

__all__ = [
    "BLOCKING_BLOCKED_REASONS",
    "GATE_VERSION",
    "BlockedReason",
    "BlockedReasonCode",
    "DiscountApplication",
    "DiscountReason",
    "DiscountScope",
    "DiscountSelection",
    "PolicyDecision",
    "PolicyEvidenceStatus",
    "PolicyFlag",
    "PolicyGateDecision",
    "QuoteBlockedLedger",
    "select_discount",
]


class DiscountScope(StrEnum):
    """What a discount rule applies to."""

    GLOBAL = "GLOBAL"
    CUSTOMER_TIER = "CUSTOMER_TIER"
    CUSTOMER = "CUSTOMER"


class DiscountApplication(DomainModel):
    """A discount actually applied to a quote, with its provenance."""

    rule_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    scope: DiscountScope
    percent: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2, max_digits=5)]
    #: True when the rule exceeds the delegated limit and needs sign-off.
    requires_approval: bool = False
    #: True when a human applied or adjusted it in the console.
    applied_by_human: bool = False


class PolicyFlag(StrEnum):
    """Non-blocking conditions the operator must be able to see."""

    STOCK_INSUFFICIENT = "STOCK_INSUFFICIENT"
    PARTIAL_STOCK = "PARTIAL_STOCK"
    DELIVERY_INFEASIBLE = "DELIVERY_INFEASIBLE"
    DISCONTINUED_PRODUCT = "DISCONTINUED_PRODUCT"
    PRICE_STALE = "PRICE_STALE"
    DATE_INFERRED = "DATE_INFERRED"
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
    DUPLICATE_THREAD = "DUPLICATE_THREAD"
    TOOL_RESULT_TRUNCATED = "TOOL_RESULT_TRUNCATED"
    MODEL_OUTPUT_REPAIRED = "MODEL_OUTPUT_REPAIRED"


class BlockedReasonCode(StrEnum):
    """Blocking conditions. Each one forces a human decision (§7)."""

    UNKNOWN_SKU = "UNKNOWN_SKU"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    MISSING_QTY = "MISSING_QTY"
    CUSTOMER_UNRESOLVED = "CUSTOMER_UNRESOLVED"
    CUSTOMER_AMBIGUOUS = "CUSTOMER_AMBIGUOUS"
    PRICE_MISSING = "PRICE_MISSING"
    STOCK_INSUFFICIENT = "STOCK_INSUFFICIENT"
    DELIVERY_INFEASIBLE = "DELIVERY_INFEASIBLE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DISCOUNT_OVER_POLICY = "DISCOUNT_OVER_POLICY"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
    CREDIT_HOLD = "CREDIT_HOLD"


#: Reasons that must always stop a quote, regardless of any future policy.
BLOCKING_BLOCKED_REASONS: frozenset[BlockedReasonCode] = frozenset(
    {
        BlockedReasonCode.UNKNOWN_SKU,
        BlockedReasonCode.AMBIGUOUS_MATCH,
        BlockedReasonCode.MISSING_QTY,
        BlockedReasonCode.CUSTOMER_UNRESOLVED,
        BlockedReasonCode.CUSTOMER_AMBIGUOUS,
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.CURRENCY_MISMATCH,
    }
)


class BlockedReason(DomainModel):
    """One entry in the quote-blocked ledger."""

    code: BlockedReasonCode
    message: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    #: Offending line ordinal, when the reason concerns a single line.
    line_ordinal: int | None = None
    #: Whether a human can clear it from the console (e.g. by entering a price).
    resolvable_by_human: bool = True


class PolicyDecision(DomainModel):
    """Outcome of a deterministic policy evaluation.

    ``allowed`` means "no unresolved business condition blocks this quote".
    It never means "send it": routing to a human is decided by the workflow.
    """

    allowed: bool
    reason_codes: tuple[BlockedReasonCode, ...] = ()
    #: Human-readable summary for the UI and the audit trail.
    explanation: Annotated[str, StringConstraints(min_length=1, max_length=400)] = ""
    #: True when the only thing standing between this quote and the customer is
    #: the V1 human-approval policy.
    requires_human_approval: bool = False

    @model_validator(mode="after")
    def _check_agreement(self) -> Self:
        """A refusal must name at least one reason; an approval must not."""
        if not self.allowed and not self.reason_codes:
            msg = "at least one reason_code is required when allowed is false"
            raise ValueError(msg)
        if self.allowed and self.reason_codes:
            msg = "reason_codes must be empty when allowed is true"
            raise ValueError(msg)
        if not self.allowed and self.requires_human_approval:
            msg = "requires_human_approval only applies to an otherwise-clean quote"
            raise ValueError(msg)
        return self


#: The rule set that produced a gate decision (Phase 1K). Bumped whenever the
#: gate's semantics change, so a recorded decision names the rules that made it -
#: the same device as ``CALC_VERSION`` on the quote.
GATE_VERSION = "gate-v1"


class PolicyEvidenceStatus(StrEnum):
    """How well the supplied evidence supports a gate decision (Phase 1K).

    ``COMPLETE`` is the only status under which a quote can be eligible for
    human review. The other three name *why* the evidence cannot carry the
    decision, so a defect is recorded as what it is instead of being encoded as
    a business reason code no fact supports:

    * ``INCOMPLETE`` - evidence the decision needs was not supplied or not
      established: the ledger omits a fact the quote proves, or the customer's
      credit-hold status was never stated.
    * ``CONTRADICTORY`` - two supplied facts disagree: the ledger claims a
      blocking code the quote's own facts deny, the two delivery assessments
      disagree, or one condition is reported as both blocking and non-blocking.
    * ``UNSUPPORTED`` - the inputs describe a state this rule set cannot certify:
      a quote that is already terminal, or a policy that does not require human
      approval.

    The statuses are ordered by severity - ``CONTRADICTORY`` over
    ``UNSUPPORTED`` over ``INCOMPLETE`` - and a decision reports the most severe
    condition it found.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    CONTRADICTORY = "CONTRADICTORY"
    UNSUPPORTED = "UNSUPPORTED"


class PolicyGateDecision(PolicyDecision):
    """A gate decision with its identity and its evidence attached (Phase 1K).

    One question, and only one: *is this quote eligible to proceed to a future
    human review step?* The answer is
    :attr:`~PolicyGateDecision.eligible_for_human_review`, and it is the only
    field a consumer of this decision may act on. It is **not** approval, not
    sendability, not the workflow's ``READY`` state and not a customer-visible
    claim: a quote can be eligible for review and still never leave the building.

    The inherited fields keep their accepted meanings (Phase 1I):

    * :attr:`~PolicyDecision.allowed` is "the supplied facts assert no unresolved
      business condition". It is deliberately *not* the eligibility bit: a
      contradiction between the ledger and the quote is not a condition the
      quote's facts contain, so ``allowed`` can be ``True`` while the decision is
      not eligible.
    * :attr:`~PolicyDecision.reason_codes` is the codes the inputs assert, in the
      contract's own order. Where the ledger reported the code, its own message is
      kept; where the gate asserts a fact for itself - a blocking fact the quote
      proves but the ledger omits (R2), or a caller fact no ledger entry carries -
      the message is the gate's own wording and is never presented as an original
      ledger entry.
    * :attr:`~PolicyDecision.requires_human_approval` is the V1 policy: even a
      clean quote needs a human.

    The new fields record what the answer was computed from, so the decision can
    be re-checked rather than trusted:

    * :attr:`evidence_status` - ``COMPLETE`` is required for eligibility; the
      other statuses name why the evidence cannot carry the decision;
    * :attr:`evidence_sha256` - a fingerprint of exactly the facts decided over,
      so the same facts can be shown to produce the same answer;
    * :attr:`quote_inputs_sha256` - the quote's own input fingerprint, binding
      the decision to the artefact it is about (``None`` when the quote carries
      none).

    The validators make the two fail-open shapes unrepresentable: an eligible
    decision must rest on complete, un-contradicted evidence with no asserted
    code, and a decision that is not eligible must say what stopped it - a code,
    or a named evidence defect.
    """

    quote_id: QuoteId
    run_id: RunId
    #: True only on complete, un-contradicted evidence under the V1 policy.
    eligible_for_human_review: bool
    evidence_status: PolicyEvidenceStatus
    #: The rule set that produced this decision; see :data:`GATE_VERSION`.
    gate_version: str = GATE_VERSION
    #: Fingerprint of the facts this decision was made over.
    evidence_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    #: The quote's own ``inputs_sha256``; ``None`` when it carries none.
    quote_inputs_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def _check_eligibility_agreement(self) -> Self:
        """Eligibility is implied by, and implies, the evidence it rests on."""
        if self.eligible_for_human_review:
            if self.evidence_status is not PolicyEvidenceStatus.COMPLETE:
                msg = "a quote can only be eligible for human review on COMPLETE evidence"
                raise ValueError(msg)
            if self.reason_codes:
                msg = "an eligible quote carries no blocking reason codes"
                raise ValueError(msg)
            if not self.requires_human_approval:
                msg = "eligibility requires the V1 policy: a human must approve"
                raise ValueError(msg)
        elif self.evidence_status is PolicyEvidenceStatus.COMPLETE and not self.reason_codes:
            msg = (
                "a decision that is not eligible must name a blocking reason code "
                "or an evidence defect"
            )
            raise ValueError(msg)
        return self


class QuoteBlockedLedger(DomainModel):
    """The blocking ledger attached to a run, for the UI and the audit trail."""

    run_id: RunId
    quote_id: QuoteId | None = None
    reasons: tuple[BlockedReason, ...] = ()
    flags: tuple[PolicyFlag, ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether any blocking reason is present."""
        return bool(self.reasons)

    @property
    def hard_blocked(self) -> bool:
        """Whether any reason is unconditionally blocking."""
        return any(reason.code in BLOCKING_BLOCKED_REASONS for reason in self.reasons)


# ---------------------------------------------------------------------------
# Discount selection (Phase 1G)
# ---------------------------------------------------------------------------
#
# The scope ranks, in precedence order: a negotiated rule for one customer is
# the narrowest, then the customer's tier, then the rules that apply to
# everyone. This is the "narrowest scope" half of the lookup the seeded data
# documents. A scope that is not in this table is never applied to anybody:
# product or family scoping does not exist in the contract yet, and reading an
# unknown scope as "everyone" is the one mistake that would discount the wrong
# customer.

_SCOPE_RANKS: Mapping[DiscountScope, int] = {
    DiscountScope.CUSTOMER: 1,
    DiscountScope.CUSTOMER_TIER: 2,
    DiscountScope.GLOBAL: 3,
}


class DiscountReason(StrEnum):
    """Why no discount rule applied, precisely.

    The selection either names the rule it applied or one of these - never a
    vague "no discount". Each one is a different fact about the data, and each
    one tells the operator something different to do about it: nothing exists,
    nothing is scoped to this customer, the rule is switched off, the window is
    wrong, or the order is too small to earn it.
    """

    #: No rules were supplied at all.
    NO_RULES = "NO_RULES"
    #: Rules exist, but none is scoped to this customer or its tier.
    NO_MATCHING_SCOPE = "NO_MATCHING_SCOPE"
    #: Every rule in scope is switched off.
    INACTIVE = "INACTIVE"
    #: Every rule in scope becomes valid after ``as_of``.
    NOT_YET_EFFECTIVE = "NOT_YET_EFFECTIVE"
    #: Every rule in scope had already stopped being valid on ``as_of``.
    EXPIRED = "EXPIRED"
    #: Rules are valid on ``as_of`` but every one needs a larger quantity.
    BELOW_MIN_QTY = "BELOW_MIN_QTY"
    #: Rules are valid on ``as_of`` but their order-value floor cannot be checked,
    #: because the caller did not supply the order value.
    ORDER_VALUE_UNKNOWN = "ORDER_VALUE_UNKNOWN"
    #: Rules are valid on ``as_of`` but every one needs a larger order.
    BELOW_MIN_ORDER_VALUE = "BELOW_MIN_ORDER_VALUE"


class DiscountSelection(DomainModel):
    """The single discount rule that applies to a quotation, or why none does.

    ``discount`` is set exactly when a rule applies and ``reason`` exactly when
    none does, so a caller cannot read terms out of a failed lookup, and cannot
    report a failed lookup without saying which condition caused it.

    ``requires_approval`` is carried as the fact the rule states - it is what
    routes a quote to a human - and nothing here approves, rejects or adjusts
    anything. A rule whose ``percent`` is zero is an applied rule like any other:
    the data chose to give no discount, which is a decision, not a gap.
    """

    customer_id: CustomerId | None = None
    #: The customer's tier, when the caller knew it. Tiers are not stored on the
    #: customer record, so this travels as a stated fact like pricing's does.
    customer_tier: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    quantity: Annotated[int, Field(ge=1)]
    #: The order value the floors were checked against, when it was known.
    order_value: Money | None = None
    as_of: date
    #: The rule that applies, as the quote's own value object. ``None`` when none does.
    discount: DiscountApplication | None = None
    #: Why nothing applies. ``None`` when a rule does.
    reason: DiscountReason | None = None
    #: One sentence for a human: the operator, the audit trail and the log.
    detail: Annotated[str, StringConstraints(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def _check_outcome_contract(self) -> Self:
        """A selection is either a rule with its terms, or a reason for none."""
        if (self.discount is None) == (self.reason is None):
            msg = "a selection carries either a discount or a reason, never both or neither"
            raise ValueError(msg)
        return self

    @property
    def applied(self) -> bool:
        """Whether a discount rule was selected."""
        return self.discount is not None

    @property
    def rule_id(self) -> str | None:
        """The selected rule's id, or ``None``."""
        return None if self.discount is None else self.discount.rule_id

    @property
    def percent(self) -> Decimal | None:
        """The selected rule's rate, or ``None``."""
        return None if self.discount is None else self.discount.percent

    @property
    def requires_approval(self) -> bool:
        """Whether the selected rule needs human sign-off before it may be used."""
        return self.discount is not None and self.discount.requires_approval


def select_discount(
    rules: Iterable[DiscountRuleRecord],
    *,
    as_of: date,
    quantity: int,
    customer_id: CustomerId | None = None,
    customer_tier: str | None = None,
    order_value: Decimal | None = None,
) -> DiscountSelection:
    """Select the single discount rule that applies, or say why none does.

    The precedence is the one the seeded rules describe in as many words - "the
    lookup (highest priority, narrowest scope, active, in window)":

    1. applicability, decided before precedence: ``active`` is consulted first,
       then the validity window (both ends inclusive), then the floors - so a
       switched-off rule never applies, whatever its window says, and an
       inapplicable rule at a *higher* precedence never stops a lower one from
       applying;
    2. the highest ``priority`` number - the seeded rules number the stronger
       rule higher: the order-value rule is 20 over the standard rule's 10, and
       a negotiated customer rule is 30 over both;
    3. the narrowest scope: :attr:`~DiscountScope.CUSTOMER`, then
       :attr:`~DiscountScope.CUSTOMER_TIER`, then :attr:`~DiscountScope.GLOBAL`;
    4. the lowest ``rule_id``, so the order is total even between two rules that
       agree on everything else.

    Nothing else is a tie-break. In particular the rate is not: a 2% rule listed
    first is the rule that applies, and a 7.5% rule behind it is not quietly
    preferred.

    Applicability is a question about facts, and a fact that is missing is never
    assumed. A rule with an order-value floor is not applicable when the caller
    did not supply the order value - the floor cannot be checked - and nothing
    here estimates an order value from prices, because that would be arithmetic.

    Args:
        rules: The discount rules the read boundary returned - typically every
            row in ``discount_rules``. The caller's sequence is never modified.
            Two rows with one ``rule_id`` are refused: that would mean choosing
            between two contradictory facts.
        as_of: The date the quotation is priced on. Both window ends are inclusive.
        quantity: Units on the line; must be at least 1. A line without a
            quantity never reaches a quote, so zero here is a caller error.
        customer_id: The resolved customer, when there is one. Another customer's
            negotiated rule is never a candidate.
        customer_tier: The customer's tier, when known; ``None`` means "not
            known", and no tier-scoped rule applies.
        order_value: The order value to check ``min_order_value`` against, when
            it is known. ``None`` means "not known", and a rule with a floor is
            then not applicable.

    Returns:
        A :class:`DiscountSelection`: ``applied`` with the rule as a
        :class:`DiscountApplication`, or a :class:`DiscountReason` and a
        human-readable ``detail``.

    Raises:
        ValueError: If ``quantity`` is below 1, if ``order_value`` is a float, if
            two rules share a ``rule_id``, or if a rule is scoped ``GLOBAL``
            while naming a ``scope_ref``.
    """
    if quantity < 1:
        msg = f"quantity must be at least 1, got {quantity}"
        raise ValueError(msg)
    if isinstance(order_value, float):
        msg = "order_value must be Decimal, int or str - never float"
        raise ValueError(msg)

    rule_list = tuple(rules)
    _refuse_duplicates(rule_list)
    _refuse_misdirected_global(rule_list)

    audience = _audience(customer_id, customer_tier)
    if not rule_list:
        return _nothing_applies(
            customer_id=customer_id,
            customer_tier=customer_tier,
            quantity=quantity,
            order_value=order_value,
            as_of=as_of,
            reason=DiscountReason.NO_RULES,
            candidates=(),
            audience=audience,
        )

    candidates = [
        (rank, rule)
        for rule in rule_list
        if (rank := _scope_rank(rule, customer_id=customer_id, customer_tier=customer_tier))
        is not None
    ]
    if not candidates:
        return _nothing_applies(
            customer_id=customer_id,
            customer_tier=customer_tier,
            quantity=quantity,
            order_value=order_value,
            as_of=as_of,
            reason=DiscountReason.NO_MATCHING_SCOPE,
            candidates=(),
            audience=audience,
        )

    applicable = [
        (rank, rule)
        for rank, rule in candidates
        if rule.active
        and _in_window(rule, as_of)
        and (rule.min_qty is None or quantity >= rule.min_qty)
        and (
            rule.min_order_value is None
            or (order_value is not None and order_value >= rule.min_order_value)
        )
    ]
    if applicable:
        _rank, chosen = min(applicable, key=_precedence_key)
        return DiscountSelection(
            customer_id=customer_id,
            customer_tier=customer_tier,
            quantity=quantity,
            order_value=order_value,
            as_of=as_of,
            discount=DiscountApplication(
                rule_id=chosen.rule_id,
                scope=chosen.scope,
                percent=chosen.percent,
                requires_approval=chosen.requires_approval,
            ),
            detail=_applied_detail(chosen, as_of=as_of, quantity=quantity, order_value=order_value),
        )

    reason = _diagnose(candidates, as_of=as_of, quantity=quantity, order_value=order_value)
    return _nothing_applies(
        customer_id=customer_id,
        customer_tier=customer_tier,
        quantity=quantity,
        order_value=order_value,
        as_of=as_of,
        reason=reason,
        candidates=candidates,
        audience=audience,
    )


def _refuse_duplicates(rules: Sequence[DiscountRuleRecord]) -> None:
    """Refuse two rules claiming one id: one of them would be lost silently."""
    duplicates = sorted(
        rule_id for rule_id, count in Counter(rule.rule_id for rule in rules).items() if count > 1
    )
    if duplicates:
        msg = (
            f"more than one discount rule for {', '.join(duplicates)}: "
            "which of two contradictory facts is true cannot be guessed"
        )
        raise ValueError(msg)


def _refuse_misdirected_global(rules: Sequence[DiscountRuleRecord]) -> None:
    """Refuse a rule that says it applies to everyone and to one customer at once."""
    misdirected = sorted(
        rule.rule_id
        for rule in rules
        if rule.scope is DiscountScope.GLOBAL and rule.scope_ref is not None
    )
    if misdirected:
        msg = (
            f"discount rule(s) {', '.join(misdirected)} are scoped GLOBAL but name a "
            "scope_ref: whether that means everyone or one customer cannot be guessed"
        )
        raise ValueError(msg)


def _scope_rank(
    rule: DiscountRuleRecord,
    *,
    customer_id: CustomerId | None,
    customer_tier: str | None,
) -> int | None:
    """Return the rule's precedence rank, or ``None`` when it is out of scope.

    A customer-scoped rule belongs to exactly one customer: another customer's
    negotiated discount is out of scope, never a fallback. A scope this rule does
    not know is out of scope as well, so an unfamiliar row can never discount
    everybody by accident.
    """
    rank = _SCOPE_RANKS.get(rule.scope)
    if rank is None:
        return None
    if rule.scope is DiscountScope.CUSTOMER:
        return rank if customer_id is not None and rule.scope_ref == customer_id else None
    if rule.scope is DiscountScope.CUSTOMER_TIER:
        return rank if customer_tier is not None and rule.scope_ref == customer_tier else None
    return rank


def _in_window(rule: DiscountRuleRecord, as_of: date) -> bool:
    """Whether ``as_of`` falls inside the rule's validity window, ends inclusive."""
    return rule.effective_from <= as_of and (
        rule.effective_to is None or as_of <= rule.effective_to
    )


def _precedence_key(item: tuple[int, DiscountRuleRecord]) -> tuple[int, int, str]:
    """Sort key implementing steps 2-4: priority, scope rank, rule id.

    ``priority`` is negated because the highest number is the strongest rule -
    the seeded rules number them that way, and the alternative reads the two
    rules that need a human as unreachable. ``rule_id`` is compared as text, and
    since the identifiers are zero-padded that is also their chronological order.
    """
    rank, rule = item
    return (-rule.priority, rank, rule.rule_id)


def _diagnose(
    candidates: Sequence[tuple[int, DiscountRuleRecord]],
    *,
    as_of: date,
    quantity: int,
    order_value: Decimal | None,
) -> DiscountReason:
    """Work out why none of ``candidates`` applied, in a fixed order.

    The order is: switched off, then the window (expiry before "not yet", as
    pricing's lookup has it), then the quantity floor, then the order-value
    floor. Each branch can only describe the candidates that survived the
    branches before it, so exactly one of them always applies.
    """
    live = [rule for _rank, rule in candidates if rule.active]
    if not live:
        return DiscountReason.INACTIVE
    in_window = [rule for rule in live if _in_window(rule, as_of)]
    if not in_window:
        if any(rule.effective_to is not None and rule.effective_to < as_of for rule in live):
            return DiscountReason.EXPIRED
        return DiscountReason.NOT_YET_EFFECTIVE
    if any(rule.min_qty is not None and quantity < rule.min_qty for rule in in_window):
        return DiscountReason.BELOW_MIN_QTY
    if order_value is None:
        return DiscountReason.ORDER_VALUE_UNKNOWN
    return DiscountReason.BELOW_MIN_ORDER_VALUE


def _applied_detail(
    rule: DiscountRuleRecord,
    *,
    as_of: date,
    quantity: int,
    order_value: Decimal | None,
) -> str:
    """Say which rule applies, why, and whether it needs sign-off."""
    detail = (
        f"{_scope_label(rule)} discount {rule.rule_id} applies on {as_of.isoformat()}: "
        f"{rule.percent}% for quantity {quantity}"
    )
    if rule.min_order_value is not None and order_value is not None:
        detail += f" against an order value of {order_value} (its floor is {rule.min_order_value})"
    if rule.requires_approval:
        detail += "; it exceeds the delegated limit and needs human sign-off"
    else:
        detail += "; delegated to the system"
    return detail


def _explain(
    reason: DiscountReason,
    candidates: Sequence[tuple[int, DiscountRuleRecord]],
    *,
    as_of: date,
    quantity: int,
    order_value: Decimal | None,
    audience: str,
) -> str:
    """One sentence a human can act on, built from the facts that produced it."""
    rules = [rule for _rank, rule in candidates]
    ids = ", ".join(rule.rule_id for rule in rules)
    if reason is DiscountReason.NO_RULES:
        return "no discount rule was supplied, so no discount applies"
    if reason is DiscountReason.NO_MATCHING_SCOPE:
        return f"no discount rule is scoped to {audience} on {as_of.isoformat()}"
    if reason is DiscountReason.INACTIVE:
        return f"every rule scoped to {audience} is switched off ({ids})"
    if reason is DiscountReason.EXPIRED:
        return f"every rule scoped to {audience} ended before {as_of.isoformat()} ({ids})"
    if reason is DiscountReason.NOT_YET_EFFECTIVE:
        return f"every rule scoped to {audience} starts after {as_of.isoformat()} ({ids})"
    if reason is DiscountReason.BELOW_MIN_QTY:
        floors = [
            rule.min_qty
            for rule in rules
            if rule.active and rule.min_qty is not None and quantity < rule.min_qty
        ]
        return (
            f"the rules scoped to {audience} need more than {quantity} units "
            f"(smallest floor {min(floor for floor in floors if floor is not None)})"
        )
    if reason is DiscountReason.ORDER_VALUE_UNKNOWN:
        return (
            f"the rules scoped to {audience} have order-value floors and no order value "
            "was supplied, so none of them can be checked"
        )
    floors = [
        rule.min_order_value for rule in rules if rule.active and rule.min_order_value is not None
    ]
    smallest = min(floor for floor in floors if floor is not None)
    return (
        f"the rules scoped to {audience} need an order value of at least {smallest}; "
        f"this order is {order_value}"
    )


def _scope_label(rule: DiscountRuleRecord) -> str:
    """Name the rule's audience the way an operator would say it."""
    if rule.scope is DiscountScope.CUSTOMER:
        return f"customer {rule.scope_ref}"
    if rule.scope is DiscountScope.CUSTOMER_TIER:
        return f"tier {rule.scope_ref}"
    return "standard"


def _audience(customer_id: CustomerId | None, customer_tier: str | None) -> str:
    """Describe who the selection was asked for, for the detail text."""
    parts = [f"customer {customer_id}"] if customer_id is not None else []
    if customer_tier is not None:
        parts.append(f"tier {customer_tier}")
    return " and ".join(parts) if parts else "no particular customer"


def _nothing_applies(
    *,
    customer_id: CustomerId | None,
    customer_tier: str | None,
    quantity: int,
    order_value: Decimal | None,
    as_of: date,
    reason: DiscountReason,
    candidates: Sequence[tuple[int, DiscountRuleRecord]],
    audience: str,
) -> DiscountSelection:
    """Build the selection for an outcome where no rule applies."""
    return DiscountSelection(
        customer_id=customer_id,
        customer_tier=customer_tier,
        quantity=quantity,
        order_value=order_value,
        as_of=as_of,
        reason=reason,
        detail=_explain(
            reason,
            candidates,
            as_of=as_of,
            quantity=quantity,
            order_value=order_value,
            audience=audience,
        ),
    )
