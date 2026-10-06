"""Pricing value objects and the deterministic price-selection rule (§4.2, §7 F13/F14/F16).

``get_price`` returns *price-book entries*, never a chosen number. Selecting
which entry applies is deterministic business logic, so the model can never pick
a price: :func:`select_price` is the only place a price is chosen, it is a pure
function over :class:`PriceEntry` objects, and it either names the entry it
selected or says why it selected none.

All money is :class:`~decimal.Decimal`. Floats are rejected at the schema
boundary because a quotation total that is off by a cent is a defect, not a
rounding detail.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import CustomerId, PriceEntryId, ProductId
from rfq_agent.domain.values import DomainModel, money_field

__all__ = [
    "LIST_TIER",
    "Money",
    "PriceEntry",
    "PriceLookupReason",
    "PriceLookupStatus",
    "PriceRef",
    "PriceSelection",
    "UnitPrice",
    "select_price",
]

#: Monetary amount: non-negative, at most two decimal places.
Money = money_field(decimal_places=2)
#: Unit price: four decimals so tier pricing stays precise before extension.
UnitPrice = money_field(decimal_places=4)

_CURRENCY_PATTERN = r"^[A-Z]{3}$"

#: The customer tier that carries the public list price.
#:
#: The data model has no unscoped *list price*: every entry is scoped to a
#: customer or to a tier (:class:`PriceEntry` refuses anything else), and the demo
#: business data puts its list prices in the ``STANDARD`` tier. A list price is
#: therefore a tier-scoped entry like any other, and this constant names the tier
#: :func:`select_price` falls back to. A caller with a different catalogue
#: convention passes its own ``list_tier``.
LIST_TIER: Final[str] = "STANDARD"


class PriceLookupStatus(StrEnum):
    """Outcome of a price lookup (§7 F13)."""

    FOUND = "FOUND"
    #: No entry at all. Never substituted, never defaulted to zero.
    MISSING = "MISSING"
    #: Several entries apply; a human or an explicit rule must choose.
    AMBIGUOUS = "AMBIGUOUS"
    #: An entry exists but its validity window has passed.
    EXPIRED = "EXPIRED"


class PriceEntry(DomainModel):
    """One row of a price book, exactly as stored in business data."""

    price_entry_id: PriceEntryId
    product_id: ProductId
    price_book_code: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    #: ``None`` means "applies to a customer tier", given by ``customer_tier``.
    customer_id: CustomerId | None = None
    customer_tier: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    min_qty: Annotated[int, Field(ge=1)] = 1
    unit_price: UnitPrice
    currency: Annotated[str, StringConstraints(pattern=_CURRENCY_PATTERN)]
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        """Validity windows must be ordered, and scope must be unambiguous."""
        if self.effective_to is not None and self.effective_to < self.effective_from:
            msg = "effective_to must be on or after effective_from"
            raise ValueError(msg)
        if self.customer_id is None and self.customer_tier is None:
            msg = "a price entry must be scoped to a customer or a customer tier"
            raise ValueError(msg)
        return self

    def applies_on(self, as_of: date) -> bool:
        """Whether this entry is valid on ``as_of``."""
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of <= self.effective_to

    def satisfies_quantity(self, quantity: int) -> bool:
        """Whether ``quantity`` reaches this entry's tier threshold."""
        return quantity >= self.min_qty


class PriceRef(DomainModel):
    """A price as *used* on a quote line: value plus its provenance.

    Storing ``price_entry_id`` on every line is what makes a sent quote
    auditable years later: the exact book row it came from is recoverable.
    """

    price_entry_id: PriceEntryId
    unit_price: UnitPrice
    currency: Annotated[str, StringConstraints(pattern=_CURRENCY_PATTERN)]
    min_qty: Annotated[int, Field(ge=1)] = 1
    as_of: date
    status: PriceLookupStatus = PriceLookupStatus.FOUND
    #: Set when ``status is not FOUND``; blocks the line from being quoted.
    blocked_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_status_contract(self) -> Self:
        """A non-FOUND lookup must say why, and carries no usable price."""
        found = self.status is PriceLookupStatus.FOUND
        if found and self.blocked_reason is not None:
            msg = "blocked_reason must be None when status is FOUND"
            raise ValueError(msg)
        if not found and self.blocked_reason is None:
            msg = "blocked_reason is required when status is not FOUND"
            raise ValueError(msg)
        return self

    @classmethod
    def from_entry(cls, entry: PriceEntry, *, as_of: date) -> PriceRef:
        """Build a usable reference from a validated price-book entry."""
        return cls(
            price_entry_id=entry.price_entry_id,
            unit_price=entry.unit_price,
            currency=entry.currency,
            min_qty=entry.min_qty,
            as_of=as_of,
            status=PriceLookupStatus.FOUND,
        )

    @classmethod
    def missing(cls, *, reason: str, as_of: date, currency: str) -> PriceRef:
        """Build a blocking reference for a price that could not be found."""
        return cls(
            price_entry_id="PRICE_MISSING",
            unit_price=Decimal("0"),
            currency=currency,
            as_of=as_of,
            status=PriceLookupStatus.MISSING,
            blocked_reason=reason,
        )

    @property
    def usable(self) -> bool:
        """Whether this price may be used on a quote line."""
        return self.status is PriceLookupStatus.FOUND


class PriceLookupReason(StrEnum):
    """Why a price lookup produced no usable price, precisely.

    :class:`PriceLookupStatus` says *what* the outcome is - and is what a quote
    line records; this says *why*, which is what the operator is told and what
    the tests assert on. Keeping them apart means an outcome cannot be "MISSING
    for some reason": every non-FOUND selection names the condition that caused
    it.
    """

    #: The catalogue holds no price entry for this product at all.
    NO_ENTRIES = "NO_ENTRIES"
    #: Prices exist, but none is scoped to this customer, its tier or the list tier.
    NO_MATCHING_SCOPE = "NO_MATCHING_SCOPE"
    #: Every entry in scope becomes valid after ``as_of``.
    NOT_YET_EFFECTIVE = "NOT_YET_EFFECTIVE"
    #: Every entry in scope had already stopped being valid on ``as_of``.
    EXPIRED = "EXPIRED"
    #: Entries are valid on ``as_of`` but every one needs a larger quantity.
    QUANTITY_BELOW_MIN = "QUANTITY_BELOW_MIN"
    #: Entries are valid on ``as_of`` but none is priced in the requested currency.
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


class PriceSelection(DomainModel):
    """The outcome of selecting one applicable price for one line.

    ``price`` is set exactly when a usable price was found and ``reason`` exactly
    when it was not, so a caller cannot read a number out of a failed lookup, and
    cannot report a failure without saying why.

    ``AMBIGUOUS`` is deliberately unreachable here. It exists in
    :class:`PriceLookupStatus` because a *product* can be ambiguous - one part
    number naming two pumps - but that is decided before pricing is asked
    anything, and the selection precedence below is total: it always has exactly
    one answer.
    """

    product_id: ProductId
    quantity: Annotated[int, Field(ge=1)]
    as_of: date
    status: PriceLookupStatus
    #: The selected entry, as the price a quote line may use. ``None`` when not FOUND.
    price: PriceRef | None = None
    #: Why nothing was selected. ``None`` when FOUND.
    reason: PriceLookupReason | None = None
    #: One sentence for a human: the operator, the audit trail and the log.
    detail: Annotated[str, StringConstraints(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def _check_outcome_contract(self) -> Self:
        """A selection is either usable with provenance, or blocking with a reason."""
        if self.status is PriceLookupStatus.AMBIGUOUS:
            msg = "pricing never reports AMBIGUOUS: the selection precedence is total"
            raise ValueError(msg)
        found = self.status is PriceLookupStatus.FOUND
        if found and (self.price is None or self.reason is not None):
            msg = "a FOUND selection carries a price and no reason"
            raise ValueError(msg)
        if not found and (self.price is not None or self.reason is None):
            msg = "a non-FOUND selection carries a reason and no price"
            raise ValueError(msg)
        return self

    @property
    def found(self) -> bool:
        """Whether a usable price was selected."""
        return self.status is PriceLookupStatus.FOUND


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
#
# The scope ranks, in precedence order. They are the primary key of the
# selection: a negotiated customer price outranks the customer's tier price,
# which outranks the list price. Quantity and date only ever order entries
# *within* one rank.

_CUSTOMER_SCOPE = 1
_TIER_SCOPE = 2
_LIST_SCOPE = 3


def select_price(
    entries: Iterable[PriceEntry],
    *,
    product_id: ProductId,
    quantity: int,
    as_of: date,
    customer_id: CustomerId | None = None,
    customer_tier: str | None = None,
    list_tier: str = LIST_TIER,
    currency: str | None = None,
) -> PriceSelection:
    """Select the single price that applies to one already-resolved line.

    The precedence, in order:

    1. a price scoped to ``customer_id`` - the negotiated contract price;
    2. a price scoped to ``customer_tier``;
    3. a price scoped to ``list_tier`` - the public list price;
    4. the highest ``min_qty`` that ``quantity`` still reaches;
    5. the latest ``effective_from``;
    6. the lowest ``price_entry_id``, which makes the order total.

    Applicability is decided before precedence: an entry is a candidate only if
    ``as_of`` falls inside its window, ``quantity`` reaches its ``min_qty``, and -
    when ``currency`` is given - the entry is priced in that currency. A scope
    whose entries are all inapplicable is skipped rather than allowed to stop the
    fallback, so an expired contract price does not prevent the list price from
    being quoted and a volume break that needs a larger order does not prevent
    the small-order price from being quoted. What never happens is *using* the
    inapplicable entry: an expired price is never silently chosen, and no price
    is ever invented, converted or pro-rated.

    Scope outranks quantity on purpose. A customer's negotiated price beats a
    larger list-tier volume break because substituting a list price for a
    negotiated one is a decision a human makes deliberately, not a rule an
    algorithm applies quietly.

    Nothing else is decided here: no discounts, no totals, no stock, no delivery.
    The lookup either names the entry it selected or states which
    :class:`PriceLookupReason` applies.

    Args:
        entries: Price entries to choose from - typically everything the read
            boundary returned for one product. Entries for other products are
            ignored, and the caller's sequence is never modified.
        product_id: The product already resolved for this line. Pricing is never
            asked to tell two products apart.
        quantity: Units requested; must be at least 1. A line without a quantity
            is stopped by the grounding gate, so a zero quantity here is a
            caller error rather than an outcome.
        as_of: Date the quotation is priced on. Both window ends are inclusive.
        customer_id: The resolved customer, when there is one. Another
            customer's contract price is never a candidate.
        customer_tier: The customer's tier, when known. Tiers are not stored on
            the customer record, so the caller states it; ``None`` means "not
            known", and no tier price applies.
        list_tier: The tier carrying the public list price. Defaults to
            :data:`LIST_TIER`.
        currency: When given, entries in another currency are not candidates.
            This is a filter, never a conversion.

    Returns:
        A :class:`PriceSelection`: ``FOUND`` with the selected entry's
        :class:`PriceRef`, or ``MISSING``/``EXPIRED`` with a
        :class:`PriceLookupReason` and a human-readable ``detail``.

    Raises:
        ValueError: If ``quantity`` is below 1.
    """
    if quantity < 1:
        msg = f"quantity must be at least 1, got {quantity}"
        raise ValueError(msg)

    for_product = [entry for entry in entries if entry.product_id == product_id]
    if not for_product:
        return _miss(
            product_id=product_id,
            quantity=quantity,
            as_of=as_of,
            reason=PriceLookupReason.NO_ENTRIES,
            detail=f"no price entry exists for {product_id} on {as_of.isoformat()}",
        )

    scoped = _scoped_entries(
        for_product,
        customer_id=customer_id,
        customer_tier=customer_tier,
        list_tier=list_tier,
    )
    audience = _audience(customer_id, customer_tier, list_tier)
    if not scoped:
        return _miss(
            product_id=product_id,
            quantity=quantity,
            as_of=as_of,
            reason=PriceLookupReason.NO_MATCHING_SCOPE,
            detail=(
                f"no price entry for {product_id} is scoped to {audience} on {as_of.isoformat()}"
            ),
        )

    applicable = [
        (rank, entry)
        for rank, entry in scoped
        if entry.applies_on(as_of)
        and entry.satisfies_quantity(quantity)
        and (currency is None or entry.currency == currency)
    ]
    if applicable:
        rank, chosen = min(applicable, key=_precedence_key)
        return PriceSelection(
            product_id=product_id,
            quantity=quantity,
            as_of=as_of,
            status=PriceLookupStatus.FOUND,
            price=PriceRef.from_entry(chosen, as_of=as_of),
            detail=(
                f"{_scope_label(rank)} price {chosen.price_entry_id} applies: "
                f"{chosen.unit_price} {chosen.currency} per unit at quantity "
                f"{quantity} on {as_of.isoformat()}"
            ),
        )

    # Nothing applied. Explain it at the deepest scope that had entries, because
    # that is the level the fallback reached before it ran out.
    deepest = max(rank for rank, _ in scoped)
    at_scope = [entry for rank, entry in scoped if rank == deepest]
    reason = _diagnose(at_scope, as_of=as_of, currency=currency)
    status = (
        PriceLookupStatus.EXPIRED
        if reason is PriceLookupReason.EXPIRED
        else PriceLookupStatus.MISSING
    )
    return _miss(
        product_id=product_id,
        quantity=quantity,
        as_of=as_of,
        reason=reason,
        status=status,
        detail=_explain(reason, at_scope, quantity=quantity, as_of=as_of, currency=currency),
    )


def _scoped_entries(
    entries: Iterable[PriceEntry],
    *,
    customer_id: CustomerId | None,
    customer_tier: str | None,
    list_tier: str,
) -> list[tuple[int, PriceEntry]]:
    """Return ``(rank, entry)`` for every entry scoped to this caller's audience."""
    scoped: list[tuple[int, PriceEntry]] = []
    for entry in entries:
        rank = _scope_rank(
            entry,
            customer_id=customer_id,
            customer_tier=customer_tier,
            list_tier=list_tier,
        )
        if rank is not None:
            scoped.append((rank, entry))
    return scoped


def _scope_rank(
    entry: PriceEntry,
    *,
    customer_id: CustomerId | None,
    customer_tier: str | None,
    list_tier: str,
) -> int | None:
    """Return the entry's precedence rank, or ``None`` when it is out of scope.

    A customer-scoped entry belongs to exactly one customer: another customer's
    contract price is out of scope, never a fallback.
    """
    if entry.customer_id is not None:
        return _CUSTOMER_SCOPE if entry.customer_id == customer_id else None
    if customer_tier is not None and entry.customer_tier == customer_tier:
        return _TIER_SCOPE
    if entry.customer_tier == list_tier:
        return _LIST_SCOPE
    return None


def _precedence_key(item: tuple[int, PriceEntry]) -> tuple[int, int, int, str]:
    """Sort key implementing steps 1-6: rank, then quantity, date and entry id.

    ``min_qty`` and ``effective_from`` are negated because both are wanted in
    descending order; ``price_entry_id`` is compared as text, and since the
    identifiers are zero-padded that is also their chronological order.
    """
    rank, entry = item
    return (rank, -entry.min_qty, -entry.effective_from.toordinal(), entry.price_entry_id)


def _diagnose(
    entries: list[PriceEntry],
    *,
    as_of: date,
    currency: str | None,
) -> PriceLookupReason:
    """Work out why none of ``entries`` applied, in a fixed priority order.

    The order is: wrong currency, then too small a quantity, then expired, then
    not yet effective. The first two can only describe an entry that *is* inside
    its window, and the last two only an entry that is not; an entry both past
    and future cannot exist, so exactly one branch always applies.
    """
    in_window = [entry for entry in entries if entry.applies_on(as_of)]
    if in_window:
        if currency is not None and not any(entry.currency == currency for entry in in_window):
            return PriceLookupReason.CURRENCY_MISMATCH
        return PriceLookupReason.QUANTITY_BELOW_MIN
    if any(entry.effective_to is not None and entry.effective_to < as_of for entry in entries):
        return PriceLookupReason.EXPIRED
    return PriceLookupReason.NOT_YET_EFFECTIVE


def _explain(
    reason: PriceLookupReason,
    entries: list[PriceEntry],
    *,
    quantity: int,
    as_of: date,
    currency: str | None,
) -> str:
    """Render the machine-readable reason as one sentence, deterministically."""
    by_id = sorted(entries, key=lambda entry: entry.price_entry_id)
    if reason is PriceLookupReason.EXPIRED:
        windows = ", ".join(f"{entry.price_entry_id} ended {entry.effective_to}" for entry in by_id)
        return f"{windows}; no price applies on {as_of.isoformat()}"
    if reason is PriceLookupReason.NOT_YET_EFFECTIVE:
        windows = ", ".join(
            f"{entry.price_entry_id} starts {entry.effective_from}" for entry in by_id
        )
        return f"{windows}; no price applies on {as_of.isoformat()}"
    if reason is PriceLookupReason.CURRENCY_MISMATCH:
        offered = ", ".join(sorted({entry.currency for entry in by_id}))
        return (
            f"price entries exist on {as_of.isoformat()} but are quoted in {offered}; "
            f"{currency} was requested"
        )
    required = min(entry.min_qty for entry in by_id)
    return (
        f"the smallest price break available on {as_of.isoformat()} needs "
        f"{required} units; {quantity} were requested"
    )


def _audience(
    customer_id: CustomerId | None,
    customer_tier: str | None,
    list_tier: str,
) -> str:
    """Describe whose prices count, for a message a human will read."""
    parts = [f"customer {customer_id}"] if customer_id is not None else []
    if customer_tier is not None:
        parts.append(f"tier {customer_tier}")
    parts.append(f"the {list_tier} list tier")
    return ", ".join(parts)


def _scope_label(rank: int) -> str:
    """Name a scope rank the way the business does."""
    return {
        _CUSTOMER_SCOPE: "customer",
        _TIER_SCOPE: "tier",
        _LIST_SCOPE: "list",
    }[rank]


def _miss(
    *,
    product_id: ProductId,
    quantity: int,
    as_of: date,
    reason: PriceLookupReason,
    detail: str,
    status: PriceLookupStatus = PriceLookupStatus.MISSING,
) -> PriceSelection:
    """Build a blocking selection: no price, one reason, one sentence."""
    return PriceSelection(
        product_id=product_id,
        quantity=quantity,
        as_of=as_of,
        status=status,
        reason=reason,
        detail=detail,
    )
