"""The deterministic quote arithmetic, on hand-built facts.

Money is the one thing a customer can hold the company to, so this module pins
the arithmetic the quote contract asserts: every amount is quantised to cents
with half-up rounding, a line is extended as ``quantity x unit_price``, the
subtotal is the sum of the extensions, the selected rule's percentage applies to
that subtotal, and ``total = subtotal - discount_amount``.

Two behaviours matter more than the rest and are tested from several angles:

* a line whose price lookup did not return ``FOUND`` is given **no** price and
  **no** amount - it is blocked, machine-readably, and never totalled;
* nothing here decides anything. The discount is applied exactly as selected, a
  rule that needs sign-off keeps needing it, and the status stays ``DRAFT``.

Prices arrive from Phase 1D's own selector rather than being hand-written, so a
test cannot quietly assert arithmetic over a price the real system would never
have chosen.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.delivery import DeliveryAssessment, DeliveryFeasibility, DeliveryPromise
from rfq_agent.domain.policy import DiscountApplication, DiscountScope
from rfq_agent.domain.pricing import (
    PriceEntry,
    PriceLookupReason,
    PriceLookupStatus,
    PriceRef,
    PriceSelection,
    select_price,
)
from rfq_agent.domain.quote import (
    CALC_VERSION,
    QuoteCalculation,
    QuoteLineInput,
    QuoteStatus,
    calculate_quote,
    quote_inputs_fingerprint,
)
from rfq_agent.domain.stock import StockStatus

AS_OF = date(2026, 10, 6)
PRODUCT = "PRD_0001"
CENTS = Decimal("0.01")


def entry(
    unit_price: str = "12.5000",
    *,
    price_entry_id: str = "PE_0001",
    product_id: str = PRODUCT,
    min_qty: int = 1,
    currency: str = "EUR",
    customer_id: str | None = None,
    customer_tier: str | None = "STANDARD",
    effective_from: str = "2026-01-01",
    effective_to: str | None = None,
) -> PriceEntry:
    """One price-book entry, with the fields a test needs to vary."""
    return PriceEntry(
        price_entry_id=price_entry_id,
        product_id=product_id,
        price_book_code="BK-EU-2026",
        customer_id=customer_id,
        customer_tier=customer_tier,
        min_qty=min_qty,
        unit_price=Decimal(unit_price),
        currency=currency,
        effective_from=date.fromisoformat(effective_from),
        effective_to=None if effective_to is None else date.fromisoformat(effective_to),
    )


def line_input(
    *,
    quantity: int = 40,
    unit_price: str = "12.5000",
    entries: Sequence[PriceEntry] | None = None,
    product_id: str = PRODUCT,
    sku: str = "PMP-A-100",
    description: str = "Centrifugal pump PMP-A-100",
    as_of: date = AS_OF,
    stock_status: StockStatus = StockStatus.SUFFICIENT,
    notes: str | None = None,
) -> QuoteLineInput:
    """A resolved line, priced by Phase 1D's selector over hand-built entries."""
    catalogue = list(entries) if entries is not None else [entry(unit_price)]
    selection = select_price(catalogue, product_id=product_id, quantity=quantity, as_of=as_of)
    return QuoteLineInput(
        product_id=product_id,
        sku=sku,
        description=description,
        quantity=quantity,
        price=selection,
        stock_status=stock_status,
        notes=notes,
    )


def rule(
    *,
    rule_id: str = "DSC_0003",
    percent: str = "3.00",
    scope: DiscountScope = DiscountScope.CUSTOMER,
    requires_approval: bool = False,
) -> DiscountApplication:
    """A discount rule as Phase 1G would have selected it."""
    return DiscountApplication(
        rule_id=rule_id,
        scope=scope,
        percent=Decimal(percent),
        requires_approval=requires_approval,
    )


def calculate(lines: Iterable[QuoteLineInput], **overrides: object) -> QuoteCalculation:
    """Run the calculator with the quote's identity filled in."""
    payload: dict[str, object] = {
        "quote_id": "QTE_0001",
        "quote_number": "Q-2026-0001",
        "run_id": "RUN_0001",
        "customer_id": "CUS_0001",
        "currency": "EUR",
        "pricing_as_of": AS_OF,
    }
    payload.update(overrides)
    return calculate_quote(lines, **payload)  # type: ignore[arg-type]


def half_up(quantity: int, unit_price: str) -> Decimal:
    """The contract's own extension rule, written out independently."""
    return (Decimal(quantity) * Decimal(unit_price)).quantize(CENTS, rounding=ROUND_HALF_UP)


class TestLineExtension:
    """One line: quantity times unit price, to the cent."""

    def test_quantity_times_unit_price(self) -> None:
        calculation = calculate([line_input(quantity=40, unit_price="12.5000")])

        (line,) = calculation.quote.lines
        assert line.line_extension == Decimal("500.00")
        assert line.unit_price == Decimal("12.5000")

    @pytest.mark.parametrize(
        ("quantity", "unit_price", "expected"),
        [
            (1, "0.1250", "0.13"),  # exactly half a cent: half-up, not half-even
            (1, "0.1350", "0.14"),
            (3, "10.0050", "30.02"),
            (2, "0.1250", "0.25"),  # exact, no rounding at all
            (1000, "0.0125", "12.50"),
            (1, "0.0049", "0.00"),  # below half a cent
            (7, "0.0050", "0.04"),  # 0.035 -> 0.04
        ],
        ids=[
            "half-cent-rounds-up",
            "half-cent-up-odd",
            "half-cent-up-even",
            "exact",
            "sub-cent-price-times-many",
            "just-below-half-a-cent",
            "half-a-cent-times-seven",
        ],
    )
    def test_the_documented_rounding_boundaries(
        self, quantity: int, unit_price: str, expected: str
    ) -> None:
        calculation = calculate([line_input(quantity=quantity, unit_price=unit_price)])

        assert calculation.quote.lines[0].line_extension == Decimal(expected)
        assert calculation.quote.lines[0].line_extension == half_up(quantity, unit_price)

    def test_the_unit_price_keeps_its_four_decimals(self) -> None:
        """The price is never rounded to cents: only the money it produces is."""
        calculation = calculate([line_input(quantity=3, unit_price="1234.5678")])

        (line,) = calculation.quote.lines
        assert line.unit_price == Decimal("1234.5678")
        assert line.line_extension == Decimal("3703.70")

    def test_a_float_would_have_drifted_and_decimal_does_not(self) -> None:
        """Three lines at 0.1000 each is 0.30 exactly - not 0.30000000000000004."""
        lines = [
            line_input(quantity=i, unit_price="0.1000", sku=f"SKU-{i}", product_id=PRODUCT)
            for i in (1, 2, 3)
        ]

        calculation = calculate(lines)

        assert calculation.quote.subtotal == Decimal("0.60")
        assert all(isinstance(line.line_extension, Decimal) for line in calculation.quote.lines)

    def test_the_largest_documented_quantity(self) -> None:
        calculation = calculate([line_input(quantity=1_000_000, unit_price="9999.9999")])

        assert calculation.quote.lines[0].line_extension == Decimal("9999999900.00")

    def test_a_free_line_is_zero_not_missing(self) -> None:
        calculation = calculate([line_input(quantity=40, unit_price="0.0000")])

        assert calculation.quote.lines[0].line_extension == Decimal("0.00")
        assert calculation.quote.subtotal == Decimal("0.00")


class TestSubtotal:
    """Many lines: the sum of the extensions, still to the cent."""

    def test_multiple_lines_sum_into_the_subtotal(self) -> None:
        lines = [
            line_input(quantity=40, unit_price="12.5000"),
            line_input(quantity=15, unit_price="1789.0000", sku="PMP-B-150"),
            line_input(quantity=3, unit_price="0.1250", sku="SPARE-KIT"),
        ]

        calculation = calculate(lines)

        assert [line.line_extension for line in calculation.quote.lines] == [
            Decimal("500.00"),
            Decimal("26835.00"),
            Decimal("0.38"),
        ]
        assert calculation.quote.subtotal == Decimal("27335.38")
        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == Decimal("27335.38")

    def test_ordinals_follow_the_order_the_lines_arrive_in(self) -> None:
        calculation = calculate([line_input(sku="A"), line_input(sku="B"), line_input(sku="C")])

        assert [line.ordinal for line in calculation.quote.lines] == [1, 2, 3]
        assert [line.sku for line in calculation.quote.lines] == ["A", "B", "C"]

    def test_the_subtotal_is_independent_of_the_line_order(self) -> None:
        lines = [
            line_input(quantity=40, unit_price="12.5000"),
            line_input(quantity=15, unit_price="1789.0000", sku="PMP-B-150"),
        ]

        forwards = calculate(lines)
        backwards = calculate(reversed(lines))

        assert forwards.quote.subtotal == backwards.quote.subtotal
        assert forwards.quote.total == backwards.quote.total
        assert forwards.quote.discount_amount == backwards.quote.discount_amount


class TestDiscountArithmetic:
    """The selected percentage, applied to the subtotal - and nothing more."""

    @pytest.mark.parametrize(
        ("percent", "expected_amount", "expected_total"),
        [
            ("2.00", "10.00", "490.00"),
            ("3.00", "15.00", "485.00"),
            ("7.50", "37.50", "462.50"),
            ("100.00", "500.00", "0.00"),
        ],
        ids=["two-percent", "three-percent", "seven-and-a-half", "everything"],
    )
    def test_the_percentage_applies_to_the_subtotal(
        self, percent: str, expected_amount: str, expected_total: str
    ) -> None:
        calculation = calculate(
            [line_input(quantity=40, unit_price="12.5000")], discount=rule(percent=percent)
        )

        assert calculation.quote.subtotal == Decimal("500.00")
        assert calculation.quote.discount_amount == Decimal(expected_amount)
        assert calculation.quote.total == Decimal(expected_total)

    @pytest.mark.parametrize(
        ("subtotal_line", "percent", "expected"),
        [
            ("0.0500", "10.00", "0.01"),  # 0.005 exactly: half-up
            ("0.0500", "2.00", "0.00"),  # 0.001: rounds down
            ("10.0100", "33.33", "3.34"),  # 3.3363: rounds up
            ("0.9900", "5.00", "0.05"),  # 0.0495: half-up
        ],
        ids=["half-a-cent-up", "below-a-cent", "long-decimal-up", "half-a-cent-up-again"],
    )
    def test_the_discount_amount_is_rounded_half_up_to_cents(
        self, subtotal_line: str, percent: str, expected: str
    ) -> None:
        calculation = calculate(
            [line_input(quantity=1, unit_price=subtotal_line)], discount=rule(percent=percent)
        )

        assert calculation.quote.discount_amount == Decimal(expected)
        assert calculation.quote.total == calculation.quote.subtotal - Decimal(expected)

    def test_the_discount_applies_to_the_subtotal_not_line_by_line(self) -> None:
        """Two half-cent lines at 10% earn 0.01 in total, not 0.02.

        Rounding each line's share separately would give a different - and
        unsupported - answer; the quote carries one discount on one subtotal.
        """
        lines = [
            line_input(quantity=1, unit_price="0.0500", sku="A"),
            line_input(quantity=1, unit_price="0.0500", sku="B"),
        ]

        calculation = calculate(lines, discount=rule(percent="10.00"))

        assert calculation.quote.subtotal == Decimal("0.10")
        assert calculation.quote.discount_amount == Decimal("0.01")
        assert calculation.quote.total == Decimal("0.09")

    def test_no_discount_produces_a_zero_amount(self) -> None:
        calculation = calculate([line_input(quantity=40, unit_price="12.5000")])

        assert calculation.quote.discount is None
        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == calculation.quote.subtotal

    def test_a_zero_percent_rule_stays_selected_and_earns_nothing(self) -> None:
        applied = rule(rule_id="DSC_0010", percent="0.00", scope=DiscountScope.GLOBAL)

        calculation = calculate([line_input(quantity=40, unit_price="12.5000")], discount=applied)

        assert calculation.quote.discount is applied
        assert calculation.quote.discount.rule_id == "DSC_0010"
        assert calculation.quote.discount.percent == Decimal("0.00")
        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == calculation.quote.subtotal

    @pytest.mark.parametrize("requires_approval", [True, False])
    def test_requires_approval_is_carried_and_changes_nothing(
        self, requires_approval: bool
    ) -> None:
        calculation = calculate(
            [line_input(quantity=40, unit_price="12.5000")],
            discount=rule(requires_approval=requires_approval),
        )

        assert calculation.quote.discount is not None
        assert calculation.quote.discount.requires_approval is requires_approval
        assert calculation.quote.discount_amount == Decimal("15.00")
        assert calculation.quote.total == Decimal("485.00")
        assert calculation.quote.status is QuoteStatus.DRAFT

    def test_nothing_here_approves_or_gates_the_quote(self) -> None:
        calculation = calculate([line_input()], discount=rule(requires_approval=True))

        assert calculation.quote.status is QuoteStatus.DRAFT
        assert not hasattr(calculation.quote, "policy_allowed")
        assert not hasattr(calculation.quote, "approved_at")
        assert calculation.complete is True

    def test_the_total_is_subtotal_minus_the_discount(self) -> None:
        calculation = calculate(
            [
                line_input(quantity=40, unit_price="12.5000"),
                line_input(quantity=7, unit_price="999.9900", sku="SKU-7"),
            ],
            discount=rule(percent="4.50"),
        )

        quote = calculation.quote
        assert quote.subtotal == Decimal("7499.93")
        assert quote.discount_amount == Decimal("337.50")
        assert quote.total == quote.subtotal - quote.discount_amount == Decimal("7162.43")


class TestRefusedLines:
    """A line without a usable price gets no money at all."""

    def test_a_missing_price_is_not_totalled(self) -> None:
        calculation = calculate([line_input(entries=[])])

        (line,) = calculation.quote.lines
        assert line.price_status is PriceLookupStatus.MISSING
        assert line.blocked is True
        assert line.unit_price == Decimal("0.0000")
        assert line.line_extension == Decimal("0.00")
        assert line.price_entry_id == "PRICE_MISSING"
        assert line.blocked_reason is not None
        assert line.blocked_reason.startswith("MISSING/NO_ENTRIES")
        assert calculation.quote.subtotal == Decimal("0.00")
        assert calculation.quote.total == Decimal("0.00")
        assert calculation.complete is False

    def test_an_expired_price_is_not_totalled(self) -> None:
        expired = entry(unit_price="100.0000", effective_to="2026-06-30")

        calculation = calculate([line_input(entries=[expired])])

        (refusal,) = calculation.refusals
        assert refusal.status is PriceLookupStatus.EXPIRED
        assert refusal.reason is PriceLookupReason.EXPIRED
        assert refusal.ordinal == 1
        assert refusal.product_id == PRODUCT
        assert calculation.quote.lines[0].blocked_reason is not None
        assert "EXPIRED" in calculation.quote.lines[0].blocked_reason

    def test_the_refusal_keeps_the_pricing_sentence(self) -> None:
        """The refusal carries Phase 1D's own sentence, in full and untruncated."""
        selection = select_price([], product_id=PRODUCT, quantity=40, as_of=AS_OF)
        calculation = calculate([line_input(entries=[])])

        (refusal,) = calculation.refusals
        assert refusal.detail == selection.detail
        assert "PRD_0001" in refusal.detail
        assert len(refusal.detail) <= 400

    def test_a_refused_line_does_not_hide_the_priced_ones(self) -> None:
        calculation = calculate(
            [
                line_input(quantity=40, unit_price="12.5000"),
                line_input(quantity=5, unit_price="1.0000", entries=[], sku="NO-PRICE"),
            ]
        )

        quote = calculation.quote
        assert quote.subtotal == Decimal("500.00")
        assert quote.total == Decimal("500.00")
        assert [line.ordinal for line in quote.blocked_lines] == [2]
        assert [refusal.ordinal for refusal in calculation.refusals] == [2]

    def test_a_refused_quote_is_never_sendable(self) -> None:
        calculation = calculate([line_input(entries=[])])

        assert calculation.quote.is_sendable is False
        ready = calculation.quote.model_copy(update={"status": QuoteStatus.READY})
        assert ready.is_sendable is False

    def test_every_line_may_be_refused(self) -> None:
        calculation = calculate([line_input(entries=[]), line_input(entries=[], sku="B")])

        assert calculation.quote.subtotal == Decimal("0.00")
        assert [refusal.ordinal for refusal in calculation.refusals] == [1, 2]

    def test_a_discount_on_a_refused_quote_earns_nothing(self) -> None:
        calculation = calculate([line_input(entries=[])], discount=rule())

        assert calculation.quote.discount_amount == Decimal("0.00")
        assert calculation.quote.total == Decimal("0.00")
        assert calculation.quote.discount is not None

    def test_the_refusal_outcome_cannot_lie(self) -> None:
        """A refusal entry must name a line that really is blocked."""
        calculation = calculate([line_input(entries=[])])

        payload = calculation.model_dump()
        payload["refusals"][0]["status"] = PriceLookupStatus.FOUND

        with pytest.raises(ValidationError, match="non-FOUND price status"):
            QuoteCalculation.model_validate(payload)

    def test_a_refusal_must_name_a_line_of_the_quote(self) -> None:
        calculation = calculate([line_input(quantity=40)])

        payload = calculation.model_dump()
        payload["refusals"] = [
            {
                "ordinal": 1,
                "product_id": PRODUCT,
                "status": PriceLookupStatus.MISSING,
                "reason": PriceLookupReason.NO_ENTRIES,
                "detail": "no price entry exists",
            }
        ]

        with pytest.raises(ValidationError, match="must name exactly the lines"):
            QuoteCalculation.model_validate(payload)


class TestStockAndDeliveryFacts:
    """Facts other phases established travel through, unchanged."""

    def test_partial_stock_is_priced_and_not_blocked(self) -> None:
        calculation = calculate([line_input(quantity=40, stock_status=StockStatus.PARTIAL)])

        (line,) = calculation.quote.lines
        assert line.stock_status is StockStatus.PARTIAL
        assert line.blocked is False
        assert line.line_extension == Decimal("500.00")
        assert calculation.complete is True

    def test_no_stock_is_priced_but_blocked(self) -> None:
        """The money is complete - the stock is not - so the line is still priced."""
        calculation = calculate([line_input(quantity=40, stock_status=StockStatus.NONE)])

        (line,) = calculation.quote.lines
        assert line.line_extension == Decimal("500.00")
        assert line.blocked is True
        assert line.blocked_reason is not None
        assert line.blocked_reason.startswith("NONE")
        assert calculation.quote.subtotal == Decimal("500.00")
        assert calculation.refusals == ()
        assert calculation.quote.is_sendable is False

    def test_none_stock_makes_the_subtotal_include_the_blocked_line(self) -> None:
        """The contract fixes ``subtotal = sum of extensions``; a priced line counts."""
        calculation = calculate(
            [
                line_input(quantity=40, unit_price="12.5000"),
                line_input(
                    quantity=40, unit_price="12.5000", sku="BLOCKED", stock_status=StockStatus.NONE
                ),
            ]
        )

        assert calculation.quote.subtotal == Decimal("1000.00")
        assert [line.ordinal for line in calculation.quote.blocked_lines] == [2]

    def test_the_delivery_assessment_is_carried_through(self) -> None:
        promise = DeliveryPromise(
            destination="Warsaw, PL",
            origin_location="WAW",
            carrier_service_code="DHL-EXP",
            transit_days=1,
            earliest_ship_date=AS_OF,
            earliest_delivery_date=date(2026, 10, 7),
            requested_date=date(2026, 10, 9),
            feasibility=DeliveryFeasibility.FEASIBLE,
            rationale="WAW to PL: shipped 2026-10-06, delivered 2026-10-07",
        )
        assessment = DeliveryAssessment(promise=promise)

        calculation = calculate([line_input()], delivery=assessment)

        assert calculation.quote.delivery is assessment

    def test_the_notes_travel_through(self) -> None:
        calculation = calculate([line_input(notes="customer asked for a painted finish")])

        assert calculation.quote.lines[0].notes == "customer asked for a painted finish"


class TestProvenance:
    """Every number keeps the row it came from."""

    def test_the_price_entry_id_is_kept(self) -> None:
        entries = [
            entry(unit_price="9.9900", price_entry_id="PE_0001"),
            entry(unit_price="8.5000", price_entry_id="PE_0002", min_qty=25),
        ]

        calculation = calculate([line_input(quantity=25, entries=entries)])

        assert calculation.quote.lines[0].price_entry_id == "PE_0002"
        assert calculation.quote.lines[0].unit_price == Decimal("8.5000")

    def test_the_discount_rule_provenance_is_kept(self) -> None:
        applied = rule(
            rule_id="DSC_0004",
            percent="4.50",
            scope=DiscountScope.CUSTOMER,
            requires_approval=True,
        )

        calculation = calculate([line_input()], discount=applied)

        assert calculation.quote.discount == applied
        assert calculation.quote.discount.scope is DiscountScope.CUSTOMER
        assert calculation.quote.discount.applied_by_human is False

    def test_the_calculation_version_is_stamped(self) -> None:
        calculation = calculate([line_input()])

        assert calculation.quote.calc_version == CALC_VERSION

    def test_the_input_fingerprint_is_stored(self) -> None:
        calculation = calculate([line_input()])

        stored = calculation.quote.inputs_sha256
        assert stored is not None
        assert len(stored) == 64
        assert stored == quote_inputs_fingerprint(calculation.quote)

    @pytest.mark.parametrize(
        "overrides",
        [{}, {"quantity": 3, "unit_price": "0.1250"}, {"stock_status": StockStatus.PARTIAL}],
    )
    def test_the_fingerprint_is_stable_for_identical_inputs(
        self, overrides: dict[str, object]
    ) -> None:
        first = calculate([line_input(**overrides)])  # type: ignore[arg-type]
        second = calculate([line_input(**overrides)])  # type: ignore[arg-type]

        assert first.quote.inputs_sha256 == second.quote.inputs_sha256
        assert first.quote == second.quote

    def test_a_different_quantity_produces_a_different_fingerprint(self) -> None:
        forty = calculate([line_input(quantity=40)])
        forty_one = calculate([line_input(quantity=41)])

        assert forty.quote.inputs_sha256 != forty_one.quote.inputs_sha256

    def test_a_different_rule_produces_a_different_fingerprint(self) -> None:
        without = calculate([line_input()])
        with_rule = calculate([line_input()], discount=rule())

        assert without.quote.inputs_sha256 != with_rule.quote.inputs_sha256


class TestRefusedInputs:
    """Inputs that cannot describe a quote are refused, not guessed at."""

    def test_no_lines(self) -> None:
        with pytest.raises(ValueError, match="at least one line"):
            calculate([])

    def test_too_many_lines(self) -> None:
        lines = [line_input(sku=f"SKU-{index}") for index in range(51)]

        with pytest.raises(ValueError, match="at most 50 lines"):
            calculate(lines)

    def test_the_line_limit_boundary_is_allowed(self) -> None:
        lines = [line_input(sku=f"SKU-{index}") for index in range(50)]

        assert len(calculate(lines).quote.lines) == 50

    def test_a_price_selected_for_another_product(self) -> None:
        foreign = select_price(
            [entry(product_id="PRD_0002")], product_id="PRD_0002", quantity=40, as_of=AS_OF
        )
        item = QuoteLineInput(
            product_id=PRODUCT,
            sku="PMP-A-100",
            description="Centrifugal pump",
            quantity=40,
            price=foreign,
        )

        with pytest.raises(ValueError, match="price was selected for PRD_0002"):
            calculate([item])

    def test_a_price_selected_for_another_quantity(self) -> None:
        item = line_input(quantity=40)
        tampered = item.model_copy(
            update={
                "quantity": 41,
                "price": select_price([entry()], product_id=PRODUCT, quantity=40, as_of=AS_OF),
            }
        )

        with pytest.raises(ValueError, match="selected for 40 units but the line carries 41"):
            calculate([tampered])

    def test_a_price_selected_for_another_date(self) -> None:
        elsewhere = date(2026, 9, 1)
        item = line_input(as_of=elsewhere, quantity=40)

        with pytest.raises(ValueError, match="priced as of 2026-10-06"):
            calculate([item])

    def test_a_price_entry_looked_up_on_another_date(self) -> None:
        selection = PriceSelection(
            product_id=PRODUCT,
            quantity=40,
            as_of=AS_OF,
            status=PriceLookupStatus.FOUND,
            price=PriceRef.from_entry(entry(), as_of=date(2026, 9, 1)),
            detail="selected for a different date than the quote is priced for",
        )
        item = QuoteLineInput(
            product_id=PRODUCT,
            sku="PMP-A-100",
            description="Centrifugal pump",
            quantity=40,
            price=selection,
        )

        with pytest.raises(ValueError, match="looked up as of 2026-09-01"):
            calculate([item])

    def test_a_price_in_another_currency(self) -> None:
        item = line_input(quantity=40, entries=[entry(currency="USD")])

        with pytest.raises(ValueError, match="is in USD but the quote is in EUR"):
            calculate([item])

    def test_a_float_unit_price_never_reaches_the_arithmetic(self) -> None:
        with pytest.raises(ValidationError, match="never float"):
            PriceEntry(
                price_entry_id="PE_0001",
                product_id=PRODUCT,
                price_book_code="BK-EU-2026",
                customer_tier="STANDARD",
                unit_price=1.5,  # type: ignore[arg-type]
                currency="EUR",
                effective_from=AS_OF,
            )

    def test_a_non_positive_quantity_cannot_be_built(self) -> None:
        with pytest.raises(ValidationError):
            QuoteLineInput(
                product_id=PRODUCT,
                sku="PMP-A-100",
                description="Centrifugal pump",
                quantity=0,
                price=select_price([entry()], product_id=PRODUCT, quantity=1, as_of=AS_OF),
            )


class TestDeterminism:
    """Same facts in, same money and same digest out."""

    def test_the_calculation_does_not_modify_its_inputs(self) -> None:
        items = [line_input(sku="A"), line_input(sku="B", quantity=3)]
        before = list(items)

        calculate(items)

        assert items == before

    def test_an_iterable_is_consumed_once(self) -> None:
        calculation = calculate(iter([line_input(sku="A"), line_input(sku="B")]))

        assert [line.sku for line in calculation.quote.lines] == ["A", "B"]

    def test_the_quote_it_returns_is_frozen(self) -> None:
        quote = calculate([line_input()]).quote

        with pytest.raises(ValidationError):
            quote.total = Decimal("1.00")  # type: ignore[misc]

    def test_the_arithmetic_matches_the_contract_clause_by_clause(self) -> None:
        calculation = calculate(
            [
                line_input(quantity=3, unit_price="10.0050"),
                line_input(quantity=1000, unit_price="0.0125", sku="SMALL"),
            ],
            discount=rule(percent="7.50"),
        )

        quote = calculation.quote
        expected_subtotal = sum(
            (half_up(line.quantity, str(line.unit_price)) for line in quote.lines),
            Decimal("0"),
        )
        assert quote.subtotal == expected_subtotal
        assert quote.total == quote.subtotal - quote.discount_amount
        assert len({line.currency for line in quote.lines}) == 1
        assert [line.ordinal for line in quote.lines] == [1, 2]

    def test_the_detail_says_what_was_computed(self) -> None:
        calculation = calculate([line_input()], discount=rule(requires_approval=True))

        assert "Q-2026-0001" in calculation.detail
        assert "DSC_0003" in calculation.detail
        assert "needs sign-off" in calculation.detail
        assert len(calculation.detail) <= 400

    def test_the_detail_names_the_lines_that_were_not_totalled(self) -> None:
        calculation = calculate([line_input(entries=[])])

        assert "1 line(s) have no usable price and were not totalled" in calculation.detail
