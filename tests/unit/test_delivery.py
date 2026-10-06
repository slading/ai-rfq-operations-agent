"""The deterministic delivery rule, on hand-built facts.

Every fact this rule reads is placed by hand here - a warehouse, a carrier
service, a public holiday, a stock evaluation - so each boundary can be put on an
exact unit: an order placed at 12:00 against a 12:00 cut-off, a transit of one
working day crossing a weekend, a lane whose destination calendar is closed.

The three rules the calendar part of the calculation follows are the ones the
data documents, and they are pinned here one at a time: the cut-off is the latest
UTC *hour* an order can ship same-day; transit is counted in working days, with
weekends excluded unless the service runs on them; and a public holiday stops the
clock in the origin country *or* the destination country. Missing facts never
produce a date - they produce ``UNKNOWN``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from rfq_agent.domain.delivery import (
    DeliveryEvaluation,
    DeliveryFeasibility,
    evaluate_delivery,
)
from rfq_agent.domain.stock import StockEvaluation, StockLevel, evaluate_stock
from rfq_agent.persistence.read_models import CarrierServiceRecord, HolidayRecord, WarehouseRecord

PRODUCT = "PRD_0001"
#: When the demo stock was counted; irrelevant to delivery, but stock needs a stamp.
STOCK_AS_OF = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
#: Tuesday 6 October 2026, 09:00 UTC - a working day, before the 12:00 cut-off.
AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: The same instant with its timezone stripped - what the rule must refuse.
NAIVE_AT = AT.replace(tzinfo=None)


def warehouse(location: str, country: str, *, active: bool = True) -> WarehouseRecord:
    """One warehouse row, with the name and city it would carry."""
    return WarehouseRecord(
        location_code=location,
        name=f"{location} DC",
        city=location,
        country_code=country,
        active=active,
    )


def service(
    code: str,
    origin: str,
    *,
    carrier: str = "DHL",
    transit: tuple[int, int] = (1, 2),
    cutoff: int = 12,
    weekends: bool = False,
    active: bool = True,
) -> CarrierServiceRecord:
    """One carrier service row, with sensible defaults and exact overrides."""
    return CarrierServiceRecord(
        service_code=code,
        carrier=carrier,
        name=code,
        origin_location=origin,
        transit_days_min=transit[0],
        transit_days_max=transit[1],
        cutoff_hour_utc=cutoff,
        runs_on_weekends=weekends,
        active=active,
    )


def holiday(country: str, iso: str, name: str = "Public holiday") -> HolidayRecord:
    """One non-working day in one country."""
    return HolidayRecord(country_code=country, holiday_date=date.fromisoformat(iso), name=name)


def stock_at(*allocations: tuple[str, int]) -> StockEvaluation:
    """A stock evaluation covering the request from those warehouses, via Phase 1E."""
    return evaluate_stock(
        [
            StockLevel(product_id=PRODUCT, location=code, on_hand_qty=qty, as_of=STOCK_AS_OF)
            for code, qty in allocations
        ],
        product_id=PRODUCT,
        requested_qty=sum(qty for _, qty in allocations),
        as_of=STOCK_AS_OF,
    )


CALENDAR = (
    holiday("PL", "2026-11-11", "Independence Day"),
    holiday("DE", "2026-05-25", "Whit Monday"),
    holiday("CZ", "2026-10-28", "Czech Statehood Day"),
)
WAREHOUSES = (warehouse("WAW", "PL"), warehouse("BER", "DE"))
CARRIERS = (
    service("DHL-EXP", "WAW", carrier="DHL", transit=(1, 2), cutoff=12),
    service("DHL-ECO", "WAW", carrier="DHL", transit=(2, 4), cutoff=15),
    service("DPD-CLS", "BER", carrier="DPD", transit=(2, 3), cutoff=15, weekends=True),
    service("GLS-EUR", "BER", carrier="GLS", transit=(3, 5), cutoff=16),
)


def evaluate(
    stock: StockEvaluation,
    *,
    as_of: datetime = AT,
    destination: str = "Hamburg",
    destination_country: str | None = "DE",
    services: Sequence[CarrierServiceRecord] = CARRIERS,
    warehouses: Sequence[WarehouseRecord] = WAREHOUSES,
    holidays: Sequence[HolidayRecord] = CALENDAR,
    requested_date: date | None = None,
) -> DeliveryEvaluation:
    """Ask the delivery question, with every fact overridable."""
    return evaluate_delivery(
        stock,
        destination=destination,
        destination_country=destination_country,
        as_of=as_of,
        services=services,
        warehouses=warehouses,
        holidays=holidays,
        requested_date=requested_date,
    )


class TestScheduling:
    """Ship dates from cut-offs, weekends and holidays; delivery from transit."""

    def test_a_weekday_order_ships_the_same_day(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)))

        assert evaluation.promise.carrier_service_code == "DHL-EXP"
        assert evaluation.earliest_ship_date == date(2026, 10, 6)
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)
        assert evaluation.promise.transit_days == 1

    @pytest.mark.parametrize(
        ("hour", "minute", "ship", "delivery", "code"),
        [
            (11, 0, "2026-10-06", "2026-10-07", "DHL-EXP"),
            (12, 0, "2026-10-06", "2026-10-07", "DHL-EXP"),
            (12, 59, "2026-10-06", "2026-10-07", "DHL-EXP"),
            (13, 0, "2026-10-06", "2026-10-08", "DHL-ECO"),
            (15, 0, "2026-10-06", "2026-10-08", "DHL-ECO"),
            (16, 0, "2026-10-07", "2026-10-08", "DHL-EXP"),
            (23, 59, "2026-10-07", "2026-10-08", "DHL-EXP"),
        ],
    )
    def test_the_cutoff_boundary(
        self, hour: int, minute: int, ship: str, delivery: str, code: str
    ) -> None:
        """The cut-off is an hour, inclusive: 12:59 ships, 13:00 is the next day."""
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            as_of=datetime(2026, 10, 6, hour, minute, tzinfo=UTC),
        )

        assert evaluation.promise.carrier_service_code == code
        assert evaluation.earliest_ship_date == date.fromisoformat(ship)
        assert evaluation.earliest_delivery_date == date.fromisoformat(delivery)

    def test_a_weekend_is_not_a_working_day(self) -> None:
        """Saturday 10 October 2026: nothing moves until Monday."""
        evaluation = evaluate(
            stock_at(("WAW", 100)), as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
        )

        assert evaluation.earliest_ship_date == date(2026, 10, 12)
        assert evaluation.earliest_delivery_date == date(2026, 10, 13)

    def test_a_friday_after_the_cutoff_ships_on_monday(self) -> None:
        """The cut-off is not carried into the next day: Monday is a fresh day."""
        evaluation = evaluate(
            stock_at(("WAW", 100)), as_of=datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
        )

        assert evaluation.earliest_ship_date == date(2026, 10, 12)
        assert evaluation.earliest_delivery_date == date(2026, 10, 13)

    def test_a_holiday_closes_the_ship_day_too(self) -> None:
        """11 November 2026 is Independence Day in Poland, so Warsaw is shut."""
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            as_of=datetime(2026, 11, 11, 9, 0, tzinfo=UTC),
        )

        assert evaluation.earliest_ship_date == date(2026, 11, 12)
        assert evaluation.earliest_delivery_date == date(2026, 11, 13)

    def test_a_holiday_at_the_origin_is_skipped_in_transit(self) -> None:
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            as_of=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        )

        assert evaluation.earliest_ship_date == date(2026, 11, 10)
        assert evaluation.earliest_delivery_date == date(2026, 11, 12)
        assert "Independence Day" in evaluation.promise.rationale

    def test_a_holiday_at_the_destination_is_skipped_in_transit(self) -> None:
        """A Berlin leg would arrive on 28 October; a Czech buyer is closed."""
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            as_of=datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
            destination="Prague",
            destination_country="CZ",
        )

        assert evaluation.earliest_ship_date == date(2026, 10, 27)
        assert evaluation.earliest_delivery_date == date(2026, 10, 29)
        assert "Czech Statehood Day" in evaluation.promise.rationale

    def test_a_weekend_capable_service_moves_on_the_weekend(self) -> None:
        """``DPD-CLS`` is the seeded case where the weekend rule does not apply."""
        evaluation = evaluate(stock_at(("BER", 20)), as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC))

        assert evaluation.promise.carrier_service_code == "DPD-CLS"
        assert evaluation.earliest_ship_date == date(2026, 10, 10)
        assert evaluation.earliest_delivery_date == date(2026, 10, 12)

    def test_a_weekend_capable_service_still_respects_a_holiday(self) -> None:
        """Weekends are the carrier's rule; a public holiday is not."""
        evaluation = evaluate(stock_at(("BER", 20)), as_of=datetime(2026, 5, 23, 9, 0, tzinfo=UTC))

        assert evaluation.earliest_ship_date == date(2026, 5, 23)
        assert evaluation.earliest_delivery_date == date(2026, 5, 26)
        assert "Whit Monday" in evaluation.promise.rationale

    def test_zero_transit_delivers_the_day_it_ships(self) -> None:
        quick = service("SAME-DAY", "WAW", transit=(0, 0), cutoff=23)

        evaluation = evaluate(stock_at(("WAW", 100)), services=(quick,))

        assert evaluation.earliest_ship_date == evaluation.earliest_delivery_date


class TestFeasibilityAgainstTheRequestedDate:
    """Asked, met, missed - or a question that was never asked."""

    def test_a_requested_date_that_is_met_is_feasible(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)), requested_date=date(2026, 10, 7))

        assert evaluation.feasibility is DeliveryFeasibility.FEASIBLE
        assert evaluation.is_blocking() is False

    def test_a_requested_date_that_is_exactly_met_is_feasible(self) -> None:
        """The customer asked for the earliest date the data can produce."""
        evaluation = evaluate(stock_at(("WAW", 100)), requested_date=date(2026, 10, 7))
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)

        assert evaluation.feasibility is DeliveryFeasibility.FEASIBLE

    def test_a_requested_date_that_is_missed_is_infeasible(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)), requested_date=date(2026, 10, 6))

        assert evaluation.earliest_delivery_date == date(2026, 10, 7)
        assert evaluation.feasibility is DeliveryFeasibility.INFEASIBLE
        assert evaluation.is_blocking() is True

    def test_no_requested_date_is_not_requested(self) -> None:
        """Nothing was asked, so nothing is judged - but the dates are still there."""
        evaluation = evaluate(stock_at(("WAW", 100)))

        assert evaluation.feasibility is DeliveryFeasibility.NOT_REQUESTED
        assert evaluation.promise.requested_date is None
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)
        assert evaluation.is_blocking() is False

    def test_the_verdict_is_the_last_legs_verdict(self) -> None:
        """A split whose later leg misses the date misses the date."""
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)), requested_date=date(2026, 10, 7))

        assert evaluation.earliest_delivery_date == date(2026, 10, 8)
        assert evaluation.feasibility is DeliveryFeasibility.INFEASIBLE


class TestCarrierSelection:
    """The documented ordering, and nothing else."""

    def test_the_service_that_arrives_earliest_wins(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)))

        assert evaluation.promise.carrier_service_code == "DHL-EXP"
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)

    def test_a_later_cutoff_beats_a_missed_one(self) -> None:
        """At 13:00 the express cut-off is gone; the economy service still ships."""
        evaluation = evaluate(
            stock_at(("WAW", 100)), as_of=datetime(2026, 10, 6, 13, 0, tzinfo=UTC)
        )

        assert evaluation.promise.carrier_service_code == "DHL-ECO"
        assert evaluation.earliest_ship_date == date(2026, 10, 6)
        assert evaluation.earliest_delivery_date == date(2026, 10, 8)

    def test_a_shared_delivery_date_is_broken_by_the_ship_date(self) -> None:
        """Both services would arrive on the 8th; the one that leaves first wins."""
        express = service("AAA-EXP", "WAW", transit=(1, 1), cutoff=12)
        economy = service("ZZZ-ECO", "WAW", transit=(2, 2), cutoff=15)

        evaluation = evaluate(
            stock_at(("WAW", 100)),
            as_of=datetime(2026, 10, 6, 13, 0, tzinfo=UTC),
            services=(express, economy),
        )

        assert evaluation.promise.carrier_service_code == "ZZZ-ECO"
        assert evaluation.earliest_ship_date == date(2026, 10, 6)

    def test_services_that_are_identical_are_broken_by_code(self) -> None:
        first = service("AAA-1", "WAW", transit=(1, 1), cutoff=12)
        second = service("AAA-2", "WAW", transit=(1, 1), cutoff=12)

        evaluation = evaluate(stock_at(("WAW", 100)), services=(second, first))

        assert evaluation.promise.carrier_service_code == "AAA-1"

    def test_transit_is_counted_at_the_earliest_advertised_day(self) -> None:
        """A wide range is still the earlier promise, and the range is quoted."""
        quick = service("QUICK", "WAW", transit=(1, 9), cutoff=12)
        slow = service("SLOW", "WAW", transit=(2, 2), cutoff=12)

        evaluation = evaluate(stock_at(("WAW", 100)), services=(quick, slow))

        assert evaluation.promise.carrier_service_code == "QUICK"
        assert evaluation.promise.transit_days == 1
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)
        assert "transit 1-9 working days (1 used)" in evaluation.promise.rationale

    def test_inactive_services_are_never_used(self) -> None:
        off = service("DHL-EXP", "WAW", transit=(1, 2), cutoff=12, active=False)

        evaluation = evaluate(stock_at(("WAW", 100)), services=(off, service("DHL-ECO", "WAW")))

        assert evaluation.feasibility is DeliveryFeasibility.NOT_REQUESTED
        assert evaluation.promise.carrier_service_code == "DHL-ECO"

    def test_services_from_another_origin_are_ignored(self) -> None:
        """The lane starts where the stock is: Berlin cannot ship a Warsaw leg."""
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            services=tuple(item for item in CARRIERS if item.origin_location == "BER"),
        )

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_delivery_date is None
        assert "no active carrier service ships from WAW" in evaluation.promise.rationale


class TestMissingFacts:
    """Every missing fact gives UNKNOWN, and never a date."""

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("no_carrier", "no active carrier service ships from WAW"),
            ("no_warehouse", "no warehouse record for origin WAW"),
            ("inactive_warehouse", "warehouse WAW is deactivated"),
            ("unknown_country", "the destination country is unknown"),
            ("no_destination_calendar", "the calendar has no rows for IT"),
            ("no_origin_calendar", "the calendar has no rows for IT"),
            ("year_not_covered", "does not cover 2027"),
        ],
    )
    def test_unknown_facts_produce_an_undated_promise(self, case: str, expected: str) -> None:
        facts: dict[str, object] = {}
        destination_country: str | None = "DE"
        stock = stock_at(("WAW", 100))
        as_of = AT
        if case == "no_carrier":
            facts["services"] = ()
        elif case == "no_warehouse":
            facts["warehouses"] = (warehouse("BER", "DE"),)
        elif case == "inactive_warehouse":
            facts["warehouses"] = (warehouse("WAW", "PL", active=False),)
        elif case == "unknown_country":
            destination_country = None
        elif case == "no_destination_calendar":
            destination_country = "IT"
        elif case == "no_origin_calendar":
            facts["warehouses"] = (warehouse("MIL", "IT"),)
            stock = stock_at(("MIL", 100))
        elif case == "year_not_covered":
            as_of = datetime(2026, 12, 31, 9, 0, tzinfo=UTC)

        evaluation = evaluate(
            stock,
            as_of=as_of,
            destination_country=destination_country,
            **facts,  # type: ignore[arg-type]
        )

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_ship_date is None
        assert evaluation.earliest_delivery_date is None
        assert evaluation.is_blocking() is True
        assert expected in evaluation.promise.rationale
        assert "no delivery date is guessed" in evaluation.promise.rationale

    def test_a_requested_date_does_not_turn_unknown_into_a_verdict(self) -> None:
        """Missing facts outrank the question: UNKNOWN, not FEASIBLE or INFEASIBLE."""
        evaluation = evaluate(stock_at(("WAW", 100)), services=(), requested_date=date(2026, 10, 7))

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_delivery_date is None
        assert evaluation.promise.requested_date == date(2026, 10, 7)

    def test_one_unschedulable_leg_makes_the_whole_promise_unknown(self) -> None:
        """Berlin can still be scheduled; the request as a whole cannot arrive."""
        evaluation = evaluate(
            stock_at(("WAW", 100), ("BER", 1)),
            services=(service("DHL-EXP", "WAW"),),
        )

        by_origin = {leg.origin_location: leg for leg in evaluation.legs}
        assert by_origin["WAW"].earliest_delivery_date == date(2026, 10, 7)
        assert by_origin["BER"].earliest_delivery_date is None
        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_delivery_date is None
        assert "BER:" in evaluation.promise.rationale


class TestMultiOrigin:
    """Each leg is scheduled on its own; the last one to arrive sets the promise."""

    def test_the_later_leg_sets_the_promise(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))

        assert evaluation.is_split is True
        assert evaluation.assessment.split_shipment_proposed is True
        assert evaluation.earliest_delivery_date == date(2026, 10, 8)
        assert evaluation.promise.origin_location == "BER"

    def test_each_leg_is_scheduled_on_its_own(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))

        scheduled = {
            leg.origin_location: (leg.carrier_service_code, leg.earliest_delivery_date)
            for leg in evaluation.legs
        }
        assert scheduled == {
            "BER": ("DPD-CLS", date(2026, 10, 8)),
            "WAW": ("DHL-EXP", date(2026, 10, 7)),
        }

    def test_legs_are_ordered_by_origin_code(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))

        assert [leg.origin_location for leg in evaluation.legs] == ["BER", "WAW"]

    def test_the_note_names_every_leg_and_decides_nothing(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))

        note = evaluation.assessment.split_shipment_note
        assert note is not None
        assert "BER 1 units arrive 2026-10-08" in note
        assert "WAW 100 units arrive 2026-10-07" in note
        assert "no shipment decision is made here" in note
        assert "BER arrives last and sets the promise" in evaluation.promise.rationale

    def test_a_single_origin_is_not_a_split(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)))

        assert evaluation.is_split is False
        assert evaluation.assessment.split_shipment_proposed is False
        assert evaluation.assessment.split_shipment_note is None
        assert evaluation.promise.rationale == evaluation.legs[0].rationale

    def test_a_faster_leg_does_not_pull_the_promise_earlier(self) -> None:
        """Over a weekend Berlin moves first, but the Warsaw leg still arrives last."""
        on_a_saturday = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

        single = evaluate(stock_at(("WAW", 100)), as_of=on_a_saturday)
        split = evaluate(stock_at(("WAW", 100), ("BER", 1)), as_of=on_a_saturday)

        arrivals = {leg.origin_location: leg.earliest_delivery_date for leg in split.legs}
        assert arrivals == {"BER": date(2026, 10, 12), "WAW": date(2026, 10, 13)}
        assert single.earliest_delivery_date == date(2026, 10, 13)
        assert split.earliest_delivery_date == date(2026, 10, 13)
        assert split.promise.origin_location == "WAW"


class TestInputContract:
    """What the rule refuses, and what it must leave alone."""

    def test_a_naive_as_of_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate(stock_at(("WAW", 100)), as_of=NAIVE_AT)

    def test_a_malformed_country_code_is_refused(self) -> None:
        with pytest.raises(ValueError, match="alpha-2"):
            evaluate(stock_at(("WAW", 100)), destination_country="de")

    def test_a_request_stock_cannot_cover_has_no_delivery_date(self) -> None:
        short = evaluate_stock(
            [StockLevel(product_id=PRODUCT, location="WAW", on_hand_qty=10, as_of=STOCK_AS_OF)],
            product_id=PRODUCT,
            requested_qty=11,
            as_of=STOCK_AS_OF,
        )
        assert short.covered is False

        with pytest.raises(ValueError, match="only for a request stock covers"):
            evaluate(short)

    def test_two_services_with_one_code_are_refused(self) -> None:
        twice = (service("DHL-EXP", "WAW"), service("DHL-EXP", "WAW", transit=(3, 3)))

        with pytest.raises(ValueError, match="more than one carrier service"):
            evaluate(stock_at(("WAW", 100)), services=twice)

    def test_two_warehouses_with_one_code_are_refused(self) -> None:
        twice = (warehouse("WAW", "PL"), warehouse("WAW", "NL"))

        with pytest.raises(ValueError, match="more than one warehouse"):
            evaluate(stock_at(("WAW", 100)), warehouses=twice)

    def test_the_callers_sequences_are_not_modified(self) -> None:
        services = list(CARRIERS)
        warehouses = list(WAREHOUSES)
        holidays = list(CALENDAR)
        stock = stock_at(("WAW", 100))
        before = [list(services), list(warehouses), list(holidays), stock.model_dump()]

        evaluate(stock, services=services, warehouses=warehouses, holidays=holidays)

        assert [list(services), list(warehouses), list(holidays), stock.model_dump()] == before

    def test_an_iterable_is_consumed_once(self) -> None:
        evaluation = evaluate(
            stock_at(("WAW", 100)),
            services=iter(CARRIERS),
            warehouses=iter(WAREHOUSES),
            holidays=iter(CALENDAR),
        )

        assert evaluation.earliest_delivery_date == date(2026, 10, 7)


class TestDeterminism:
    """Same facts, same answer - whatever order they arrive in."""

    def test_repeated_evaluations_are_equal(self) -> None:
        stock = stock_at(("WAW", 100), ("BER", 1))

        assert evaluate(stock) == evaluate(stock)

    def test_input_order_does_not_change_the_result(self) -> None:
        stock = stock_at(("WAW", 100), ("BER", 1))

        forwards = evaluate(stock)
        backwards = evaluate(
            stock,
            services=tuple(reversed(CARRIERS)),
            warehouses=tuple(reversed(WAREHOUSES)),
            holidays=tuple(reversed(CALENDAR)),
        )

        assert forwards == backwards

    def test_the_rationale_names_the_facts_that_produced_it(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)))

        rationale = evaluation.promise.rationale
        assert "WAW (PL) to DE via DHL-EXP" in rationale
        assert "2026-10-06 09:00 UTC" in rationale
        assert "12:00 UTC cut-off" in rationale
        assert "Calendars: PL + DE" in rationale

    def test_generated_text_stays_inside_its_contracts(self) -> None:
        """A long holiday name must not push a rationale past 400 characters."""
        long_calendar = (
            holiday("PL", "2026-10-06", "A public holiday with an unusually long official name"),
            holiday("PL", "2026-10-07", "Another public holiday with a long official name"),
            holiday("PL", "2026-10-08", "A third public holiday with a long official name"),
            holiday("DE", "2026-10-09", "A fourth public holiday with a long official name"),
        )

        evaluation = evaluate(
            stock_at(("WAW", 100), ("BER", 1)),
            as_of=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
            holidays=long_calendar,
        )

        note = evaluation.assessment.split_shipment_note
        assert note is not None
        assert len(note) <= 300
        assert all(len(leg.rationale) <= 400 for leg in evaluation.legs)
        assert len(evaluation.promise.rationale) <= 400


class TestEvaluationContract:
    """A delivery evaluation cannot state a promise its legs do not support."""

    @staticmethod
    def payload(**overrides: object) -> dict[str, object]:
        """A real two-leg evaluation, dumped so one field can be tampered with."""
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))
        payload = evaluation.model_dump()
        payload.update(overrides)
        return payload

    def test_a_consistent_evaluation_survives_a_round_trip(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100), ("BER", 1)))

        assert DeliveryEvaluation.model_validate(evaluation.model_dump()) == evaluation

    def test_a_promise_must_be_the_last_leg_not_the_fastest(self) -> None:
        payload = self.payload()
        assessment = dict(payload["assessment"])  # type: ignore[arg-type]
        assessment["promise"] = dict(payload["legs"][1])  # type: ignore[index]
        payload["assessment"] = assessment

        with pytest.raises(ValidationError, match="must be the leg that arrives last"):
            DeliveryEvaluation.model_validate(payload)

    def test_two_legs_are_a_split_shipment(self) -> None:
        payload = self.payload()
        assessment = dict(payload["assessment"])  # type: ignore[arg-type]
        assessment["split_shipment_proposed"] = False
        assessment["split_shipment_note"] = None
        payload["assessment"] = assessment

        with pytest.raises(ValidationError, match="split shipment is proposed exactly when"):
            DeliveryEvaluation.model_validate(payload)

    def test_legs_must_be_ordered_by_origin_code(self) -> None:
        payload = self.payload()
        payload["legs"] = list(reversed(payload["legs"]))  # type: ignore[arg-type]

        with pytest.raises(ValidationError, match="ordered by location code"):
            DeliveryEvaluation.model_validate(payload)

    def test_every_leg_carries_the_same_question(self) -> None:
        payload = self.payload()
        legs = [dict(leg) for leg in payload["legs"]]  # type: ignore[union-attr]
        legs[1]["destination"] = "Rotterdam"
        payload["legs"] = legs

        with pytest.raises(ValidationError, match="evaluation's destination"):
            DeliveryEvaluation.model_validate(payload)

    def test_a_leg_without_an_origin_is_refused(self) -> None:
        payload = self.payload()
        legs = [dict(leg) for leg in payload["legs"]]  # type: ignore[union-attr]
        legs[0]["origin_location"] = None
        payload["legs"] = legs

        with pytest.raises(ValidationError, match="each leg needs an origin"):
            DeliveryEvaluation.model_validate(payload)

    def test_an_evaluation_needs_a_leg(self) -> None:
        payload = self.payload()
        payload["legs"] = []

        with pytest.raises(ValidationError, match="at least one leg"):
            DeliveryEvaluation.model_validate(payload)

    def test_an_unknown_promise_must_not_carry_dates(self) -> None:
        evaluation = evaluate(stock_at(("WAW", 100)), services=())
        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN

        payload = evaluation.model_dump()
        assessment = dict(payload["assessment"])  # type: ignore[arg-type]
        promise = dict(assessment["promise"])  # type: ignore[arg-type]
        promise["earliest_ship_date"] = "2026-10-06"
        promise["earliest_delivery_date"] = "2026-10-08"
        assessment["promise"] = promise
        payload["assessment"] = assessment

        with pytest.raises(ValidationError, match="must not carry dates"):
            DeliveryEvaluation.model_validate(payload)

    def test_the_evaluation_instant_must_be_aware(self) -> None:
        payload = self.payload()
        payload["as_of"] = NAIVE_AT

        with pytest.raises(ValidationError, match="must be timezone-aware"):
            DeliveryEvaluation.model_validate(payload)

    def test_the_promise_carries_the_question_it_answered(self) -> None:
        payload = self.payload(requested_date=date(2026, 10, 7))
        payload["requested_date"] = date(2026, 10, 8)

        with pytest.raises(ValidationError, match="requested date"):
            DeliveryEvaluation.model_validate(payload)
