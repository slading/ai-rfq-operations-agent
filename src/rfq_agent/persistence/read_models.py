"""Read models: the shapes a repository caller receives.

These are **read models**, not domain value objects and not database rows. Three
things are being kept apart on purpose:

* a *row* (``rfq_agent.persistence.models``) is a mutable SQLAlchemy object that
  belongs to a session, can trigger a lazy load, and disappears when the session
  closes. Nothing outside this package ever sees one.
* a *domain value object* (``rfq_agent.domain``) is a frozen, validated model
  that business rules operate on. Where the domain already has one - prices and
  stock levels do - the repositories return **that**, not a look-alike.
* a *read model* (here) is the plain, immutable carrier this project uses for
  master data the domain does not model yet: a customer, a catalogue item, a
  price book, a warehouse, a carrier service, a holiday, a discount rule.

The distinction is not ceremony. A read model carries exactly the columns that
exist, with the types the database holds them in (``Decimal`` money, ``date``
windows, timezone-aware UTC instants, enum members), and nothing derived. There
is no ``available_qty``, no ``price_for``, no ``is_ambiguous_match``: those are
business decisions with their own phase, their own tests and their own failure
modes.

Columns that are *facts about the data* - ``active``, ``credit_hold``,
``requires_approval`` - travel as they are. Deciding what they mean for a
quotation belongs to the deterministic core, not to the code that read them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from rfq_agent.domain.policy import DiscountScope
from rfq_agent.persistence.enums import AliasKind

__all__ = [
    "CarrierServiceRecord",
    "CustomerMatch",
    "CustomerRecord",
    "CustomerSearch",
    "DiscountRuleRecord",
    "HolidayRecord",
    "MatchSource",
    "PriceBookRecord",
    "ProductFamilyRecord",
    "ProductMatch",
    "ProductRecord",
    "ProductSearch",
    "WarehouseRecord",
]


class MatchSource(StrEnum):
    """Where a search string matched, as stored - a fact, not a ranking.

    ``SKU`` is the item's own catalogue number, ``ALIAS`` is a stored alternate
    string (a customer part number, a legacy SKU, a colloquial name) and ``NAME``
    is the descriptive name on the record itself. Which of the three is "better"
    is a scoring question for the resolver; a read model only records which one
    actually matched.
    """

    SKU = "SKU"
    ALIAS = "ALIAS"
    NAME = "NAME"


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    """One customer as stored. Not a CRM record: only what a quote needs."""

    customer_id: str
    legal_name: str
    display_name: str
    country_code: str
    #: Currency this customer is quoted in. A price in another currency is a
    #: blocking condition, never a conversion.
    default_currency: str
    payment_terms_days: int
    #: ``None`` means no limit is recorded - not "zero".
    credit_limit: Decimal | None
    credit_hold: bool
    active: bool
    notes: str | None


@dataclass(frozen=True, slots=True)
class ProductRecord:
    """One catalogue item as stored. Prices deliberately live elsewhere."""

    product_id: str
    sku: str
    family_code: str
    name: str
    description: str
    uom: str
    active: bool


@dataclass(frozen=True, slots=True)
class ProductFamilyRecord:
    """A product family, in its configured display order."""

    family_code: str
    name: str
    description: str | None
    sort_order: int


@dataclass(frozen=True, slots=True)
class CustomerMatch:
    """One stored string that identifies one customer, and how it matched.

    The same customer can legitimately appear more than once in one result: a
    message may name the trading name *and* the e-mail domain, and both are
    separate pieces of evidence. Nothing here says which evidence matters more.
    """

    customer: CustomerRecord
    source: MatchSource
    #: The stored text that matched - the alias, or the name.
    matched_text: str
    #: Which kind of alias matched, when ``source`` is :attr:`MatchSource.ALIAS`.
    alias_kind: AliasKind | None = None


@dataclass(frozen=True, slots=True)
class ProductMatch:
    """One stored string that identifies one product, and how it matched."""

    product: ProductRecord
    source: MatchSource
    matched_text: str
    alias_kind: AliasKind | None = None


@dataclass(frozen=True, slots=True)
class CustomerSearch:
    """The candidates a piece of text could refer to - all of them.

    ``is_ambiguous`` is a statement about the *data*: the query matched more than
    one customer. It is not the resolver's ``AMBIGUOUS_MATCH`` status, which is a
    decision with consequences. A caller that needs one customer must choose
    explicitly, or ask a human; taking ``matches[0]`` is exactly the guess this
    boundary exists to prevent.
    """

    query: str
    matches: tuple[CustomerMatch, ...]

    @property
    def found(self) -> bool:
        """Whether anything matched at all."""
        return bool(self.matches)

    @property
    def matched_ids(self) -> tuple[str, ...]:
        """Distinct customer ids in match order."""
        return tuple(dict.fromkeys(match.customer.customer_id for match in self.matches))

    @property
    def is_ambiguous(self) -> bool:
        """Whether the query identifies more than one customer."""
        return len(self.matched_ids) > 1


@dataclass(frozen=True, slots=True)
class ProductSearch:
    """The catalogue candidates a piece of text could refer to - all of them.

    ``PMP-A-100`` is the deliberate example: it is one pump's SKU and the other
    pump's stored alias, so this result carries two matches and
    ``is_ambiguous`` is true. Silently resolving it would be a wrong quotation
    delivered under one customer's part number.
    """

    query: str
    sources: frozenset[MatchSource]
    matches: tuple[ProductMatch, ...]

    @property
    def found(self) -> bool:
        """Whether anything matched at all."""
        return bool(self.matches)

    @property
    def matched_ids(self) -> tuple[str, ...]:
        """Distinct product ids in match order."""
        return tuple(dict.fromkeys(match.product.product_id for match in self.matches))

    @property
    def is_ambiguous(self) -> bool:
        """Whether the query identifies more than one product."""
        return len(self.matched_ids) > 1


@dataclass(frozen=True, slots=True)
class PriceBookRecord:
    """A named set of prices valid over a window."""

    price_book_code: str
    name: str
    currency: str
    #: Customer tier the book applies to; ``None`` means it is not tier-scoped.
    customer_tier: str | None
    effective_from: date
    effective_to: date | None
    active: bool


@dataclass(frozen=True, slots=True)
class WarehouseRecord:
    """A stocking location."""

    location_code: str
    name: str
    city: str
    country_code: str
    active: bool


@dataclass(frozen=True, slots=True)
class CarrierServiceRecord:
    """A shipping service from one origin, with its transit time and cutoff."""

    service_code: str
    carrier: str
    name: str
    origin_location: str
    transit_days_min: int
    transit_days_max: int
    cutoff_hour_utc: int
    runs_on_weekends: bool
    active: bool


@dataclass(frozen=True, slots=True)
class HolidayRecord:
    """One non-working day in one country."""

    country_code: str
    holiday_date: date
    name: str


@dataclass(frozen=True, slots=True)
class DiscountRuleRecord:
    """A discount rule as stored - including whether it is switched on.

    ``active``, the validity window and ``requires_approval`` travel unchanged.
    Which rule *applies* to a quotation, and what an approval requirement does to
    it, are decisions for the core.
    """

    rule_id: str
    scope: DiscountScope
    #: What the scope points at: a customer id, a tier name, or nothing.
    scope_ref: str | None
    percent: Decimal
    min_qty: int | None
    min_order_value: Decimal | None
    requires_approval: bool
    priority: int
    active: bool
    effective_from: date
    effective_to: date | None
