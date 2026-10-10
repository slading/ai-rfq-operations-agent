"""The resolved-RFQ to ``QuoteRequest`` seam's contract (Phase 1M-A).

Two kinds of check live here. The first are the adapter's own refusals and
mappings: what travels from the resolved facts into a request, what is
refused rather than invented, and what a caller must state outright. Every
refusal is checked by message, so a future edit that quietly starts dropping
a delivery request or filling in a quantity fails here.

The second parses ``rfq_agent.quote_adapter`` itself: a pure adapter that
"mints no identifier and reads no clock" is a claim a reviewer should be able
to re-run, and an AST walk over the module is how the quoting tests re-run
their own version of that claim. The purity of this seam matters because the
quote engine above it is deterministic only given a request that was built
without side effects.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from rfq_agent import quote_adapter
from rfq_agent.domain.extraction import DateResolution, ExtractedLine, RequestedDelivery
from rfq_agent.domain.resolution import (
    CustomerMatchStatus,
    ResolutionMatchStatus,
    ResolutionSource,
    ResolvedCustomer,
    ResolvedLine,
)
from rfq_agent.quote_adapter import to_quote_request

MODULE_PATH = Path(quote_adapter.__file__)

#: Modules a pure seam has no business importing: clocks, minting, I/O.
_BANNED_IMPORTS = frozenset({"uuid", "random", "secrets", "time", "rfq_agent.persistence"})

#: Call attributes that would make the adapter stateful or non-deterministic.
_BANNED_CALLS = frozenset({"now", "utcnow", "today", "uuid4", "perf_counter", "monotonic"})


def make_extracted(
    ordinal: int = 1,
    *,
    description: str | None = "Hydraulic pump X-120",
    quantity: int | None = 40,
) -> ExtractedLine:
    payload: dict[str, object] = {
        "ordinal": ordinal,
        "raw_text": "40 units of X-120",
        "requested_sku": "X-120",
        "description": description,
        "evidence": "40 units of X-120",
    }
    if quantity is None:
        payload["missing_reason"] = "NOT_STATED"
    else:
        payload["quantity"] = quantity
    return ExtractedLine.model_validate(payload)


def make_line(
    ordinal: int = 1,
    *,
    status: ResolutionMatchStatus = ResolutionMatchStatus.RESOLVED,
    extracted: ExtractedLine | None = None,
    product_id: str | None = "PRD-0001",
    sku: str | None = "X-120",
    source: ResolutionSource | None = ResolutionSource.SYSTEM,
) -> ResolvedLine:
    return ResolvedLine(
        line_item_id=f"LI-000{ordinal}",
        ordinal=ordinal,
        extracted=extracted or make_extracted(ordinal),
        product_id=product_id,
        sku=sku,
        status=status,
        source=source,
    )


def make_customer(
    *,
    customer_id: str | None = "CUST-0001",
    match_status: CustomerMatchStatus = CustomerMatchStatus.EXACT,
    source: ResolutionSource | None = ResolutionSource.SYSTEM,
) -> ResolvedCustomer:
    return ResolvedCustomer(customer_id=customer_id, match_status=match_status, source=source)


def stated(**overrides: object) -> dict[str, object]:
    """The facts only a caller can hold: identifiers and instants."""
    inputs: dict[str, object] = {
        "run_id": "RUN-0001",
        "quote_id": "QTE-0001",
        "quote_number": "Q-2026-000123",
        "pricing_as_of": date(2026, 10, 10),
        "stock_as_of": datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
    }
    inputs.update(overrides)
    return inputs


class TestMapping:
    def test_single_line_request_carries_every_stated_fact(self) -> None:
        request = to_quote_request(make_customer(), [make_line()], **stated())

        assert request.run_id == "RUN-0001"
        assert request.quote_id == "QTE-0001"
        assert request.quote_number == "Q-2026-000123"
        assert request.customer_id == "CUST-0001"
        assert request.pricing_as_of == date(2026, 10, 10)
        assert request.stock_as_of == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

        (line,) = request.lines
        assert line.product_id == "PRD-0001"
        assert line.sku == "X-120"
        assert line.description == "Hydraulic pump X-120"
        assert line.quantity == 40

    def test_multi_line_request_preserves_the_resolved_order(self) -> None:
        lines = [
            make_line(1, extracted=make_extracted(1)),
            make_line(
                2,
                extracted=make_extracted(2, description="Seal kit", quantity=5),
                product_id="PRD-0002",
                sku="X-240",
            ),
        ]
        request = to_quote_request(make_customer(), lines, **stated())

        assert [line.product_id for line in request.lines] == ["PRD-0001", "PRD-0002"]
        assert [line.quantity for line in request.lines] == [40, 5]

    def test_explicit_delivery_request_is_asked_about(self) -> None:
        requested = RequestedDelivery(
            raw="on 2026-10-09",
            resolution=DateResolution.EXPLICIT,
            requested_delivery_date=date(2026, 10, 9),
            destination="Warsaw",
        )
        request = to_quote_request(
            make_customer(),
            [make_line()],
            requested_delivery=requested,
            delivery_as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
            **stated(),
        )

        assert request.delivery is not None
        assert request.delivery.destination == "Warsaw"
        assert request.delivery.as_of == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
        assert request.delivery.requested_date == date(2026, 10, 9)
        assert request.delivery.destination_country is None

    def test_inferred_delivery_never_asserts_a_date(self) -> None:
        requested = RequestedDelivery(
            raw="by Friday",
            resolution=DateResolution.INFERRED,
            destination="Warsaw",
        )
        request = to_quote_request(
            make_customer(),
            [make_line()],
            requested_delivery=requested,
            delivery_as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
            **stated(),
        )

        assert request.delivery is not None
        assert request.delivery.requested_date is None

    def test_a_recorded_destination_travels_without_a_date_request(self) -> None:
        requested = RequestedDelivery(destination="Warsaw")
        request = to_quote_request(
            make_customer(),
            [make_line()],
            requested_delivery=requested,
            delivery_as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
            **stated(),
        )

        assert request.delivery is not None
        assert request.delivery.destination == "Warsaw"
        assert request.delivery.requested_date is None

    def test_no_delivery_claim_leaves_the_question_unasked(self) -> None:
        request = to_quote_request(
            make_customer(),
            [make_line()],
            requested_delivery=RequestedDelivery(),
            **stated(),
        )
        assert request.delivery is None

    def test_unstated_fields_stay_unset(self) -> None:
        request = to_quote_request(make_customer(), [make_line()], **stated())

        (line,) = request.lines
        assert line.notes is None
        assert request.customer_tier is None
        assert request.discount is None

    def test_human_approval_stays_mandatory(self) -> None:
        request = to_quote_request(make_customer(), [make_line()], **stated())
        assert request.require_human_approval is True

    def test_the_same_inputs_produce_the_same_request(self) -> None:
        first = to_quote_request(make_customer(), [make_line()], **stated())
        second = to_quote_request(make_customer(), [make_line()], **stated())
        assert first == second

    def test_a_customer_bound_by_a_human_is_quotable(self) -> None:
        customer = make_customer(
            customer_id="CUST-0002",
            match_status=CustomerMatchStatus.NO_MATCH,
            source=ResolutionSource.HUMAN,
        )
        request = to_quote_request(customer, [make_line()], **stated())
        assert request.customer_id == "CUST-0002"


class TestRefusals:
    def test_refuses_an_unbound_customer(self) -> None:
        customer = make_customer(
            customer_id=None,
            match_status=CustomerMatchStatus.UNBOUND,
            source=None,
        )
        with pytest.raises(ValueError, match="customer is unresolved"):
            to_quote_request(customer, [make_line()], **stated())

    @pytest.mark.parametrize(
        "match_status",
        [
            CustomerMatchStatus.SINGLE_CANDIDATE,
            CustomerMatchStatus.AMBIGUOUS,
            CustomerMatchStatus.NO_MATCH,
        ],
    )
    def test_refuses_a_customer_awaiting_a_human_decision(
        self, match_status: CustomerMatchStatus
    ) -> None:
        customer = make_customer(match_status=match_status, source=ResolutionSource.AGENT)
        with pytest.raises(ValueError, match="requires a human decision"):
            to_quote_request(customer, [make_line()], **stated())

    def test_refuses_no_lines(self) -> None:
        with pytest.raises(ValueError, match="at least one line"):
            to_quote_request(make_customer(), [], **stated())

    @pytest.mark.parametrize(
        ("status", "line_overrides"),
        [
            (
                ResolutionMatchStatus.AMBIGUOUS,
                {"product_id": "PRD-0001", "sku": "X-120", "source": None},
            ),
            (
                ResolutionMatchStatus.UNMATCHED,
                {"product_id": None, "sku": None, "source": None},
            ),
            (
                ResolutionMatchStatus.MISSING_QTY,
                {"product_id": "PRD-0001", "sku": "X-120", "source": None},
            ),
            (
                ResolutionMatchStatus.REJECTED,
                {"product_id": None, "sku": None, "source": None},
            ),
            (
                ResolutionMatchStatus.DISCONTINUED,
                {"product_id": "PRD-0001", "sku": "X-120", "source": ResolutionSource.SYSTEM},
            ),
        ],
    )
    def test_refuses_a_line_that_is_not_resolved(
        self, status: ResolutionMatchStatus, line_overrides: dict[str, object]
    ) -> None:
        line = make_line(status=status, **line_overrides)
        with pytest.raises(ValueError, match=r"line 1 is not RESOLVED"):
            to_quote_request(make_customer(), [line], **stated())

    def test_refuses_a_line_without_a_description(self) -> None:
        line = make_line(extracted=make_extracted(description=None))
        with pytest.raises(ValueError, match="has no description"):
            to_quote_request(make_customer(), [line], **stated())

    def test_refuses_a_line_without_a_quantity(self) -> None:
        line = make_line(extracted=make_extracted(quantity=None))
        with pytest.raises(ValueError, match="has no quantity"):
            to_quote_request(make_customer(), [line], **stated())

    def test_refuses_a_product_resolved_on_two_lines(self) -> None:
        lines = [
            make_line(1, extracted=make_extracted(1)),
            make_line(2, extracted=make_extracted(2)),
        ]
        with pytest.raises(ValueError, match="PRD-0001 was resolved on more than one line"):
            to_quote_request(make_customer(), lines, **stated())

    @pytest.mark.parametrize(
        "requested_delivery",
        [
            RequestedDelivery(
                raw="on 2026-10-09",
                resolution=DateResolution.EXPLICIT,
                requested_delivery_date=date(2026, 10, 9),
            ),
            RequestedDelivery(raw="by Friday", resolution=DateResolution.INFERRED),
        ],
    )
    def test_refuses_a_requested_delivery_without_a_destination(
        self, requested_delivery: RequestedDelivery
    ) -> None:
        with pytest.raises(ValueError, match="has no destination"):
            to_quote_request(
                make_customer(),
                [make_line()],
                requested_delivery=requested_delivery,
                delivery_as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
                **stated(),
            )

    def test_refuses_a_delivery_question_about_more_than_one_line(self) -> None:
        requested = RequestedDelivery(destination="Warsaw")
        lines = [
            make_line(1, extracted=make_extracted(1)),
            make_line(
                2,
                extracted=make_extracted(2, description="Seal kit", quantity=5),
                product_id="PRD-0002",
                sku="X-240",
            ),
        ]
        with pytest.raises(ValueError, match="answered about one line"):
            to_quote_request(
                make_customer(),
                lines,
                requested_delivery=requested,
                delivery_as_of=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
                **stated(),
            )

    def test_refuses_a_delivery_question_without_an_instant(self) -> None:
        requested = RequestedDelivery(destination="Warsaw")
        with pytest.raises(ValueError, match="needs the instant it is asked at"):
            to_quote_request(
                make_customer(),
                [make_line()],
                requested_delivery=requested,
                **stated(),
            )


class TestPurity:
    def test_source_reads_no_clock_and_mints_nothing(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
                assert not names & _BANNED_IMPORTS
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(module.startswith(banned) for banned in _BANNED_IMPORTS)
                names = {alias.name for alias in node.names}
                assert not names & _BANNED_IMPORTS
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _BANNED_CALLS

    def test_the_seam_is_exactly_one_public_function(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        public = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        ]
        assert public == ["to_quote_request"]
