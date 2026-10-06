"""Stock availability against the demo dataset, through the read boundary.

The rule itself is tested in ``tests/unit/test_stock.py``, where every boundary
can be placed on an exact unit. This module checks it against the Northwind
Components warehouse data - Warsaw and Berlin, their reservations and their
inbound shipments - read the way the rest of the system will read it: database →
read models → deterministic evaluation. It lives with the persistence tests
because that seam is what it exercises.

The interesting seeded cases are all real ones: a request only Warsaw can cover,
a request only the *two* warehouses together can cover, a Berlin position that
is entirely inbound, and a discontinued product that has no position at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from rfq_agent.domain.stock import (
    StockCoverageReason,
    StockEvaluation,
    StockLevel,
    StockStatus,
    evaluate_stock,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import reset_and_seed

#: A "now" after the demo stock stamp (2026-10-01 06:00 UTC), by five days.
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


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


def levels_for(reader: BusinessReader, *product_ids: str) -> tuple[StockLevel, ...]:
    """Return the stock positions the read boundary sees for those products."""
    return reader.stock.levels_for_products(list(product_ids))


def evaluate(
    levels: Sequence[StockLevel],
    *,
    product_id: str,
    requested: int,
    as_of: datetime = NOW,
    max_age: timedelta | None = None,
) -> StockEvaluation:
    """Ask whether ``requested`` units are covered, with the clock stated."""
    return evaluate_stock(
        levels,
        product_id=product_id,
        requested_qty=requested,
        as_of=as_of,
        max_age=max_age,
    )


class TestSeededCoverage:
    def test_warsaw_alone_covers_the_demo_request(self, reader: BusinessReader) -> None:
        """``PRD_0001``: 120 on hand at Warsaw, 20 of them reserved - 100 available."""
        evaluation = evaluate(levels_for(reader, "PRD_0001"), product_id="PRD_0001", requested=100)

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.available_qty == 145
        assert evaluation.covering_locations == ("WAW",)
        assert evaluation.single_warehouse_cover == "WAW"
        assert evaluation.requires_split is False
        assert "WAW alone covers 100" in evaluation.detail

    def test_reservations_are_what_makes_the_decision(self, reader: BusinessReader) -> None:
        """175 units are on hand, but only 145 are unreserved."""
        levels = levels_for(reader, "PRD_0001")
        evaluation = evaluate(levels, product_id="PRD_0001", requested=146)

        assert sum(level.on_hand_qty for level in levels) == 175
        assert evaluation.available_qty == 145
        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.shortfall_qty == 1
        assert evaluation.reason is StockCoverageReason.INSUFFICIENT_TOTAL

    @pytest.mark.parametrize(
        ("requested", "status"),
        [
            (100, StockStatus.SUFFICIENT),
            (101, StockStatus.SUFFICIENT),
            (145, StockStatus.SUFFICIENT),
            (146, StockStatus.PARTIAL),
        ],
    )
    def test_the_boundaries_of_the_demo_positions(
        self, reader: BusinessReader, requested: int, status: StockStatus
    ) -> None:
        evaluation = evaluate(
            levels_for(reader, "PRD_0001"), product_id="PRD_0001", requested=requested
        )

        assert evaluation.status is status

    def test_two_warehouses_together_cover_what_neither_covers_alone(
        self, reader: BusinessReader
    ) -> None:
        warsaw = reader.stock.level("WAW", "PRD_0001")
        assert warsaw is not None
        assert warsaw.available_qty == 100  # Warsaw alone cannot reach 101

        evaluation = evaluate(levels_for(reader, "PRD_0001"), product_id="PRD_0001", requested=101)

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.covering_locations == ()
        assert evaluation.single_warehouse_cover is None
        assert evaluation.requires_split is True
        assert [(item.location, item.qty) for item in evaluation.split] == [
            ("WAW", 100),
            ("BER", 1),
        ]

    def test_the_demo_split_is_reported_not_decided(self, reader: BusinessReader) -> None:
        """The proposal says what would work; it says nothing about shipping it."""
        evaluation = evaluate(levels_for(reader, "PRD_0001"), product_id="PRD_0001", requested=145)

        assert sum(item.qty for item in evaluation.split) == 145
        assert evaluation.detail.startswith("145 of PRD_0001 is covered only by combining")
        # Every fact behind the proposal is visible: nothing was hidden in a choice.
        assert [level.location for level in evaluation.levels] == ["BER", "WAW"]

    def test_a_small_stock_can_need_a_split_that_no_big_warehouse_does(
        self, reader: BusinessReader
    ) -> None:
        """``PRD_0015``: 18 at Warsaw and 6 at Berlin, requested 20."""
        evaluation = evaluate(levels_for(reader, "PRD_0015"), product_id="PRD_0015", requested=20)

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.requires_split is True
        assert [(item.location, item.qty) for item in evaluation.split] == [
            ("WAW", 18),
            ("BER", 2),
        ]

    def test_two_warehouses_each_covering_are_both_reported(self, reader: BusinessReader) -> None:
        """``PRD_0009``: 800 unreserved at Warsaw, 450 at Berlin."""
        evaluation = evaluate(levels_for(reader, "PRD_0009"), product_id="PRD_0009", requested=400)

        assert evaluation.covering_locations == ("BER", "WAW")
        assert evaluation.single_warehouse_cover == "BER"
        assert evaluation.requires_split is False


class TestSeededInbound:
    def test_inbound_stock_is_reported_and_not_counted(self, reader: BusinessReader) -> None:
        """``PRD_0002``: 40 unreserved now, 60 more promised for 2026-10-20."""
        evaluation = evaluate(levels_for(reader, "PRD_0002"), product_id="PRD_0002", requested=90)

        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.available_qty == 40
        assert evaluation.shortfall_qty == 50
        assert evaluation.inbound_qty == 60
        assert evaluation.earliest_inbound_eta is not None
        assert evaluation.earliest_inbound_eta.isoformat() == "2026-10-20"
        assert "not counted as available" in evaluation.detail

    def test_a_position_that_is_entirely_inbound_offers_nothing(
        self, reader: BusinessReader
    ) -> None:
        """``BER``/``PRD_0011`` is empty today with 120 units arriving on 2026-10-13."""
        position = reader.stock.level("BER", "PRD_0011")
        assert position is not None

        evaluation = evaluate([position], product_id="PRD_0011", requested=10)

        assert evaluation.status is StockStatus.NONE
        assert evaluation.reason is StockCoverageReason.ZERO_AVAILABLE
        assert evaluation.available_qty == 0
        assert evaluation.inbound_qty == 120
        assert evaluation.earliest_inbound_eta is not None
        assert evaluation.earliest_inbound_eta.isoformat() == "2026-10-13"

    def test_inbound_at_another_warehouse_does_not_close_the_gap(
        self, reader: BusinessReader
    ) -> None:
        """Warsaw holds 750 of ``PRD_0011``; Berlin's 120 inbound adds nothing to it."""
        evaluation = evaluate(levels_for(reader, "PRD_0011"), product_id="PRD_0011", requested=800)

        assert evaluation.available_qty == 750
        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.shortfall_qty == 50
        assert evaluation.inbound_qty == 120


class TestSeededAbsence:
    def test_a_product_with_no_position_anywhere_is_not_stocked(
        self, reader: BusinessReader
    ) -> None:
        """The discontinued ``PRD_0006`` is still a catalogue item, with no stock."""
        assert reader.catalog.search("PMP-D-300").matched_ids == ("PRD_0006",)
        assert levels_for(reader, "PRD_0006") == ()

        evaluation = evaluate((), product_id="PRD_0006", requested=1)

        assert evaluation.status is StockStatus.NONE
        assert evaluation.reason is StockCoverageReason.NOT_STOCKED
        assert evaluation.levels == ()
        assert "not stocked in any warehouse" in evaluation.detail


class TestSeededDeterminismAndFreshness:
    def test_the_evidence_is_ordered_by_warehouse_code(self, reader: BusinessReader) -> None:
        evaluation = evaluate(levels_for(reader, "PRD_0001"), product_id="PRD_0001", requested=1)

        assert [level.location for level in evaluation.levels] == ["BER", "WAW"]

    def test_a_batch_read_is_answered_for_one_product(self, reader: BusinessReader) -> None:
        """The boundary returns batches; the evaluation answers about one product."""
        mixed = levels_for(reader, "PRD_0001", "PRD_0002")

        evaluation = evaluate(mixed, product_id="PRD_0002", requested=10)

        assert [level.location for level in evaluation.levels] == ["WAW"]
        assert evaluation.available_qty == 40

    def test_the_demo_stamp_is_five_days_old(self, reader: BusinessReader) -> None:
        levels = levels_for(reader, "PRD_0001")

        generous = evaluate(levels, product_id="PRD_0001", requested=1, max_age=timedelta(days=30))
        strict = evaluate(levels, product_id="PRD_0001", requested=1, max_age=timedelta(days=1))

        assert generous.stale_locations == ()
        assert strict.stale_locations == ("BER", "WAW")
        # Age is a fact about the evidence; the coverage numbers are unchanged.
        assert generous.status is strict.status is StockStatus.SUFFICIENT
        assert "older than allowed" in strict.detail

    def test_every_seeded_position_crosses_as_derived_integers(
        self, reader: BusinessReader
    ) -> None:
        levels = levels_for(reader, "PRD_0001", "PRD_0011", "PRD_0015")

        assert levels
        for level in levels:
            assert isinstance(level.available_qty, int)
            assert level.available_qty == level.on_hand_qty - level.reserved_qty

    def test_the_same_question_has_the_same_answer(self, reader: BusinessReader) -> None:
        levels = levels_for(reader, "PRD_0001")

        assert evaluate(levels, product_id="PRD_0001", requested=101) == evaluate(
            levels, product_id="PRD_0001", requested=101
        )
        assert evaluate(levels, product_id="PRD_0001", requested=101) == evaluate(
            tuple(reversed(levels)), product_id="PRD_0001", requested=101
        )
