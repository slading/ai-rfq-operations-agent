"""Delivery scheduling against the demo dataset, read through the read boundary.

The rule itself is tested in ``tests/unit/test_delivery.py``, with every boundary
placed by hand. This module checks the same rule against the real business data -
the Warsaw and Berlin services with their cut-offs and transit ranges, the 2026
public-holiday calendar of the seven countries - read the way the rest of the
system reads it: database → read models → stock evaluation → delivery
evaluation. It lives with the persistence tests because that seam is what it
exercises.

What is pinned here is that the seeded data, the read boundary and the delivery
rule agree: that the express service really does beat the economy one at 09:00
and lose at 13:00, that Poland's Independence Day really does push a Warsaw
delivery by a day, that a Czech destination really is closed on 28 October, and
that a date leaving the loaded calendar year comes back as UNKNOWN rather than as
a plausible-looking guess.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryEvaluation, DeliveryFeasibility, evaluate_delivery
from rfq_agent.domain.stock import StockEvaluation, evaluate_stock
from rfq_agent.persistence import Database
from rfq_agent.persistence.read_models import CarrierServiceRecord
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import reset_and_seed

#: Tuesday 6 October 2026, 09:00 UTC: a working day, before every cut-off.
AS_OF = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
#: A stamp for the stock facts; the seeded rows all carry the same one.
STOCK_STAMP = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


@pytest.fixture
def seeded(session: Session) -> Session:
    """A committed, freshly seeded database."""
    reset_and_seed(session)
    session.commit()
    return session


@pytest.fixture
def reader(seeded: Session, db: Database) -> Iterator[BusinessReader]:
    """The read boundary, over a second session that only ever reads."""
    del seeded  # dependency only: the database must be seeded before reading
    with db.session_factory() as active:
        yield BusinessReader.for_session(active)


def stock_for(reader: BusinessReader, product_id: str, requested_qty: int) -> StockEvaluation:
    """Cover ``requested_qty`` of one product from the stock the boundary returns."""
    return evaluate_stock(
        reader.stock.levels_for_products([product_id]),
        product_id=product_id,
        requested_qty=requested_qty,
        as_of=STOCK_STAMP,
    )


def deliver(
    reader: BusinessReader,
    stock: StockEvaluation,
    *,
    as_of: datetime = AS_OF,
    destination: str = "Hamburg",
    destination_country: str | None = "DE",
    requested_date: date | None = None,
    services: Sequence[CarrierServiceRecord] | None = None,
) -> DeliveryEvaluation:
    """Ask the delivery question with the facts the read boundary holds."""
    return evaluate_delivery(
        stock,
        destination=destination,
        destination_country=destination_country,
        as_of=as_of,
        requested_date=requested_date,
        services=reader.delivery.services() if services is None else services,
        warehouses=reader.stock.warehouses(),
        holidays=reader.delivery.holidays(),
    )


class TestSeededScheduling:
    def test_the_express_service_wins_a_morning_order(self, reader: BusinessReader) -> None:
        """``DHL-EXP`` leaves Warsaw same-day before 12:00 and arrives next day."""
        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100))

        assert evaluation.promise.carrier_service_code == "DHL-EXP"
        assert evaluation.earliest_ship_date == date(2026, 10, 6)
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)

    def test_the_same_order_after_the_cutoff_takes_the_economy_service(
        self, reader: BusinessReader
    ) -> None:
        """At 13:00 the express cut-off is gone; ``DHL-ECO``'s later one is not."""
        at_one = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)

        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100), as_of=at_one)

        assert evaluation.promise.carrier_service_code == "DHL-ECO"
        assert evaluation.earliest_ship_date == date(2026, 10, 6)
        assert evaluation.earliest_delivery_date == date(2026, 10, 8)

    def test_a_friday_order_crosses_the_weekend(self, reader: BusinessReader) -> None:
        """Friday 9 October at 13:00 ships Friday and arrives on Tuesday."""
        friday = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)

        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100), as_of=friday)

        assert evaluation.earliest_ship_date == date(2026, 10, 9)
        assert evaluation.earliest_delivery_date == date(2026, 10, 13)

    def test_polands_independence_day_pushes_the_delivery(self, reader: BusinessReader) -> None:
        """11 November 2026 is a Polish public holiday, and the Warsaw leg is shut."""
        day_before = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

        evaluation = deliver(reader, stock_for(reader, "PRD_0007", 200), as_of=day_before)

        assert evaluation.earliest_ship_date == date(2026, 11, 10)
        assert evaluation.earliest_delivery_date == date(2026, 11, 12)
        assert "Independence Day" in evaluation.promise.rationale

    def test_the_destination_calendar_changes_the_answer(self, reader: BusinessReader) -> None:
        """Same lane and same day; a Czech buyer is closed on 28 October, a German is not."""
        day_before = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
        stock = stock_for(reader, "PRD_0003", 40)

        to_germany = deliver(reader, stock, as_of=day_before)
        to_czechia = deliver(
            reader, stock, as_of=day_before, destination="Prague", destination_country="CZ"
        )

        assert to_germany.earliest_delivery_date == date(2026, 10, 28)
        assert to_czechia.earliest_delivery_date == date(2026, 10, 29)
        assert "Independent Czechoslovak State Day" in to_czechia.promise.rationale

    def test_the_weekend_capable_service_moves_on_a_saturday(self, reader: BusinessReader) -> None:
        """``DPD-CLS`` is seeded as the service the weekend rule does not apply to."""
        saturday = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 101), as_of=saturday)

        berlin = next(leg for leg in evaluation.legs if leg.origin_location == "BER")
        assert berlin.carrier_service_code == "DPD-CLS"
        assert berlin.earliest_ship_date == date(2026, 10, 10)
        assert berlin.earliest_delivery_date == date(2026, 10, 12)


class TestSeededFeasibility:
    def test_a_requested_date_that_is_met(self, reader: BusinessReader) -> None:
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 100),
            requested_date=date(2026, 10, 7),
        )

        assert evaluation.feasibility is DeliveryFeasibility.FEASIBLE
        assert evaluation.is_blocking() is False

    def test_a_requested_date_that_is_missed(self, reader: BusinessReader) -> None:
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 100),
            requested_date=date(2026, 10, 6),
        )

        assert evaluation.feasibility is DeliveryFeasibility.INFEASIBLE
        assert evaluation.is_blocking() is True

    def test_no_requested_date_asks_nothing_but_still_dates_the_delivery(
        self, reader: BusinessReader
    ) -> None:
        evaluation = deliver(reader, stock_for(reader, "PRD_0002", 40))

        assert evaluation.feasibility is DeliveryFeasibility.NOT_REQUESTED
        assert evaluation.earliest_delivery_date == date(2026, 10, 7)

    def test_the_last_leg_decides_a_split_request(self, reader: BusinessReader) -> None:
        """101 units need both warehouses; Berlin arrives a day after Warsaw."""
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 101),
            requested_date=date(2026, 10, 7),
        )

        arrivals = {leg.origin_location: leg.earliest_delivery_date for leg in evaluation.legs}
        assert arrivals == {"BER": date(2026, 10, 8), "WAW": date(2026, 10, 7)}
        assert evaluation.earliest_delivery_date == date(2026, 10, 8)
        assert evaluation.feasibility is DeliveryFeasibility.INFEASIBLE


class TestSeededSplitShipment:
    def test_a_split_is_reported_leg_by_leg_and_not_decided(self, reader: BusinessReader) -> None:
        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 101))

        note = evaluation.assessment.split_shipment_note
        assert evaluation.is_split is True
        assert evaluation.assessment.split_shipment_proposed is True
        assert note is not None
        assert "WAW 100 units arrive 2026-10-07" in note
        assert "BER 1 units arrive 2026-10-08" in note
        assert "no shipment decision is made here" in note

    def test_each_leg_keeps_its_own_service(self, reader: BusinessReader) -> None:
        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 101))

        chosen = {
            leg.origin_location: (leg.carrier_service_code, leg.transit_days)
            for leg in evaluation.legs
        }
        assert chosen == {"BER": ("DPD-CLS", 2), "WAW": ("DHL-EXP", 1)}

    def test_a_single_covered_warehouse_is_not_a_split(self, reader: BusinessReader) -> None:
        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100))

        assert evaluation.is_split is False
        assert evaluation.assessment.split_shipment_proposed is False
        assert evaluation.assessment.split_shipment_note is None
        assert evaluation.promise.origin_location == "WAW"


class TestSeededUnknownFacts:
    def test_a_country_without_a_calendar_has_no_promise(self, reader: BusinessReader) -> None:
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 100),
            destination="Milan",
            destination_country="IT",
        )

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_ship_date is None
        assert evaluation.earliest_delivery_date is None
        assert "the calendar has no rows for IT" in evaluation.promise.rationale
        assert "no delivery date is guessed" in evaluation.promise.rationale

    def test_a_date_outside_the_calendar_year_has_no_promise(self, reader: BusinessReader) -> None:
        """The seeded calendar is 2026; a delivery landing in 2027 is a hole, not a date."""
        last_day = datetime(2026, 12, 31, 9, 0, tzinfo=UTC)

        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100), as_of=last_day)

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert evaluation.earliest_delivery_date is None
        assert "does not cover 2027" in evaluation.promise.rationale

    def test_services_from_the_wrong_origin_cannot_schedule_a_leg(
        self, reader: BusinessReader
    ) -> None:
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 100),
            services=reader.delivery.services_from("BER"),
        )

        assert evaluation.feasibility is DeliveryFeasibility.UNKNOWN
        assert "no active carrier service ships from WAW" in evaluation.promise.rationale


class TestSeededDeterminism:
    def test_the_same_question_has_the_same_answer(self, reader: BusinessReader) -> None:
        stock = stock_for(reader, "PRD_0001", 101)

        assert deliver(reader, stock) == deliver(reader, stock)

    def test_input_order_does_not_change_the_result(self, reader: BusinessReader) -> None:
        stock = stock_for(reader, "PRD_0001", 101)

        forwards = deliver(reader, stock, services=reader.delivery.services())
        backwards = deliver(reader, stock, services=tuple(reversed(reader.delivery.services())))

        assert forwards == backwards

    def test_a_generator_is_consumed_once(self, reader: BusinessReader) -> None:
        evaluation = deliver(
            reader,
            stock_for(reader, "PRD_0001", 100),
            services=iter(reader.delivery.services()),
        )

        assert evaluation.earliest_delivery_date == date(2026, 10, 7)

    def test_reads_leave_the_dataset_alone(self, reader: BusinessReader) -> None:
        services = reader.delivery.services()
        warehouses = reader.stock.warehouses()
        holidays = reader.delivery.holidays()

        deliver(reader, stock_for(reader, "PRD_0001", 101), services=services)

        assert reader.delivery.services() == services
        assert reader.stock.warehouses() == warehouses
        assert reader.delivery.holidays() == holidays

    def test_every_rationale_stays_inside_its_contract(self, reader: BusinessReader) -> None:
        """Long holiday names and long lanes still fit 400 characters."""
        for day in (
            datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
        ):
            for quantity in (100, 101):
                evaluation = deliver(reader, stock_for(reader, "PRD_0001", quantity), as_of=day)
                note = evaluation.assessment.split_shipment_note
                assert all(len(leg.rationale) <= 400 for leg in evaluation.legs)
                assert len(evaluation.promise.rationale) <= 400
                assert note is None or len(note) <= 300


class TestSeededFreshnessOfTheFacts:
    def test_the_promise_is_computed_from_the_stock_the_boundary_returned(
        self, reader: BusinessReader
    ) -> None:
        """Every origin the stock evaluation names gets a leg, and no other does."""
        stock = stock_for(reader, "PRD_0001", 101)

        evaluation = deliver(reader, stock)

        assert {leg.origin_location for leg in evaluation.legs} == {"BER", "WAW"}
        assert len(evaluation.legs) == len(stock.split)

    def test_the_rationale_names_the_calendars_it_applied(self, reader: BusinessReader) -> None:
        evaluation = deliver(reader, stock_for(reader, "PRD_0001", 100))

        assert "Calendars: PL + DE" in evaluation.promise.rationale
