"""Tests for the extraction schemas (architecture §4.6).

The contract under test: a model may say "I don't know", but it may not leave a
gap unexplained, and it may not attach a field without evidence.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.extraction import (
    DateResolution,
    ExtractedLine,
    ExtractionResult,
    LineExtractionStatus,
    MissingFieldReason,
    RequestedDelivery,
)


def make_line(**overrides: object) -> ExtractedLine:
    payload: dict[str, object] = {
        "ordinal": 1,
        "raw_text": "40 units of X-120",
        "requested_sku": "X-120",
        "quantity": 40,
        "uom": "units",
        "evidence": "40 units of X-120",
        "confidence": Decimal("0.95"),
    }
    payload.update(overrides)
    return ExtractedLine.model_validate(payload)


class TestNullQuantityContract:
    def test_null_quantity_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="missing_reason is required"):
            make_line(quantity=None)

    def test_null_quantity_with_a_reason_is_valid(self) -> None:
        line = make_line(quantity=None, missing_reason=MissingFieldReason.NOT_STATED)
        assert line.quantity is None
        assert line.missing_reason is MissingFieldReason.NOT_STATED

    def test_present_quantity_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="must be None when quantity is present"):
            make_line(quantity=40, missing_reason=MissingFieldReason.NOT_STATED)

    def test_zero_quantity_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_line(quantity=0)

    def test_negative_quantity_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_line(quantity=-5)


class TestIdentifiers:
    def test_sku_and_description_cannot_both_be_absent(self) -> None:
        with pytest.raises(ValidationError, match="requested_sku or description"):
            make_line(requested_sku=None, description=None)

    def test_description_only_line_is_valid(self) -> None:
        line = make_line(requested_sku=None, description="the blue 120 model")
        assert line.requested_sku is None

    def test_rejected_status_requires_a_rejection_reason(self) -> None:
        with pytest.raises(ValidationError, match="rejection_reason is required"):
            make_line(status=LineExtractionStatus.REJECTED)

    def test_rejected_line_with_reason_is_valid(self) -> None:
        line = make_line(
            status=LineExtractionStatus.REJECTED,
            rejection_reason="evidence span not verbatim",
        )
        assert line.status is LineExtractionStatus.REJECTED


class TestFieldConstraints:
    def test_empty_evidence_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_line(evidence="")

    def test_empty_raw_text_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_line(raw_text="")

    @pytest.mark.parametrize("confidence", [Decimal("-0.1"), Decimal("1.1")])
    def test_confidence_is_bounded(self, confidence: Decimal) -> None:
        with pytest.raises(ValidationError):
            make_line(confidence=confidence)

    def test_ordinal_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            make_line(ordinal=0)

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_line(unit_price=Decimal("9.99"))

    def test_objects_are_immutable(self) -> None:
        line = make_line()
        with pytest.raises(ValidationError):
            line.quantity = 99  # type: ignore[misc]


class TestRequestedDelivery:
    def test_explicit_requires_a_date(self) -> None:
        with pytest.raises(ValidationError, match="requested_delivery_date is required"):
            RequestedDelivery(raw="on 2026-10-09", resolution=DateResolution.EXPLICIT)

    def test_inferred_must_not_carry_a_date(self) -> None:
        with pytest.raises(ValidationError, match="must be None unless resolution is EXPLICIT"):
            RequestedDelivery(
                raw="by Friday",
                resolution=DateResolution.INFERRED,
                requested_delivery_date=date(2026, 10, 9),
            )

    def test_inferred_requires_the_verbatim_phrase(self) -> None:
        with pytest.raises(ValidationError, match="raw is required"):
            RequestedDelivery(resolution=DateResolution.INFERRED)

    def test_absent_must_not_carry_a_phrase(self) -> None:
        with pytest.raises(ValidationError, match="raw must be None"):
            RequestedDelivery(raw="by Friday", resolution=DateResolution.ABSENT)

    def test_valid_explicit_request(self) -> None:
        delivery = RequestedDelivery(
            raw="on 2026-10-09",
            resolution=DateResolution.EXPLICIT,
            requested_delivery_date=date(2026, 10, 9),
            destination="Warsaw",
        )
        assert delivery.destination == "Warsaw"

    def test_default_is_absent(self) -> None:
        assert RequestedDelivery().resolution is DateResolution.ABSENT


class TestExtractionResult:
    def test_ordinals_must_be_dense(self) -> None:
        with pytest.raises(ValidationError, match=r"1\.\.N without gaps"):
            ExtractionResult(lines=(make_line(ordinal=1), make_line(ordinal=3)))

    def test_ordinals_must_be_unique(self) -> None:
        with pytest.raises(ValidationError, match="must be unique"):
            ExtractionResult(lines=(make_line(ordinal=1), make_line(ordinal=1)))

    def test_empty_extraction_is_representable(self) -> None:
        # An empty extraction is valid at the schema level; routing it to
        # NEEDS_HUMAN_INPUT is the workflow's job, not the schema's.
        result = ExtractionResult()
        assert result.lines == ()

    def test_open_questions_are_capped(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult(open_questions=tuple(f"question {i}" for i in range(11)))

    def test_line_count_is_capped(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult(
                lines=tuple(make_line(ordinal=i) for i in range(1, 52)),
            )
