"""Tests for pricing value objects, the money type and price selection.

The selection tests are the ones that matter commercially: the chosen entry is
the number a customer will be invoiced at, so the precedence, the window
boundaries and the failure reasons are pinned exactly, and every outcome is
checked to be either a usable price with provenance or an explicit reason.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.ids import CustomerId, ProductId
from rfq_agent.domain.pricing import (
    LIST_TIER,
    PriceEntry,
    PriceLookupReason,
    PriceLookupStatus,
    PriceRef,
    PriceSelection,
    select_price,
)
from rfq_agent.domain.values import DomainModel, money_field
from tests.conftest import CUSTOMER_ID, FOREIGN_CUSTOMER, PRODUCT_X, PRODUCT_Y

#: The date the quotation is priced on in these tests.
AS_OF = date(2026, 10, 6)


class Sample(DomainModel):
    amount: money_field(decimal_places=2)


def make_entry(**overrides: object) -> PriceEntry:
    payload: dict[str, object] = {
        "price_entry_id": "PE-X120-EU-1",
        "product_id": PRODUCT_X,
        "price_book_code": "EU-STANDARD",
        "customer_tier": "STANDARD",
        "min_qty": 1,
        "unit_price": Decimal("12.5000"),
        "currency": "EUR",
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2026, 12, 31),
    }
    payload.update(overrides)
    return PriceEntry.model_validate(payload)


class TestMoneyType:
    def test_accepts_decimal_and_string(self) -> None:
        assert Sample(amount=Decimal("10.50")).amount == Decimal("10.50")
        assert Sample(amount="10.50").amount == Decimal("10.50")

    def test_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="never float"):
            Sample(amount=10.5)  # type: ignore[arg-type]

    def test_rejects_excess_precision(self) -> None:
        with pytest.raises(ValidationError):
            Sample(amount=Decimal("10.005"))

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            Sample(amount=Decimal("-1"))

    def test_rejects_non_numeric_string(self) -> None:
        with pytest.raises(ValidationError):
            Sample(amount="twelve")


class TestPriceEntry:
    def test_valid_entry(self) -> None:
        entry = make_entry()
        assert entry.applies_on(date(2026, 6, 1)) is True
        assert entry.satisfies_quantity(1) is True

    def test_inverted_validity_window_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="effective_to"):
            make_entry(effective_from=date(2026, 12, 1), effective_to=date(2026, 1, 1))

    def test_entry_requires_a_scope(self) -> None:
        with pytest.raises(ValidationError, match="scoped to a customer"):
            make_entry(customer_id=None, customer_tier=None)

    def test_customer_specific_entry_is_valid(self) -> None:
        entry = make_entry(customer_id=CUSTOMER_ID, customer_tier=None)
        assert entry.customer_id == CUSTOMER_ID

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            (date(2025, 12, 31), False),
            (date(2026, 1, 1), True),
            (date(2026, 12, 31), True),
            (date(2027, 1, 1), False),
        ],
    )
    def test_validity_boundaries_are_inclusive(self, as_of: date, expected: bool) -> None:
        assert make_entry().applies_on(as_of) is expected

    def test_open_ended_entry_never_expires(self) -> None:
        assert make_entry(effective_to=None).applies_on(date(2099, 1, 1)) is True

    @pytest.mark.parametrize(
        ("min_qty", "quantity", "expected"),
        [(1, 1, True), (25, 24, False), (25, 25, True), (25, 40, True)],
    )
    def test_tier_threshold_is_inclusive(self, min_qty: int, quantity: int, expected: bool) -> None:
        assert make_entry(min_qty=min_qty).satisfies_quantity(quantity) is expected


class TestPriceRef:
    def test_reference_from_entry_is_usable(self) -> None:
        ref = PriceRef.from_entry(make_entry(), as_of=date(2026, 10, 6))
        assert ref.usable is True
        assert ref.status is PriceLookupStatus.FOUND
        assert ref.price_entry_id == "PE-X120-EU-1"

    def test_missing_price_is_blocking_and_says_why(self) -> None:
        ref = PriceRef.missing(
            reason="no price book entry for this product/tier",
            as_of=date(2026, 10, 6),
            currency="EUR",
        )
        assert ref.usable is False
        assert ref.status is PriceLookupStatus.MISSING
        assert ref.blocked_reason is not None

    def test_found_status_must_not_carry_a_block_reason(self) -> None:
        with pytest.raises(ValidationError, match="must be None when status is FOUND"):
            PriceRef(
                price_entry_id="PE-1",
                unit_price=Decimal("1.0000"),
                currency="EUR",
                as_of=date(2026, 10, 6),
                status=PriceLookupStatus.FOUND,
                blocked_reason="nope",
            )

    def test_non_found_status_requires_a_block_reason(self) -> None:
        with pytest.raises(ValidationError, match="blocked_reason is required"):
            PriceRef(
                price_entry_id="PE-1",
                unit_price=Decimal("0"),
                currency="EUR",
                as_of=date(2026, 10, 6),
                status=PriceLookupStatus.MISSING,
            )


def price_entry(
    entry_id: str = "PE-LIST-0001",
    *,
    price: str = "12.5000",
    min_qty: int = 1,
    customer_id: CustomerId | None = None,
    customer_tier: str | None = LIST_TIER,
    valid_from: date = date(2026, 1, 1),
    valid_to: date | None = date(2026, 12, 31),
    currency: str = "EUR",
    product_id: ProductId = PRODUCT_X,
) -> PriceEntry:
    """Build one price entry, so a test only states what it is about.

    The defaults describe the demo business: a EUR list price for ``PRODUCT_X``,
    valid for the whole of 2026. A customer-scoped entry passes
    ``customer_tier=None`` - the data model scopes an entry to a customer *or* a
    tier, never both.
    """
    return make_entry(
        price_entry_id=entry_id,
        product_id=product_id,
        price_book_code="BK-EU-CONTRACT-2026" if customer_id else "BK-EU-2026",
        customer_id=customer_id,
        customer_tier=customer_tier,
        min_qty=min_qty,
        unit_price=Decimal(price),
        currency=currency,
        effective_from=valid_from,
        effective_to=valid_to,
    )


def contract_entry(
    entry_id: str = "PE-CUST-0001",
    *,
    price: str = "9.0000",
    customer_id: CustomerId = CUSTOMER_ID,
    **overrides: object,
) -> PriceEntry:
    """A negotiated price: scoped to one customer, with no tier."""
    return price_entry(
        entry_id,
        price=price,
        customer_id=customer_id,
        customer_tier=None,
        **overrides,  # type: ignore[arg-type]
    )


def select(
    entries: object,
    *,
    quantity: int = 10,
    as_of: date = AS_OF,
    **overrides: object,
) -> PriceSelection:
    """Select a price for ``PRODUCT_X`` with everything else defaulted."""
    return select_price(
        entries,  # type: ignore[arg-type]
        product_id=PRODUCT_X,
        quantity=quantity,
        as_of=as_of,
        **overrides,  # type: ignore[arg-type]
    )


def chosen_id(selection: PriceSelection) -> str | None:
    """The selected entry's id, or ``None`` when nothing was selected."""
    return None if selection.price is None else selection.price.price_entry_id


class TestPrecedence:
    """Steps 1-3: which scope wins, and which scope is merely a fallback."""

    def test_customer_contract_price_beats_tier_and_list_prices(self) -> None:
        entries = [
            price_entry("PE-LIST", price="12.5000"),
            price_entry("PE-TIER", price="10.0000", customer_tier="PREMIUM"),
            contract_entry("PE-CONTRACT", price="9.0000"),
        ]

        selection = select(entries, customer_id=CUSTOMER_ID, customer_tier="PREMIUM")

        assert selection.status is PriceLookupStatus.FOUND
        assert chosen_id(selection) == "PE-CONTRACT"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("9.0000")

    def test_tier_price_beats_the_list_price(self) -> None:
        entries = [
            price_entry("PE-LIST", price="12.5000"),
            price_entry("PE-TIER", price="10.0000", customer_tier="PREMIUM"),
        ]

        selection = select(entries, customer_tier="PREMIUM")

        assert chosen_id(selection) == "PE-TIER"

    def test_the_list_price_applies_when_the_customer_tier_is_unknown(self) -> None:
        entries = [
            price_entry("PE-LIST", price="12.5000"),
            price_entry("PE-TIER", price="10.0000", customer_tier="PREMIUM"),
        ]

        selection = select(entries, customer_tier=None)

        assert chosen_id(selection) == "PE-LIST"

    def test_the_list_price_applies_when_the_customer_tier_has_no_price(self) -> None:
        selection = select([price_entry("PE-LIST", price="12.5000")], customer_tier="GOLD")

        assert chosen_id(selection) == "PE-LIST"

    def test_another_customers_contract_is_never_a_candidate(self) -> None:
        """A negotiated price belongs to one customer; it is not a discount for all."""
        entries = [
            contract_entry("PE-FOREIGN", price="0.5000", customer_id=FOREIGN_CUSTOMER),
            price_entry("PE-LIST", price="12.5000"),
        ]

        selection = select(entries, customer_id=CUSTOMER_ID)

        assert chosen_id(selection) == "PE-LIST"


class TestQuantityBreaks:
    """Step 4: the highest break the requested quantity still reaches."""

    @pytest.mark.parametrize(
        ("quantity", "expected"),
        [(1, "PE-BREAK-1"), (24, "PE-BREAK-1"), (25, "PE-BREAK-25"), (100, "PE-BREAK-100")],
    )
    def test_the_highest_reachable_break_wins(self, quantity: int, expected: str) -> None:
        entries = [
            price_entry("PE-BREAK-1", price="12.5000", min_qty=1),
            price_entry("PE-BREAK-25", price="11.0000", min_qty=25),
            price_entry("PE-BREAK-100", price="9.5000", min_qty=100),
        ]

        assert chosen_id(select(entries, quantity=quantity)) == expected

    def test_scope_outranks_a_larger_volume_break(self) -> None:
        """A negotiated price is not silently replaced by a bigger list break.

        Scope is step 1 and quantity is step 4, so the customer's contract price
        applies even where the list tier would pay less per unit. Substituting one
        for the other is a decision for a human, not a rule.
        """
        entries = [
            contract_entry("PE-CONTRACT", price="10.0000"),
            price_entry("PE-LIST-BREAK", price="5.0000", min_qty=100),
        ]

        selection = select(entries, quantity=100, customer_id=CUSTOMER_ID)

        assert chosen_id(selection) == "PE-CONTRACT"

    def test_a_break_nobody_reaches_is_not_a_candidate(self) -> None:
        selection = select([price_entry("PE-BREAK-100", price="9.5000", min_qty=100)], quantity=99)

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.QUANTITY_BELOW_MIN


class TestTieBreaks:
    """Steps 5-6: latest ``effective_from``, then ``price_entry_id``."""

    def test_the_latest_effective_from_wins(self) -> None:
        entries = [
            price_entry("PE-OLD", price="12.5000", valid_from=date(2026, 1, 1)),
            price_entry("PE-NEW", price="13.0000", valid_from=date(2026, 6, 1)),
        ]

        assert chosen_id(select(entries)) == "PE-NEW"

    def test_the_lowest_entry_id_breaks_a_final_tie(self) -> None:
        entries = [
            price_entry("PE-0010", price="13.0000", valid_from=date(2026, 6, 1)),
            price_entry("PE-0002", price="12.5000", valid_from=date(2026, 6, 1)),
        ]

        assert chosen_id(select(entries)) == "PE-0002"

    def test_a_quantity_break_outranks_a_later_start_date(self) -> None:
        entries = [
            price_entry("PE-LATE", price="10.0000", min_qty=1, valid_from=date(2026, 9, 1)),
            price_entry("PE-EARLY-BREAK", price="9.0000", min_qty=50, valid_from=date(2026, 1, 1)),
        ]

        assert chosen_id(select(entries, quantity=50)) == "PE-EARLY-BREAK"


class TestValidityWindows:
    """Both ends of the window are inclusive; an expired entry is never used."""

    @pytest.mark.parametrize(
        ("offset_from", "offset_to", "expected_status"),
        [
            (0, 0, PriceLookupStatus.FOUND),
            (-1, 0, PriceLookupStatus.FOUND),
            (0, 1, PriceLookupStatus.FOUND),
            (-1, 1, PriceLookupStatus.FOUND),
            (1, 1, PriceLookupStatus.MISSING),
            (-10, -1, PriceLookupStatus.EXPIRED),
        ],
    )
    def test_window_boundaries_are_exact(
        self, offset_from: int, offset_to: int, expected_status: PriceLookupStatus
    ) -> None:
        """Offsets are relative to ``AS_OF``: both ends of the window are inclusive."""
        start = AS_OF + timedelta(days=offset_from)
        end = AS_OF + timedelta(days=offset_to)
        entry = price_entry("PE-WINDOW", valid_from=start, valid_to=end)

        assert select([entry]).status is expected_status

    def test_an_open_ended_entry_never_expires(self) -> None:
        entry = price_entry("PE-OPEN", valid_from=date(2026, 1, 1), valid_to=None)

        assert select([entry], as_of=date(2031, 7, 1)).status is PriceLookupStatus.FOUND

    def test_an_expired_contract_does_not_block_the_list_price(self) -> None:
        """A lapsed contract price is skipped, not used and not fatal."""
        entries = [
            contract_entry("PE-CONTRACT", price="9.0000", valid_to=date(2026, 6, 30)),
            price_entry("PE-LIST", price="12.5000"),
        ]

        selection = select(entries, customer_id=CUSTOMER_ID)

        assert selection.status is PriceLookupStatus.FOUND
        assert chosen_id(selection) == "PE-LIST"
        assert selection.price is not None
        assert selection.price.unit_price == Decimal("12.5000")

    def test_an_out_of_window_contract_does_not_block_the_list_price(self) -> None:
        entries = [
            contract_entry("PE-CONTRACT", price="9.0000", valid_from=date(2026, 11, 1)),
            price_entry("PE-LIST", price="12.5000"),
        ]

        assert chosen_id(select(entries, customer_id=CUSTOMER_ID)) == "PE-LIST"

    def test_an_unreachable_contract_break_does_not_block_the_list_price(self) -> None:
        entries = [
            contract_entry("PE-CONTRACT", price="9.0000", min_qty=100),
            price_entry("PE-LIST", price="12.5000"),
        ]

        selection = select(entries, quantity=10, customer_id=CUSTOMER_ID)

        assert chosen_id(selection) == "PE-LIST"


class TestNonFoundOutcomes:
    """MISSING and EXPIRED: a reason, a sentence, and never a price."""

    def test_an_expired_only_product_reports_expired(self) -> None:
        entries = [price_entry("PE-0020", valid_to=date(2026, 6, 30))]

        selection = select(entries)

        assert selection.status is PriceLookupStatus.EXPIRED
        assert selection.reason is PriceLookupReason.EXPIRED
        assert selection.price is None
        assert selection.found is False
        assert "PE-0020 ended 2026-06-30" in selection.detail

    def test_a_future_only_product_reports_not_yet_effective(self) -> None:
        entries = [price_entry("PE-0030", valid_from=date(2026, 11, 1))]

        selection = select(entries)

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.NOT_YET_EFFECTIVE
        assert "PE-0030 starts 2026-11-01" in selection.detail

    def test_an_expired_and_a_future_entry_together_report_expired(self) -> None:
        """The fixed priority: expiry is the fact an operator must act on."""
        entries = [
            price_entry("PE-PAST", valid_to=date(2026, 6, 30)),
            price_entry("PE-FUTURE", valid_from=date(2026, 11, 1)),
        ]

        assert select(entries).reason is PriceLookupReason.EXPIRED

    def test_no_entries_at_all_reports_no_entries(self) -> None:
        selection = select([])

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.NO_ENTRIES
        assert selection.price is None
        assert PRODUCT_X in selection.detail

    def test_entries_for_another_product_are_ignored(self) -> None:
        """Pricing is told which product was resolved; it does not search."""
        entries = [price_entry("PE-OTHER", product_id=PRODUCT_Y)]

        assert select(entries).reason is PriceLookupReason.NO_ENTRIES

    def test_entries_outside_the_audience_report_no_matching_scope(self) -> None:
        entries = [
            contract_entry("PE-FOREIGN", customer_id=FOREIGN_CUSTOMER),
            price_entry("PE-TIER", customer_tier="PREMIUM"),
        ]

        selection = select(entries, customer_id=CUSTOMER_ID, customer_tier="STANDARD")

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.NO_MATCHING_SCOPE
        assert "the STANDARD list tier" in selection.detail

    def test_a_currency_mismatch_is_never_converted(self) -> None:
        entries = [price_entry("PE-USD", currency="USD")]

        selection = select(entries, currency="EUR")

        assert selection.status is PriceLookupStatus.MISSING
        assert selection.reason is PriceLookupReason.CURRENCY_MISMATCH
        assert "USD" in selection.detail
        assert selection.price is None

    def test_the_currency_filter_is_opt_in(self) -> None:
        """Without a requested currency, the entry's own currency is reported."""
        selection = select([price_entry("PE-USD", currency="USD")])

        assert selection.status is PriceLookupStatus.FOUND
        assert selection.price is not None
        assert selection.price.currency == "USD"

    def test_a_wrong_currency_entry_does_not_hide_a_matching_one(self) -> None:
        entries = [
            price_entry("PE-USD", price="500.0000", currency="USD"),
            price_entry("PE-EUR", price="480.0000", currency="EUR"),
        ]

        assert chosen_id(select(entries, currency="EUR")) == "PE-EUR"


class TestSelectionContract:
    """What a selection is allowed to be."""

    def test_a_found_selection_carries_the_price_and_its_provenance(self) -> None:
        entry = price_entry("PE-LIST", price="1234.5600", min_qty=25)

        selection = select([entry], quantity=40)

        assert selection.found is True
        assert selection.reason is None
        assert selection.as_of == AS_OF
        assert selection.quantity == 40
        assert selection.product_id == PRODUCT_X
        assert selection.price is not None
        assert selection.price.price_entry_id == "PE-LIST"
        assert selection.price.unit_price == Decimal("1234.5600")
        assert selection.price.currency == "EUR"
        assert selection.price.min_qty == 25
        assert selection.price.status is PriceLookupStatus.FOUND
        assert selection.price.usable is True

    @pytest.mark.parametrize(
        "reason",
        [
            PriceLookupReason.NO_ENTRIES,
            PriceLookupReason.NO_MATCHING_SCOPE,
            PriceLookupReason.NOT_YET_EFFECTIVE,
            PriceLookupReason.EXPIRED,
            PriceLookupReason.QUANTITY_BELOW_MIN,
            PriceLookupReason.CURRENCY_MISMATCH,
        ],
    )
    def test_every_reason_maps_to_a_status_and_no_price(self, reason: PriceLookupReason) -> None:
        expected = (
            PriceLookupStatus.EXPIRED
            if reason is PriceLookupReason.EXPIRED
            else PriceLookupStatus.MISSING
        )
        selection = PriceSelection(
            product_id=PRODUCT_X,
            quantity=1,
            as_of=AS_OF,
            status=expected,
            reason=reason,
            detail="nothing applied",
        )

        assert selection.status is expected
        assert selection.price is None
        assert selection.found is False

    def test_pricing_never_reports_ambiguity(self) -> None:
        """Ambiguity is decided before pricing; the precedence is total."""
        with pytest.raises(ValidationError, match="never reports AMBIGUOUS"):
            PriceSelection(
                product_id=PRODUCT_X,
                quantity=1,
                as_of=AS_OF,
                status=PriceLookupStatus.AMBIGUOUS,
                reason=PriceLookupReason.NO_MATCHING_SCOPE,
                detail="ambiguous",
            )

    def test_a_found_selection_rejects_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="carries a price and no reason"):
            PriceSelection(
                product_id=PRODUCT_X,
                quantity=1,
                as_of=AS_OF,
                status=PriceLookupStatus.FOUND,
                price=PriceRef.from_entry(price_entry(), as_of=AS_OF),
                reason=PriceLookupReason.EXPIRED,
                detail="contradiction",
            )

    def test_a_failed_selection_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="carries a reason and no price"):
            PriceSelection(
                product_id=PRODUCT_X,
                quantity=1,
                as_of=AS_OF,
                status=PriceLookupStatus.MISSING,
                detail="no reason given",
            )

    def test_a_quantity_below_one_is_a_caller_error(self) -> None:
        with pytest.raises(ValueError, match="quantity must be at least 1"):
            select([price_entry()], quantity=0)


class TestDeterminism:
    """Same inputs, same answer - whatever order they arrive in."""

    def test_input_order_does_not_change_the_result(self) -> None:
        entries = [
            price_entry("PE-BREAK-25", price="11.0000", min_qty=25),
            price_entry("PE-BREAK-1", price="12.5000", min_qty=1),
            contract_entry("PE-CONTRACT", price="9.0000"),
            price_entry("PE-TIER", price="10.0000", customer_tier="PREMIUM"),
        ]
        arguments = {"quantity": 30, "customer_id": CUSTOMER_ID, "customer_tier": "PREMIUM"}

        results = {select(list(order), **arguments) for order in itertools.permutations(entries)}

        assert len(results) == 1
        assert chosen_id(next(iter(results))) == "PE-CONTRACT"

    def test_repeated_calls_return_equal_selections(self) -> None:
        entries = [price_entry("PE-LIST", price="12.5000")]

        assert select(entries) == select(entries)

    def test_the_callers_sequence_is_not_modified(self) -> None:
        entries = [
            price_entry("PE-B", price="11.0000"),
            price_entry("PE-A", price="12.5000"),
        ]
        snapshot = list(entries)

        select(entries)

        assert entries == snapshot

    def test_an_iterable_is_consumed_once(self) -> None:
        entries = [price_entry("PE-LIST", price="12.5000")]

        from_generator = select(entry for entry in entries)

        assert from_generator == select(entries)

    def test_the_stored_price_is_used_exactly(self) -> None:
        """Four-decimal prices stay four-decimal: nothing is rounded on the way through."""
        entries = [
            price_entry("PE-A", price="0.0002", min_qty=1),
            price_entry("PE-B", price="0.0001", min_qty=2),
        ]

        selection = select(entries, quantity=5)

        assert selection.price is not None
        assert selection.price.unit_price == Decimal("0.0001")
        assert str(selection.price.unit_price) == "0.0001"
        assert isinstance(selection.price.unit_price, Decimal)
