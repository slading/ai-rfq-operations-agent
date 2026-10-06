"""Quote schemas (architecture §5.2).

The quote is the one artefact that reaches a customer, so its schema enforces
the arithmetic instead of trusting it:

* ``total`` must equal subtotal minus discount, to the cent;
* ``subtotal`` must equal the sum of the line extensions;
* every line must carry the same currency as the quote;
* a blocked line makes the whole quote non-finalisable.

Phase 1 computes these values. Phase 0 makes an inconsistent quote
unrepresentable.

:func:`calculate_quote` (Phase 1H) is the only place a monetary amount is worked
out, and it is arithmetic over facts that other phases already established: the
quantity from the resolved line, the unit price from a price selection, the
discount from a discount selection. It chooses nothing. Its rules are the ones
the validators above enforce, applied in one direction only - quantise every
amount to cents with :func:`_round_money` (the module's single rounding rule),
extend each line as ``quantity x unit_price``, sum the extensions into the
subtotal, apply the selected rule's percentage to that subtotal, and subtract.
A line whose price lookup did not return ``FOUND`` is given no price and no
amount at all: it is blocked, it says why in machine-readable terms, and it is
never totalled.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.delivery import DeliveryAssessment
from rfq_agent.domain.ids import CustomerId, PriceEntryId, ProductId, QuoteId, QuoteNumber
from rfq_agent.domain.policy import DiscountApplication
from rfq_agent.domain.pricing import (
    Money,
    PriceLookupReason,
    PriceLookupStatus,
    PriceRef,
    PriceSelection,
)
from rfq_agent.domain.stock import StockStatus
from rfq_agent.domain.values import DomainModel, canonical_json, money_field, sha256_text

__all__ = [
    "CALC_VERSION",
    "Quote",
    "QuoteCalculation",
    "QuoteLine",
    "QuoteLineInput",
    "QuoteLineRefusal",
    "QuoteStatus",
    "calculate_quote",
    "quote_inputs_fingerprint",
]

#: Version of the deterministic calculator that produced a quote. Bumped when
#: the calculation changes, so a historical quote can always be reproduced.
CALC_VERSION = "calc-v1"

_QUANT = Decimal("0.01")
_MAX_LINES = 50
_CURRENCY_PATTERN = r"^[A-Z]{3}$"


class QuoteStatus(StrEnum):
    """Quote lifecycle (architecture §5.2, §6)."""

    DRAFT = "DRAFT"
    READY = "READY"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"


#: Quote statuses that cannot be left.
TERMINAL_QUOTE_STATUSES: frozenset[QuoteStatus] = frozenset(
    {QuoteStatus.REJECTED, QuoteStatus.SENT}
)


def _round_money(value: Decimal) -> Decimal:
    """Quantise a monetary amount to cents using half-up rounding."""
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


class QuoteLine(DomainModel):
    """One priced line of a quote."""

    ordinal: Annotated[int, Field(ge=1)]
    product_id: ProductId
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    quantity: Annotated[int, Field(ge=1, le=1_000_000)]
    unit_price: money_field(decimal_places=4)
    #: The exact price-book row this unit price came from.
    price_entry_id: PriceEntryId
    line_extension: Money
    currency: Annotated[str, StringConstraints(pattern=_CURRENCY_PATTERN)]
    stock_status: StockStatus = StockStatus.UNKNOWN
    price_status: PriceLookupStatus = PriceLookupStatus.FOUND
    blocked: bool = False
    blocked_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    notes: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None

    @model_validator(mode="after")
    def _check_arithmetic_and_blocking(self) -> Self:
        """Extension must equal qty x unit price; blocked lines must say why."""
        expected = _round_money(Decimal(self.quantity) * self.unit_price)
        if self.line_extension != expected:
            msg = f"line_extension {self.line_extension} != quantity*unit_price {expected}"
            raise ValueError(msg)

        if self.blocked and self.blocked_reason is None:
            msg = "blocked_reason is required when blocked is true"
            raise ValueError(msg)
        if not self.blocked and self.blocked_reason is not None:
            msg = "blocked_reason must be None when blocked is false"
            raise ValueError(msg)

        unusable_price = self.price_status is not PriceLookupStatus.FOUND
        if unusable_price and not self.blocked:
            msg = "a line with a non-FOUND price status must be blocked"
            raise ValueError(msg)
        if self.stock_status is StockStatus.NONE and not self.blocked:
            msg = "a line with no stock must be blocked"
            raise ValueError(msg)
        return self


class Quote(DomainModel):
    """A computed quotation, ready for human review."""

    quote_id: QuoteId
    quote_number: QuoteNumber
    run_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    #: A quote always has a bound customer; an unbound request never gets here.
    customer_id: CustomerId
    currency: Annotated[str, StringConstraints(pattern=_CURRENCY_PATTERN)]
    lines: Annotated[tuple[QuoteLine, ...], Field(min_length=1, max_length=_MAX_LINES)]
    subtotal: Money
    discount: DiscountApplication | None = None
    discount_amount: Money = Decimal("0")
    total: Money
    pricing_as_of: date
    calc_version: str = CALC_VERSION
    status: QuoteStatus = QuoteStatus.DRAFT
    delivery: DeliveryAssessment | None = None
    #: Fingerprint of the exact inputs the calculator consumed.
    inputs_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _check_totals(self) -> Self:
        """Enforce the arithmetic contract of the whole quote."""
        expected_subtotal = _round_money(
            sum((line.line_extension for line in self.lines), Decimal(0))
        )
        if self.subtotal != expected_subtotal:
            msg = f"subtotal {self.subtotal} != sum of line extensions {expected_subtotal}"
            raise ValueError(msg)

        expected_total = _round_money(self.subtotal - self.discount_amount)
        if self.total != expected_total:
            msg = f"total {self.total} != subtotal - discount {expected_total}"
            raise ValueError(msg)

        for line in self.lines:
            if line.currency != self.currency:
                msg = f"line {line.ordinal} currency {line.currency} != quote currency"
                raise ValueError(msg)
            if self.discount is None and self.discount_amount != 0:
                msg = "discount_amount must be zero when no discount is applied"
                raise ValueError(msg)

        ordinals = [line.ordinal for line in self.lines]
        if sorted(ordinals) != list(range(1, len(self.lines) + 1)):
            msg = "line ordinals must be 1..N without gaps"
            raise ValueError(msg)
        return self

    @property
    def blocked_lines(self) -> tuple[QuoteLine, ...]:
        """Lines that prevent this quote from being sent as-is."""
        return tuple(line for line in self.lines if line.blocked)

    @property
    def is_sendable(self) -> bool:
        """Whether the quote itself is in a state a human could approve."""
        return not self.blocked_lines and self.status in {
            QuoteStatus.READY,
            QuoteStatus.APPROVED,
        }

    def inputs_fingerprint(self) -> str:
        """Stable fingerprint of the quote's inputs (excludes status/timestamps).

        Two runs over identical business data must produce an identical
        fingerprint - that is the deterministic-reproducibility assertion the
        eval suite checks (architecture §9.2).
        """
        return quote_inputs_fingerprint(self)


def quote_inputs_fingerprint(quote: Quote) -> str:
    """Compute :attr:`Quote.inputs_fingerprint` for an arbitrary quote."""
    payload = {
        "customer_id": quote.customer_id,
        "currency": quote.currency,
        "pricing_as_of": quote.pricing_as_of.isoformat(),
        "calc_version": quote.calc_version,
        "lines": [
            {
                "ordinal": line.ordinal,
                "product_id": line.product_id,
                "quantity": line.quantity,
                "unit_price": str(line.unit_price),
                "price_entry_id": line.price_entry_id,
            }
            for line in quote.lines
        ],
        "discount_rule_id": quote.discount.rule_id if quote.discount else None,
    }
    return sha256_text(canonical_json(payload))


# ---------------------------------------------------------------------------
# Calculation (Phase 1H)
# ---------------------------------------------------------------------------
#
# Arithmetic, and nothing else: every fact this needs - the quantity, the unit
# price, the stock status, the selected discount - arrives already decided. The
# two rules that make the arithmetic reproducible are the module's own:
# every monetary amount is quantised to cents with half-up rounding, and the
# discount applies to the subtotal the contract defines (`total` is
# `subtotal - discount_amount`, so there is nowhere else it could apply).

#: The price-entry id a refused line carries. Matches :meth:`PriceRef.missing`,
#: which uses the same marker for the same reason: a blocked line has no price
#: row, and the id says so rather than pointing at one that was never used.
MISSING_PRICE_ENTRY_ID = "PRICE_MISSING"
#: Money is quantised against this, in :func:`_round_money`.
_HUNDRED = Decimal("100")
#: Contract limits, named so a generated message can be kept inside them.
_MAX_BLOCKED_REASON = 200
_MAX_DETAIL = 400


def _fit(text: str, limit: int) -> str:
    """Keep a generated message inside a contract limit, without splitting a word."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _extension(quantity: int, unit_price: Decimal) -> Decimal:
    """Extend a line: ``quantity x unit_price``, quantised to cents.

    The same rule :class:`QuoteLine` validates against, so the calculator cannot
    produce a line the contract would reject.
    """
    return _round_money(Decimal(quantity) * unit_price)


def _discount_amount(subtotal: Decimal, discount: DiscountApplication | None) -> Decimal:
    """The amount the selected rule earns on this subtotal, to the cent.

    No rule means no amount: the contract forbids a non-zero discount without a
    rule, and a rule that was selected with a zero percent earns zero - which is
    a decision the data made, not a missing fact.

    The percentage applies to the *subtotal*: a quote carries one discount, the
    lines carry none, and ``total = subtotal - discount_amount`` leaves nowhere
    else for it to apply. Dividing by 100 is exact in ``Decimal``, so the only
    rounding is the quantisation to cents every amount in the quote gets.
    """
    if discount is None:
        return Decimal("0.00")
    return _round_money(subtotal * discount.percent / _HUNDRED)


def _price_blocked_reason(selection: PriceSelection) -> str:
    """Say, code first, why no money was computed for a line.

    The machine-readable cause leads, so it survives the contract's 200-character
    limit whatever the pricing detail says; the full sentence stays on the
    calculation's refusal entry.
    """
    reason = selection.reason.value if selection.reason is not None else selection.status.value
    return _fit(f"{selection.status.value}/{reason}: {selection.detail}", _MAX_BLOCKED_REASON)


def _stock_blocked_reason() -> str:
    """Say, code first, why a priced line cannot be quoted as it stands."""
    return f"{StockStatus.NONE.value}: no available stock covers this line"


class QuoteLineInput(DomainModel):
    """One resolved line, ready for arithmetic.

    Everything here is a fact another phase established: what was asked for, and
    what the deterministic selectors answered. ``price`` is Phase 1D's
    :class:`~rfq_agent.domain.pricing.PriceSelection`, so a line that could not be
    priced arrives with its machine-readable reason attached instead of a number.
    """

    product_id: ProductId
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    quantity: Annotated[int, Field(ge=1, le=1_000_000)]
    #: The price selected for exactly this product and this quantity.
    price: PriceSelection
    #: Phase 1E's outcome for this line, carried as a fact. Only ``NONE`` blocks.
    stock_status: StockStatus = StockStatus.UNKNOWN
    notes: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None


class QuoteLineRefusal(DomainModel):
    """A line the calculator refused to total, and why.

    ``NONE`` was used for a price that could not be used: the line carries no
    price, its extension is zero, and it is blocked. This is the machine-readable
    record of that; ``detail`` is the pricing selection's own sentence.
    """

    ordinal: Annotated[int, Field(ge=1)]
    product_id: ProductId
    status: PriceLookupStatus
    reason: PriceLookupReason
    detail: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_DETAIL)]

    @model_validator(mode="after")
    def _check_refusal_names_a_failure(self) -> Self:
        """A refusal is always about a price that could not be used."""
        if self.status is PriceLookupStatus.FOUND:
            msg = "a refusal names a non-FOUND price status"
            raise ValueError(msg)
        return self


class QuoteCalculation(DomainModel):
    """The arithmetic answer for one quote: the quote, and what it refused.

    The quote is always returned. A line without a usable price is represented
    rather than hidden - blocked, unpriced, not totalled - because that is what
    an operator needs to see, and the contract can say it. ``refusals`` is the
    machine-readable summary of exactly those lines, so a caller can tell whether
    the quote is complete without re-deriving the arithmetic.
    """

    quote: Quote
    refusals: tuple[QuoteLineRefusal, ...] = ()
    detail: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_DETAIL)]

    @model_validator(mode="after")
    def _check_refusals_match_the_quote(self) -> Self:
        """Refusals must be exactly the quote's un-priced lines, in ordinal order."""
        stated = [refusal.ordinal for refusal in self.refusals]
        un_priced = sorted(
            line.ordinal
            for line in self.quote.lines
            if line.price_status is not PriceLookupStatus.FOUND
        )
        if stated != un_priced:
            msg = (
                f"refusals {stated} must name exactly the lines whose price is not "
                f"FOUND {un_priced}"
            )
            raise ValueError(msg)

        by_ordinal = {line.ordinal: line for line in self.quote.lines}
        for refusal in self.refusals:
            line = by_ordinal[refusal.ordinal]
            if not line.blocked or line.price_status is not refusal.status:
                msg = f"line {refusal.ordinal} must be blocked with status {refusal.status}"
                raise ValueError(msg)
            if line.product_id != refusal.product_id:
                msg = f"line {refusal.ordinal} is {line.product_id}, not {refusal.product_id}"
                raise ValueError(msg)
        return self

    @property
    def complete(self) -> bool:
        """Whether every line could be priced: nothing needs correcting."""
        return not self.refusals


def calculate_quote(
    lines: Iterable[QuoteLineInput],
    *,
    quote_id: QuoteId,
    quote_number: QuoteNumber,
    run_id: str,
    customer_id: CustomerId,
    currency: str,
    pricing_as_of: date,
    discount: DiscountApplication | None = None,
    delivery: DeliveryAssessment | None = None,
) -> QuoteCalculation:
    """Compute the money for a quote from facts that are already decided.

    What it does, in order, using the contract's own rules:

    1. refuse the inputs that cannot describe a quote at all (no lines, more than
       :data:`_MAX_LINES`, or a line whose price was selected for a different
       product, quantity, currency or pricing date - that price is not this
       line's price, and re-selecting it is not arithmetic);
    2. extend every line that has a usable price: ``quantity x unit_price``,
       quantised to cents, keeping the price row's id as provenance;
    3. a line whose price lookup did not return ``FOUND`` gets **no** price and
       **no** amount: a zero placeholder, ``blocked``, ``price_status`` from the
       lookup, and a code-first reason. It is not totalled, and the quote says so;
    4. sum the extensions into the subtotal, apply the selected rule's percentage
       to that subtotal, and subtract - ``total = subtotal - discount_amount``,
       all to the cent.

    It does not select anything, gate anything or decide anything: ``discount``
    is applied exactly as given, ``requires_approval`` stays a fact the quote
    carries, the status stays the contract's default ``DRAFT``, and no stock,
    delivery or policy decision is taken.

    Args:
        lines: The resolved lines, in the order they should appear. Ordinals are
            assigned 1..N from this order; the caller's sequence is not modified.
        quote_id: The quote's identifier.
        quote_number: The human-visible reference.
        run_id: The run this quote belongs to.
        customer_id: The customer the quote is bound to.
        currency: The quote's currency; every usable price must be in it.
        pricing_as_of: The date the prices were selected for. Every line's price
            must have been selected for exactly this date, or the quote could not
            be replayed.
        discount: The already-selected rule, or ``None`` for no discount.
        delivery: Phase 1F's assessment, carried through unchanged when present.

    Returns:
        A :class:`QuoteCalculation`: the :class:`Quote` with its monetary facts,
        plus one :class:`QuoteLineRefusal` per line that was not totalled.

    Raises:
        ValueError: If no lines were supplied, more than :data:`_MAX_LINES` were,
            or a line's price selection does not belong to that line.
        pydantic.ValidationError: If the resulting quote would violate the quote
            contract (an impossible outcome, and a defect if it happens).
    """
    items = tuple(lines)
    if not items:
        msg = "a quote needs at least one line: there is nothing to calculate"
        raise ValueError(msg)
    if len(items) > _MAX_LINES:
        msg = f"a quote carries at most {_MAX_LINES} lines, got {len(items)}"
        raise ValueError(msg)

    quote_lines: list[QuoteLine] = []
    refusals: list[QuoteLineRefusal] = []
    for ordinal, item in enumerate(items, start=1):
        _refuse_foreign_selection(item, currency=currency, pricing_as_of=pricing_as_of)
        reference = item.price.price
        if reference is None:
            quote_lines.append(_refused_line(item, ordinal=ordinal, currency=currency))
            refusals.append(
                QuoteLineRefusal(
                    ordinal=ordinal,
                    product_id=item.product_id,
                    status=item.price.status,
                    reason=_reason_of(item.price),
                    detail=item.price.detail,
                )
            )
            continue
        quote_lines.append(_priced_line(item, reference, ordinal=ordinal))

    subtotal = _round_money(sum((line.line_extension for line in quote_lines), Decimal(0)))
    discount_amount = _discount_amount(subtotal, discount)
    total = _round_money(subtotal - discount_amount)

    quote = Quote(
        quote_id=quote_id,
        quote_number=quote_number,
        run_id=run_id,
        customer_id=customer_id,
        currency=currency,
        lines=tuple(quote_lines),
        subtotal=subtotal,
        discount=discount,
        discount_amount=discount_amount,
        total=total,
        pricing_as_of=pricing_as_of,
        calc_version=CALC_VERSION,
        status=QuoteStatus.DRAFT,
        delivery=delivery,
    )
    #: The contract stores the fingerprint of the inputs the calculation consumed,
    #: and it can only be taken once the quote exists.
    quote = quote.model_copy(update={"inputs_sha256": quote.inputs_fingerprint()})
    return QuoteCalculation(
        quote=quote,
        refusals=tuple(refusals),
        detail=_calculation_detail(quote, refused=len(refusals)),
    )


def _reason_of(selection: PriceSelection) -> PriceLookupReason:
    """The machine-readable cause out of a non-FOUND selection.

    The selection contract guarantees a reason whenever the status is not
    ``FOUND``; a selection that somehow has neither is a contradiction and is
    reported as one rather than guessed at.
    """
    reason = selection.reason
    if reason is None:
        msg = f"a {selection.status} selection must name a reason"
        raise ValueError(msg)
    return reason


def _refuse_foreign_selection(item: QuoteLineInput, *, currency: str, pricing_as_of: date) -> None:
    """Refuse a price that was selected for something other than this line.

    A price belongs to one product, one quantity and one date. Reusing it for a
    different line would silently quote a number nobody selected, so each
    mismatch is refused with the two facts that disagree.
    """
    selection = item.price
    if selection.product_id != item.product_id:
        msg = f"price was selected for {selection.product_id} but the line is {item.product_id}"
        raise ValueError(msg)
    if selection.quantity != item.quantity:
        msg = (
            f"price was selected for {selection.quantity} units but the line carries "
            f"{item.quantity}: it must be re-selected for the quantity being quoted"
        )
        raise ValueError(msg)
    if selection.as_of != pricing_as_of:
        msg = (
            f"price was selected as of {selection.as_of} but the quote is priced as of "
            f"{pricing_as_of}"
        )
        raise ValueError(msg)

    reference = selection.price
    if reference is None:
        return
    if reference.as_of != pricing_as_of:
        msg = (
            f"price entry {reference.price_entry_id} was looked up as of "
            f"{reference.as_of}, not {pricing_as_of}"
        )
        raise ValueError(msg)
    if reference.currency != currency:
        msg = (
            f"price entry {reference.price_entry_id} is in {reference.currency} but the "
            f"quote is in {currency}"
        )
        raise ValueError(msg)


def _priced_line(item: QuoteLineInput, reference: PriceRef, *, ordinal: int) -> QuoteLine:
    """One line with a usable price, extended and provenance attached."""
    return QuoteLine(
        ordinal=ordinal,
        product_id=item.product_id,
        sku=item.sku,
        description=item.description,
        quantity=item.quantity,
        unit_price=reference.unit_price,
        price_entry_id=reference.price_entry_id,
        line_extension=_extension(item.quantity, reference.unit_price),
        currency=reference.currency,
        stock_status=item.stock_status,
        price_status=PriceLookupStatus.FOUND,
        blocked=item.stock_status is StockStatus.NONE,
        blocked_reason=_stock_blocked_reason() if item.stock_status is StockStatus.NONE else None,
        notes=item.notes,
    )


def _refused_line(item: QuoteLineInput, *, ordinal: int, currency: str) -> QuoteLine:
    """One line with no usable price: blocked, unpriced, and not totalled."""
    return QuoteLine(
        ordinal=ordinal,
        product_id=item.product_id,
        sku=item.sku,
        description=item.description,
        quantity=item.quantity,
        unit_price=Decimal("0"),
        price_entry_id=MISSING_PRICE_ENTRY_ID,
        line_extension=_extension(item.quantity, Decimal("0")),
        currency=currency,
        stock_status=item.stock_status,
        price_status=item.price.status,
        blocked=True,
        blocked_reason=_price_blocked_reason(item.price),
        notes=item.notes,
    )


def _calculation_detail(quote: Quote, *, refused: int) -> str:
    """One sentence for the operator: what was computed, and what was not."""
    text = (
        f"quote {quote.quote_number}: {len(quote.lines)} line(s), subtotal "
        f"{quote.subtotal} {quote.currency}, discount {quote.discount_amount}, total "
        f"{quote.total} ({quote.calc_version})"
    )
    if quote.discount is not None:
        text += (
            f"; rule {quote.discount.rule_id} at {quote.discount.percent}% "
            f"({'needs sign-off' if quote.discount.requires_approval else 'delegated'})"
        )
    if refused:
        text += f"; {refused} line(s) have no usable price and were not totalled"
    return _fit(text, _MAX_DETAIL)
