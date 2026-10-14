"""Verify source-backed RFQ claims before constructing a ``QuoteRequest``.

Resolution establishes customer/product identity and catalog facts; embedded
extraction remains a claim until checked against the original intake. The
adapter requires ``InboundRfq``, the complete ``ExtractionResult`` and the
resolver's ``ResolvedCustomer``/``ResolvedLine`` outputs. The quote engine's
contracts remain unchanged.

Quantities must be independently source-supported and the catalog unit must be
canonical EA; no quantity repair or unit conversion is permitted. Descriptions
come only from catalog names. Unsupported or contradictory claims refuse the
whole request rather than silently dropping lines or delivery requirements.
Only explicitly stated delivery dates can enter the delivery question; inferred
dates remain unresolved.

Every request requires human approval. This module neither approves nor
persists anything, reads no clock and mints no identifiers. The caller supplies
all identifiers and as-of instants, including ``delivery_as_of`` when a delivery
question is asked. The same stated inputs produce the same request.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from rfq_agent.domain.claim_verification import verify_extraction_claims
from rfq_agent.domain.extraction import DateResolution, ExtractionResult, RequestedDelivery
from rfq_agent.domain.ids import CustomerId, ProductId, QuoteId, QuoteNumber, RunId
from rfq_agent.domain.intake import InboundRfq
from rfq_agent.domain.resolution import (
    HUMAN_REQUIRED_CUSTOMER_STATUSES,
    GroundingErrorCode,
    GroundingIssue,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.quoting import DeliveryQuestion, QuoteLineRequest, QuoteRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["ClaimValidationError", "to_quote_request"]


class ClaimValidationError(ValueError):
    """Structured pre-quote refusal; never a quote, gate decision or approval."""

    def __init__(self, issues: tuple[GroundingIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            "; ".join(f"{issue.code}: {issue.field}: {issue.detail}" for issue in issues)
        )


def to_quote_request(
    customer: ResolvedCustomer,
    lines: Sequence[ResolvedLine],
    *,
    source: InboundRfq,
    extraction: ExtractionResult,
    run_id: RunId,
    quote_id: QuoteId,
    quote_number: QuoteNumber,
    pricing_as_of: date,
    stock_as_of: datetime,
    requested_delivery: RequestedDelivery | None = None,
    delivery_as_of: datetime | None = None,
) -> QuoteRequest:
    """Assemble one quote request from resolved facts and stated inputs.

    ``source`` is the complete original intake, supplied by trusted composition;
    ``extraction`` is the complete model claim, including delivery and questions.
    ``customer`` and ``lines`` are the deterministic resolution stage's outputs.
    The optional legacy ``requested_delivery`` must equal the full extraction;
    omitting it cannot omit verification of delivery. The identifiers and
    instants are stated by the caller - this module mints no identifier and
    reads no clock - and ``delivery_as_of`` is required exactly when the
    delivery claims map to a question.

    A request is produced only when every fact it needs is already established:
    the customer is quotable, every line is ``RESOLVED`` with a catalog name and
    canonical EA unit, and its quantity is independently supported by the source.
    No product is asked for twice, and any
    requested delivery can actually be asked about. Human approval is not an
    option here: the request is built with ``require_human_approval=True``
    because V1 approves nothing without a human.

    Raises:
        ClaimValidationError: When source, extraction or unit verification fails;
            ``issues`` carries deterministic field/code/detail/ordinal records.
        ValueError: when the resolved facts or stated inputs cannot form a
            quote request without inventing something, silently discarding
            something, or asking the engine a question it refuses.

    """
    if stock_as_of.utcoffset() is None:
        msg = "stock_as_of must be timezone-aware: stock age is judged against this instant"
        raise ValueError(msg)
    customer_id = _require_quotable_customer(customer)
    if not lines:
        msg = "a quote request asks for at least one line: none were given"
        raise ValueError(msg)
    quote_lines = tuple(_to_quote_line(line) for line in lines)
    _require_distinct_products(quote_lines)
    if requested_delivery is not None and requested_delivery != extraction.requested_delivery:
        raise ClaimValidationError(
            (
                GroundingIssue(
                    code=GroundingErrorCode.EXTRACTION_INCONSISTENT,
                    field="requested_delivery",
                    detail="separately supplied delivery differs from the complete extraction",
                ),
            )
        )
    delivery = _to_delivery_question(
        extraction.requested_delivery,
        delivery_as_of=delivery_as_of,
        line_count=len(quote_lines),
    )
    _verify_claims(source, extraction, lines)
    return QuoteRequest(
        run_id=run_id,
        quote_id=quote_id,
        quote_number=quote_number,
        customer_id=customer_id,
        lines=quote_lines,
        pricing_as_of=pricing_as_of,
        stock_as_of=stock_as_of,
        delivery=delivery,
        require_human_approval=True,
    )


def _require_quotable_customer(customer: ResolvedCustomer) -> CustomerId:
    """Return the customer a request may name, or refuse the binding.

    ``HUMAN_REQUIRED_CUSTOMER_STATUSES`` is the resolution contract's own set
    of outcomes that "require a human decision before pricing can proceed", so
    this module consults that set rather than reclassifying anything: an
    ``EXACT`` match stands as it is, and any other outcome is quotable only
    once a human made the binding (``ResolutionSource.HUMAN``). A binding with
    no ``customer_id`` at all is refused either way - a quote request names
    exactly one customer, and this module never picks one.
    """
    if customer.customer_id is None:
        msg = "customer is unresolved: a quote request names exactly one customer"
        raise ValueError(msg)
    if (
        customer.match_status in HUMAN_REQUIRED_CUSTOMER_STATUSES
        and customer.source is not ResolutionSource.HUMAN
    ):
        msg = (
            f"customer match {customer.match_status} requires a human decision "
            "before pricing can proceed"
        )
        raise ValueError(msg)
    return customer.customer_id


def _to_quote_line(line: ResolvedLine) -> QuoteLineRequest:
    """Map one resolved line onto the quote engine's line contract.

    Only a ``RESOLVED`` line is mapped. A ``DISCONTINUED`` line is refused even
    though resolution matched it: ``QuoteLineRequest`` has no way to carry
    "discontinued", so mapping one would assert an orderable product - a human
    decides what to offer instead. A missing quantity is refused; the
    quotation description comes only from the catalog name, never the claim.
    ``notes`` stays ``None``: what a resolved line carries about
    *resolution* is operator provenance, not a note on a quote line.
    """
    if line.status is not ResolutionMatchStatus.RESOLVED:
        msg = f"line {line.ordinal} is not RESOLVED ({line.status}): refusing to map it"
        raise ValueError(msg)
    extracted = line.extracted
    if extracted.quantity is None:
        msg = (
            f"line {line.ordinal} has no quantity: "
            "a quote line requires one, and the adapter never invents"
        )
        raise ValueError(msg)
    if line.catalog_uom != "EA" or line.catalog_name is None:
        raise ClaimValidationError(
            (
                GroundingIssue(
                    code=GroundingErrorCode.UNIT_UNSUPPORTED,
                    field=f"lines[ordinal={line.ordinal}].catalog_uom",
                    detail="catalog name and canonical EA unit required; no conversion permitted",
                    ordinal=line.ordinal,
                ),
            )
        )
    return QuoteLineRequest(
        product_id=line.product_id,
        sku=line.sku,
        description=line.catalog_name,
        quantity=extracted.quantity,
    )


def _verify_claims(
    source: InboundRfq, extraction: ExtractionResult, lines: Sequence[ResolvedLine]
) -> None:
    """Require exact extraction/line correspondence before source verification.

    The trusted caller supplies the source snapshot used for this run. A model
    must never populate the source or the resolver's catalog fields. This pure
    seam cannot authenticate database ownership of a caller-supplied snapshot.
    """
    if (
        tuple(line.ordinal for line in lines) != tuple(claim.ordinal for claim in extraction.lines)
        or tuple(line.extracted for line in lines) != extraction.lines
    ):
        raise ClaimValidationError(
            (
                GroundingIssue(
                    code=GroundingErrorCode.EXTRACTION_INCONSISTENT,
                    field="lines",
                    detail="resolved lines must exactly match the complete extraction in order",
                ),
            )
        )
    issues = verify_extraction_claims(source, extraction)
    if issues:
        raise ClaimValidationError(issues)


def _require_distinct_products(lines: tuple[QuoteLineRequest, ...]) -> None:
    """Refuse a request that would price the same product twice.

    Two resolved lines may legitimately point at one product - the customer
    wrote it twice - but the quote request contract prices each product on one
    line only, so its stock cannot be asked for twice. Rather than let the two
    lines collide later, the duplication is refused here, where the mapping
    created it.
    """
    seen: set[ProductId] = set()
    for line in lines:
        if line.product_id in seen:
            msg = (
                f"product {line.product_id} was resolved on more than one line: "
                "a quote request prices each product once"
            )
            raise ValueError(msg)
        seen.add(line.product_id)


def _to_delivery_question(
    requested_delivery: RequestedDelivery | None,
    *,
    delivery_as_of: datetime | None,
    line_count: int,
) -> DeliveryQuestion | None:
    """Map the extraction's delivery claims onto the quote engine's question.

    A recorded destination travels whether or not the customer asked for a
    date - ``requested_date`` only ever carries a date the customer stated
    explicitly, because the extraction contract keeps an ``INFERRED`` date out
    of ``requested_delivery_date``. A *requested* delivery (``EXPLICIT`` or
    ``INFERRED``) with no destination is refused rather than discarded: the
    question cannot be asked, and dropping the request would be a silent lie
    about what the customer asked for. The same refusal covers a delivery fact
    on a multi-line request, which the quote engine's contract cannot ask
    about. ``destination_country`` stays ``None`` ("not known"): the accepted
    contract reads it from the customer record, and this module never guesses.
    """
    if requested_delivery is None:
        return None
    destination = requested_delivery.destination
    if requested_delivery.resolution is not DateResolution.ABSENT and destination is None:
        msg = (
            "a requested delivery has no destination: "
            "refusing to discard the request or invent where the goods go"
        )
        raise ValueError(msg)
    if destination is None:
        return None
    if line_count > 1:
        msg = (
            "a delivery question is answered about one line, and the request has "
            f"{line_count}: refusing to discard the delivery request"
        )
        raise ValueError(msg)
    if delivery_as_of is None:
        msg = "a delivery question needs the instant it is asked at: none was stated"
        raise ValueError(msg)
    if delivery_as_of.utcoffset() is None:
        msg = "delivery_as_of must be timezone-aware: cut-offs are UTC"
        raise ValueError(msg)
    return DeliveryQuestion(
        destination=destination,
        as_of=delivery_as_of,
        requested_date=requested_delivery.requested_delivery_date,
    )
