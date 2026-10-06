"""Discount selection against the demo dataset, through the read boundary.

The rule itself is tested in ``tests/unit/test_discounts.py``, where every
boundary can be placed on an exact day. This module runs it against the six
rules Northwind Components actually ships - the two standard ones that need an
order value, the negotiated rules for two customers, the expired one, and the
suspended campaign - read the way the rest of the system reads them: database →
read models → deterministic selection.

The seeded data is what makes the interesting cases real rather than invented: a
customer whose own rule outranks the standard ones, a customer whose rule has
expired so the standard rule has to apply instead, a rule that is switched off
inside a window that would otherwise apply, and a contract that ends on a date
the boundary can be placed on exactly.

One gap worth naming: no seeded rule sets ``min_qty``, so the demo data cannot
exercise the quantity floor - that lives in the unit tests, and this module
asserts instead that quantity never changes a seeded outcome.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from rfq_agent.domain.policy import (
    DiscountReason,
    DiscountScope,
    DiscountSelection,
    select_discount,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.read_models import DiscountRuleRecord
from rfq_agent.persistence.repositories import BusinessReader
from rfq_agent.seed import TIER_STANDARD, reset_and_seed

#: A date inside every seeded window, and inside no expired one.
TODAY = date(2026, 10, 6)

#: The customer whose negotiated rule closed on 2026-06-30.
CONTRACT_CUSTOMER = "CUS_0006"


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


def select(
    rules: Sequence[DiscountRuleRecord],
    *,
    customer_id: str | None = None,
    customer_tier: str | None = None,
    quantity: int = 40,
    order_value: str | None = None,
    as_of: date = TODAY,
) -> DiscountSelection:
    """Ask the discount question, with the facts the caller supplies."""
    return select_discount(
        rules,
        as_of=as_of,
        quantity=quantity,
        customer_id=customer_id,
        customer_tier=customer_tier,
        order_value=None if order_value is None else Decimal(order_value),
    )


class TestTheSeededRules:
    """What the boundary hands over before any decision is made."""

    def test_the_six_seeded_rules_arrive_in_id_order(self, reader: BusinessReader) -> None:
        rules = reader.discounts.rules()

        assert [rule.rule_id for rule in rules] == [
            "DSC_0001",
            "DSC_0002",
            "DSC_0003",
            "DSC_0004",
            "DSC_0005",
            "DSC_0006",
        ]

    def test_the_facts_arrive_as_the_types_the_rules_need(self, reader: BusinessReader) -> None:
        rules = {rule.rule_id: rule for rule in reader.discounts.rules()}

        storefront = rules["DSC_0001"]
        assert storefront.scope is DiscountScope.GLOBAL
        assert storefront.scope_ref is None
        assert storefront.percent == Decimal("2.00")
        assert storefront.min_order_value == Decimal("5000.00")
        assert storefront.requires_approval is False
        assert storefront.effective_from == date(2026, 1, 1)
        assert storefront.effective_to is None

        negotiated = rules["DSC_0003"]
        assert negotiated.scope is DiscountScope.CUSTOMER
        assert negotiated.scope_ref == "CUS_0001"
        assert negotiated.percent == Decimal("3.00")
        assert negotiated.min_order_value is None
        assert negotiated.effective_to == date(2026, 12, 31)

    def test_the_suspended_campaign_is_the_one_switched_off_rule(
        self, reader: BusinessReader
    ) -> None:
        """``DSC_0006`` is the seed's proof that ``active`` is consulted first."""
        rules = {rule.rule_id: rule for rule in reader.discounts.rules()}

        assert rules["DSC_0006"].active is False
        assert rules["DSC_0006"].percent == Decimal("7.50")
        assert rules["DSC_0006"].priority == 5
        assert [rule.rule_id for rule in reader.discounts.rules() if not rule.active] == [
            "DSC_0006"
        ]

    def test_no_seeded_rule_is_tier_scoped(self, reader: BusinessReader) -> None:
        """The demo data has named customers and no tiers: nothing else is on offer."""
        tier_scoped = [
            rule.rule_id
            for rule in reader.discounts.rules()
            if rule.scope is DiscountScope.CUSTOMER_TIER
        ]

        assert tier_scoped == []


class TestSeededSelections:
    """The lookup the demo data documents, one real case at a time."""

    @pytest.mark.parametrize(
        ("customer", "order_value", "expected"),
        [
            # The negotiated rules do the work for the two customers that have one.
            ("CUS_0001", "30000.00", "DSC_0003"),
            ("CUS_0001", "1000.00", "DSC_0003"),
            ("CUS_0004", "1000.00", "DSC_0004"),
            # Everyone else falls to the standard rules, the strongest first.
            ("CUS_0002", "30000.00", "DSC_0002"),
            ("CUS_0002", "6000.00", "DSC_0001"),
            ("CUS_0008", "30000.00", "DSC_0002"),
        ],
        ids=[
            "negotiated-deep-order",
            "negotiated-small-order",
            "negotiated-other-customer",
            "standard-25k-floor",
            "standard-5k-floor",
            "deactivated-customer",
        ],
    )
    def test_the_seeded_rules_pick_the_documented_rule(
        self, reader: BusinessReader, customer: str, order_value: str, expected: str
    ) -> None:
        selection = select(reader.discounts.rules(), customer_id=customer, order_value=order_value)

        assert selection.rule_id == expected
        assert expected in selection.detail

    def test_the_negotiated_rule_outranks_the_standard_ones(self, reader: BusinessReader) -> None:
        """``CUS_0001``'s 3% beats the 5% standard rule, on both documented keys.

        The seeded data keeps the narrower scopes at the higher priority, so
        "highest priority" and "narrowest scope" agree here - which is why the
        unit tests are where the two keys are pulled apart.
        """
        rules = {rule.rule_id: rule for rule in reader.discounts.rules()}
        assert rules["DSC_0002"].priority > rules["DSC_0001"].priority
        assert rules["DSC_0003"].scope is DiscountScope.CUSTOMER

        selection = select(reader.discounts.rules(), customer_id="CUS_0001", order_value="30000.00")

        assert selection.rule_id == "DSC_0003"
        assert selection.percent == Decimal("3.00")

    def test_an_expired_negotiated_rule_falls_through_to_the_standard_one(
        self, reader: BusinessReader
    ) -> None:
        """``CUS_0006``'s 6% rule closed on 2026-06-30; the 5% rule takes over."""
        selection = select(
            reader.discounts.rules(), customer_id=CONTRACT_CUSTOMER, order_value="30000.00"
        )

        assert selection.rule_id == "DSC_0002"
        assert selection.percent == Decimal("5.00")

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            ("2026-01-01", "DSC_0005"),
            ("2026-06-30", "DSC_0005"),
            ("2026-07-01", "DSC_0002"),
        ],
        ids=["first-day", "last-day-inclusive", "day-after"],
    )
    def test_the_expired_contract_holds_right_up_to_its_last_day(
        self, reader: BusinessReader, as_of: str, expected: str
    ) -> None:
        selection = select(
            reader.discounts.rules(),
            customer_id=CONTRACT_CUSTOMER,
            order_value="30000.00",
            as_of=date.fromisoformat(as_of),
        )

        assert selection.rule_id == expected

    @pytest.mark.parametrize(
        ("as_of", "expected"),
        [
            ("2025-12-31", None),
            ("2026-01-01", "DSC_0003"),
            ("2026-12-31", "DSC_0003"),
            ("2027-01-01", "DSC_0002"),
        ],
        ids=[
            "before-the-list",
            "list-start-inclusive",
            "contract-end-inclusive",
            "after-the-contract",
        ],
    )
    def test_both_ends_of_every_seeded_window_are_inclusive(
        self, reader: BusinessReader, as_of: str, expected: str | None
    ) -> None:
        selection = select(
            reader.discounts.rules(),
            customer_id="CUS_0001",
            order_value="30000.00",
            as_of=date.fromisoformat(as_of),
        )

        assert selection.rule_id == expected
        if expected is None:
            assert selection.reason is DiscountReason.NOT_YET_EFFECTIVE

    @pytest.mark.parametrize(
        ("order_value", "expected"),
        [
            (None, DiscountReason.ORDER_VALUE_UNKNOWN),
            ("4999.99", DiscountReason.BELOW_MIN_ORDER_VALUE),
            ("5000.00", None),
            ("24999.99", None),
            ("25000.00", None),
        ],
        ids=[
            "unknown",
            "below-both-floors",
            "at-the-5k-floor",
            "below-the-25k-floor",
            "at-the-25k-floor",
        ],
    )
    def test_the_order_value_floors_of_the_standard_rules(
        self, reader: BusinessReader, order_value: str | None, expected: DiscountReason | None
    ) -> None:
        selection = select(
            reader.discounts.rules(), customer_id="CUS_0002", order_value=order_value
        )

        if expected is None:
            assert selection.applied is True
        else:
            assert selection.reason is expected

    def test_no_rule_applies_to_a_customer_nobody_negotiated_for(
        self, reader: BusinessReader
    ) -> None:
        selection = select(reader.discounts.rules(), customer_id="CUS_0005", order_value="1000.00")

        assert selection.applied is False
        assert selection.reason is DiscountReason.BELOW_MIN_ORDER_VALUE
        assert selection.rule_id is None
        assert selection.percent is None

    def test_an_unresolved_customer_still_gets_the_standard_rules(
        self, reader: BusinessReader
    ) -> None:
        """No customer is a fact, not a failure: the storefront rules apply."""
        selection = select(reader.discounts.rules(), customer_id=None, order_value="30000.00")

        assert selection.rule_id == "DSC_0002"
        assert selection.customer_id is None

    def test_a_customer_that_does_not_exist_gets_nothing_negotiated(
        self, reader: BusinessReader
    ) -> None:
        """An unresolvable customer id is not a licence to use someone else's rule."""
        selection = select(reader.discounts.rules(), customer_id="CUS_9999", order_value="30000.00")

        assert selection.rule_id == "DSC_0002"
        assert selection.rule_id != "DSC_0003"


class TestTheSuspendedCampaign:
    """The 7.5% rule is never the answer - whatever the order looks like."""

    @pytest.mark.parametrize("customer", [None, "CUS_0001", "CUS_0002", "CUS_0006"])
    @pytest.mark.parametrize("order_value", [None, "1.00", "30000.00", "999999.99"])
    @pytest.mark.parametrize("as_of", ["2026-01-01", "2026-10-06", "2027-06-01"])
    def test_it_is_never_selected(
        self,
        reader: BusinessReader,
        customer: str | None,
        order_value: str | None,
        as_of: str,
    ) -> None:
        selection = select(
            reader.discounts.rules(),
            customer_id=customer,
            order_value=order_value,
            as_of=date.fromisoformat(as_of),
        )

        assert selection.rule_id != "DSC_0006"

    def test_the_rule_that_is_switched_off_is_named_when_it_is_the_only_candidate(
        self, reader: BusinessReader
    ) -> None:
        """Told about one suspended rule, the selection says why it does not apply."""
        suspended = [rule for rule in reader.discounts.rules() if rule.rule_id == "DSC_0006"]

        selection = select(suspended, order_value="30000.00")

        assert selection.reason is DiscountReason.INACTIVE
        assert "DSC_0006" in selection.detail


class TestTheApprovalFact:
    """``requires_approval`` is carried out of the dataset, not interpreted."""

    @pytest.mark.parametrize(
        ("customer", "order_value", "expected"),
        [
            ("CUS_0001", "30000.00", False),
            ("CUS_0002", "30000.00", True),
            ("CUS_0002", "6000.00", False),
            ("CUS_0004", "1000.00", True),
        ],
        ids=[
            "negotiated-delegated",
            "standard-sign-off",
            "small-order-delegated",
            "negotiated-sign-off",
        ],
    )
    def test_it_matches_the_seeded_rule(
        self, reader: BusinessReader, customer: str, order_value: str, expected: bool
    ) -> None:
        selection = select(reader.discounts.rules(), customer_id=customer, order_value=order_value)

        assert selection.applied is True
        assert selection.requires_approval is expected
        assert selection.discount is not None
        assert selection.discount.requires_approval is expected

    def test_nothing_here_approves_anything(self, reader: BusinessReader) -> None:
        selection = select(reader.discounts.rules(), customer_id="CUS_0002", order_value="30000.00")

        assert selection.discount is not None
        assert selection.discount.applied_by_human is False
        assert not hasattr(selection, "approved")

    def test_the_selection_knows_nothing_but_discounts(self, reader: BusinessReader) -> None:
        """A credit hold on the customer is not a discount fact and must not leak."""
        selection = select(reader.discounts.rules(), customer_id="CUS_0007", order_value="30000.00")

        assert selection.rule_id == "DSC_0002"
        assert "credit" not in selection.detail.lower()


class TestTheSeededGapsAndDeterminism:
    """What the demo data cannot vary, and what must not vary at all."""

    @pytest.mark.parametrize("quantity", [1, 40, 10_000])
    def test_no_seeded_rule_has_a_quantity_floor(
        self, reader: BusinessReader, quantity: int
    ) -> None:
        selection = select(
            reader.discounts.rules(),
            customer_id="CUS_0002",
            order_value="6000.00",
            quantity=quantity,
        )

        assert selection.rule_id == "DSC_0001"
        assert selection.quantity == quantity

    def test_an_unknown_order_value_is_never_read_as_zero(self, reader: BusinessReader) -> None:
        """With no order value, only floor-free rules can apply - never a guess."""
        for customer in ["CUS_0001", "CUS_0002", "CUS_0005", None]:
            selection = select(reader.discounts.rules(), customer_id=customer, order_value=None)
            assert selection.reason is not DiscountReason.BELOW_MIN_ORDER_VALUE, customer
            if not selection.applied:
                assert selection.reason is DiscountReason.ORDER_VALUE_UNKNOWN, customer

    def test_the_tier_changes_nothing_in_this_dataset(self, reader: BusinessReader) -> None:
        """The tier is offered to the selection; no seeded rule is tier-scoped.

        The decision is the same either way. The tier is still echoed back, as
        the fact it was, so the audit trail says what the caller knew.
        """
        without = select(reader.discounts.rules(), customer_id="CUS_0002", order_value="30000.00")
        with_tier = select(
            reader.discounts.rules(),
            customer_id="CUS_0002",
            customer_tier=TIER_STANDARD,
            order_value="30000.00",
        )

        assert with_tier.rule_id == without.rule_id == "DSC_0002"
        assert with_tier.reason is without.reason
        assert with_tier.detail == without.detail
        assert with_tier.customer_tier == TIER_STANDARD
        assert without.customer_tier is None

    def test_the_order_the_rules_arrive_in_does_not_matter(self, reader: BusinessReader) -> None:
        rules = reader.discounts.rules()
        forwards = select(rules, customer_id="CUS_0001", order_value="30000.00")
        backwards = select(tuple(reversed(rules)), customer_id="CUS_0001", order_value="30000.00")

        assert forwards == backwards

    def test_the_same_question_gets_the_same_answer(self, reader: BusinessReader) -> None:
        rules = reader.discounts.rules()

        first = select(rules, customer_id="CUS_0004", order_value="1000.00")
        second = select(rules, customer_id="CUS_0004", order_value="1000.00")

        assert first == second
        assert first.rule_id == "DSC_0004"
