"""Tests for the quote schema's arithmetic contract (architecture §5.2).

The point of these tests is that an inconsistent quote cannot be *constructed*,
so no downstream stage can ever forward one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.policy import DiscountApplication, DiscountScope
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import (
    CALC_VERSION,
    TERMINAL_QUOTE_STATUSES,
    Quote,
    QuoteLine,
    QuoteStatus,
)
from rfq_agent.domain.stock import StockStatus
from tests.conftest import make_quote, make_quote_line, utc


class TestQuoteLineArithmetic:
    def test_extension_must_equal_quantity_times_unit_price(self) -> None:
        with pytest.raises(ValidationError, match="line_extension"):
            make_quote_line(quantity=40, unit_price="12.5000", line_extension=Decimal("499.99"))

    def test_extension_is_rounded_half_up_to_cents(self) -> None:
        # 3 x 10.005 = 30.015 -> 30.02 (half-up), not 30.01.
        line = make_quote_line(quantity=3, unit_price="10.0050", sku="X-120")
        assert line.line_extension == Decimal("30.02")

    def test_unit_price_precision_is_preserved(self) -> None:
        line = make_quote_line(unit_price="0.0125", quantity=1000)
        assert line.unit_price == Decimal("0.0125")
        assert line.line_extension == Decimal("12.50")


class TestBlockingContract:
    def test_blocked_line_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="blocked_reason is required"):
            make_quote_line(blocked=True)

    def test_unblocked_line_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="must be None when blocked is false"):
            make_quote_line(blocked=False, blocked_reason="spurious")

    def test_missing_price_forces_the_line_to_be_blocked(self) -> None:
        with pytest.raises(ValidationError, match="non-FOUND price status must be blocked"):
            make_quote_line(price_status=PriceLookupStatus.MISSING)

    def test_no_stock_forces_the_line_to_be_blocked(self) -> None:
        with pytest.raises(ValidationError, match="no stock must be blocked"):
            make_quote_line(stock_status=StockStatus.NONE)

    def test_partial_stock_does_not_by_itself_block_the_line(self) -> None:
        line = make_quote_line(stock_status=StockStatus.PARTIAL)
        assert line.blocked is False


class TestQuoteTotals:
    def test_subtotal_must_equal_the_sum_of_lines(self) -> None:
        lines = (make_quote_line(1, quantity=40), make_quote_line(2, sku="Y-500", quantity=15))
        with pytest.raises(ValidationError, match="subtotal"):
            make_quote(lines, subtotal=Decimal("1.00"), total=Decimal("1.00"))

    def test_total_must_equal_subtotal_minus_discount(self) -> None:
        lines = (make_quote_line(1, quantity=40, unit_price="12.5000"),)
        with pytest.raises(ValidationError, match="total"):
            make_quote(
                lines,
                discount=DiscountApplication(
                    rule_id="DR-1", scope=DiscountScope.GLOBAL, percent=Decimal("10")
                ),
                discount_amount=Decimal("50.00"),
                total=Decimal("499.99"),
            )

    def test_discount_amount_requires_a_discount(self) -> None:
        lines = (make_quote_line(1, quantity=40, unit_price="12.5000"),)
        with pytest.raises(ValidationError, match="discount_amount must be zero"):
            make_quote(lines, discount_amount=Decimal("10.00"), total=Decimal("490.00"))

    def test_valid_discounted_quote(self) -> None:
        lines = (make_quote_line(1, quantity=40, unit_price="12.5000"),)
        quote = make_quote(
            lines,
            discount=DiscountApplication(
                rule_id="DR-1", scope=DiscountScope.CUSTOMER, percent=Decimal("10")
            ),
            discount_amount=Decimal("50.00"),
            total=Decimal("450.00"),
        )
        assert quote.total == Decimal("450.00")

    def test_currency_mismatch_is_rejected(self) -> None:
        lines = (make_quote_line(1, currency="USD"),)
        with pytest.raises(ValidationError, match="currency"):
            make_quote(lines)

    def test_line_ordinals_must_be_dense(self) -> None:
        lines = (make_quote_line(1), make_quote_line(3, sku="Y-500"))
        with pytest.raises(ValidationError, match=r"1\.\.N without gaps"):
            make_quote(lines)

    def test_empty_quote_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_quote(())

    def test_quote_number_pattern_is_enforced(self) -> None:
        with pytest.raises(ValidationError):
            make_quote(quote_number="QUOTE-1")


class TestSendability:
    def test_draft_quote_is_not_sendable(self) -> None:
        assert make_quote().is_sendable is False

    def test_ready_quote_without_blocks_is_sendable(self) -> None:
        assert make_quote(status=QuoteStatus.READY).is_sendable is True

    def test_blocked_line_makes_the_quote_unsendable(self) -> None:
        line = make_quote_line(blocked=True, blocked_reason="PRICE_MISSING")
        assert make_quote((line,), status=QuoteStatus.READY).is_sendable is False

    def test_blocked_lines_are_listed(self) -> None:
        lines = (
            make_quote_line(1),
            make_quote_line(2, sku="Y-500", blocked=True, blocked_reason="PRICE_MISSING"),
        )
        quote = make_quote(lines)
        assert [line.ordinal for line in quote.blocked_lines] == [2]


class TestInputsFingerprint:
    def test_identical_inputs_produce_an_identical_fingerprint(self) -> None:
        first = make_quote()
        second = make_quote()
        assert first.inputs_fingerprint() == second.inputs_fingerprint()

    def test_fingerprint_ignores_status_and_timestamps(self) -> None:
        first = make_quote(status=QuoteStatus.DRAFT, created_at=None)
        second = make_quote(status=QuoteStatus.READY, created_at=utc())
        assert first.inputs_fingerprint() == second.inputs_fingerprint()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"quantity": 41},
            {"unit_price": "12.6000"},
            {"sku": "X-121", "price_entry_id": "PE-X121-01"},
        ],
    )
    def test_fingerprint_changes_with_the_inputs(self, overrides: dict[str, object]) -> None:
        baseline = make_quote().inputs_fingerprint()
        changed = make_quote((make_quote_line(**overrides),)).inputs_fingerprint()
        assert baseline != changed

    def test_stored_fingerprint_field_is_optional_until_calculated(self) -> None:
        quote = make_quote()
        assert quote.inputs_sha256 is None
        assert quote.calc_version == CALC_VERSION


class TestQuoteIdentity:
    def test_quote_carries_its_run_and_customer(self) -> None:
        quote = make_quote()
        assert quote.run_id == "RUN-0001"
        assert quote.customer_id == "CUST-0001"
        assert quote.pricing_as_of == date(2026, 10, 6)

    def test_terminal_statuses(self) -> None:
        assert frozenset({QuoteStatus.REJECTED, QuoteStatus.SENT}) == TERMINAL_QUOTE_STATUSES


def test_quote_line_is_immutable() -> None:
    line = make_quote_line()
    with pytest.raises(ValidationError):
        line.quantity = 100  # type: ignore[misc]


def test_quote_line_class_is_exported() -> None:
    assert QuoteLine.__name__ == "QuoteLine"
    assert Quote.__name__ == "Quote"
