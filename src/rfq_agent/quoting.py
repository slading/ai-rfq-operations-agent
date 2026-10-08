"""The deterministic quote run: business data in, accepted decisions out.

``run_quote`` is the composition the accepted phases left unbuilt. It reads the
business data a quote needs through
:class:`~rfq_agent.persistence.repositories.BusinessReader` and hands it,
unchanged, to the deterministic functions the earlier phases already define:
Phase 1D's :func:`~rfq_agent.domain.pricing.select_price`, Phase 1E's
:func:`~rfq_agent.domain.stock.evaluate_stock`, Phase 1F's
:func:`~rfq_agent.domain.delivery.evaluate_delivery`, Phase 1G's
:func:`~rfq_agent.domain.policy.select_discount`, Phase 1H's
:func:`~rfq_agent.domain.quote.calculate_quote`, Phase 1I's
:func:`~rfq_agent.domain.gating.project_blocked_ledger` and Phase 1K's
:func:`~rfq_agent.domain.gating.evaluate_quote_gate`.

Where each responsibility lives, so a reviewer can hold this module to its
boundary:

* **the decisions** are those domain functions. This module implements no
  pricing, stock, delivery, discount or policy rule of its own: it selects
  nothing, adds nothing up, renames no reason code and can approve nothing;
* **the business data** is whatever ``BusinessReader`` returns. This module
  reads through that boundary and nothing else, and never reaches a row;
* **the composition** is :func:`run_quote`: which question is asked in which
  order, which facts travel with which, and what an outcome looks like;
* **the persistence** is untouched. Nothing here opens a session, writes a row
  or stores a gate decision; the caller persists the accepted artefacts with
  ``QuoteWriter`` when it is ready to, and the gate decision stays where Phase
  1K left it - un-stored, pending the phase that owns its evidence table.

A run is deterministic **given the business data**: it reads no clock, mints no
identifier and keeps no state, so an identical request over identical data
produces an identical calculation, an identical ledger and an identical gate
decision. It is deliberately not described as *pure* - it reads the database -
and it is not a decision function either: every verdict in its output was taken
by one of the accepted domain functions above.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.delivery import DeliveryAssessment, DeliveryPromise, evaluate_delivery
from rfq_agent.domain.gating import PolicyGateInput, evaluate_quote_gate, project_blocked_ledger
from rfq_agent.domain.ids import CustomerId, ProductId, QuoteId, QuoteNumber, RunId
from rfq_agent.domain.policy import (
    DiscountApplication,
    PolicyGateDecision,
    QuoteBlockedLedger,
    select_discount,
)
from rfq_agent.domain.pricing import Money, PriceSelection, select_price
from rfq_agent.domain.quote import Quote, QuoteCalculation, QuoteLineInput, calculate_quote
from rfq_agent.domain.stock import StockEvaluation, evaluate_stock
from rfq_agent.domain.values import DomainModel
from rfq_agent.persistence.read_models import CustomerRecord
from rfq_agent.persistence.repositories import BusinessReader

__all__ = [
    "DeliveryQuestion",
    "DiscountQuestion",
    "QuoteLineRequest",
    "QuoteRequest",
    "QuoteRunResult",
    "QuoteRunStatus",
    "run_quote",
]


class QuoteLineRequest(DomainModel):
    """One resolved line, as the caller established it.

    Everything :class:`~rfq_agent.domain.quote.QuoteLineInput` carries, minus the
    two facts this run decides for itself: the price selection (Phase 1D) and the
    stock status (Phase 1E). The field limits are the accepted line contract's
    own, so a line this request accepts is a line the calculator can carry.
    """

    product_id: ProductId
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    quantity: Annotated[int, Field(ge=1, le=1_000_000)]
    notes: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None


class DeliveryQuestion(DomainModel):
    """The delivery facts the caller holds, asked about a single-line request.

    ``evaluate_delivery`` answers about one ``StockEvaluation`` - one product and
    one quantity, the request the stock rule just judged - and a quote carries
    one delivery assessment, so the question belongs to a request with exactly
    one line. ``destination_country`` is the code of the *delivery address*: the
    accepted contract reads it from the customer record, so a caller holding one
    states ``record.country_code``, and ``None`` stays "not known" and yields an
    ``UNKNOWN`` promise rather than a guess.
    """

    #: Where the goods go, as recorded - free text, carried and never parsed.
    destination: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: The instant the question is asked. Must be timezone-aware: cut-offs are UTC.
    as_of: datetime
    #: ISO 3166-1 alpha-2 code of the delivery address; ``None`` means not known.
    destination_country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    #: The date the customer asked for, when they asked for one.
    requested_date: date | None = None


class DiscountQuestion(DomainModel):
    """The discount facts the caller holds, asked about a single-line request.

    The rule's floors are checked against the line's own quantity - Phase 1G
    documents ``quantity`` as "units on the line" - and against ``order_value``
    when the caller states one. Nothing here estimates an order value from
    prices: Phase 1G refused to, because that would be arithmetic, and a rule
    whose floor cannot be checked is inapplicable rather than assumed.
    """

    order_value: Money | None = None


class QuoteRequest(DomainModel):
    """Everything one deterministic quote run is asked for, and nothing more.

    Identity, the resolved customer and lines, the instants the questions are
    asked at, and the optional questions. Nothing here is a rule: no price, no
    stock fact, no discount, no policy - those are what the run reads from the
    business data and decides with the accepted functions.
    """

    run_id: RunId
    quote_id: QuoteId
    quote_number: QuoteNumber
    customer_id: CustomerId
    lines: Annotated[tuple[QuoteLineRequest, ...], Field(min_length=1)]
    #: The date every price is selected for; the quote's own ``pricing_as_of``.
    pricing_as_of: date
    #: The instant the stock question is asked at. Stock facts are stamped, and
    #: their age is judged against this, never against a clock read here.
    stock_as_of: datetime
    #: The customer's tier, when the caller knows it. Tiers are not stored on the
    #: customer record, so they travel as a stated fact (Phase 1G).
    customer_tier: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    delivery: DeliveryQuestion | None = None
    discount: DiscountQuestion | None = None
    #: The V1 approval policy, as configuration states it. ``True`` throughout
    #: V1; anything else makes the gate report the state it cannot certify (R8)
    #: rather than let a run pass without a human.
    require_human_approval: bool = True

    @model_validator(mode="after")
    def _check_product_ids_are_unique(self) -> Self:
        """A product may appear on one line, so its stock cannot be reused twice."""
        duplicates = sorted(
            product_id
            for product_id, count in Counter(line.product_id for line in self.lines).items()
            if count > 1
        )
        if duplicates:
            product_ids = ", ".join(duplicates)
            msg = f"duplicate product_id values are not allowed across quote lines: {product_ids}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_the_questions_fit_the_lines(self) -> Self:
        """Delivery and discount are each asked about one line, or not at all.

        Both accepted selectors answer about a single position - one
        ``StockEvaluation`` for delivery, "units on the line" for discount - and
        a quote carries one delivery assessment and one discount. Where a request
        has more than one line, no accepted contract says which line the question
        is about, so the request refuses to ask rather than let the run guess.
        """
        if len(self.lines) > 1:
            for name, question in (("delivery", self.delivery), ("discount", self.discount)):
                if question is not None:
                    msg = (
                        f"a {name} question is answered for one line, and this request has "
                        f"{len(self.lines)}: ask per line, or omit the question"
                    )
                    raise ValueError(msg)
        return self


class QuoteRunStatus(StrEnum):
    """How far one run got."""

    #: Every question the request asked was answered and decided.
    COMPLETED = "COMPLETED"
    #: The request named a customer the business data does not hold. Nothing was
    #: priced, nothing was decided and nothing may be quoted.
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"


class QuoteRunResult(DomainModel):
    """What one run produced: the accepted artefacts, or why there are none.

    On :attr:`QuoteRunStatus.COMPLETED` the run hands back the customer record it
    read, the calculator's own :class:`~rfq_agent.domain.quote.QuoteCalculation`,
    the projected :class:`~rfq_agent.domain.policy.QuoteBlockedLedger` and the
    :class:`~rfq_agent.domain.policy.PolicyGateDecision`. Those are the accepted
    value objects themselves - the run wraps them rather than restating them, so
    no field here can disagree with the decision that was taken.

    On :attr:`QuoteRunStatus.CUSTOMER_NOT_FOUND` every one of them is ``None``:
    the run fails closed and says so, because a quote for a customer nobody can
    find is not a quote this system may prepare, and there is no artefact for a
    gate to decide about. Eligibility is false for that outcome by construction.
    """

    request: QuoteRequest
    status: QuoteRunStatus
    detail: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    customer: CustomerRecord | None = None
    calculation: QuoteCalculation | None = None
    ledger: QuoteBlockedLedger | None = None
    decision: PolicyGateDecision | None = None

    @model_validator(mode="after")
    def _check_the_outcome_matches_the_status(self) -> Self:
        """A completed run carries every artefact; a failed one carries none."""
        artefacts = (self.customer, self.calculation, self.ledger, self.decision)
        if self.status is QuoteRunStatus.COMPLETED and any(part is None for part in artefacts):
            msg = (
                "a completed run carries the customer, the calculation, the ledger and the decision"
            )
            raise ValueError(msg)
        if self.status is not QuoteRunStatus.COMPLETED and any(
            part is not None for part in artefacts
        ):
            msg = "a run that did not complete produced no quote, no ledger and no decision"
            raise ValueError(msg)
        return self

    @property
    def quote(self) -> Quote | None:
        """The calculated quote, or ``None`` when the run did not complete."""
        return None if self.calculation is None else self.calculation.quote

    @property
    def eligible_for_human_review(self) -> bool:
        """The gate's answer, and only when a gate decision was actually taken.

        This is the one field a consumer may act on, and on a run that decided
        nothing it is ``False`` - never a default that reads as approval.
        """
        return self.decision is not None and self.decision.eligible_for_human_review


def run_quote(request: QuoteRequest, reader: BusinessReader) -> QuoteRunResult:
    """Answer one quote request from the business data ``reader`` holds.

    The order is the order the accepted decisions require, and every step is one
    of them:

    1. read the customer record; a request naming a customer the data does not
       hold stops here, fail closed, with nothing priced and nothing decided;
    2. for each line, select the price over the entries the reader returns
       (Phase 1D) and evaluate stock over the levels it returns (Phase 1E) - the
       run supplies the facts, the selectors decide;
    3. answer the optional delivery question for the request's single line
       (Phase 1F), when the caller asked one and the stock rule covers it;
    4. select the discount (Phase 1G) for the request's single line, when the
       caller asked one - which rule applies is never derived here;
    5. calculate the quote (Phase 1H) from those facts, project the blocking
       ledger (Phase 1I) from the calculation, and decide eligibility for a
       future human review (Phase 1K) from the ledger and the customer's facts.

    What it does *not* do matters as much: it never approves, sends, transitions
    a workflow or stores anything, and it takes no decision itself - each verdict
    belongs to the accepted function that produced it. Two calls with equal
    requests and equal business data return equal results, down to the
    fingerprints the calculation and the decision carry.

    Args:
        request: The quote request: identity, resolved lines, the instants and
            the optional questions.
        reader: The read boundary. The run reads through it and nothing else; it
            neither commits nor closes the session the boundary was built with.

    Returns:
        A :class:`QuoteRunResult`: on success the calculation, the ledger and the
        gate decision; otherwise the reason no quote was prepared.

    Raises:
        ValueError: If the business data itself is one the accepted contracts
            refuse - a stock level that is naive or duplicated, a destination
            country that is not an ISO 3166-1 alpha-2 code, a reader whose price
            entries cannot form the line they were selected for. Those are data
            defects, and the accepted functions already say so.
    """
    customer = reader.customers.get(request.customer_id)
    if customer is None:
        return QuoteRunResult(
            request=request,
            status=QuoteRunStatus.CUSTOMER_NOT_FOUND,
            detail=(
                f"customer {request.customer_id} is not in the business data: "
                "no quote was priced and nothing was decided"
            ),
        )

    inputs: list[QuoteLineInput] = []
    stocks: list[StockEvaluation] = []
    for line in request.lines:
        price = _select_price(request, reader, line, currency=customer.default_currency)
        stock = evaluate_stock(
            reader.stock.levels_for_products([line.product_id]),
            product_id=line.product_id,
            requested_qty=line.quantity,
            as_of=request.stock_as_of,
        )
        stocks.append(stock)
        inputs.append(
            QuoteLineInput(
                product_id=line.product_id,
                sku=line.sku,
                description=line.description,
                quantity=line.quantity,
                price=price,
                stock_status=stock.status,
                notes=line.notes,
            )
        )

    calculation = calculate_quote(
        inputs,
        quote_id=request.quote_id,
        quote_number=request.quote_number,
        run_id=request.run_id,
        customer_id=request.customer_id,
        currency=customer.default_currency,
        pricing_as_of=request.pricing_as_of,
        discount=_select_discount(request, reader),
        delivery=_evaluate_delivery(request, reader, stocks=stocks),
    )
    ledger = project_blocked_ledger(
        calculation,
        run_id=request.run_id,
        customer_on_credit_hold=customer.credit_hold,
    )
    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=calculation.quote,
            blocked_reasons=ledger.reasons,
            flags=ledger.flags,
            delivery=_promise_of(calculation),
            customer_on_credit_hold=customer.credit_hold,
            require_human_approval=request.require_human_approval,
        )
    )
    return QuoteRunResult(
        request=request,
        status=QuoteRunStatus.COMPLETED,
        detail=_completed_detail(calculation, ledger, decision),
        customer=customer,
        calculation=calculation,
        ledger=ledger,
        decision=decision,
    )


def _select_price(
    request: QuoteRequest,
    reader: BusinessReader,
    line: QuoteLineRequest,
    *,
    currency: str,
) -> PriceSelection:
    """Ask Phase 1D for one line's price, over the entries the reader returns."""
    return select_price(
        reader.pricing.entries_for_products([line.product_id]),
        product_id=line.product_id,
        quantity=line.quantity,
        as_of=request.pricing_as_of,
        customer_id=request.customer_id,
        customer_tier=request.customer_tier,
        currency=currency,
    )


def _select_discount(request: QuoteRequest, reader: BusinessReader) -> DiscountApplication | None:
    """Ask Phase 1G the discount question, when the caller asked one.

    The selected rule travels as the rule the contract selected; which rule that
    is, and why, is Phase 1G's answer. When no rule applies the quote carries no
    discount, which is the same state the accepted pipelines pass on.
    """
    question = request.discount
    if question is None:
        return None
    # The request guarantees one line when it asks this question, and Phase 1G
    # checks the floors against "units on the line".
    selection = select_discount(
        reader.discounts.rules(),
        as_of=request.pricing_as_of,
        quantity=request.lines[0].quantity,
        customer_id=request.customer_id,
        customer_tier=request.customer_tier,
        order_value=question.order_value,
    )
    return selection.discount


def _evaluate_delivery(
    request: QuoteRequest,
    reader: BusinessReader,
    *,
    stocks: list[StockEvaluation],
) -> DeliveryAssessment | None:
    """Ask Phase 1F the delivery question, when the caller asked one.

    ``evaluate_delivery`` computes a promise only for a request stock covers, and
    that precondition is its contract's own: a position the stock rule did not
    cover gets no promise here rather than an exception, and no delivery fact is
    invented for it. Nothing is lost by that - the uncovered position is already
    a blocking fact the projected ledger reports - and a promise that could not
    be computed is never replaced by a guess.
    """
    question = request.delivery
    if question is None:
        return None
    # The request guarantees one line when it asks this question, so the question
    # is about that line's stock position.
    stock = stocks[0]
    if not stock.covered:
        return None
    return evaluate_delivery(
        stock,
        destination=question.destination,
        destination_country=question.destination_country,
        as_of=question.as_of,
        requested_date=question.requested_date,
        services=reader.delivery.services(),
        warehouses=reader.stock.warehouses(),
        holidays=reader.delivery.holidays(),
    ).assessment


def _promise_of(calculation: QuoteCalculation) -> DeliveryPromise | None:
    """The delivery promise the gate re-checks, taken from the quote itself.

    R5 compares the caller's promise with the quote's own assessment; here they
    are one and the same fact, because the run attaches the assessment it
    computed and then reports exactly that back. Passing anything else would be a
    claim about a quote this run did not build.
    """
    assessment = calculation.quote.delivery
    return None if assessment is None else assessment.promise


def _completed_detail(
    calculation: QuoteCalculation,
    ledger: QuoteBlockedLedger,
    decision: PolicyGateDecision,
) -> str:
    """One sentence naming what the run decided, from the artefacts themselves."""
    return (
        f"quote {calculation.quote.quote_number} priced from the business data: "
        f"{len(calculation.refusals)} refused of {len(calculation.quote.lines)} lines, "
        f"{len(ledger.reasons)} blocking reasons, "
        f"eligible for human review: {decision.eligible_for_human_review}"
    )
