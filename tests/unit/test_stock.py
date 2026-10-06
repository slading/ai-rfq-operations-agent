"""Tests for the deterministic stock-availability rule.

Stock decides whether a quotation can be honoured, so the rule is pinned
exactly: what counts as available (unreserved stock, and nothing else), where
the boundaries sit, why a request is not covered, and what the multi-warehouse
answer says without deciding anything about shipping.

The tests are table-driven where the table *is* the specification - the
coverage outcomes and the invariants an evaluation may not violate - and
otherwise state one fact each.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from rfq_agent.domain.stock import (
    StockCoverageReason,
    StockEvaluation,
    StockLevel,
    StockStatus,
    WarehouseAllocation,
    evaluate_stock,
)
from tests.conftest import PRODUCT_X, PRODUCT_Y

#: The instant the question is asked in these tests.
AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
#: The same instant with its timezone stripped - what the rule must refuse.
NAIVE = AT.replace(tzinfo=None)


def stock_level(
    location: str = "WAW",
    *,
    on_hand: int = 0,
    reserved: int = 0,
    inbound: int = 0,
    eta: date | None = None,
    as_of: datetime = AT,
    product_id: str = PRODUCT_X,
) -> StockLevel:
    """Build one warehouse position, so a test states only what it is about.

    ``inbound`` gets a default ETA because the contract requires one whenever
    inbound stock is positive; a test that cares about the date passes its own.
    """
    return StockLevel.model_validate(
        {
            "product_id": product_id,
            "location": location,
            "on_hand_qty": on_hand,
            "reserved_qty": reserved,
            "inbound_qty": inbound,
            "inbound_eta": eta if eta is not None else (date(2026, 10, 20) if inbound else None),
            "as_of": as_of,
        }
    )


def evaluate(
    levels: object,
    *,
    requested: int = 10,
    as_of: datetime = AT,
    max_age: timedelta | None = None,
    product_id: str = PRODUCT_X,
) -> StockEvaluation:
    """Evaluate a request for ``PRODUCT_X`` with everything else defaulted."""
    return evaluate_stock(
        levels,  # type: ignore[arg-type]
        product_id=product_id,
        requested_qty=requested,
        as_of=as_of,
        max_age=max_age,
    )


class TestCoverageOutcomes:
    """The three outcomes, and the reason that goes with each."""

    @pytest.mark.parametrize(
        ("on_hand", "reserved", "requested", "status", "reason"),
        [
            (100, 0, 40, StockStatus.SUFFICIENT, None),
            (40, 0, 40, StockStatus.SUFFICIENT, None),
            (40, 1, 40, StockStatus.PARTIAL, StockCoverageReason.INSUFFICIENT_TOTAL),
            (40, 0, 41, StockStatus.PARTIAL, StockCoverageReason.INSUFFICIENT_TOTAL),
            (40, 40, 1, StockStatus.NONE, StockCoverageReason.ZERO_AVAILABLE),
            (0, 0, 1, StockStatus.NONE, StockCoverageReason.ZERO_AVAILABLE),
        ],
    )
    def test_coverage_table(
        self,
        on_hand: int,
        reserved: int,
        requested: int,
        status: StockStatus,
        reason: StockCoverageReason | None,
    ) -> None:
        evaluation = evaluate(
            [stock_level(on_hand=on_hand, reserved=reserved)], requested=requested
        )

        assert evaluation.status is status
        assert evaluation.reason is reason

    def test_a_covered_request_reports_the_warehouse_that_covers_it(self) -> None:
        evaluation = evaluate([stock_level("WAW", on_hand=100)], requested=40)

        assert evaluation.covered is True
        assert evaluation.available_qty == 100
        assert evaluation.shortfall_qty == 0
        assert evaluation.covering_locations == ("WAW",)
        assert evaluation.single_warehouse_cover == "WAW"
        assert evaluation.requires_split is False
        assert evaluation.split == ()
        assert "WAW alone covers 40" in evaluation.detail

    def test_the_exact_quantity_is_covered(self) -> None:
        """The boundary is inclusive: 40 available covers a request for 40."""
        evaluation = evaluate([stock_level(on_hand=40)], requested=40)

        assert evaluation.status is StockStatus.SUFFICIENT

    def test_one_unit_short_is_partial(self) -> None:
        evaluation = evaluate([stock_level(on_hand=39)], requested=40)

        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.available_qty == 39
        assert evaluation.shortfall_qty == 1
        assert evaluation.reason is StockCoverageReason.INSUFFICIENT_TOTAL

    def test_reserved_stock_is_not_available(self) -> None:
        """On hand is not the promise: 120 on hand with 20 reserved is 100."""
        position = [stock_level(on_hand=120, reserved=20)]

        assert evaluate(position, requested=100).status is StockStatus.SUFFICIENT
        short = evaluate(position, requested=101)

        assert short.status is StockStatus.PARTIAL
        assert short.available_qty == 100
        assert short.shortfall_qty == 1

    def test_a_fully_reserved_position_offers_nothing(self) -> None:
        evaluation = evaluate([stock_level(on_hand=40, reserved=40)], requested=1)

        assert evaluation.status is StockStatus.NONE
        assert evaluation.available_qty == 0
        assert evaluation.reason is StockCoverageReason.ZERO_AVAILABLE
        assert "40 units on hand, all reserved" in evaluation.detail

    def test_a_product_with_no_position_anywhere_is_not_stocked(self) -> None:
        evaluation = evaluate([], requested=1)

        assert evaluation.status is StockStatus.NONE
        assert evaluation.reason is StockCoverageReason.NOT_STOCKED
        assert evaluation.available_qty == 0
        assert evaluation.levels == ()
        assert PRODUCT_X in evaluation.detail

    def test_positions_for_another_product_are_ignored(self) -> None:
        """The read boundary returns batches; the evaluation answers about one product."""
        mixed = [
            stock_level("WAW", on_hand=100, product_id=PRODUCT_Y),
            stock_level("BER", on_hand=7),
        ]

        evaluation = evaluate(mixed, requested=5)

        assert evaluation.available_qty == 7
        assert [level.location for level in evaluation.levels] == ["BER"]


class TestInboundIsInformational:
    """Inbound stock is reported and never counted."""

    def test_inbound_alone_does_not_cover_a_request(self) -> None:
        evaluation = evaluate(
            [stock_level(on_hand=0, inbound=100, eta=date(2026, 10, 20))], requested=10
        )

        assert evaluation.status is StockStatus.NONE
        assert evaluation.reason is StockCoverageReason.ZERO_AVAILABLE
        assert evaluation.available_qty == 0
        assert evaluation.inbound_qty == 100
        assert evaluation.earliest_inbound_eta == date(2026, 10, 20)
        assert "100 inbound units are not counted as available" in evaluation.detail

    def test_inbound_does_not_soften_a_partial_verdict(self) -> None:
        evaluation = evaluate([stock_level(on_hand=40, inbound=60)], requested=90)

        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.available_qty == 40
        assert evaluation.shortfall_qty == 50
        assert evaluation.inbound_qty == 60

    def test_inbound_does_not_change_availability_at_all(self) -> None:
        without = evaluate([stock_level(on_hand=40)], requested=10)
        with_inbound = evaluate([stock_level(on_hand=40, inbound=500)], requested=10)

        assert without.available_qty == with_inbound.available_qty == 40
        assert without.status is with_inbound.status

    def test_the_earliest_inbound_eta_is_reported(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=1, inbound=10, eta=date(2026, 11, 2)),
                stock_level("BER", on_hand=1, inbound=5, eta=date(2026, 10, 13)),
            ],
            requested=1,
        )

        assert evaluation.inbound_qty == 15
        assert evaluation.earliest_inbound_eta == date(2026, 10, 13)

    def test_a_covered_request_needs_no_inbound_caveat(self) -> None:
        evaluation = evaluate([stock_level(on_hand=40, inbound=60)], requested=10)

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.inbound_qty == 60
        assert "not counted" not in evaluation.detail


class TestMultiWarehouse:
    """What several positions together do and do not mean."""

    def test_one_of_several_warehouses_covers_the_request(self) -> None:
        evaluation = evaluate(
            [stock_level("WAW", on_hand=100), stock_level("BER", on_hand=10)], requested=40
        )

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.covering_locations == ("WAW",)
        assert evaluation.single_warehouse_cover == "WAW"
        assert evaluation.requires_split is False

    def test_every_covering_warehouse_is_reported_in_code_order(self) -> None:
        evaluation = evaluate(
            [stock_level("WAW", on_hand=100), stock_level("BER", on_hand=60)], requested=50
        )

        assert evaluation.covering_locations == ("BER", "WAW")
        # The canonical pick is the first by code - a reproducible answer, not a
        # shipping preference.
        assert evaluation.single_warehouse_cover == "BER"

    def test_combined_stock_can_cover_a_request_no_single_warehouse_covers(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=10),
                stock_level("BER", on_hand=20),
                stock_level("XXX", on_hand=30),
            ],
            requested=45,
        )

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.covering_locations == ()
        assert evaluation.single_warehouse_cover is None
        assert evaluation.requires_split is True
        assert evaluation.available_qty == 60
        assert "covered only by combining warehouses" in evaluation.detail

    def test_the_split_fills_the_largest_position_first(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=10),
                stock_level("BER", on_hand=20),
                stock_level("XXX", on_hand=30),
            ],
            requested=45,
        )

        assert [(item.location, item.qty) for item in evaluation.split] == [
            ("XXX", 30),
            ("BER", 15),
        ]

    def test_split_ties_are_broken_by_location_code(self) -> None:
        evaluation = evaluate(
            [stock_level("BER", on_hand=14), stock_level("WAW", on_hand=14)], requested=25
        )

        assert [(item.location, item.qty) for item in evaluation.split] == [
            ("BER", 14),
            ("WAW", 11),
        ]

    def test_a_position_with_nothing_available_gets_no_share(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=0),
                stock_level("BER", on_hand=30),
                stock_level("XXX", on_hand=20),
            ],
            requested=40,
        )

        assert [(item.location, item.qty) for item in evaluation.split] == [
            ("BER", 30),
            ("XXX", 10),
        ]

    def test_the_combined_boundary_is_covered_exactly(self) -> None:
        evaluation = evaluate(
            [stock_level("WAW", on_hand=10), stock_level("BER", on_hand=20)], requested=30
        )

        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.shortfall_qty == 0
        assert sum(item.qty for item in evaluation.split) == 30
        assert "5 short" not in evaluation.detail

    def test_no_split_is_proposed_when_one_warehouse_covers(self) -> None:
        evaluation = evaluate(
            [stock_level("WAW", on_hand=100), stock_level("BER", on_hand=10)], requested=40
        )

        assert evaluation.split == ()
        assert evaluation.requires_split is False

    def test_no_split_is_proposed_when_the_total_falls_short(self) -> None:
        evaluation = evaluate(
            [stock_level("WAW", on_hand=10), stock_level("BER", on_hand=20)], requested=40
        )

        assert evaluation.status is StockStatus.PARTIAL
        assert evaluation.shortfall_qty == 10
        assert evaluation.split == ()

    def test_warehouses_are_ordered_by_location_code(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=10),
                stock_level("BER", on_hand=10),
                stock_level("XXX", on_hand=10),
            ],
            requested=5,
        )

        assert [level.location for level in evaluation.levels] == ["BER", "WAW", "XXX"]


class TestFreshnessOfFacts:
    """Age is reported as a fact; it never changes the numbers."""

    def test_recent_facts_are_not_stale(self) -> None:
        evaluation = evaluate(
            [stock_level(as_of=AT - timedelta(hours=1))],
            requested=1,
            max_age=timedelta(days=1),
        )

        assert evaluation.stale_locations == ()

    def test_an_old_fact_is_reported_and_the_numbers_stand(self) -> None:
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=100, as_of=AT - timedelta(hours=1)),
                stock_level("BER", on_hand=5, as_of=AT - timedelta(days=3)),
            ],
            requested=40,
            max_age=timedelta(days=1),
        )

        assert evaluation.stale_locations == ("BER",)
        assert evaluation.status is StockStatus.SUFFICIENT
        assert evaluation.available_qty == 105
        assert "facts for BER are older than allowed" in evaluation.detail

    def test_a_fact_exactly_at_the_tolerance_is_still_fresh(self) -> None:
        evaluation = evaluate(
            [stock_level(as_of=AT - timedelta(days=1))],
            requested=1,
            max_age=timedelta(days=1),
        )

        assert evaluation.stale_locations == ()

    def test_without_a_tolerance_nothing_is_called_stale(self) -> None:
        """No default tolerance: a threshold nobody chose is a hidden policy."""
        evaluation = evaluate([stock_level(as_of=AT - timedelta(days=3650))], requested=1)

        assert evaluation.stale_locations == ()

    def test_positions_are_judged_on_their_own_timestamps(self) -> None:
        """Different warehouses count at different moments; each ages on its own."""
        evaluation = evaluate(
            [
                stock_level("WAW", on_hand=100, as_of=AT - timedelta(minutes=5)),
                stock_level("BER", on_hand=5, as_of=AT - timedelta(days=2)),
            ],
            requested=1,
            max_age=timedelta(days=1),
        )

        assert evaluation.stale_locations == ("BER",)
        assert [level.as_of for level in evaluation.levels] != []

    def test_a_fact_dated_after_the_evaluation_is_reported(self) -> None:
        evaluation = evaluate(
            [stock_level(on_hand=100, as_of=AT + timedelta(hours=1))], requested=10
        )

        assert evaluation.future_dated_locations == ("WAW",)
        assert evaluation.status is StockStatus.SUFFICIENT
        assert "dated after the evaluation" in evaluation.detail


class TestInputContract:
    """Inputs that are refused rather than guessed at."""

    def test_two_positions_for_one_warehouse_are_refused(self) -> None:
        with pytest.raises(ValueError, match="more than one stock level for WAW"):
            evaluate([stock_level(on_hand=10), stock_level(on_hand=20)], requested=1)

    @pytest.mark.parametrize("requested", [0, -1])
    def test_a_request_below_one_is_a_caller_error(self, requested: int) -> None:
        with pytest.raises(ValueError, match="requested_qty must be at least 1"):
            evaluate([stock_level(on_hand=10)], requested=requested)

    def test_the_evaluation_instant_must_be_aware(self) -> None:
        with pytest.raises(ValueError, match="as_of must be timezone-aware"):
            evaluate([stock_level()], requested=1, as_of=NAIVE)

    def test_a_naive_position_timestamp_is_refused(self) -> None:
        naive = StockLevel.model_validate(
            {
                "product_id": PRODUCT_X,
                "location": "WAW",
                "on_hand_qty": 10,
                "as_of": NAIVE,
            }
        )

        with pytest.raises(ValueError, match="naive as_of"):
            evaluate([naive], requested=1)

    def test_a_negative_tolerance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_age must not be negative"):
            evaluate([stock_level()], requested=1, max_age=timedelta(minutes=-1))


class TestEvaluationContract:
    """An evaluation may not claim more than its evidence supports."""

    def valid_payload(self, **overrides: object) -> dict[str, object]:
        """A minimal consistent evaluation, for tests that break one field."""
        payload: dict[str, object] = {
            "product_id": PRODUCT_X,
            "requested_qty": 10,
            "as_of": AT,
            "status": StockStatus.SUFFICIENT,
            "available_qty": 50,
            "shortfall_qty": 0,
            "levels": (stock_level(on_hand=50),),
            "covering_locations": ("WAW",),
            "single_warehouse_cover": "WAW",
            "detail": "one warehouse covers it",
        }
        payload.update(overrides)
        return payload

    def test_a_consistent_evaluation_is_accepted(self) -> None:
        evaluation = StockEvaluation.model_validate(self.valid_payload())

        assert evaluation.covered is True
        assert evaluation.requires_split is False

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("status", StockStatus.UNKNOWN, "never UNKNOWN"),
            ("available_qty", 49, "!= sum of levels"),
            ("shortfall_qty", 3, "shortfall_qty"),
            ("covering_locations", (), "covering_locations"),
            ("single_warehouse_cover", "BER", "first covering location"),
            ("stale_locations", ("BER",), "must have a level"),
        ],
    )
    def test_an_evaluation_that_contradicts_its_evidence_is_refused(
        self, field: str, value: object, message: str
    ) -> None:
        with pytest.raises(ValidationError, match=message):
            StockEvaluation.model_validate(self.valid_payload(**{field: value}))

    def test_a_status_that_overstates_the_stock_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="inconsistent with available"):
            StockEvaluation.model_validate(
                self.valid_payload(
                    status=StockStatus.SUFFICIENT,
                    available_qty=0,
                    levels=(stock_level(on_hand=0),),
                    covering_locations=(),
                    single_warehouse_cover=None,
                )
            )

    def test_a_split_that_is_not_needed_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="only when coverage needs more"):
            StockEvaluation.model_validate(
                self.valid_payload(split=(WarehouseAllocation(location="WAW", qty=10),))
            )

    def test_a_split_that_does_not_add_up_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must add up"):
            StockEvaluation.model_validate(
                self.valid_payload(
                    available_qty=12,
                    levels=(stock_level("WAW", on_hand=6), stock_level("BER", on_hand=6)),
                    covering_locations=(),
                    single_warehouse_cover=None,
                    split=(WarehouseAllocation(location="WAW", qty=6),),
                )
            )

    def test_a_split_taking_more_than_a_warehouse_has_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="which has less"):
            StockEvaluation.model_validate(
                self.valid_payload(
                    available_qty=12,
                    levels=(stock_level("WAW", on_hand=6), stock_level("BER", on_hand=6)),
                    covering_locations=(),
                    single_warehouse_cover=None,
                    split=(WarehouseAllocation(location="WAW", qty=10),),
                )
            )

    def test_an_uncovered_request_must_carry_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="must say why"):
            StockEvaluation.model_validate(
                self.valid_payload(
                    status=StockStatus.PARTIAL,
                    available_qty=5,
                    shortfall_qty=5,
                    levels=(stock_level(on_hand=5),),
                    covering_locations=(),
                    single_warehouse_cover=None,
                )
            )

    def test_a_covered_request_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="carries no reason"):
            StockEvaluation.model_validate(
                self.valid_payload(reason=StockCoverageReason.NOT_STOCKED)
            )


class TestDeterminism:
    """Same facts, same answer - whatever order they arrive in."""

    def test_input_order_does_not_change_the_result(self) -> None:
        levels = [
            stock_level("WAW", on_hand=10),
            stock_level("BER", on_hand=20),
            stock_level("XXX", on_hand=30),
        ]

        results = {
            evaluate(list(order), requested=45, max_age=timedelta(days=30))
            for order in itertools.permutations(levels)
        }

        assert len(results) == 1
        assert next(iter(results)).requires_split is True

    def test_repeated_evaluations_are_equal(self) -> None:
        levels = [stock_level(on_hand=100)]

        assert evaluate(levels, requested=10) == evaluate(levels, requested=10)

    def test_the_callers_sequence_is_not_modified(self) -> None:
        levels = [stock_level("WAW", on_hand=1), stock_level("BER", on_hand=2)]
        snapshot = list(levels)

        evaluate(levels, requested=1)

        assert levels == snapshot

    def test_an_iterable_is_consumed_once(self) -> None:
        levels = [stock_level(on_hand=100)]

        from_generator = evaluate((level for level in levels), requested=10)

        assert from_generator == evaluate(levels, requested=10)

    def test_the_evidence_carries_the_facts_unrounded(self) -> None:
        evaluation = evaluate([stock_level(on_hand=120, reserved=20)], requested=40)

        assert evaluation.levels[0].on_hand_qty == 120
        assert evaluation.levels[0].reserved_qty == 20
        assert evaluation.levels[0].available_qty == 100
        assert isinstance(evaluation.available_qty, int)
