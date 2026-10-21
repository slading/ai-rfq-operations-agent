"""Fresh RFQ acceptance: declared input in, a persisted DRAFT or a typed refusal out.

This is the composition the accepted phases left for a caller: intake stores the
request, the deterministic resolution core rules on it, the adapter verifies the
claims against the *stored* source, the quote engine decides, and the accepted
write path persists the artefacts. Every decision in that sentence belongs to
the phase that already owns it; this module decides nothing.

What it adds is the order, the boundaries and the outcome vocabulary:

* **the source is read back from the database before anything is verified.**
  ``provision_intake`` stores the declaration, and ``load_source`` rebuilds
  :class:`~rfq_agent.domain.intake.InboundRfq` from the row, digests re-checked.
  A caller therefore cannot have one body verified and a different one stored.
* **the extraction is a claim, never a fact.** The declaration carries
  :class:`~rfq_agent.domain.extraction.ExtractionResult` - the same contract a
  model's output must satisfy - and only ``to_quote_request`` decides whether it
  is supported by the source.
* **a refusal is reported, not persisted.** The schema has no state for "this
  intake was refused before quotation": ``run_events`` only accepts legal
  transition edges, ``intake_events`` has no refusal kind, and ``quotes.policy_*``
  belongs to a gate that never ran. Rather than invent one, this module returns
  the refusal as a typed outcome and stores nothing beyond the intake rows.
  That limitation is deliberate and is printed by the command.
* **the human gate is untouched.** Nothing here approves, sends, transitions a
  run, stores a gate decision or writes a human action; the quote is a ``DRAFT``
  a human must still review, and the gate's verdict is reported and discarded.

The five outcomes are the caller's vocabulary, not new business states:
``DRAFT_AWAITING_HUMAN_REVIEW`` and ``DRAFT_BLOCKED`` name what the accepted
write path stored, ``DUPLICATE`` names a replay that stored nothing new,
``HUMAN_REQUIRED_BEFORE_QUOTE`` names a pre-quote refusal, and ``CONFLICT``
names a declaration that contradicts stored history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self, TypeVar

from pydantic import Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError

from rfq_agent.domain.extraction import ExtractionResult
from rfq_agent.domain.ids import LineItemId, QuoteId, QuoteNumber
from rfq_agent.domain.intake import InboundRfq
from rfq_agent.domain.quote import QuoteLine, QuoteStatus
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.domain.values import DomainModel
from rfq_agent.intake import (
    IntakeConflictError,
    IntakeConflictKind,
    IntakeDeclaration,
    IntakeIntegrityError,
    StoredIntake,
    load_source,
    provision_intake,
)
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.persistence.writers import QuoteWriter
from rfq_agent.quote_adapter import ClaimValidationError, to_quote_request
from rfq_agent.quoting import QuoteRunStatus, run_quote
from rfq_agent.resolving import bind_customer, resolve_lines

if TYPE_CHECKING:
    from rfq_agent.persistence.engine import Database
    from rfq_agent.quoting import QuoteRequest

__all__ = [
    "AcceptanceError",
    "AcceptanceOutcome",
    "AcceptanceReason",
    "AcceptanceReport",
    "FreshRfq",
    "accept_rfq",
]

_T = TypeVar("_T")


class AcceptanceOutcome(StrEnum):
    """What one acceptance established, stated in the caller's vocabulary."""

    #: A clean calculation persisted as a DRAFT; human approval still required.
    DRAFT_AWAITING_HUMAN_REVIEW = "DRAFT_AWAITING_HUMAN_REVIEW"
    #: A calculation with blocking evidence persisted as a DRAFT; not eligible.
    DRAFT_BLOCKED = "DRAFT_BLOCKED"
    #: An identical request was already accepted; nothing new was written.
    DUPLICATE = "DUPLICATE"
    #: Resolution or the adapter refused: no quote was prepared or stored.
    HUMAN_REQUIRED_BEFORE_QUOTE = "HUMAN_REQUIRED_BEFORE_QUOTE"
    #: The declaration contradicts stored intake or quotation history.
    CONFLICT = "CONFLICT"


class AcceptanceReason(StrEnum):
    """Why an outcome stored no quote. ``None`` on a stored or duplicate outcome."""

    #: The sender's address is not a stored alias of exactly one active customer.
    CUSTOMER_NOT_EXACT = "CUSTOMER_NOT_EXACT"
    #: At least one claimed line is not ``RESOLVED``.
    LINE_NOT_RESOLVED = "LINE_NOT_RESOLVED"
    #: The adapter refused the extraction claims against the stored source.
    CLAIM_VERIFICATION_REFUSED = "CLAIM_VERIFICATION_REFUSED"
    #: The adapter refused the request for a reason none of the above names.
    REQUEST_REFUSED = "REQUEST_REFUSED"
    #: The identity resolved to a customer the business data does not hold.
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    #: The declaration reuses stored intake identity with different content.
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    #: Stored intake or quotation history no longer supports this request.
    STORED_STATE_CONFLICT = "STORED_STATE_CONFLICT"


class AcceptanceError(Exception):
    """An internal invariant of the acceptance composition was violated."""


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    """Everything one acceptance established, ready for a caller to print.

    A report never carries untrusted content: no body text, no subject, no
    sender address. The fields are identifiers, enum values, amounts and fixed
    sentences, so printing a report cannot echo customer input.
    """

    outcome: AcceptanceOutcome
    rfq_id: str
    run_id: str
    #: ``True`` only when this call wrote the intake rows.
    created: bool = False
    #: The resolution core's ruling on the sender, when it was reached.
    customer_status: CustomerMatchStatus | None = None
    #: One ruling per claimed line, in source order.
    line_statuses: tuple[ResolutionMatchStatus, ...] = ()
    #: The quote the write path stored, or found already stored.
    quote_id: str | None = None
    quote_number: str | None = None
    quote_status: QuoteStatus | None = None
    total: Decimal | None = None
    currency: str | None = None
    line_count: int | None = None
    #: One rendered line: quantity, catalogue SKU, unit price and extension.
    line_summaries: tuple[str, ...] = ()
    subtotal: Decimal | None = None
    discount_amount: Decimal | None = None
    #: Blocking reason codes, verbatim from the accepted ledger projection.
    blocked_codes: tuple[str, ...] = ()
    blocked_messages: tuple[str, ...] = ()
    #: The gate's answer - printed, never stored.
    eligible_for_review: bool | None = None
    approval_required: bool | None = None
    #: Why nothing was stored, when nothing was.
    reason: AcceptanceReason | None = None
    #: Safe extras: enum values, ordinals or a closed error-code set.
    details: tuple[str, ...] = ()


class FreshRfq(DomainModel):
    """One fresh RFQ a caller asks this path to accept.

    ``intake`` is exactly what gets stored. The remaining fields are the
    caller's orchestration state: the identifiers the surrounding system
    assigned, the instants the deterministic questions are asked at, and the
    extraction claims. Nothing here is a business fact, and nothing here can
    populate the source that verification reads.
    """

    intake: IntakeDeclaration
    quote_id: QuoteId
    quote_number: QuoteNumber
    #: One stated identifier per claimed line, in order, unique.
    line_item_ids: Annotated[tuple[LineItemId, ...], Field(min_length=1)]
    pricing_as_of: date
    stock_as_of: datetime
    extraction: ExtractionResult
    #: The instant a stated delivery question is asked at; required exactly when
    #: the extraction states a destination.
    delivery_as_of: datetime | None = None

    @model_validator(mode="after")
    def _check_the_request_is_sane(self) -> Self:
        """Refuse a declaration that could not be answered without inventing.

        These are declaration-shape checks, not business rules: the resolver and
        the adapter still decide every substantive question.
        """
        if len(self.line_item_ids) != len(self.extraction.lines):
            msg = (
                "line_item_ids must pair one-to-one with the claimed lines: "
                f"{len(self.line_item_ids)} identifiers for {len(self.extraction.lines)} lines"
            )
            raise ValueError(msg)
        if len(set(self.line_item_ids)) != len(self.line_item_ids):
            msg = "line_item_ids must be unique: identity is stated, never generated"
            raise ValueError(msg)
        if self.stock_as_of.utcoffset() is None:
            msg = "stock_as_of must be timezone-aware: stock age is judged against this instant"
            raise ValueError(msg)
        if self.delivery_as_of is not None and self.delivery_as_of.utcoffset() is None:
            msg = "delivery_as_of must be timezone-aware: cut-offs are UTC"
            raise ValueError(msg)
        if (
            self.extraction.requested_delivery.destination is not None
            and self.delivery_as_of is None
        ):
            msg = "a stated delivery destination needs the instant it is asked at"
            raise ValueError(msg)
        return self


def accept_rfq(request: FreshRfq, database: Database) -> AcceptanceReport:
    """Store the intake, verify the claims and persist a DRAFT or refuse.

    The order is the contract:

    1. the declaration is written and committed, then the source is read back
       from the database - verification never runs against the caller's copy;
    2. the resolver binds the customer and rules on every claimed line;
    3. the adapter verifies the claims against the stored source and builds the
       quote request, or refuses it;
    4. the quote engine decides over the business data, and the accepted write
       path stores the calculation and its ledger in one transaction.

    Every refusal is a typed outcome. Nothing is persisted for a refusal beyond
    the intake rows the first step already committed, and nothing here writes a
    gate decision, a run transition, an approval or an outbound message.

    Args:
        request: The declaration: content, identity, claims and instants.
        database: A migrated and seeded database.

    Returns:
        A report naming what was stored, reused or refused.

    Raises:
        AcceptanceError: Only if a completed run's artefacts are missing, which
            the accepted contracts already make unrepresentable.
    """
    try:
        with database.session() as session:
            stored = provision_intake(session, request.intake)
    except IntakeConflictError as error:
        reason = (
            AcceptanceReason.STORED_STATE_CONFLICT
            if error.kind is IntakeConflictKind.LIFECYCLE_MOVED
            else AcceptanceReason.IDENTITY_CONFLICT
        )
        return _refused(request, AcceptanceOutcome.CONFLICT, reason)
    except IntegrityError:
        # The database's own uniqueness rules are the real guarantee; a race
        # that slips past the explicit checks lands here.
        return _refused(request, AcceptanceOutcome.CONFLICT, AcceptanceReason.IDENTITY_CONFLICT)

    with database.session_factory() as session:
        reader = BusinessReader.for_session(session)
        try:
            source = load_source(session, request.intake.rfq_id)
        except IntakeIntegrityError:
            return _refused(
                request,
                AcceptanceOutcome.CONFLICT,
                AcceptanceReason.STORED_STATE_CONFLICT,
                stored=stored,
            )
        customer, _search = bind_customer(source.sender_email, reader=reader)
        lines = resolve_lines(
            request.extraction.lines,
            line_item_ids=request.line_item_ids,
            reader=reader,
        )
        statuses = tuple(line.status for line in lines)
        try:
            quote_request = _quote_request(request, customer, lines, source)
        except ClaimValidationError as error:
            codes = tuple(sorted({issue.code.value for issue in error.issues}))
            return _refused(
                request,
                AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
                AcceptanceReason.CLAIM_VERIFICATION_REFUSED,
                stored=stored,
                customer=customer,
                statuses=statuses,
                details=codes,
            )
        except (ValidationError, ValueError):
            # Adapter messages can embed stored data; the reason is classified
            # from the rulings already taken, never from the exception text.
            return _refused(
                request,
                AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
                _refusal_reason(customer, lines),
                stored=stored,
                customer=customer,
                statuses=statuses,
                details=_refusal_details(customer, lines),
            )
        result = run_quote(quote_request, reader)
        if result.status is not QuoteRunStatus.COMPLETED:
            return _refused(
                request,
                AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
                AcceptanceReason.CUSTOMER_NOT_FOUND,
                stored=stored,
                customer=customer,
                statuses=statuses,
            )
        calculation = _require(result.calculation, "calculation")
        ledger = _require(result.ledger, "blocked ledger")
        decision = _require(result.decision, "gate decision")

    try:
        write = QuoteWriter(database).persist(calculation, ledger)
    except IntegrityError:
        # A different calculation for a run that already has one: the database
        # refuses a second revision, so this is a contradiction, not a retry.
        return _refused(
            request,
            AcceptanceOutcome.CONFLICT,
            AcceptanceReason.STORED_STATE_CONFLICT,
            stored=stored,
            customer=customer,
            statuses=statuses,
        )

    quote = calculation.quote
    blocked = bool(ledger.reasons) or not decision.eligible_for_human_review
    outcome = (
        AcceptanceOutcome.DUPLICATE
        if write.duplicate
        else (
            AcceptanceOutcome.DRAFT_BLOCKED
            if blocked
            else AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW
        )
    )
    return AcceptanceReport(
        outcome=outcome,
        rfq_id=request.intake.rfq_id,
        run_id=request.intake.run_id,
        created=stored.created,
        customer_status=customer.match_status,
        line_statuses=statuses,
        quote_id=quote.quote_id,
        quote_number=quote.quote_number,
        quote_status=quote.status,
        total=quote.total,
        currency=quote.currency,
        line_count=len(quote.lines),
        line_summaries=tuple(_line_summary(line) for line in quote.lines),
        subtotal=quote.subtotal,
        discount_amount=quote.discount_amount,
        blocked_codes=tuple(reason.code.value for reason in ledger.reasons),
        blocked_messages=tuple(
            f"{reason.code.value} - {reason.message}"
            + (f" (line {reason.line_ordinal})" if reason.line_ordinal is not None else "")
            for reason in ledger.reasons
        ),
        eligible_for_review=decision.eligible_for_human_review,
        approval_required=decision.requires_human_approval,
    )


def _quote_request(
    request: FreshRfq,
    customer: ResolvedCustomer,
    lines: tuple[ResolvedLine, ...],
    source: InboundRfq,
) -> QuoteRequest:
    """Hand the accepted adapter the stored source and the caller's claims.

    The adapter is the only component that decides whether the claims are
    supported; this function only supplies what the caller stated.
    """
    return to_quote_request(
        customer,
        lines,
        source=source,
        extraction=request.extraction,
        run_id=request.intake.run_id,
        quote_id=request.quote_id,
        quote_number=request.quote_number,
        pricing_as_of=request.pricing_as_of,
        stock_as_of=request.stock_as_of,
        delivery_as_of=request.delivery_as_of,
    )


def _refusal_reason(
    customer: ResolvedCustomer, lines: tuple[ResolvedLine, ...]
) -> AcceptanceReason:
    """Classify the adapter's refusal from the rulings already taken.

    The adapter refuses first on the customer and then on any non-``RESOLVED``
    line, so naming which of those it was requires no second copy of its rules -
    only the outcomes it was handed.
    """
    if customer.match_status is not CustomerMatchStatus.EXACT:
        return AcceptanceReason.CUSTOMER_NOT_EXACT
    if any(line.status is not ResolutionMatchStatus.RESOLVED for line in lines):
        return AcceptanceReason.LINE_NOT_RESOLVED
    return AcceptanceReason.REQUEST_REFUSED


def _refusal_details(
    customer: ResolvedCustomer, lines: tuple[ResolvedLine, ...]
) -> tuple[str, ...]:
    """Explain a refusal with enum values and ordinals only - never source text."""
    if customer.match_status is not CustomerMatchStatus.EXACT:
        return (f"customer match {customer.match_status.value} needs a human decision",)
    if any(line.status is not ResolutionMatchStatus.RESOLVED for line in lines):
        return tuple(
            f"line {line.ordinal}: {line.status.value}"
            for line in lines
            if line.status is not ResolutionMatchStatus.RESOLVED
        )
    return ()


def _line_summary(line: QuoteLine) -> str:
    """Render one accepted line: quantity, SKU, price, extension and any block."""
    rendered = (
        f"{line.quantity} x {line.sku} @ {line.unit_price} {line.currency} = {line.line_extension}"
    )
    if line.blocked:
        rendered += f" BLOCKED ({line.price_status.value}): {line.blocked_reason}"
    return rendered


def _refused(
    request: FreshRfq,
    outcome: AcceptanceOutcome,
    reason: AcceptanceReason,
    *,
    stored: StoredIntake | None = None,
    customer: ResolvedCustomer | None = None,
    statuses: tuple[ResolutionMatchStatus, ...] = (),
    details: tuple[str, ...] = (),
) -> AcceptanceReport:
    """Build the report for a request that stored no quote."""
    return AcceptanceReport(
        outcome=outcome,
        rfq_id=request.intake.rfq_id,
        run_id=request.intake.run_id,
        created=stored.created if stored is not None else False,
        customer_status=None if customer is None else customer.match_status,
        line_statuses=statuses,
        reason=reason,
        details=details,
    )


def _require(value: _T | None, name: str) -> _T:
    """Return a completed run's artefact, or fail loudly if the contract broke."""
    if value is None:
        msg = f"a completed run must carry its {name}"
        raise AcceptanceError(msg)
    return value
