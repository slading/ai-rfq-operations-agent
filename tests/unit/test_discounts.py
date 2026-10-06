"""The deterministic discount-selection rule, on hand-built rules.

Discount selection answers one question - which single rule, if any, applies to
this customer, this line and this order - and every fact it reads is placed by
hand here: the scope, the priority, the window ends, the floors, the rate and
whether the rule needs sign-off.

The precedence is the one the seeded rules document in as many words - "the
lookup (highest priority, narrowest scope, active, in window)" - and it is pinned
here one key at a time, in the order the keys are applied: applicability first
(so an inapplicable rule at a higher precedence never blocks a lower one), then
the highest priority number, then the narrowest scope, then the rule id. Two
things this module is careful about: a rule whose rate is zero is an applied rule
(a decision the data made), and a rule that needs sign-off is still selected -
the requirement travels as a fact and is not a reason to skip it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rfq_agent.domain.policy import (
    DiscountReason,
    DiscountScope,
    DiscountSelection,
    select_discount,
)
from rfq_agent.persistence.read_models import DiscountRuleRecord

AS_OF = date(2026, 10, 6)
LIST_START = "2026-01-01"
CONTRACT_END = "2026-12-31"


def rule(
    rule_id: str,
    *,
    scope: DiscountScope | str = DiscountScope.GLOBAL,
    scope_ref: str | None = None,
    percent: str = "2.00",
    min_qty: int | None = None,
    min_order_value: str | None = None,
    requires_approval: bool = False,
    priority: int = 0,
    active: bool = True,
    effective_from: str = LIST_START,
    effective_to: str | None = None,
) -> DiscountRuleRecord:
    """One discount rule row, with the awkward fields left open on purpose."""
    return DiscountRuleRecord(
        rule_id=rule_id,
        scope=scope,  # type: ignore[arg-type]
        scope_ref=scope_ref,
        percent=Decimal(percent),
        min_qty=min_qty,
        min_order_value=None if min_order_value is None else Decimal(min_order_value),
        requires_approval=requires_approval,
        priority=priority,
        active=active,
        effective_from=date.fromisoformat(effective_from),
        effective_to=None if effective_to is None else date.fromisoformat(effective_to),
    )


def select(
    rules: Sequence[DiscountRuleRecord],
    *,
    as_of: date = AS_OF,
    quantity: int = 10,
    customer_id: str | None = None,
    customer_tier: str | None = None,
    order_value: str | None = None,
) -> DiscountSelection:
    """Ask the discount question, with every fact overridable."""
    return select_discount(
        rules,
        as_of=as_of,
        quantity=quantity,
        customer_id=customer_id,
        customer_tier=customer_tier,
        order_value=None if order_value is None else Decimal(order_value),
    )


class TestApplicability:
    """What has to be true before precedence is even asked."""

    def test_a_plain_global_rule_applies(self) -> None:
        selection = select([rule("DSC_0001")])

        assert selection.applied is True
        assert selection.rule_id == "DSC_0001"
        assert selection.percent == Decimal("2.00")
        assert selection.reason is None

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            ("2026-01-01", "DSC_0001"),
            ("2026-10-06", "DSC_0001"),
            ("2026-12-31", "DSC_0001"),
        ],
    )
    def test_both_window_ends_are_inclusive(self, as_of: str, expected: str) -> None:
        selection = select(
            [rule("DSC_0001", effective_from=LIST_START, effective_to=CONTRACT_END)],
            as_of=date.fromisoformat(as_of),
        )

        assert selection.rule_id == expected

    def test_a_rule_that_starts_tomorrow_does_not_apply(self) -> None:
        selection = select([rule("DSC_0001", effective_from="2026-10-07")])

        assert selection.applied is False
        assert selection.reason is DiscountReason.NOT_YET_EFFECTIVE
        assert "starts after 2026-10-06" in selection.detail

    def test_an_expired_rule_does_not_apply(self) -> None:
        selection = select([rule("DSC_0001", effective_to="2026-10-05")])

        assert selection.applied is False
        assert selection.reason is DiscountReason.EXPIRED
        assert "ended before 2026-10-06" in selection.detail

    def test_an_open_ended_window_never_expires(self) -> None:
        selection = select([rule("DSC_0001", effective_to=None)], as_of=date(2030, 1, 1))

        assert selection.rule_id == "DSC_0001"

    def test_a_switched_off_rule_is_ignored_even_inside_its_window(self) -> None:
        """``active`` is consulted first: a suspended campaign never discounts."""
        selection = select([rule("DSC_0006", percent="7.50", active=False)])

        assert selection.applied is False
        assert selection.reason is DiscountReason.INACTIVE
        assert "switched off" in selection.detail

    @pytest.mark.parametrize(
        ("quantity", "applied"),
        [(4, False), (5, True), (6, True), (100, True)],
    )
    def test_the_quantity_floor(self, quantity: int, applied: bool) -> None:
        selection = select([rule("DSC_0001", min_qty=5)], quantity=quantity)

        assert selection.applied is applied
        if not applied:
            assert selection.reason is DiscountReason.BELOW_MIN_QTY
            assert "smallest floor 5" in selection.detail

    @pytest.mark.parametrize(
        ("order_value", "applied"),
        [(None, False), ("4999.99", False), ("5000.00", True), ("5000.01", True)],
    )
    def test_the_order_value_floor(self, order_value: str | None, applied: bool) -> None:
        selection = select([rule("DSC_0001", min_order_value="5000.00")], order_value=order_value)

        assert selection.applied is applied
        if order_value is None:
            assert selection.reason is DiscountReason.ORDER_VALUE_UNKNOWN
            assert "no order value was supplied" in selection.detail
        elif not applied:
            assert selection.reason is DiscountReason.BELOW_MIN_ORDER_VALUE
            assert "at least 5000.00" in selection.detail

    def test_a_rule_without_floors_needs_no_order_value(self) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
            customer_id="CUS_0001",
        )

        assert selection.rule_id == "DSC_0003"


class TestScope:
    """Who a rule belongs to - and who it never belongs to."""

    def test_a_customer_rule_applies_to_its_customer(self) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
            customer_id="CUS_0001",
        )

        assert selection.rule_id == "DSC_0003"

    @pytest.mark.parametrize("customer_id", ["CUS_0002", None])
    def test_a_customer_rule_never_applies_to_anyone_else(self, customer_id: str | None) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
            customer_id=customer_id,
        )

        assert selection.applied is False
        assert selection.reason is DiscountReason.NO_MATCHING_SCOPE

    def test_a_customer_rule_naming_nobody_never_applies(self) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref=None)], customer_id="CUS_0001"
        )

        assert selection.applied is False
        assert selection.reason is DiscountReason.NO_MATCHING_SCOPE

    def test_a_tier_rule_applies_to_its_tier(self) -> None:
        selection = select(
            [rule("DSC_0007", scope=DiscountScope.CUSTOMER_TIER, scope_ref="GOLD")],
            customer_id="CUS_0001",
            customer_tier="GOLD",
        )

        assert selection.rule_id == "DSC_0007"

    @pytest.mark.parametrize("customer_tier", ["STANDARD", None])
    def test_a_tier_rule_never_applies_to_another_tier(self, customer_tier: str | None) -> None:
        selection = select(
            [rule("DSC_0007", scope=DiscountScope.CUSTOMER_TIER, scope_ref="GOLD")],
            customer_id="CUS_0001",
            customer_tier=customer_tier,
        )

        assert selection.applied is False
        assert selection.reason is DiscountReason.NO_MATCHING_SCOPE

    def test_a_global_rule_applies_with_no_customer_at_all(self) -> None:
        selection = select([rule("DSC_0001")])

        assert selection.rule_id == "DSC_0001"

    def test_a_scope_the_contract_does_not_have_is_never_applied(self) -> None:
        """There is no product- or family-scoped discount; an unknown scope is not global.

        The contract cannot even express this scope, so the row is tampered with
        by hand: it stands in for a scope a future schema might add, and it must
        not be read as "applies to everyone".
        """
        unknown = rule("DSC_0009", scope="PRODUCT_FAMILY", scope_ref="FAM_PUMPS")

        selection = select([unknown], customer_id="CUS_0001", customer_tier="STANDARD")

        assert selection.applied is False
        assert selection.reason is DiscountReason.NO_MATCHING_SCOPE

    def test_every_scope_in_the_contract_is_understood(self) -> None:
        """A new :class:`DiscountScope` needs a rank, and this is where it breaks."""
        specs: dict[DiscountScope, dict[str, str]] = {
            DiscountScope.GLOBAL: {},
            DiscountScope.CUSTOMER_TIER: {"scope_ref": "GOLD"},
            DiscountScope.CUSTOMER: {"scope_ref": "CUS_0001"},
        }
        for scope in DiscountScope:
            if scope not in specs:
                pytest.fail(f"a new DiscountScope needs a rank in _SCOPE_RANKS: {scope}")

        for scope, spec in specs.items():
            selection = select(
                [rule("DSC_0001", scope=scope, **spec)],  # type: ignore[arg-type]
                customer_id="CUS_0001",
                customer_tier="GOLD",
            )
            assert selection.rule_id == "DSC_0001", scope


class TestPrecedence:
    """The documented keys, in the order they are applied."""

    @pytest.mark.parametrize(
        ("scopes", "winner"),
        [
            (("CUSTOMER", "CUSTOMER_TIER", "GLOBAL"), "CUSTOMER"),
            (("CUSTOMER_TIER", "GLOBAL"), "CUSTOMER_TIER"),
            (("GLOBAL",), "GLOBAL"),
        ],
        ids=["all-three", "no-customer-rule", "global-only"],
    )
    def test_the_narrowest_scope_wins_at_equal_priority(
        self, scopes: tuple[str, ...], winner: str
    ) -> None:
        by_scope = {
            "CUSTOMER": rule(
                "DSC_0001", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001", priority=5
            ),
            "CUSTOMER_TIER": rule(
                "DSC_0002", scope=DiscountScope.CUSTOMER_TIER, scope_ref="GOLD", priority=5
            ),
            "GLOBAL": rule("DSC_0003", scope=DiscountScope.GLOBAL, priority=5),
        }

        selection = select(
            [by_scope[scope] for scope in scopes], customer_id="CUS_0001", customer_tier="GOLD"
        )

        assert selection.rule_id == by_scope[winner].rule_id
        assert selection.discount is not None
        assert selection.discount.scope is by_scope[winner].scope
        assert selection.discount.percent == Decimal("2.00")

    @pytest.mark.parametrize(("stronger", "weaker"), [(20, 10), (0, -10), (31, 30)])
    def test_the_highest_priority_number_wins(self, stronger: int, weaker: int) -> None:
        """The seeded rules number the stronger rule higher: 20 beats 10."""
        selection = select(
            [rule("DSC_WEAK", priority=weaker), rule("DSC_STRONG", priority=stronger)]
        )

        assert selection.rule_id == "DSC_STRONG"

    def test_priority_outranks_scope(self) -> None:
        """The documented order is priority first, then scope - not the reverse."""
        wider_but_stronger = rule("DSC_GLOBAL", scope=DiscountScope.GLOBAL, priority=30)
        narrower_but_weaker = rule(
            "DSC_CUSTOMER", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001", priority=10
        )

        selection = select([wider_but_stronger, narrower_but_weaker], customer_id="CUS_0001")

        assert selection.rule_id == "DSC_GLOBAL"

    def test_the_final_tie_break_is_the_lowest_rule_id(self) -> None:
        higher = rule("DSC_0002", priority=10, percent="7.50")
        lower = rule("DSC_0001", priority=10, percent="2.00")

        selection = select([higher, lower])

        assert selection.rule_id == "DSC_0001"
        assert selection.percent == Decimal("2.00")

    def test_the_rate_is_not_a_tie_break(self) -> None:
        """A bigger number is not "better": the rule id decides, and the id is stable."""
        selection = select(
            [
                rule("DSC_0002", percent="99.00", priority=10),
                rule("DSC_0001", percent="1.00", priority=10),
            ]
        )

        assert selection.rule_id == "DSC_0001"

    def test_input_order_does_not_change_the_answer(self) -> None:
        candidates = [
            rule("DSC_0001", priority=10),
            rule("DSC_0002", priority=20),
            rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001", priority=30),
        ]

        forwards = select(candidates, customer_id="CUS_0001")
        backwards = select(tuple(reversed(candidates)), customer_id="CUS_0001")

        assert forwards == backwards
        assert forwards.rule_id == "DSC_0003"

    @pytest.mark.parametrize(
        "unusable",
        [
            rule(
                "DSC_CUSTOMER",
                scope=DiscountScope.CUSTOMER,
                scope_ref="CUS_0001",
                priority=30,
                active=False,
            ),
            rule(
                "DSC_CUSTOMER",
                scope=DiscountScope.CUSTOMER,
                scope_ref="CUS_0001",
                priority=30,
                effective_from="2026-11-01",
            ),
            rule(
                "DSC_CUSTOMER",
                scope=DiscountScope.CUSTOMER,
                scope_ref="CUS_0001",
                priority=30,
                effective_to="2026-09-30",
            ),
            rule(
                "DSC_CUSTOMER",
                scope=DiscountScope.CUSTOMER,
                scope_ref="CUS_0001",
                priority=30,
                min_qty=500,
            ),
            rule(
                "DSC_CUSTOMER",
                scope=DiscountScope.CUSTOMER,
                scope_ref="CUS_0001",
                priority=30,
                min_order_value="50000.00",
            ),
        ],
    )
    def test_an_inapplicable_higher_precedence_rule_falls_through(
        self, unusable: DiscountRuleRecord
    ) -> None:
        """Whatever stopped the customer's own rule, the global one still applies."""
        fallback = rule("DSC_0001", scope=DiscountScope.GLOBAL, priority=10)

        selection = select([unusable, fallback], customer_id="CUS_0001", order_value="1000.00")

        assert selection.rule_id == "DSC_0001"


class TestOutcomes:
    """What a selection says when it has nothing to select."""

    def test_no_rules_at_all(self) -> None:
        selection = select([])

        assert selection.applied is False
        assert selection.rule_id is None
        assert selection.percent is None
        assert selection.reason is DiscountReason.NO_RULES
        assert "no discount rule was supplied" in selection.detail

    def test_rules_exist_but_none_is_scoped_to_this_customer(self) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
            customer_id="CUS_0002",
        )

        assert selection.reason is DiscountReason.NO_MATCHING_SCOPE
        assert "customer CUS_0002" in selection.detail

    def test_every_outcome_names_its_cause(self) -> None:
        """The reason and the detail are always both present, and they agree."""
        cases: list[tuple[list[DiscountRuleRecord], DiscountReason]] = [
            ([], DiscountReason.NO_RULES),
            (
                [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
                DiscountReason.NO_MATCHING_SCOPE,
            ),
            ([rule("DSC_0001", active=False)], DiscountReason.INACTIVE),
            ([rule("DSC_0001", effective_from="2027-01-01")], DiscountReason.NOT_YET_EFFECTIVE),
            ([rule("DSC_0001", effective_to="2026-01-01")], DiscountReason.EXPIRED),
            ([rule("DSC_0001", min_qty=999)], DiscountReason.BELOW_MIN_QTY),
            ([rule("DSC_0001", min_order_value="1.00")], DiscountReason.ORDER_VALUE_UNKNOWN),
            (
                [rule("DSC_0001", min_order_value="1.00")],
                DiscountReason.BELOW_MIN_ORDER_VALUE,
            ),
        ]

        for candidates, expected in cases:
            selection = select(
                candidates,
                customer_id="CUS_0002",
                order_value=None if expected is DiscountReason.ORDER_VALUE_UNKNOWN else "0.50",
            )
            assert selection.reason is expected, expected
            assert selection.detail


class TestZeroPercentAndApproval:
    """Two facts that must survive selection untouched."""

    def test_a_zero_percent_rule_is_an_applied_rule(self) -> None:
        """The data chose "no discount": that is a decision, not a missing rule."""
        selection = select([rule("DSC_0010", percent="0.00")])

        assert selection.applied is True
        assert selection.rule_id == "DSC_0010"
        assert selection.percent == Decimal("0.00")
        assert selection.reason is None
        assert "0.00%" in selection.detail

    def test_a_zero_percent_rule_is_not_passed_over_for_a_live_one(self) -> None:
        inside_window = rule("DSC_0001", percent="0.00", priority=30)
        lower = rule("DSC_0002", percent="9.00", priority=10)

        selection = select([inside_window, lower])

        assert selection.rule_id == "DSC_0001"

    @pytest.mark.parametrize("requires_approval", [True, False])
    def test_requires_approval_travels_as_a_fact(self, requires_approval: bool) -> None:
        selection = select([rule("DSC_0004", requires_approval=requires_approval)])

        assert selection.applied is True
        assert selection.requires_approval is requires_approval
        assert selection.discount is not None
        assert selection.discount.requires_approval is requires_approval
        assert selection.discount.applied_by_human is False

    def test_a_rule_that_needs_sign_off_is_still_selected(self) -> None:
        """Nothing here approves or rejects it: the requirement routes the quote."""
        selection = select([rule("DSC_0002", percent="5.00", requires_approval=True)])

        assert selection.rule_id == "DSC_0002"
        assert selection.requires_approval is True
        assert "needs human sign-off" in selection.detail

    def test_a_delegated_rule_says_so(self) -> None:
        selection = select([rule("DSC_0001", requires_approval=False)])

        assert "delegated to the system" in selection.detail


class TestSelectionContract:
    """What the selection model itself refuses, and what it never does."""

    @staticmethod
    def payload(**overrides: object) -> dict[str, object]:
        """A real selection, dumped so one field can be tampered with."""
        payload = select([rule("DSC_0001")]).model_dump()
        payload.update(overrides)
        return payload

    def test_a_consistent_selection_survives_a_round_trip(self) -> None:
        selection = select(
            [rule("DSC_0003", scope=DiscountScope.CUSTOMER, scope_ref="CUS_0001")],
            customer_id="CUS_0001",
            customer_tier="GOLD",
            order_value="1000.00",
        )

        assert DiscountSelection.model_validate(selection.model_dump()) == selection

    def test_a_selection_cannot_carry_both_a_rule_and_a_reason(self) -> None:
        payload = self.payload(reason="INACTIVE")

        with pytest.raises(ValidationError, match="never both or neither"):
            DiscountSelection.model_validate(payload)

    def test_a_selection_cannot_carry_neither(self) -> None:
        payload = self.payload(discount=None, reason=None)

        with pytest.raises(ValidationError, match="never both or neither"):
            DiscountSelection.model_validate(payload)

    def test_a_quantity_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="quantity must be at least 1"):
            select([rule("DSC_0001")], quantity=0)

    def test_a_float_order_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never float"):
            select_discount([rule("DSC_0001")], as_of=AS_OF, quantity=1, order_value=1000.0)  # type: ignore[arg-type]

    def test_two_rules_with_one_id_are_refused(self) -> None:
        with pytest.raises(ValueError, match="more than one discount rule"):
            select([rule("DSC_0001"), rule("DSC_0001", percent="9.00")])

    def test_a_global_rule_naming_a_scope_ref_is_refused(self) -> None:
        """Saying "everyone" and "this customer" at once cannot be resolved by guesswork."""
        with pytest.raises(ValueError, match="scoped GLOBAL but name a scope_ref"):
            select([rule("DSC_0001", scope=DiscountScope.GLOBAL, scope_ref="CUS_0001")])

    def test_the_question_is_echoed_back(self) -> None:
        selection = select(
            [rule("DSC_0001")],
            customer_id="CUS_0001",
            customer_tier="GOLD",
            quantity=25,
            order_value="1000.00",
        )

        assert selection.customer_id == "CUS_0001"
        assert selection.customer_tier == "GOLD"
        assert selection.quantity == 25
        assert selection.order_value == Decimal("1000.00")
        assert selection.as_of == AS_OF

    def test_no_money_is_computed(self) -> None:
        """The rule is returned as a rate, and no amount is derived from it here."""
        selection = select([rule("DSC_0001", percent="2.00")], order_value="30000.00")

        assert selection.percent == Decimal("2.00")
        assert set(DiscountSelection.model_fields) == {
            "customer_id",
            "customer_tier",
            "quantity",
            "order_value",
            "as_of",
            "discount",
            "reason",
            "detail",
        }
        assert selection.order_value == Decimal("30000.00")
        assert not hasattr(selection, "discount_amount")


class TestDeterminism:
    """Same facts, same answer - whatever order they arrive in."""

    def test_repeated_calls_are_equal(self) -> None:
        candidates = [rule("DSC_0001"), rule("DSC_0002", priority=20)]

        assert select(candidates) == select(candidates)

    def test_the_callers_sequence_is_not_modified(self) -> None:
        candidates = [rule("DSC_0002", priority=20), rule("DSC_0001")]
        before = list(candidates)

        select(candidates, customer_id="CUS_0001", order_value="1000.00")

        assert candidates == before

    def test_an_iterable_is_consumed_once(self) -> None:
        selection = select(iter([rule("DSC_0001"), rule("DSC_0002", priority=20)]))

        assert selection.rule_id == "DSC_0002"

    def test_the_detail_stays_inside_its_contract(self) -> None:
        many = [rule(f"DSC_{index:04d}", min_order_value="999999.99") for index in range(20)]

        selection = select(many, customer_id="CUS_0001", order_value="1.00")

        assert len(selection.detail) <= 400
