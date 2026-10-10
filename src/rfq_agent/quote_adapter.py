"""The resolved-RFQ to ``QuoteRequest`` seam (Phase 1M-A).

Customer messages become extraction claims, claims become resolved facts, and
the deterministic quote run in :mod:`rfq_agent.quoting` answers exactly the
questions a :class:`~rfq_agent.quoting.QuoteRequest` asks. This module is the
pure seam between the two: it assembles a request from what the resolution
stage established and what the caller states outright, and it refuses
everything else. ``quoting.py`` and ``run_quote`` keep their accepted
contracts untouched - this module only ever *builds* their input.

Where each responsibility lives, so a reviewer can hold this module to its
boundary:

* **no new domain models.** The accepted contracts - ``ResolvedCustomer``,
  ``ResolvedLine`` and ``RequestedDelivery`` in, ``QuoteRequest`` out - are
  used exactly as the accepted phases defined them. There is no ``ExtractedRFQ``
  and no parallel line schema;
* **nothing is invented.** Every field of the request is a resolved fact, a
  claim the resolved line carries, or an explicitly stated input (the
  identifiers and instants only a caller can hold). A missing description,
  quantity or delivery destination is refused, never filled in; a requested
  delivery date is only ever one the customer stated explicitly (the
  extraction contract keeps ``INFERRED`` dates out of
  ``requested_delivery_date``, so they stay out of the question too);
* **refusals are loud.** An unquotable customer, a line that is not
  ``RESOLVED``, a missing required fact, a naive as-of instant, a requested
  delivery that cannot be asked about and a product resolved on more than
  one line all raise ``ValueError`` before any request exists. Nothing is
  silently dropped: a
  delivery request that cannot be mapped is a refusal, not a ``delivery=None``;
* **human approval is mandatory.** Every request built here carries
  ``require_human_approval=True`` and no parameter can ask for anything else -
  V1 approves nothing without a human;
* **pure.** No clock, no persistence, no minted identifier. The same facts and
  the same stated inputs produce the same request, and every instant the
  request is asked about arrives as an argument.

The caller is the orchestrator a later phase owns: it holds the identifiers
(``run_id``, ``quote_id``, ``quote_number``) and the instants (``pricing_as_of``,
``stock_as_of``, and ``delivery_as_of`` when a delivery question is asked),
and it is the caller that refuses nothing and learns nothing here - the
refusals are this module's, precisely so that "the facts were not there" can
never be discovered inside the quote engine.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from rfq_agent.domain.extraction import DateResolution, RequestedDelivery
from rfq_agent.domain.ids import CustomerId, ProductId, QuoteId, QuoteNumber, RunId
from rfq_agent.domain.resolution import (
    HUMAN_REQUIRED_CUSTOMER_STATUSES,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.quoting import DeliveryQuestion, QuoteLineRequest, QuoteRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["to_quote_request"]


def to_quote_request(
    customer: ResolvedCustomer,
    lines: Sequence[ResolvedLine],
    *,
    run_id: RunId,
    quote_id: QuoteId,
    quote_number: QuoteNumber,
    pricing_as_of: date,
    stock_as_of: datetime,
    requested_delivery: RequestedDelivery | None = None,
    delivery_as_of: datetime | None = None,
) -> QuoteRequest:
    """Assemble one quote request from resolved facts and stated inputs.

    ``customer`` and ``lines`` are the resolution stage's own outputs;
    ``requested_delivery`` is the extraction claim about delivery, carried
    separately because the resolved lines do not hold it. The identifiers and
    instants are stated by the caller - this module mints no identifier and
    reads no clock - and ``delivery_as_of`` is required exactly when the
    delivery claims map to a question.

    A request is produced only when every fact it needs is already established:
    the customer is quotable, every line is ``RESOLVED`` with a description and
    a quantity its claim recorded, no product is asked for twice, and any
    requested delivery can actually be asked about. Human approval is not an
    option here: the request is built with ``require_human_approval=True``
    because V1 approves nothing without a human.

    Raises:
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
    delivery = _to_delivery_question(
        requested_delivery,
        delivery_as_of=delivery_as_of,
        line_count=len(quote_lines),
    )
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
    decides what to offer instead. A claim without a description or a quantity
    is refused for the same reason: filling either in would invent a business
    fact. ``notes`` stays ``None``: what a resolved line carries about
    *resolution* is operator provenance, not a note on a quote line.
    """
    if line.status is not ResolutionMatchStatus.RESOLVED:
        msg = f"line {line.ordinal} is not RESOLVED ({line.status}): refusing to map it"
        raise ValueError(msg)
    extracted = line.extracted
    if extracted.description is None:
        msg = (
            f"line {line.ordinal} has no description: "
            "a quote line requires one, and the adapter never invents"
        )
        raise ValueError(msg)
    if extracted.quantity is None:
        msg = (
            f"line {line.ordinal} has no quantity: "
            "a quote line requires one, and the adapter never invents"
        )
        raise ValueError(msg)
    return QuoteLineRequest(
        product_id=line.product_id,
        sku=line.sku,
        description=extracted.description,
        quantity=extracted.quantity,
    )


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
