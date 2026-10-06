"""Delivery value objects and the deterministic delivery rule (§4.2, §7 F15).

The model records what the customer *asked for*. The delivery promise is
computed here from stock and carrier data. A model-generated date never
reaches a customer.

:func:`evaluate_delivery` is the one place those dates are produced. It is a pure
function over facts - the origins a request would ship from, the carrier services
that leave them, the public-holiday calendars at both ends of the lane and the
instant the question is asked - and it either computes dates it can defend or
says ``UNKNOWN``. It reserves nothing, ships nothing, and decides no commercial
question: a split shipment is *reported*, because it is the only way the quantity
could move.

The rules are short, and every one of them is written down somewhere in the data
this project already had:

* ``cutoff_hour_utc`` is "the latest hour (UTC) an order can ship same-day on
  this service", so an order placed at or before it ships the same day, and one
  placed after it starts the next working day.
* The calendar exists because "a delivery promise is counted in *working* days",
  and ``runs_on_weekends`` is the documented case where the weekend part of that
  rule does not apply.
* A holiday is "a non-working day per country, subtracted by the delivery
  calculation" - and both countries of a lane are needed, because a lane has a
  warehouse country and a customer country and the seeded calendar covers both.
* The promise is the *earliest* one the data supports, so the fastest service
  that can leave first wins and transit is counted at ``transit_days_min``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, NamedTuple, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.intake import QuoteValidityWindow
from rfq_agent.domain.stock import StockEvaluation
from rfq_agent.domain.values import DomainModel

if TYPE_CHECKING:
    # Named for typing only: the read boundary's own records are what a caller
    # passes in, and the domain never imports the persistence layer at run time.
    from rfq_agent.persistence.read_models import (
        CarrierServiceRecord,
        HolidayRecord,
        WarehouseRecord,
    )

__all__ = [
    "CountryCode",
    "DeliveryEvaluation",
    "DeliveryFeasibility",
    "DeliveryPromise",
    "TransitDays",
    "evaluate_delivery",
]

#: Whole transit days for a carrier service.
TransitDays = Annotated[int, Field(ge=0, le=120)]
_MAX_LEG_NOTES = 400


class DeliveryFeasibility(StrEnum):
    """Whether the customer's requested date can be met."""

    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    #: No date was requested, so feasibility is not a question.
    NOT_REQUESTED = "NOT_REQUESTED"
    #: Destination or carrier data is missing; never guessed.
    UNKNOWN = "UNKNOWN"


class DeliveryPromise(DomainModel):
    """Deterministically computed delivery position for one RFQ.

    ``rationale`` is generated from data (stock availability, carrier transit,
    cutoffs), which is what lets the operator - and the customer - see *why*
    the date is what it is.
    """

    destination: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    origin_location: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    carrier_service_code: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = (
        None
    )
    transit_days: TransitDays | None = None
    earliest_ship_date: date | None = None
    earliest_delivery_date: date | None = None
    requested_date: date | None = None
    feasibility: DeliveryFeasibility = DeliveryFeasibility.UNKNOWN
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_LEG_NOTES)]

    @model_validator(mode="after")
    def _check_dates_and_feasibility(self) -> Self:
        """Dates must be ordered, and feasibility must be derivable from them."""
        if (
            self.earliest_ship_date is not None
            and self.earliest_delivery_date is not None
            and self.earliest_delivery_date < self.earliest_ship_date
        ):
            msg = "earliest_delivery_date must not precede earliest_ship_date"
            raise ValueError(msg)

        if (
            self.feasibility is DeliveryFeasibility.NOT_REQUESTED
            and self.requested_date is not None
        ):
            msg = "requested_date must be None when feasibility is NOT_REQUESTED"
            raise ValueError(msg)
        if self.feasibility is not DeliveryFeasibility.NOT_REQUESTED and (
            self.requested_date is None
            and self.feasibility in {DeliveryFeasibility.FEASIBLE, DeliveryFeasibility.INFEASIBLE}
        ):
            msg = "requested_date is required to state FEASIBLE or INFEASIBLE"
            raise ValueError(msg)

        if (
            self.feasibility in {DeliveryFeasibility.FEASIBLE, DeliveryFeasibility.INFEASIBLE}
            and self.earliest_delivery_date is None
        ):
            msg = "earliest_delivery_date is required to state feasibility"
            raise ValueError(msg)
        if (
            self.feasibility in {DeliveryFeasibility.FEASIBLE, DeliveryFeasibility.INFEASIBLE}
            and self.requested_date is not None
            and self.earliest_delivery_date is not None
        ):
            feasible = self.requested_date >= self.earliest_delivery_date
            expected = DeliveryFeasibility.FEASIBLE if feasible else DeliveryFeasibility.INFEASIBLE
            if self.feasibility is not expected:
                msg = f"feasibility {self.feasibility} inconsistent with the computed dates"
                raise ValueError(msg)
        return self

    def is_blocking(self) -> bool:
        """Whether this delivery position requires human attention."""
        return self.feasibility in {DeliveryFeasibility.INFEASIBLE, DeliveryFeasibility.UNKNOWN}


class DeliveryAssessment(DomainModel):
    """Delivery position attached to a quote, including validity constraints."""

    promise: DeliveryPromise
    #: Optional customer-requested validity window; checked in Phase 1.
    validity: QuoteValidityWindow | None = None
    #: Set when a partial shipment was computed as an option (§7 F12).
    split_shipment_proposed: bool = False
    split_shipment_note: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = (
        None
    )

    @model_validator(mode="after")
    def _check_split_note(self) -> Self:
        """A split-shipment note requires the split-shipment flag."""
        if self.split_shipment_proposed and self.split_shipment_note is None:
            msg = "split_shipment_note is required when split_shipment_proposed is true"
            raise ValueError(msg)
        if not self.split_shipment_proposed and self.split_shipment_note is not None:
            msg = "split_shipment_note must be None unless split_shipment_proposed"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# The deterministic delivery rule (Phase 1F)
# ---------------------------------------------------------------------------

#: Country code as the dataset and the calendar rows store it, e.g. ``DE``.
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
#: Longest window the working-day search walks before it reports that it failed.
_MAX_SEARCH_DAYS = 400
#: How many skipped non-working days a rationale names before it stops listing.
_MAX_NAMED_SKIPS = 2
#: Length limit of ``DeliveryAssessment.split_shipment_note``.
_MAX_SPLIT_NOTE = 300
#: ``date.weekday()`` counts from Monday, so Saturday is 5 and Sunday is 6.
_WEEKEND_START = 5


class _Schedule(NamedTuple):
    """What one carrier service can offer, or the fact that stopped it."""

    ship_date: date | None = None
    delivery_date: date | None = None
    problem: str | None = None

    @property
    def scheduled(self) -> bool:
        """Whether both dates were found."""
        return self.ship_date is not None and self.delivery_date is not None


class _Lane(NamedTuple):
    """The working-day facts one leg needs: both ends of the lane, and the carrier."""

    origin_country: str
    destination_country: str
    holidays: Mapping[tuple[str, date], str]
    origin_years: frozenset[int]
    destination_years: frozenset[int]
    runs_on_weekends: bool

    def covers(self, day: date) -> bool:
        """Whether both country calendars speak for the year ``day`` falls in."""
        return day.year in self.origin_years and day.year in self.destination_years

    def closed_for(self, day: date) -> bool:
        """Whether a public holiday closes either end of the lane on ``day``."""
        return (self.origin_country, day) in self.holidays or (
            self.destination_country,
            day,
        ) in self.holidays

    def is_working(self, day: date) -> bool:
        """Whether goods can move on ``day``: the weekend rule, then both calendars."""
        if day.weekday() >= _WEEKEND_START and not self.runs_on_weekends:
            return False
        return not self.closed_for(day)

    def skipped(self, first: date, last: date) -> tuple[str, ...]:
        """Name the holidays after ``first`` and up to ``last``, earliest first.

        A day both countries are closed on appears once, naming the first country
        in code order that closes it, so the rationale stays readable.
        """
        closed: dict[date, str] = {}
        for country, day in sorted(self.holidays, key=lambda item: (item[1], item[0])):
            if first < day <= last and country in {self.origin_country, self.destination_country}:
                closed.setdefault(day, country)
        return tuple(
            f"{day:%Y-%m-%d} ({self.holidays[country, day]}, {country})"
            for day, country in closed.items()
        )


class _Candidate(NamedTuple):
    """A service that can actually move the shipment, with the dates it offers."""

    service: CarrierServiceRecord
    lane: _Lane
    ship_date: date
    delivery_date: date


class DeliveryEvaluation(DomainModel):
    """The delivery position for one request: one leg per shipping origin.

    ``legs`` is the evidence - one independently scheduled promise per origin,
    ordered by location code - and ``assessment`` is the answer a quote carries:
    the promise for the *last* leg to arrive, plus the split-shipment flag and
    note when more than one origin is involved. The model refuses to hold a
    summary that contradicts the legs behind it, so a promise cannot be stated
    that the dates do not support.

    Nothing here decides whether a split shipment is acceptable, reserves stock
    or books a carrier: those are commercial decisions, and this is a statement
    about dates.
    """

    destination: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: Country of the delivery address, when it is known.
    destination_country: CountryCode | None = None
    as_of: datetime
    requested_date: date | None = None
    legs: tuple[DeliveryPromise, ...]
    assessment: DeliveryAssessment

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Every part of the evaluation must agree with the legs behind it."""
        self._check_as_of()
        self._check_legs()
        self._check_aggregate()
        return self

    def _check_as_of(self) -> None:
        """The instant the question was asked must be timezone-aware."""
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            msg = "as_of must be timezone-aware: cut-offs are stated in UTC"
            raise ValueError(msg)

    def _check_legs(self) -> None:
        """Every leg must name an origin, carry the same question, and be ordered."""
        origins = [leg.origin_location for leg in self.legs]
        if not origins or any(origin is None for origin in origins):
            msg = "a delivery evaluation needs at least one leg, and each leg needs an origin"
            raise ValueError(msg)
        if sorted(origins) != origins or len(set(origins)) != len(origins):
            msg = "legs must name one origin each and be ordered by location code"
            raise ValueError(msg)
        for leg in self.legs:
            if leg.destination != self.destination:
                msg = "every leg must carry the evaluation's destination"
                raise ValueError(msg)
            if leg.requested_date != self.requested_date:
                msg = "every leg must carry the evaluation's requested date"
                raise ValueError(msg)

    def _check_aggregate(self) -> None:
        """The quoted promise must be the last leg, or UNKNOWN and dateless."""
        promise = self.assessment.promise
        if promise.destination != self.destination or promise.requested_date != self.requested_date:
            msg = "the quoted promise must carry the evaluation's destination and requested date"
            raise ValueError(msg)
        if self.assessment.split_shipment_proposed != (len(self.legs) > 1):
            msg = "a split shipment is proposed exactly when more than one origin ships"
            raise ValueError(msg)

        unscheduled = [leg for leg in self.legs if leg.feasibility is DeliveryFeasibility.UNKNOWN]
        if unscheduled:
            expected = min(unscheduled, key=_controlling_key).origin_location
            if promise.feasibility is not DeliveryFeasibility.UNKNOWN:
                msg = "one leg that cannot be scheduled makes the promise UNKNOWN"
                raise ValueError(msg)
            if promise.earliest_ship_date is not None or promise.earliest_delivery_date is not None:
                msg = "a promise whose facts are missing must not carry dates"
                raise ValueError(msg)
            if promise.origin_location != expected:
                msg = f"an UNKNOWN promise names the first unschedulable leg, expected {expected}"
                raise ValueError(msg)
            return

        controlling = max(self.legs, key=_controlling_key)
        stated = (
            promise.feasibility,
            promise.origin_location,
            promise.carrier_service_code,
            promise.transit_days,
            promise.earliest_ship_date,
            promise.earliest_delivery_date,
        )
        expected_facts = (
            controlling.feasibility,
            controlling.origin_location,
            controlling.carrier_service_code,
            controlling.transit_days,
            controlling.earliest_ship_date,
            controlling.earliest_delivery_date,
        )
        if stated != expected_facts:
            msg = "the quoted promise must be the leg that arrives last"
            raise ValueError(msg)

    @property
    def promise(self) -> DeliveryPromise:
        """The promise a quote carries: the last leg to arrive, or UNKNOWN."""
        return self.assessment.promise

    @property
    def feasibility(self) -> DeliveryFeasibility:
        """The verdict for the request as a whole."""
        return self.promise.feasibility

    @property
    def earliest_ship_date(self) -> date | None:
        """The date the shipment starts moving, when it can be promised."""
        return self.promise.earliest_ship_date

    @property
    def earliest_delivery_date(self) -> date | None:
        """The date the whole request would arrive, when it can be promised."""
        return self.promise.earliest_delivery_date

    @property
    def is_split(self) -> bool:
        """Whether more than one origin ships this request."""
        return len(self.legs) > 1

    def is_blocking(self) -> bool:
        """Whether a human must look before this promise can be quoted."""
        return self.promise.is_blocking()


def evaluate_delivery(
    stock: StockEvaluation,
    *,
    destination: str,
    as_of: datetime,
    services: Iterable[CarrierServiceRecord],
    warehouses: Iterable[WarehouseRecord],
    holidays: Iterable[HolidayRecord],
    destination_country: str | None = None,
    requested_date: date | None = None,
) -> DeliveryEvaluation:
    """Compute the earliest ship and delivery date for a request stock can cover.

    The rules, and where each one is written down:

    * **Origins.** The request ships from the warehouses the stock evaluation
      names: the one warehouse that covers it alone, or its split proposal, one
      leg per origin.
    * **Cut-off.** ``cutoff_hour_utc`` is documented on the carrier row as "the
      latest hour (UTC) an order can ship same-day on this service", so an order
      placed at or before that hour on a working day ships that day. The
      comparison is on the UTC hour, which is the unit the column holds: 12:59
      is inside a 12:00 cut-off, 13:00 is not.
    * **Weekends.** Saturday and Sunday are not working days, because the seeded
      calendar exists precisely because "a delivery promise is counted in
      *working* days", and ``runs_on_weekends`` is the documented case where that
      rule does not apply - a weekend-capable service ships and counts them.
    * **Holidays.** A public holiday is a non-working day "subtracted by the
      delivery calculation", in the origin country *or* the destination country:
      the calendar covers the warehouse countries and the customer countries
      alike, and the seeded data says both are needed for a working-day promise.
    * **Earliest.** The dates are the earliest the data supports: the service
      that arrives first wins, and its advertised range is counted at
      ``transit_days_min`` - the range itself is quoted in the rationale.
    * **Nothing is guessed.** A missing warehouse, a deactivated warehouse, an
      unknown destination country, a country whose calendar the loaded rows do
      not cover, a date outside the covered years, no active service from an
      origin, or no working day inside the search window all produce ``UNKNOWN``
      with no dates at all.
    * **Nothing is decided.** A split shipment is reported leg by leg, with the
      later leg setting the promise, because that is the only way the quantity
      could move; whether to offer it, and how to ship it, is not this function's
      call. No stock is reserved, no carrier is booked, no policy is set.

    Every fact is read from the arguments: the same arguments always produce the
    same evaluation, whatever order they arrive in.

    Args:
        stock: The evaluation the stock rule produced for this request. It must
            cover the request, because a request that cannot ship has no
            delivery date to compute.
        destination: Where the goods go, as recorded - free text such as
            ``"Hamburg"`` or ``"Rotterdam, NL"``. It is carried, never parsed.
        as_of: The instant the question is asked. Must be timezone-aware, since
            cut-off hours are UTC.
        services: The carrier services the read boundary returned. Services from
            other origins are ignored, inactive services are never used, and two
            rows with one service code are refused.
        warehouses: The warehouses the read boundary returned, needed for each
            origin's country. An origin with no row, or a deactivated one, cannot
            be scheduled.
        holidays: The public-holiday rows read for the countries involved. Rows
            for other countries are ignored, and rows are indexed in a stable
            order so a repeated date resolves the same way every time.
        destination_country: ISO 3166-1 alpha-2 code of the delivery address,
            taken from the customer record. ``None`` means it is not known,
            which makes the promise ``UNKNOWN`` instead of a guess.
        requested_date: The date the customer asked for, if they asked for one.

    Returns:
        A :class:`DeliveryEvaluation` holding one leg per origin and the
        assessment a quote carries.

    Raises:
        ValueError: If ``as_of`` is naive, if ``destination_country`` is not an
            alpha-2 code, if ``stock`` does not cover the request, or if two
            facts claim one identity.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        msg = "as_of must be timezone-aware: cut-offs are stated in UTC"
        raise ValueError(msg)
    if destination_country is not None and not _COUNTRY_CODE.fullmatch(destination_country):
        msg = f"destination_country must be an ISO 3166-1 alpha-2 code, got {destination_country!r}"
        raise ValueError(msg)
    if not stock.covered:
        msg = (
            f"delivery is evaluated only for a request stock covers: {stock.product_id} is "
            f"{stock.status} (short by {stock.shortfall_qty})"
        )
        raise ValueError(msg)

    allocations = _shipments(stock)
    service_list = tuple(services)
    warehouse_list = tuple(warehouses)
    holiday_list = tuple(holidays)
    _refuse_duplicates("carrier service", [service.service_code for service in service_list])
    _refuse_duplicates("warehouse", [warehouse.location_code for warehouse in warehouse_list])
    by_warehouse = {warehouse.location_code: warehouse for warehouse in warehouse_list}
    names: dict[tuple[str, date], str] = {}
    years: dict[str, frozenset[int]] = {}
    for record in sorted(holiday_list, key=_holiday_order):
        names.setdefault((record.country_code, record.holiday_date), record.name)
    for country in sorted({country for country, _ in names}):
        years[country] = frozenset(
            day.year for country_code, day in names if country_code == country
        )

    legs = tuple(
        _leg(
            origin,
            destination=destination,
            destination_country=destination_country,
            as_of=as_of,
            requested_date=requested_date,
            warehouses=by_warehouse,
            services=service_list,
            holidays=names,
            years=years,
        )
        for origin, _quantity in allocations
    )
    note = _split_note(allocations, legs) if len(legs) > 1 else None
    assessment = DeliveryAssessment(
        promise=_aggregate(legs=legs, destination=destination, requested_date=requested_date),
        split_shipment_proposed=len(legs) > 1,
        split_shipment_note=note,
    )
    return DeliveryEvaluation(
        destination=destination,
        destination_country=destination_country,
        as_of=as_of,
        requested_date=requested_date,
        legs=legs,
        assessment=assessment,
    )


def _shipments(stock: StockEvaluation) -> tuple[tuple[str, int], ...]:
    """The origins a covered request would ship from, in location-code order.

    This is the stock evaluation's own fact, read straight off it: one warehouse
    when one covers the request alone, and otherwise the split proposal it
    stated. Each leg's quantity is carried through because the operator needs it
    in the note; it does not influence a date - the data holds no weight, volume
    or vehicle rule, and inventing one would not be a calculation.
    """
    if stock.single_warehouse_cover is not None:
        return ((stock.single_warehouse_cover, stock.requested_qty),)
    allocations = tuple(sorted((item.location, item.qty) for item in stock.split))
    if not allocations:
        msg = f"{stock.product_id} is covered but names no origin: there is no shipment to schedule"
        raise ValueError(msg)
    return allocations


def _refuse_duplicates(kind: str, codes: Sequence[str]) -> None:
    """Refuse two facts claiming one identity: one of them would be lost silently."""
    duplicates = sorted(code for code, count in Counter(codes).items() if count > 1)
    if duplicates:
        msg = (
            f"more than one {kind} for {', '.join(duplicates)}: "
            "which of two contradictory facts is true cannot be guessed"
        )
        raise ValueError(msg)


def _holiday_order(record: HolidayRecord) -> tuple[str, date, str]:
    """Order holiday rows so a repeated date always resolves to the same name."""
    return (record.country_code, record.holiday_date, record.name)


def _leg(
    origin: str,
    *,
    destination: str,
    destination_country: str | None,
    as_of: datetime,
    requested_date: date | None,
    warehouses: Mapping[str, WarehouseRecord],
    services: Sequence[CarrierServiceRecord],
    holidays: Mapping[tuple[str, date], str],
    years: Mapping[str, frozenset[int]],
) -> DeliveryPromise:
    """Schedule one shipping origin: choose its service, or say what is missing."""
    warehouse = warehouses.get(origin)
    if warehouse is None:
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=f"no warehouse record for origin {origin}",
        )
    if not warehouse.active:
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=f"warehouse {origin} is deactivated",
        )
    if destination_country is None:
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=(
                "the destination country is unknown, so the receiving calendar cannot be applied"
            ),
        )
    origin_years = years.get(warehouse.country_code, frozenset())
    destination_years = years.get(destination_country, frozenset())
    if not origin_years or not destination_years:
        missing = warehouse.country_code if not origin_years else destination_country
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=f"the calendar has no rows for {missing}",
        )

    candidates = sorted(
        (service for service in services if service.origin_location == origin and service.active),
        key=lambda service: service.service_code,
    )
    if not candidates:
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=f"no active carrier service ships from {origin}",
        )

    scheduled: list[_Candidate] = []
    problems: list[str] = []
    for service in candidates:
        lane = _Lane(
            origin_country=warehouse.country_code,
            destination_country=destination_country,
            holidays=holidays,
            origin_years=origin_years,
            destination_years=destination_years,
            runs_on_weekends=service.runs_on_weekends,
        )
        schedule = _schedule(service, as_of, lane)
        if schedule.ship_date is None or schedule.delivery_date is None:
            problems.append(f"{service.service_code}: {schedule.problem}")
        else:
            scheduled.append(
                _Candidate(
                    service=service,
                    lane=lane,
                    ship_date=schedule.ship_date,
                    delivery_date=schedule.delivery_date,
                )
            )

    if not scheduled:
        return _unknown_promise(
            destination=destination,
            origin=origin,
            requested_date=requested_date,
            reason=f"no service from {origin} could be scheduled ({'; '.join(problems)})",
        )

    best = min(scheduled, key=_selection_key)
    return DeliveryPromise(
        destination=destination,
        origin_location=origin,
        carrier_service_code=best.service.service_code,
        transit_days=best.service.transit_days_min,
        earliest_ship_date=best.ship_date,
        earliest_delivery_date=best.delivery_date,
        requested_date=requested_date,
        feasibility=_feasibility(requested_date, best.delivery_date),
        rationale=_leg_rationale(best, as_of=as_of, origin=origin),
    )


def _schedule(service: CarrierServiceRecord, as_of: datetime, lane: _Lane) -> _Schedule:
    """Find the earliest dates a service can offer, or the fact that stopped it.

    The search starts on the day the question is asked. The order ships that day
    when it is a working day for both ends of the lane and the order was placed
    at or before the service's cut-off hour; otherwise it starts on the next
    working day, where the cut-off no longer applies because that day has not
    started yet. Delivery is then counted forward in working days, so a transit
    of one day means the next working day, and a transit of zero means the goods
    arrive the day they ship.
    """
    ship_date = as_of.date()
    within_cutoff = as_of.hour <= service.cutoff_hour_utc
    for _ in range(_MAX_SEARCH_DAYS):
        if not lane.covers(ship_date):
            return _Schedule(problem=_uncovered(ship_date))
        if lane.is_working(ship_date) and (within_cutoff or ship_date > as_of.date()):
            break
        ship_date += timedelta(days=1)
    else:
        return _Schedule(problem=_no_working_day(as_of.date()))

    delivery_date = ship_date
    for _ in range(service.transit_days_min):
        delivery_date += timedelta(days=1)
        for _ in range(_MAX_SEARCH_DAYS):
            if not lane.covers(delivery_date):
                return _Schedule(problem=_uncovered(delivery_date))
            if lane.is_working(delivery_date):
                break
            delivery_date += timedelta(days=1)
        else:
            return _Schedule(problem=_no_working_day(ship_date))
    return _Schedule(ship_date=ship_date, delivery_date=delivery_date)


def _uncovered(day: date) -> str:
    """Say that a date falls outside the years the loaded calendar rows cover."""
    return f"the calendar does not cover {day.year}, so {day:%Y-%m-%d} cannot be checked"


def _no_working_day(from_day: date) -> str:
    """Say that no working day was found inside the window the search walks."""
    return f"no working day was found within {_MAX_SEARCH_DAYS} days of {from_day:%Y-%m-%d}"


def _selection_key(candidate: _Candidate) -> tuple[date, date, int, int, str]:
    """Order candidate services: earliest arrival first, then stable tie-breakers.

    The rule, in full and in order: the earliest delivery date; then the earliest
    ship date; then the shortest advertised transit; then the shortest advertised
    worst case; then the service code. The code is the table's primary key, so
    the order is total and no two services can tie.
    """
    service = candidate.service
    return (
        candidate.delivery_date,
        candidate.ship_date,
        service.transit_days_min,
        service.transit_days_max,
        service.service_code,
    )


def _controlling_key(leg: DeliveryPromise) -> tuple[date, date, str]:
    """Order legs: the last to arrive controls, ties by ship date then origin code."""
    return (
        leg.earliest_delivery_date or date.min,
        leg.earliest_ship_date or date.min,
        leg.origin_location or "",
    )


def _feasibility(requested_date: date | None, delivery_date: date) -> DeliveryFeasibility:
    """The verdict for a promise that could be computed: asked, met, or missed."""
    if requested_date is None:
        return DeliveryFeasibility.NOT_REQUESTED
    if requested_date >= delivery_date:
        return DeliveryFeasibility.FEASIBLE
    return DeliveryFeasibility.INFEASIBLE


def _fit(text: str, limit: int) -> str:
    """Keep a generated sentence inside the length its contract allows."""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 4].rstrip()} ..."


def _leg_rationale(candidate: _Candidate, *, as_of: datetime, origin: str) -> str:
    """Render one leg's schedule as a sentence built from the facts behind it."""
    service, lane = candidate.service, candidate.lane
    text = (
        f"{origin} ({lane.origin_country}) to {lane.destination_country} via "
        f"{service.service_code} ({service.carrier}); order {as_of.astimezone(UTC):%Y-%m-%d %H:%M} "
        f"UTC against the {service.cutoff_hour_utc:02d}:00 UTC cut-off, so ship "
        f"{candidate.ship_date:%Y-%m-%d}; transit {service.transit_days_min}-"
        f"{service.transit_days_max} working days ({service.transit_days_min} used), so delivery "
        f"{candidate.delivery_date:%Y-%m-%d}."
    )
    skipped = lane.skipped(candidate.ship_date, candidate.delivery_date)[:_MAX_NAMED_SKIPS]
    if skipped:
        text += f" Skipped: {'; '.join(skipped)}."
    return _fit(
        f"{text} Calendars: {lane.origin_country} + {lane.destination_country}.", _MAX_LEG_NOTES
    )


def _split_note(allocations: Sequence[tuple[str, int]], legs: Sequence[DeliveryPromise]) -> str:
    """Describe a split shipment leg by leg, without deciding anything about it."""
    by_origin = {leg.origin_location: leg for leg in legs}
    parts: list[str] = []
    for origin, quantity in allocations:
        leg = by_origin[origin]
        if leg.earliest_delivery_date is None:
            parts.append(f"{origin} {quantity} units cannot be scheduled")
        else:
            parts.append(f"{origin} {quantity} units arrive {leg.earliest_delivery_date:%Y-%m-%d}")
    return _fit(
        f"Split shipment: {', '.join(parts)}. Each leg was scheduled on its own; "
        "no shipment decision is made here.",
        _MAX_SPLIT_NOTE,
    )


def _unknown_promise(
    *, destination: str, origin: str, requested_date: date | None, reason: str
) -> DeliveryPromise:
    """A promise that carries no date, because a fact it needs is missing."""
    return DeliveryPromise(
        destination=destination,
        origin_location=origin,
        requested_date=requested_date,
        feasibility=DeliveryFeasibility.UNKNOWN,
        rationale=_fit(f"UNKNOWN: {reason}; no delivery date is guessed.", _MAX_LEG_NOTES),
    )


def _reason_of(rationale: str) -> str:
    """The cause out of an UNKNOWN rationale, so a summary can repeat it."""
    return rationale.removeprefix("UNKNOWN: ").removesuffix("; no delivery date is guessed.")


def _aggregate(
    *, legs: Sequence[DeliveryPromise], destination: str, requested_date: date | None
) -> DeliveryPromise:
    """The one promise a quote carries: the last leg's dates, or ``UNKNOWN``.

    An unschedulable leg makes the whole promise ``UNKNOWN``: the request would
    not arrive in one piece, and a date for the part that would move is a guess
    dressed as a promise.
    """
    unscheduled = [leg for leg in legs if leg.feasibility is DeliveryFeasibility.UNKNOWN]
    if unscheduled:
        first = min(unscheduled, key=_controlling_key)
        if first.origin_location is None:
            msg = "a leg that cannot be scheduled must still name its origin"
            raise RuntimeError(msg)
        return _unknown_promise(
            destination=destination,
            origin=first.origin_location,
            requested_date=requested_date,
            reason=f"{first.origin_location}: {_reason_of(first.rationale)}",
        )
    if len(legs) == 1:
        return legs[0]
    controlling = max(legs, key=_controlling_key)
    return controlling.model_copy(
        update={
            "rationale": _fit(
                f"{controlling.rationale} Of {len(legs)} legs, "
                f"{controlling.origin_location} arrives last and sets the promise.",
                _MAX_LEG_NOTES,
            )
        }
    )
