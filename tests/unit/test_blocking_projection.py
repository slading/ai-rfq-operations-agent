"""The blocking-ledger projection: facts in, contract entries out.

Phase 1H produced the deterministic facts - which lines priced, which did not,
what each line's stock position is - and Phase 0 already defined how a blocked
quote is *represented*. This module is about the join between the two, and it is
deliberately a pure matrix: every case builds a calculation from literals, so a
failure here means the mapping is wrong, never that the data is.

Three things are asserted that a reader may not expect:

* the projection **collapses** by code, because the contract's own gate input
  refuses duplicate codes (``duplicate codes``) and the ledger has to be usable
  as ``PolicyGateInput.blocked_reasons`` unchanged. The per-line detail is not
  lost - it stays on the blocked line and in the refusal entry, and the entry
  names the lines it covers;
* ``UNKNOWN`` stock and ``UNKNOWN`` delivery are reported as
  ``STOCK_INSUFFICIENT`` and ``DELIVERY_INFEASIBLE``, because the contracts say
  so (``BLOCKING_STOCK_STATUSES`` and ``DeliveryPromise.is_blocking()``), not
  because the projection guessed what "unknown" meant for the business;
* no flags are raised. Which facts are *warnings* is not fixed by any accepted
  contract, and inventing that split here would be new policy semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal

import pytest

from rfq_agent.domain.delivery import (
    DeliveryAssessment,
    DeliveryFeasibility,
    DeliveryPromise,
)
from rfq_agent.domain.gating import (
    PolicyGateInput,
    evaluate_quote_gate,
    project_blocked_ledger,
)
from rfq_agent.domain.policy import (
    BlockedReasonCode,
    DiscountApplication,
    DiscountScope,
    QuoteBlockedLedger,
)
from rfq_agent.domain.pricing import PriceEntry, select_price
from rfq_agent.domain.quote import QuoteCalculation, QuoteLineInput, calculate_quote
from rfq_agent.domain.stock import StockStatus

#: The date every price is selected for, and the date the demo data is dated for.
AS_OF = date(2026, 10, 6)
#: The run every ledger in this module belongs to.
RUN_ID = "RUN_0001"
#: The customer every quote in this module is written for.
CUSTOMER_ID = "CUS_0001"


def entry(
    *,
    product_id: str = "PRD_0001",
    price_entry_id: str = "PE_0001",
    unit_price: str = "1234.5600",
    min_qty: int = 1,
    effective_to: date | None = None,
    customer_id: str | None = None,
) -> PriceEntry:
    """One price entry, in the shape Phase 1D's selector consumes."""
    return PriceEntry(
        price_entry_id=price_entry_id,
        product_id=product_id,
        price_book_code="BK-EU-2026",
        customer_id=customer_id,
        customer_tier="STANDARD" if customer_id is None else None,
        min_qty=min_qty,
        unit_price=Decimal(unit_price),
        currency="EUR",
        effective_from=date(2026, 1, 1),
        effective_to=effective_to,
    )


def line(
    ordinal: int,
    *,
    entries: Sequence[PriceEntry] | None = None,
    quantity: int = 5,
    product_id: str = "PRD_0001",
    stock_status: StockStatus = StockStatus.SUFFICIENT,
) -> QuoteLineInput:
    """One priced line input, with its price chosen by the accepted selector."""
    catalogue = [entry(product_id=product_id)] if entries is None else list(entries)
    selection = select_price(
        catalogue,
        product_id=product_id,
        quantity=quantity,
        as_of=AS_OF,
    )
    return QuoteLineInput(
        product_id=product_id,
        sku=f"SKU-{ordinal:04d}",
        description=f"Line {ordinal}",
        quantity=quantity,
        price=selection,
        stock_status=stock_status,
    )


def build(
    lines: Sequence[QuoteLineInput],
    *,
    discount: DiscountApplication | None = None,
    delivery: DeliveryAssessment | None = None,
) -> QuoteCalculation:
    """Run the accepted calculator over these lines."""
    return calculate_quote(
        lines,
        quote_id="QTE_0001",
        quote_number="Q-2026-0001",
        run_id=RUN_ID,
        customer_id=CUSTOMER_ID,
        currency="EUR",
        pricing_as_of=AS_OF,
        discount=discount,
        delivery=delivery,
    )


def project(
    calculation: QuoteCalculation,
    *,
    customer_on_credit_hold: bool = False,
) -> QuoteBlockedLedger:
    """Project one calculation, the way the caller will."""
    return project_blocked_ledger(
        calculation,
        run_id=RUN_ID,
        customer_on_credit_hold=customer_on_credit_hold,
    )


def promise(
    feasibility: DeliveryFeasibility,
    *,
    rationale: str = "earliest delivery 2026-10-16 via WAW DHL-EXP",
    requested: date | None = date(2026, 10, 9),
    delivered: date | None = date(2026, 10, 16),
) -> DeliveryAssessment:
    """A delivery assessment, dateless when the feasibility carries no dates."""
    return DeliveryAssessment(
        promise=DeliveryPromise(
            destination="Hamburg",
            origin_location="WAW",
            carrier_service_code="DHL-EXP",
            transit_days=None if delivered is None else 1,
            earliest_ship_date=None if delivered is None else AS_OF,
            earliest_delivery_date=delivered,
            requested_date=requested,
            feasibility=feasibility,
            rationale=rationale,
        )
    )


def approval_discount(
    *,
    percent: Decimal = Decimal("5.00"),
) -> DiscountApplication:
    """The selected rule that exceeded the delegated limit."""
    return DiscountApplication(
        rule_id="DSC_0002",
        scope=DiscountScope.GLOBAL,
        percent=percent,
        requires_approval=True,
    )


# ---------------------------------------------------------------------------
# A clean quote
# ---------------------------------------------------------------------------


def test_a_clean_quote_produces_an_empty_ledger() -> None:
    """Happy path: nothing to explain, so nothing is claimed."""
    calculation = build([line(1), line(2)])
    assert calculation.complete

    ledger = project(calculation)

    assert ledger.reasons == ()
    assert ledger.blocked is False
    assert ledger.hard_blocked is False


def test_a_clean_ledger_still_identifies_its_run_and_quote() -> None:
    """The ledger is addressable even when it is empty."""
    ledger = project(build([line(1)]))

    assert ledger.run_id == RUN_ID
    assert ledger.quote_id == "QTE_0001"


def test_a_clean_ledger_raises_no_flags() -> None:
    """Flags are not invented; the accepted gate says nothing about them either."""
    ledger = project(build([line(1)], discount=approval_discount()))

    assert ledger.flags == ()


def test_a_clean_ledger_is_what_the_accepted_gate_accepts() -> None:
    """The projection's output is usable as gate input without translation."""
    calculation = build([line(1)])
    ledger = project(calculation)

    decision = evaluate_quote_gate(
        PolicyGateInput(quote=calculation.quote, blocked_reasons=ledger.reasons)
    )

    assert decision.allowed is True
    assert decision.reason_codes == ()


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def test_a_line_without_a_price_entry_is_price_missing() -> None:
    """The refusal the calculator recorded becomes the reason an operator sees."""
    calculation = build([line(1), line(2, entries=[])])

    ledger = project(calculation)

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.PRICE_MISSING]
    assert ledger.reasons[0].line_ordinal == 2


def test_the_price_entry_reason_keeps_the_calculator_sentence() -> None:
    """The projection quotes the recorded detail; it does not paraphrase it."""
    calculation = build([line(1, entries=[])])
    refusal = calculation.refusals[0]

    message = project(calculation).reasons[0].message

    assert refusal.detail in message
    assert refusal.product_id in message
    assert f"line {refusal.ordinal}" in message


def test_every_flavour_of_unusable_price_projects_to_price_missing() -> None:
    """Missing, expired and out-of-scope prices are one code, not three."""
    expired = entry(effective_to=date(2026, 6, 30))
    too_small = entry(min_qty=25)
    cases = {"no entries": [], "expired": [expired], "below minimum": [too_small]}

    for label, entries in cases.items():
        calculation = build([line(1, entries=entries)])
        assert calculation.refusals, label
        ledger = project(calculation)
        assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.PRICE_MISSING], (
            label
        )
        assert ledger.reasons[0].line_ordinal == 1, label


def test_a_refused_line_says_so_in_the_contract_and_by_addition_nothing_else() -> None:
    """Only the five mapped codes exist: a refusal never invents a sixth."""
    ledger = project(build([line(1, entries=[])]))

    assert {reason.code for reason in ledger.reasons} == {BlockedReasonCode.PRICE_MISSING}


def test_two_refused_lines_collapse_into_one_entry_that_names_both() -> None:
    """The gate input refuses duplicate codes, so the ledger must too."""
    calculation = build([line(1, entries=[]), line(2), line(3, entries=[])])
    assert [refusal.ordinal for refusal in calculation.refusals] == [1, 3]

    ledger = project(calculation)

    assert len(ledger.reasons) == 1
    message = ledger.reasons[0].message
    assert "line 1 " in message
    assert "line 3 " in message
    assert ledger.reasons[0].line_ordinal == 1


def test_a_refusal_that_is_not_the_first_line_keeps_its_own_ordinal() -> None:
    """Attribution is the line's own position, never the position in the tuple."""
    ledger = project(build([line(1), line(2), line(3, entries=[])]))

    assert ledger.reasons[0].line_ordinal == 3


def test_a_long_list_of_refusals_is_summarised_rather_than_truncated_silently() -> None:
    """A capped message must still say how much it left out."""
    lines = [line(position) for position in range(1, 10)]
    for position in range(3, 10):
        lines[position - 1] = line(position, entries=[])

    ledger = project(build(lines))

    assert len(ledger.reasons) == 1
    message = ledger.reasons[0].message
    assert message.startswith("7 lines have no usable price")
    assert "line 8 " in message
    assert "line 9" not in message
    assert message.endswith("and 1 more")


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [StockStatus.PARTIAL, StockStatus.NONE, StockStatus.UNKNOWN],
)
def test_a_stock_status_the_contract_calls_blocking_is_reported(status: StockStatus) -> None:
    """``BLOCKING_STOCK_STATUSES`` is the authority, not this module's opinion."""
    ledger = project(build([line(1, stock_status=status)]))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.STOCK_INSUFFICIENT]
    assert ledger.reasons[0].line_ordinal == 1


@pytest.mark.parametrize(
    "status",
    [StockStatus.PARTIAL, StockStatus.NONE, StockStatus.UNKNOWN],
)
def test_the_stock_reason_names_the_status_it_saw(status: StockStatus) -> None:
    """The operator can tell "none left" from "nobody checked"."""
    message = project(build([line(1, stock_status=status)])).reasons[0].message

    assert status.value in message
    assert "PRD_0001" in message


def test_available_stock_is_not_a_reason() -> None:
    """The blocking set is exhaustive: everything outside it is silent."""
    ledger = project(build([line(1, stock_status=StockStatus.SUFFICIENT)]))

    assert ledger.reasons == ()


def test_a_priced_line_with_partial_stock_is_reported_even_though_it_totals() -> None:
    """Phase 1H totals a ``PARTIAL`` line; Phase 1I is where that shows up as a block."""
    calculation = build([line(1, stock_status=StockStatus.PARTIAL)])

    assert calculation.complete
    assert calculation.quote.lines[0].stock_status is StockStatus.PARTIAL
    assert [reason.code for reason in project(calculation).reasons] == [
        BlockedReasonCode.STOCK_INSUFFICIENT
    ]


def test_refusal_and_stock_on_the_same_line_are_two_distinct_reasons() -> None:
    """Two facts, two codes - a line blocked twice is explained twice."""
    calculation = build([line(1, entries=[], stock_status=StockStatus.NONE)])

    ledger = project(calculation)

    assert {reason.code for reason in ledger.reasons} == {
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
    }
    assert {reason.line_ordinal for reason in ledger.reasons} == {1}


def test_several_offending_lines_collapse_into_one_stock_entry() -> None:
    """Same rule as prices: one code, one entry, all the evidence inside it."""
    lines = [line(position, stock_status=StockStatus.PARTIAL) for position in range(1, 4)]

    ledger = project(build(lines))

    assert len(ledger.reasons) == 1
    message = ledger.reasons[0].message
    assert message.startswith("3 lines need stock review")
    assert ledger.reasons[0].line_ordinal == 1


def test_many_offending_lines_are_capped_in_the_message_but_counted() -> None:
    """Eight bad lines do not become an eight-line sentence."""
    lines = [line(position, stock_status=StockStatus.NONE) for position in range(1, 9)]

    ledger = project(build(lines))

    message = ledger.reasons[0].message
    assert message.startswith("8 lines need stock review")
    assert "line 6 " in message
    assert "line 7 " not in message
    assert message.endswith("and 2 more")
    assert len(message) <= 300


def test_a_refused_line_with_plenty_in_stock_is_reported_once() -> None:
    """A price problem does not drag a stock claim in behind it."""
    ledger = project(build([line(1, entries=[], stock_status=StockStatus.SUFFICIENT)]))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.PRICE_MISSING]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_an_infeasible_delivery_is_reported_with_its_rationale() -> None:
    """The promise's own words are carried through, not replaced."""
    assessment = promise(DeliveryFeasibility.INFEASIBLE)

    ledger = project(build([line(1)], delivery=assessment))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.DELIVERY_INFEASIBLE]
    assert assessment.promise.rationale in ledger.reasons[0].message


def test_an_unknown_delivery_is_reported_as_infeasible_and_as_nothing_else() -> None:
    """``is_blocking()`` covers UNKNOWN; the reason is not re-labelled."""
    assessment = promise(
        DeliveryFeasibility.UNKNOWN,
        rationale="no carrier service ships from WAW to Milan",
        requested=None,
        delivered=None,
    )

    ledger = project(build([line(1)], delivery=assessment))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.DELIVERY_INFEASIBLE]
    assert len(ledger.reasons) == 1


def test_a_feasible_delivery_is_not_a_reason() -> None:
    """Meeting the requested date needs no explanation."""
    ledger = project(
        build(
            [line(1)],
            delivery=promise(DeliveryFeasibility.FEASIBLE, delivered=date(2026, 10, 9)),
        )
    )

    assert ledger.reasons == ()


def test_a_quote_nobody_priced_delivery_for_is_silent_about_delivery() -> None:
    """Not assessing delivery is not the same as failing to deliver it."""
    ledger = project(build([line(1)], delivery=None))

    assert ledger.reasons == ()


def test_a_delivery_that_was_never_requested_is_not_a_reason() -> None:
    """``NOT_REQUESTED`` is a fact about the enquiry, not a failure."""
    ledger = project(
        build([line(1)], delivery=promise(DeliveryFeasibility.NOT_REQUESTED, requested=None))
    )

    assert ledger.reasons == ()


def test_delivery_reasons_belong_to_the_quote_not_to_a_line() -> None:
    """There is no offending line ordinal, and none is invented."""
    ledger = project(
        build([line(1)], delivery=promise(DeliveryFeasibility.INFEASIBLE)),
    )

    assert ledger.reasons[0].line_ordinal is None


# ---------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------


def test_a_discount_that_exceeds_the_delegated_limit_is_reported() -> None:
    """``requires_approval`` is the fact; reporting it is not acting on it."""
    ledger = project(build([line(1)], discount=approval_discount()))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.DISCOUNT_OVER_POLICY]


def test_the_discount_reason_names_the_rule_that_was_applied() -> None:
    """Provenance survives the projection, as it did through the arithmetic."""
    message = project(build([line(1)], discount=approval_discount())).reasons[0].message

    assert "DSC_0002" in message
    assert "5.00" in message


def test_a_delegated_discount_is_not_a_reason() -> None:
    """An approved-by-policy discount is a fact the quote carries, nothing more."""
    delegated = DiscountApplication(
        rule_id="DSC_0003",
        scope=DiscountScope.CUSTOMER,
        percent=Decimal("3.00"),
    )

    ledger = project(build([line(1)], discount=delegated))

    assert ledger.reasons == ()


def test_a_zero_percent_rule_needing_approval_is_still_reported() -> None:
    """The projection reads the flag the rule carries, not the amount it produces."""
    ledger = project(build([line(1)], discount=approval_discount(percent=Decimal("0.00"))))

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.DISCOUNT_OVER_POLICY]


def test_a_quote_with_no_discount_is_silent_about_discounts() -> None:
    """No selection, no claim."""
    ledger = project(build([line(1)], discount=None))

    assert ledger.reasons == ()


# ---------------------------------------------------------------------------
# Credit hold
# ---------------------------------------------------------------------------


def test_a_customer_on_credit_hold_is_reported_in_the_gates_own_words() -> None:
    """The same condition must read identically whoever reports it."""
    calculation = build([line(1)])

    ledger = project(calculation, customer_on_credit_hold=True)
    by_gate = evaluate_quote_gate(
        PolicyGateInput(
            quote=calculation.quote,
            blocked_reasons=ledger.reasons,
            customer_on_credit_hold=True,
        )
    )

    assert [reason.code for reason in ledger.reasons] == [BlockedReasonCode.CREDIT_HOLD]
    assert ledger.reasons[0].message in by_gate.explanation


def test_a_customer_without_a_credit_hold_is_not_a_reason() -> None:
    """Absence of the fact is silence, never an inference in either direction."""
    ledger = project(build([line(1)]), customer_on_credit_hold=False)

    assert ledger.reasons == ()


def test_the_credit_hold_fact_is_taken_from_the_caller() -> None:
    """The projection never looks a customer up; the same quote flips with the fact."""
    calculation = build([line(1)])

    assert project(calculation, customer_on_credit_hold=True).blocked is True
    assert project(calculation, customer_on_credit_hold=False).blocked is False


def test_a_credit_hold_belongs_to_the_quote_not_to_a_line() -> None:
    """No line is accused of the customer's account standing."""
    ledger = project(build([line(1)]), customer_on_credit_hold=True)

    assert ledger.reasons[0].line_ordinal is None


# ---------------------------------------------------------------------------
# Everything at once, ordering, determinism
# ---------------------------------------------------------------------------


def everything_at_once() -> QuoteBlockedLedger:
    """One quote that is wrong in all five ways at the same time."""
    calculation = build(
        [
            line(1),
            line(2, stock_status=StockStatus.PARTIAL),
            line(3, entries=[]),
        ],
        discount=approval_discount(),
        delivery=promise(DeliveryFeasibility.INFEASIBLE),
    )
    return project(calculation, customer_on_credit_hold=True)


def test_every_fact_gets_its_own_code() -> None:
    """Five independent problems cannot overwrite each other."""
    ledger = everything_at_once()

    assert {reason.code for reason in ledger.reasons} == {
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
        BlockedReasonCode.DELIVERY_INFEASIBLE,
        BlockedReasonCode.DISCOUNT_OVER_POLICY,
        BlockedReasonCode.CREDIT_HOLD,
    }


def test_a_fully_blocked_quote_is_hard_blocked() -> None:
    """A missing price is in the contract's hard set, so the ledger says so."""
    ledger = everything_at_once()

    assert ledger.blocked is True
    assert ledger.hard_blocked is True


@pytest.mark.parametrize(
    ("make_ledger", "hard"),
    [
        (lambda: project(build([line(1, stock_status=StockStatus.NONE)])), False),
        (
            lambda: project(build([line(1)], delivery=promise(DeliveryFeasibility.INFEASIBLE))),
            False,
        ),
        (lambda: project(build([line(1)], discount=approval_discount())), False),
        (lambda: project(build([line(1)]), customer_on_credit_hold=True), False),
        (lambda: project(build([line(1, entries=[])])), True),
    ],
)
def test_hard_blocking_is_the_contracts_set_and_not_the_projections(
    make_ledger: Callable[[], QuoteBlockedLedger],
    hard: bool,
) -> None:
    """A reason outside the contract's hard set blocks, but is not "hard"."""
    ledger = make_ledger()

    assert ledger.blocked is True
    assert ledger.hard_blocked is hard


def test_reasons_are_ordered_by_the_contract_not_by_the_facts() -> None:
    """The code declaration order is the ledger's order, whatever happened first."""
    ledger = project(
        build(
            [line(1, stock_status=StockStatus.NONE), line(2, entries=[])],
        )
    )

    assert [reason.code for reason in ledger.reasons] == [
        BlockedReasonCode.PRICE_MISSING,
        BlockedReasonCode.STOCK_INSUFFICIENT,
    ]
    assert [reason.line_ordinal for reason in ledger.reasons] == [2, 1]


def test_reason_order_is_non_decreasing_in_the_contracts_enumeration() -> None:
    """The invariant behind the previous test, stated once and generically."""
    rank = {code: index for index, code in enumerate(BlockedReasonCode)}

    ranks = [rank[reason.code] for reason in everything_at_once().reasons]

    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_line_reasons_are_ordered_by_their_line_ordinal_within_a_code() -> None:
    """Within one code the earliest offending line leads."""
    ledger = project(
        build([line(1, entries=[]), line(2, entries=[]), line(3, entries=[])]),
    )

    assert ledger.reasons[0].line_ordinal == 1


def test_the_same_facts_always_produce_the_same_ledger() -> None:
    """Determinism: same input, same output, no hidden state between calls."""
    calculation = build(
        [line(1), line(2, entries=[]), line(3, stock_status=StockStatus.NONE)],
        discount=approval_discount(),
    )

    first = project(calculation, customer_on_credit_hold=True)
    second = project(calculation, customer_on_credit_hold=True)

    assert first == second
    assert first.model_dump() == second.model_dump()


def test_only_the_run_id_changes_when_only_the_run_id_changes() -> None:
    """The ledger is a pure function of its arguments."""
    calculation = build([line(1, entries=[])])

    first = project_blocked_ledger(calculation, run_id="RUN_0001")
    second = project_blocked_ledger(calculation, run_id="RUN_0002")

    assert first.reasons == second.reasons
    assert first.run_id != second.run_id


def test_every_projected_reason_is_a_message_a_human_can_read() -> None:
    """The contract's own bounds, checked against the projection's longest output."""
    ledger = everything_at_once()

    assert ledger.reasons
    for reason in ledger.reasons:
        assert 1 <= len(reason.message) <= 300
        assert reason.resolvable_by_human is True


def test_projection_does_not_mutate_its_inputs() -> None:
    """Nothing is re-scaled, re-numbered or added to on the way through."""
    lines = [line(1), line(2, entries=[]), line(3, stock_status=StockStatus.NONE)]
    discount = approval_discount()
    assessment = promise(DeliveryFeasibility.INFEASIBLE)
    calculation = build(lines, discount=discount, delivery=assessment)

    before = (
        calculation.model_dump(),
        [item.model_dump() for item in lines],
        discount.model_dump(),
        assessment.model_dump(),
    )
    project(calculation, customer_on_credit_hold=True)

    after = (
        calculation.model_dump(),
        [item.model_dump() for item in lines],
        discount.model_dump(),
        assessment.model_dump(),
    )
    assert before == after


def test_projection_hands_the_gate_a_ledger_it_accepts() -> None:
    """The two halves of the accepted design meet without translation."""
    calculation = build(
        [line(1), line(2, stock_status=StockStatus.NONE), line(3, entries=[])],
        discount=approval_discount(),
    )
    ledger = project(calculation, customer_on_credit_hold=True)

    decision = evaluate_quote_gate(
        PolicyGateInput(
            quote=calculation.quote,
            blocked_reasons=ledger.reasons,
            customer_on_credit_hold=True,
        )
    )

    assert decision.allowed is False
    assert set(decision.reason_codes) == {reason.code for reason in ledger.reasons}
    assert ledger.flags == ()


def test_the_quote_status_is_untouched_by_projection() -> None:
    """Nothing is promoted, approved or sent here - the quote stays a draft."""
    calculation = build([line(1, entries=[])])
    status_before = calculation.quote.status

    project(calculation, customer_on_credit_hold=True)

    assert calculation.quote.status is status_before
