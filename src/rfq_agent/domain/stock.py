"""Stock value objects and the deterministic availability rule (§4.2, §7 F12).

``check_stock`` returns facts: on-hand, reserved, available and inbound per
location. Deciding what to *do* about a shortfall (split-ship, backorder,
escalate) is deterministic policy, never a model judgement - and the part of it
that can be decided from the data alone lives here, in :func:`evaluate_stock`. It
answers one question: can the requested quantity be covered by what is
unreserved right now, and if not, exactly why not. It reserves nothing, ships
nothing and promises no dates.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import ProductId
from rfq_agent.domain.values import DomainModel

__all__ = [
    "LocationCode",
    "StockAvailability",
    "StockCheck",
    "StockCoverageReason",
    "StockEvaluation",
    "StockLevel",
    "StockStatus",
    "WarehouseAllocation",
    "evaluate_stock",
]

#: Warehouse identifier, e.g. ``WAW`` or ``BER``.
LocationCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
_MAX_LOCATIONS = 10


class StockStatus(StrEnum):
    """Availability of a requested quantity."""

    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    NONE = "NONE"
    #: The stock tool failed or returned no data; treated as blocking, never
    #: interpreted as "available".
    UNKNOWN = "UNKNOWN"


#: Statuses that force human review before a quote may be sent.
BLOCKING_STOCK_STATUSES: frozenset[StockStatus] = frozenset(
    {StockStatus.PARTIAL, StockStatus.NONE, StockStatus.UNKNOWN}
)


class StockLevel(DomainModel):
    """One warehouse's position for one product, at a point in time."""

    product_id: ProductId
    location: LocationCode
    on_hand_qty: Annotated[int, Field(ge=0)] = 0
    reserved_qty: Annotated[int, Field(ge=0)] = 0
    inbound_qty: Annotated[int, Field(ge=0)] = 0
    inbound_eta: date | None = None
    as_of: datetime

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """Reservations cannot exceed on-hand; inbound needs an ETA."""
        if self.reserved_qty > self.on_hand_qty:
            msg = "reserved_qty must not exceed on_hand_qty"
            raise ValueError(msg)
        if self.inbound_qty > 0 and self.inbound_eta is None:
            msg = "inbound_eta is required when inbound_qty is positive"
            raise ValueError(msg)
        return self

    @property
    def available_qty(self) -> int:
        """Unreserved stock, i.e. what could actually be promised."""
        return self.on_hand_qty - self.reserved_qty


class StockAvailability(DomainModel):
    """Aggregate availability for one requested quantity."""

    requested_qty: Annotated[int, Field(ge=1)]
    available_qty: Annotated[int, Field(ge=0)] = 0
    status: StockStatus = StockStatus.UNKNOWN
    #: Earliest date the full quantity could be available, from inbound data.
    earliest_full_availability: date | None = None

    @model_validator(mode="after")
    def _check_status_matches_quantities(self) -> Self:
        """The status must agree with the numbers, so it cannot be asserted."""
        if self.status is StockStatus.UNKNOWN:
            return self
        expected = StockStatus.SUFFICIENT
        if self.available_qty <= 0:
            expected = StockStatus.NONE
        elif self.available_qty < self.requested_qty:
            expected = StockStatus.PARTIAL
        if self.status is not expected:
            msg = f"status {self.status} inconsistent with available={self.available_qty}"
            raise ValueError(msg)
        return self

    @classmethod
    def from_levels(cls, requested_qty: int, levels: tuple[StockLevel, ...]) -> StockAvailability:
        """Aggregate per-location levels into one availability statement."""
        if not levels:
            return cls(requested_qty=requested_qty, status=StockStatus.UNKNOWN)
        available = sum(level.available_qty for level in levels)
        if available <= 0:
            status = StockStatus.NONE
        elif available < requested_qty:
            status = StockStatus.PARTIAL
        else:
            status = StockStatus.SUFFICIENT
        etas = [level.inbound_eta for level in levels if level.inbound_eta is not None]
        return cls(
            requested_qty=requested_qty,
            available_qty=available,
            status=status,
            earliest_full_availability=max(etas) if etas else None,
        )


class StockCheck(DomainModel):
    """Recorded result of one ``check_stock`` call, for the trace and the UI."""

    product_id: ProductId
    requested_qty: Annotated[int, Field(ge=1)]
    levels: Annotated[tuple[StockLevel, ...], Field(max_length=_MAX_LOCATIONS)] = ()
    availability: StockAvailability
    as_of: datetime

    @model_validator(mode="after")
    def _check_levels_agree(self) -> Self:
        """The recorded aggregate must match the recorded levels."""
        if self.levels:
            expected = sum(level.available_qty for level in self.levels)
            if self.availability.available_qty != expected and (
                self.availability.status is not StockStatus.UNKNOWN
            ):
                msg = "availability.available_qty does not match the recorded levels"
                raise ValueError(msg)
        return self


class StockCoverageReason(StrEnum):
    """Why a requested quantity is not covered, precisely.

    :class:`StockStatus` says *what* the outcome is - and is what a quote line
    records; this says *why*, so "not available" is never an unexplained verdict.
    A covered request carries no reason, because there is nothing to explain.
    """

    #: The business data holds no stock row for this product in any warehouse.
    NOT_STOCKED = "NOT_STOCKED"
    #: Rows exist, but every location has all of its stock reserved.
    ZERO_AVAILABLE = "ZERO_AVAILABLE"
    #: Unreserved stock exists, but in total it is less than requested.
    INSUFFICIENT_TOTAL = "INSUFFICIENT_TOTAL"


class WarehouseAllocation(DomainModel):
    """A quantity the data says could come from one warehouse.

    Data, not an instruction: it records which combination of positions could
    cover the request, and says nothing about whether that shipment should be
    made. Choosing a carrier, a cost or a customer-facing promise is policy that
    belongs somewhere else.
    """

    location: LocationCode
    qty: Annotated[int, Field(ge=1)]


class StockEvaluation(DomainModel):
    """Whether one requested quantity is covered, and on what evidence.

    Every number here is derived from the levels it carries, and the validators
    re-derive them: an evaluation cannot claim ``SUFFICIENT`` with nothing
    available, cannot name a covering warehouse that does not cover, and cannot
    propose a split that does not add up. That is deliberate - this object is
    what an operator reads to decide whether to ship, so it is not allowed to be
    merely plausible.

    ``UNKNOWN`` is refused for the same reason: this evaluation is built from
    facts in hand, so "the tool failed" is not one of its answers. That failure
    is reported by the tool and workflow layers, where it is blocking.
    """

    product_id: ProductId
    requested_qty: Annotated[int, Field(ge=1)]
    #: The instant the question was asked. Stock facts are stamped, and their
    #: age is judged against this, not against a clock read inside the rule.
    as_of: datetime
    status: StockStatus
    #: Unreserved stock summed over every location that stocks the product.
    available_qty: Annotated[int, Field(ge=0)]
    shortfall_qty: Annotated[int, Field(ge=0)]
    #: The evidence, one entry per warehouse, ordered by location code.
    levels: Annotated[tuple[StockLevel, ...], Field(max_length=_MAX_LOCATIONS)] = ()
    #: Locations that could cover the whole request on their own, in code order.
    covering_locations: tuple[LocationCode, ...] = ()
    #: The first covering location: a canonical choice, not a shipping preference.
    single_warehouse_cover: LocationCode | None = None
    #: A factual combination that covers the request, only when no single
    #: warehouse can. Empty when a single location already covers it.
    split: tuple[WarehouseAllocation, ...] = ()
    #: Reported, never counted towards ``available_qty``: inbound stock is not
    #: on a shelf yet, whatever its ETA says.
    inbound_qty: Annotated[int, Field(ge=0)] = 0
    earliest_inbound_eta: date | None = None
    #: Locations whose facts are older than the caller's tolerance, or dated
    #: after ``as_of``. Reported as facts; neither changes the numbers.
    stale_locations: tuple[LocationCode, ...] = ()
    future_dated_locations: tuple[LocationCode, ...] = ()
    #: Why the request is not covered. ``None`` when it is.
    reason: StockCoverageReason | None = None
    detail: Annotated[str, StringConstraints(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def _check_evaluation_is_consistent(self) -> Self:
        """Derive every claim from the levels, and refuse any that disagrees."""
        if self.status is StockStatus.UNKNOWN:
            msg = "an evaluation built from facts is never UNKNOWN"
            raise ValueError(msg)
        self._check_quantities()
        self._check_covering()
        self._check_split()
        self._check_reason()
        self._check_fact_locations()
        return self

    def _check_quantities(self) -> None:
        """Status and shortfall must follow from the levels' unreserved stock."""
        locations = [level.location for level in self.levels]
        if len(set(locations)) != len(locations):
            msg = "levels must hold at most one position per warehouse"
            raise ValueError(msg)

        available = sum(level.available_qty for level in self.levels)
        if self.available_qty != available:
            msg = f"available_qty {self.available_qty} != sum of levels {available}"
            raise ValueError(msg)

        status = _coverage_status(available, self.requested_qty)
        if self.status is not status:
            msg = f"status {self.status} inconsistent with available={available}"
            raise ValueError(msg)

        shortfall = max(0, self.requested_qty - available)
        if self.shortfall_qty != shortfall:
            msg = f"shortfall_qty {self.shortfall_qty} != {shortfall}"
            raise ValueError(msg)

    def _check_covering(self) -> None:
        """A named covering warehouse must actually cover the request alone."""
        covering = tuple(
            level.location for level in self.levels if level.available_qty >= self.requested_qty
        )
        if self.covering_locations != covering:
            msg = f"covering_locations {self.covering_locations} != {covering}"
            raise ValueError(msg)
        if self.single_warehouse_cover != (covering[0] if covering else None):
            msg = "single_warehouse_cover must be the first covering location"
            raise ValueError(msg)

    def _check_split(self) -> None:
        """A split is proposed only when it is needed, and it must add up."""
        if bool(self.split) and (
            self.covering_locations or self.status is not StockStatus.SUFFICIENT
        ):
            msg = "a split is proposed only when coverage needs more than one warehouse"
            raise ValueError(msg)
        if not self.split:
            return
        if sum(allocation.qty for allocation in self.split) != self.requested_qty:
            msg = "a split proposal must add up to the requested quantity"
            raise ValueError(msg)
        by_location = {level.location: level.available_qty for level in self.levels}
        for allocation in self.split:
            if allocation.qty > by_location.get(allocation.location, 0):
                msg = f"split takes {allocation.qty} from {allocation.location}, which has less"
                raise ValueError(msg)

    def _check_reason(self) -> None:
        """A request that is not covered must say why; one that is, must not."""
        if self.reason is None and self.status is not StockStatus.SUFFICIENT:
            msg = "a request that is not covered must say why"
            raise ValueError(msg)
        if self.reason is not None and self.status is StockStatus.SUFFICIENT:
            msg = "a covered request carries no reason"
            raise ValueError(msg)

    def _check_fact_locations(self) -> None:
        """Stale and future-dated facts must name locations that have a level."""
        named = {level.location for level in self.levels}
        for reported in (self.stale_locations, self.future_dated_locations):
            if not set(reported) <= named:
                msg = "every reported location must have a level"
                raise ValueError(msg)

    @property
    def covered(self) -> bool:
        """Whether the requested quantity is covered by unreserved stock."""
        return self.status is StockStatus.SUFFICIENT

    @property
    def requires_split(self) -> bool:
        """Whether covering the request needs more than one warehouse.

        True only when the stock is there but no single location holds all of
        it: the fact the operator needs, without a shipping decision attached.
        """
        return self.covered and not self.covering_locations


def evaluate_stock(
    levels: Iterable[StockLevel],
    *,
    product_id: ProductId,
    requested_qty: int,
    as_of: datetime,
    max_age: timedelta | None = None,
) -> StockEvaluation:
    """Decide whether ``requested_qty`` is covered, and explain the answer.

    The rule is short, and every part of it is a fact rather than a preference:

    * a location's contribution is ``StockLevel.available_qty`` - on hand minus
      reserved, and nothing else;
    * ``inbound_qty`` and ``inbound_eta`` are reported and never counted: goods
      that have not arrived are not stock, whatever the ETA says;
    * the request is covered when the unreserved total reaches it (``SUFFICIENT``),
      partially covered when some but not enough is there (``PARTIAL``), and not
      covered when there is nothing unreserved anywhere (``NONE``) - the same
      rule :meth:`StockAvailability.from_levels` applies;
    * a single warehouse covering the request is reported as a fact
      (``covering_locations``, and the first of them as
      ``single_warehouse_cover``). When the total covers the request but no
      single warehouse does, that is stated rather than decided: ``split`` holds
      a combination that would work, and whether to ship it is someone else's
      call.

    Ordering is by location code everywhere, so the answer is reproducible; the
    split proposal fills from the largest position first (ties by location code)
    so that the fewest warehouses are involved, which is a way of drawing the
    list, not a shipping preference.

    Nothing is reserved, allocated or written: the evaluation is a statement
    about data, and the same data always produces the same statement.

    Args:
        levels: The stock positions held for the product - typically exactly what
            the read boundary returned. A level for another product is ignored,
            because that is how the boundary's batch reads are shaped. Two levels
            for one warehouse are refused: that would mean choosing which of two
            contradictory facts to believe.
        product_id: The product already resolved for this line.
        requested_qty: Units requested; must be at least 1.
        as_of: The instant the question is asked. Must be timezone-aware, since
            stock facts are stamped UTC-aware and their age is judged against
            this.
        max_age: How old a stock fact may be before it is reported as stale.
            ``None`` means the caller asked no freshness question, and nothing is
            called stale. There is no default: a tolerance nobody chose would be
            a hidden policy.

    Returns:
        A :class:`StockEvaluation` carrying the status, the combined and short
        quantities, the per-warehouse evidence, any split proposal and - when the
        request is not covered - a :class:`StockCoverageReason`.

    Raises:
        ValueError: If ``requested_qty`` is below 1, if ``as_of`` or a level's
            timestamp is naive, if ``max_age`` is negative, or if two levels
            describe the same warehouse.
    """
    if requested_qty < 1:
        msg = f"requested_qty must be at least 1, got {requested_qty}"
        raise ValueError(msg)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        msg = "as_of must be timezone-aware: stock facts are stamped UTC"
        raise ValueError(msg)
    if max_age is not None and max_age < timedelta(0):
        msg = f"max_age must not be negative, got {max_age}"
        raise ValueError(msg)

    positions = sorted(
        (level for level in levels if level.product_id == product_id),
        key=lambda level: level.location,
    )
    seen: set[str] = set()
    duplicates: list[str] = []
    for level in positions:
        if level.as_of.tzinfo is None or level.as_of.utcoffset() is None:
            msg = f"the stock level for {level.location} has a naive as_of"
            raise ValueError(msg)
        if level.location in seen:
            duplicates.append(level.location)
        seen.add(level.location)
    if duplicates:
        msg = (
            f"more than one stock level for {', '.join(sorted(duplicates))}: "
            "which of two contradictory facts is true cannot be guessed"
        )
        raise ValueError(msg)

    available = sum(level.available_qty for level in positions)
    status = _coverage_status(available, requested_qty)
    covering = tuple(level.location for level in positions if level.available_qty >= requested_qty)
    etas = [level.inbound_eta for level in positions if level.inbound_eta is not None]
    split = (
        _split_proposal(positions, requested_qty)
        if status is StockStatus.SUFFICIENT and not covering
        else ()
    )
    reason = None if status is StockStatus.SUFFICIENT else _coverage_reason(positions, available)
    stale = (
        tuple(
            level.location
            for level in positions
            if max_age is not None and level.as_of < as_of - max_age
        )
        if max_age is not None
        else ()
    )

    return StockEvaluation(
        product_id=product_id,
        requested_qty=requested_qty,
        as_of=as_of,
        status=status,
        available_qty=available,
        shortfall_qty=max(0, requested_qty - available),
        levels=tuple(positions),
        covering_locations=covering,
        single_warehouse_cover=covering[0] if covering else None,
        split=split,
        inbound_qty=sum(level.inbound_qty for level in positions),
        earliest_inbound_eta=min(etas) if etas else None,
        stale_locations=stale,
        future_dated_locations=tuple(level.location for level in positions if level.as_of > as_of),
        reason=reason,
        detail=_explain(
            product_id=product_id,
            requested_qty=requested_qty,
            status=status,
            positions=positions,
            available=available,
            covering=covering,
            split=split,
            inbound_qty=sum(level.inbound_qty for level in positions),
            stale=stale,
            future_dated=tuple(level.location for level in positions if level.as_of > as_of),
        ),
    )


def _coverage_status(available: int, requested_qty: int) -> StockStatus:
    """The locked rule, in one place: nothing, not enough, or enough."""
    if available <= 0:
        return StockStatus.NONE
    if available < requested_qty:
        return StockStatus.PARTIAL
    return StockStatus.SUFFICIENT


def _coverage_reason(positions: list[StockLevel], available: int) -> StockCoverageReason:
    """Say which of the three ways a request fails to be covered happened."""
    if not positions:
        return StockCoverageReason.NOT_STOCKED
    if available <= 0:
        return StockCoverageReason.ZERO_AVAILABLE
    return StockCoverageReason.INSUFFICIENT_TOTAL


def _split_proposal(
    positions: list[StockLevel], requested_qty: int
) -> tuple[WarehouseAllocation, ...]:
    """Fill the request from the largest positions first, ties by location code."""
    remaining = requested_qty
    proposal: list[WarehouseAllocation] = []
    for level in sorted(positions, key=lambda level: (-level.available_qty, level.location)):
        if remaining <= 0:
            break
        take = min(remaining, level.available_qty)
        if take > 0:
            proposal.append(WarehouseAllocation(location=level.location, qty=take))
            remaining -= take
    return tuple(proposal)


def _explain(
    *,
    product_id: ProductId,
    requested_qty: int,
    status: StockStatus,
    positions: list[StockLevel],
    available: int,
    covering: tuple[str, ...],
    split: tuple[WarehouseAllocation, ...],
    inbound_qty: int,
    stale: tuple[str, ...],
    future_dated: tuple[str, ...],
) -> str:
    """Render the outcome as one sentence a human can act on."""
    locations = ", ".join(level.location for level in positions) or "no warehouse"
    if status is StockStatus.SUFFICIENT and covering:
        head = (
            f"{covering[0]} alone covers {requested_qty} of {product_id} "
            f"({available} unreserved in total)"
        )
    elif status is StockStatus.SUFFICIENT:
        taken = ", ".join(f"{item.qty} from {item.location}" for item in split)
        head = f"{requested_qty} of {product_id} is covered only by combining warehouses: {taken}"
    elif status is StockStatus.NONE and not positions:
        head = f"{product_id} is not stocked in any warehouse"
    elif status is StockStatus.NONE:
        head = (
            f"no unreserved stock of {product_id}: {locations} reports "
            f"{sum(level.on_hand_qty for level in positions)} units on hand, all reserved"
        )
    else:
        head = (
            f"{available} of {requested_qty} of {product_id} is available "
            f"({locations}); {requested_qty - available} short"
        )

    notes = []
    if status is not StockStatus.SUFFICIENT and inbound_qty > 0:
        notes.append(f"{inbound_qty} inbound units are not counted as available")
    if stale:
        notes.append(f"facts for {', '.join(stale)} are older than allowed")
    if future_dated:
        notes.append(f"facts for {', '.join(future_dated)} are dated after the evaluation")
    return "; ".join([head, *notes])
