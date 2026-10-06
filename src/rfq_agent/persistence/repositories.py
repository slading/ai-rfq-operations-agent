"""Read repositories: the boundary business data crosses to leave the database.

Everything that reads master data goes through here, and what comes back is
never a row: it is a domain value object (:class:`~rfq_agent.domain.pricing.PriceEntry`,
:class:`~rfq_agent.domain.stock.StockLevel`) or a read model
(:mod:`rfq_agent.persistence.read_models`). SQLAlchemy stays inside this package,
which is what makes the rest of the system testable without a database and keeps
a schema change from reaching into business logic.

The rules these repositories follow:

**Reads only.** No method writes, commits, flushes or owns a transaction. The
caller's session lifecycle is untouched, so a repository can be used inside a
larger unit of work without surprising it.

**Deterministic order.** Every read that can return several rows has an explicit
``ORDER BY`` that ends in a unique column, so two identical calls return
identical sequences. Ordering is by stored data, never by a preference: a caller
must not read "first" as "best".

**No decisions.** Nothing here chooses a price, evaluates a validity window,
computes availability, ranks a candidate or picks between two matches. Those are
the business rules of the phases that own them, and they are easier to trust when
the code that reads the data cannot quietly pre-empt them. Concretely: this
module does not filter on ``active``, does not compare dates to "now", and does
not treat a multi-row result as a problem to be resolved.

**Ambiguity is reported, not resolved.** ``PMP-A-100`` is one pump's SKU and the
other pump's stored alias. A search for it returns *both* matches; the
convenience flags on the result say plainly that the query identifies two
products. Guessing there would produce a confidently wrong quotation.

**One normaliser.** Alias lookups compare against the stored ``normalized``
column and normalise the query with
:func:`rfq_agent.domain.resolution.normalize_alias` - the same function the seed
used to write those columns. A second, subtly different normaliser would make the
index meaningless while every test still passed.

**What absence means.** A single-identifier lookup returns ``None`` when no such
row exists. A search returns an empty result when nothing matched. Neither is a
statement about whether the thing is sellable, in stock or worth quoting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from rfq_agent.domain.pricing import PriceEntry
from rfq_agent.domain.resolution import normalize_alias
from rfq_agent.domain.stock import StockLevel
from rfq_agent.persistence.models import (
    CarrierServiceRow,
    CustomerAliasRow,
    CustomerRow,
    DiscountRuleRow,
    HolidayRow,
    PriceBookRow,
    PriceEntryRow,
    ProductAliasRow,
    ProductFamilyRow,
    ProductRow,
    StockLevelRow,
    WarehouseRow,
)
from rfq_agent.persistence.read_models import (
    CarrierServiceRecord,
    CustomerMatch,
    CustomerRecord,
    CustomerSearch,
    DiscountRuleRecord,
    HolidayRecord,
    MatchSource,
    PriceBookRecord,
    ProductFamilyRecord,
    ProductMatch,
    ProductRecord,
    ProductSearch,
    WarehouseRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

__all__ = [
    "BusinessReader",
    "CatalogRepository",
    "CustomerRepository",
    "DeliveryRepository",
    "DiscountRepository",
    "PricingRepository",
    "StockRepository",
]

# ``normalize_alias`` is deliberately *not* re-exported here: it is one rule, and
# it lives in ``rfq_agent.domain.resolution``. A caller that needs to normalise a
# string for its own purposes should import it from where it is defined rather
# than from the module that happens to use it.


# ---------------------------------------------------------------------------
# Row -> caller-visible object
# ---------------------------------------------------------------------------
#
# The seam. Each function below is where a row stops being a row, and where the
# three conversions that matter become visible in one place:
#
# * **enums** arrive as members, because the column type binds the enum's value
#   (`enum_type` in `persistence.types`); nothing is re-parsed here, and an
#   unexpected value cannot be silently coerced into a string;
# * **money** arrives as `Decimal` with the scale its column declares
#   (`MONEY` is 14,2; `UNIT_PRICE` is 14,4) - never as `float`;
# * **dates and instants** arrive as `date` and as timezone-aware UTC `datetime`,
#   because `UtcDateTime` re-attaches `tzinfo=UTC` on the way out. A naive
#   value here would mean the audit trail lost its timezone, so the tests assert
#   awareness rather than trusting the column type.
#
# The mappings do not re-parse or coerce: a value that needs "fixing" on the way
# out is a defect in the column type, and hiding it here would hide it from every
# test that reads the database directly.


def _customer(record: CustomerRow) -> CustomerRecord:
    """Map a ``customers`` row to its read model."""
    return CustomerRecord(
        customer_id=record.customer_id,
        legal_name=record.legal_name,
        display_name=record.display_name,
        country_code=record.country_code,
        default_currency=record.default_currency,
        payment_terms_days=record.payment_terms_days,
        credit_limit=record.credit_limit,
        credit_hold=record.credit_hold,
        active=record.active,
        notes=record.notes,
    )


def _product(record: ProductRow) -> ProductRecord:
    """Map a ``products`` row to its read model."""
    return ProductRecord(
        product_id=record.product_id,
        sku=record.sku,
        family_code=record.family_code,
        name=record.name,
        description=record.description,
        uom=record.uom,
        active=record.active,
    )


def _family(record: ProductFamilyRow) -> ProductFamilyRecord:
    """Map a ``product_families`` row to its read model."""
    return ProductFamilyRecord(
        family_code=record.family_code,
        name=record.name,
        description=record.description,
        sort_order=record.sort_order,
    )


def _price_book(record: PriceBookRow) -> PriceBookRecord:
    """Map a ``price_books`` row to its read model."""
    return PriceBookRecord(
        price_book_code=record.price_book_code,
        name=record.name,
        currency=record.currency,
        customer_tier=record.customer_tier,
        effective_from=record.effective_from,
        effective_to=record.effective_to,
        active=record.active,
    )


def _price_entry(record: PriceEntryRow) -> PriceEntry:
    """Map a ``price_entries`` row to the domain's :class:`PriceEntry`.

    The domain object is what the pricing rules operate on, so the boundary
    produces it directly rather than a look-alike: the window rule, the
    quantity rule and the "an entry is scoped to a customer or a tier" invariant
    are validated as the value crosses over.
    """
    return PriceEntry(
        price_entry_id=record.price_entry_id,
        product_id=record.product_id,
        price_book_code=record.price_book_code,
        customer_id=record.customer_id,
        customer_tier=record.customer_tier,
        min_qty=record.min_qty,
        unit_price=record.unit_price,
        currency=record.currency,
        effective_from=record.effective_from,
        effective_to=record.effective_to,
    )


def _stock_level(record: StockLevelRow) -> StockLevel:
    """Map a ``stock_levels`` row to the domain's :class:`StockLevel`.

    ``as_of`` crosses as an aware UTC instant; ``location_code`` becomes the
    domain's ``location``. Nothing is computed: ``available_qty`` is a domain
    property over the two quantities, not a stored or derived field here.
    """
    return StockLevel(
        product_id=record.product_id,
        location=record.location_code,
        on_hand_qty=record.on_hand_qty,
        reserved_qty=record.reserved_qty,
        inbound_qty=record.inbound_qty,
        inbound_eta=record.inbound_eta,
        as_of=record.as_of,
    )


def _warehouse(record: WarehouseRow) -> WarehouseRecord:
    """Map a ``warehouses`` row to its read model."""
    return WarehouseRecord(
        location_code=record.location_code,
        name=record.name,
        city=record.city,
        country_code=record.country_code,
        active=record.active,
    )


def _carrier_service(record: CarrierServiceRow) -> CarrierServiceRecord:
    """Map a ``carrier_services`` row to its read model."""
    return CarrierServiceRecord(
        service_code=record.service_code,
        carrier=record.carrier,
        name=record.name,
        origin_location=record.origin_location,
        transit_days_min=record.transit_days_min,
        transit_days_max=record.transit_days_max,
        cutoff_hour_utc=record.cutoff_hour_utc,
        runs_on_weekends=record.runs_on_weekends,
        active=record.active,
    )


def _holiday(record: HolidayRow) -> HolidayRecord:
    """Map a ``holidays`` row to its read model."""
    return HolidayRecord(
        country_code=record.country_code,
        holiday_date=record.holiday_date,
        name=record.name,
    )


def _discount_rule(record: DiscountRuleRow) -> DiscountRuleRecord:
    """Map a ``discount_rules`` row to its read model."""
    return DiscountRuleRecord(
        rule_id=record.rule_id,
        scope=record.scope,
        scope_ref=record.scope_ref,
        percent=record.percent,
        min_qty=record.min_qty,
        min_order_value=record.min_order_value,
        requires_approval=record.requires_approval,
        priority=record.priority,
        active=record.active,
        effective_from=record.effective_from,
        effective_to=record.effective_to,
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class _ReadRepository:
    """Shared plumbing: one session, and no way for a caller to reach it."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to ``session``.

        Args:
            session: Session to read through. The repository neither commits nor
                closes it: the caller owns the unit of work.
        """
        self._session = session


class CustomerRepository(_ReadRepository):
    """Read customers and find the ones a piece of text could refer to."""

    def get(self, customer_id: str) -> CustomerRecord | None:
        """Return the customer with ``customer_id``, or ``None`` if there is none."""
        record = self._session.get(CustomerRow, customer_id)
        return None if record is None else _customer(record)

    def search(self, text: str) -> CustomerSearch:
        """Return every customer ``text`` could identify, with the evidence.

        Two sources are consulted: the stored aliases (trading names, e-mail
        addresses, e-mail domains, legacy legal names), which are compared
        against the indexed ``normalized`` column, and the customers' own
        ``display_name`` and ``legal_name``.

        Names are compared in Python with
        :func:`~rfq_agent.domain.resolution.normalize_alias` rather than with
        SQL's ``lower()``, because SQLite's ``lower()`` is ASCII-only and would
        disagree with the stored forms for exactly the European names this
        business deals in. That is a scan over customer master data - a small,
        slowly-changing table - and it is the reason the alias column exists and
        is indexed.

        A customer may appear twice when two separate strings match: both are
        evidence, and neither is preferred.

        Args:
            text: A name, e-mail address or domain, as written by a human.

        Returns:
            Every match, ordered by customer id, source and matched text. An
            empty search means nothing matched; it is not an error.
        """
        key = normalize_alias(text)
        if not key:
            return CustomerSearch(query=text, matches=())

        customers = {
            record.customer_id: record
            for record in self._session.scalars(
                select(CustomerRow).order_by(CustomerRow.customer_id)
            )
        }
        matches: list[CustomerMatch] = []

        alias_rows = self._session.scalars(
            select(CustomerAliasRow)
            .where(CustomerAliasRow.normalized == key)
            .order_by(CustomerAliasRow.customer_id, CustomerAliasRow.alias)
        )
        for alias in alias_rows:
            record = customers.get(alias.customer_id)
            if record is not None:
                matches.append(
                    CustomerMatch(
                        customer=_customer(record),
                        source=MatchSource.ALIAS,
                        matched_text=alias.alias,
                        alias_kind=alias.kind,
                    )
                )

        matches.extend(
            CustomerMatch(
                customer=_customer(record),
                source=MatchSource.NAME,
                matched_text=name,
            )
            for record in customers.values()
            for name in (record.display_name, record.legal_name)
            if normalize_alias(name) == key
        )

        matches.sort(
            key=lambda match: (
                match.customer.customer_id,
                match.source.value,
                match.matched_text,
            )
        )
        return CustomerSearch(query=text, matches=tuple(matches))


class CatalogRepository(_ReadRepository):
    """Read the catalogue: products, their families, and what a string means."""

    def get(self, product_id: str) -> ProductRecord | None:
        """Return the product with ``product_id``, or ``None`` if there is none."""
        record = self._session.get(ProductRow, product_id)
        return None if record is None else _product(record)

    def search(
        self,
        text: str,
        *,
        sources: frozenset[MatchSource] | None = None,
    ) -> ProductSearch:
        """Return every product ``text`` could identify, with the evidence.

        The sources are the product's own ``sku``, its stored ``product_aliases``
        (customer part numbers, legacy SKUs, colloquial names) and its ``name``.
        A caller that wants to ask the narrower question "is this exactly a
        catalogue number?" passes ``sources={MatchSource.SKU}`` and gets at most
        one match per product - which is also how the deliberate ``PMP-A-100``
        collision is shown to come from the catalogue alias rather than from a
        duplicated SKU.

        Inactive products are returned like any other. A discontinued item that
        resolves is a quotation blocked by policy with an explanation; the same
        item reported as unknown is an operator wondering whether the catalogue
        is out of date.

        Args:
            text: A SKU, customer part number or product name, as written by a
                human - case, spacing and punctuation are normalised.
            sources: Which kinds of match to consider; all of them by default.
                An empty set is allowed and matches nothing.

        Returns:
            Every match, ordered by product id, source and matched text; with
            ``is_ambiguous`` true when more than one product matched.
        """
        key = normalize_alias(text)
        effective = frozenset(MatchSource) if sources is None else frozenset(sources)
        if not key or not effective:
            return ProductSearch(query=text, sources=effective, matches=())

        products = {
            record.product_id: record
            for record in self._session.scalars(select(ProductRow).order_by(ProductRow.product_id))
        }
        matches: list[ProductMatch] = []

        if MatchSource.SKU in effective:
            matches.extend(
                ProductMatch(
                    product=_product(record),
                    source=MatchSource.SKU,
                    matched_text=record.sku,
                )
                for record in products.values()
                if normalize_alias(record.sku) == key
            )

        if MatchSource.ALIAS in effective:
            alias_rows = self._session.scalars(
                select(ProductAliasRow)
                .where(ProductAliasRow.normalized == key)
                .order_by(ProductAliasRow.product_id, ProductAliasRow.alias)
            )
            for alias in alias_rows:
                record = products.get(alias.product_id)
                if record is not None:
                    matches.append(
                        ProductMatch(
                            product=_product(record),
                            source=MatchSource.ALIAS,
                            matched_text=alias.alias,
                            alias_kind=alias.kind,
                        )
                    )

        if MatchSource.NAME in effective:
            matches.extend(
                ProductMatch(
                    product=_product(record),
                    source=MatchSource.NAME,
                    matched_text=record.name,
                )
                for record in products.values()
                if normalize_alias(record.name) == key
            )

        matches.sort(
            key=lambda match: (
                match.product.product_id,
                match.source.value,
                match.matched_text,
            )
        )
        return ProductSearch(query=text, sources=effective, matches=tuple(matches))

    def family(self, family_code: str) -> ProductFamilyRecord | None:
        """Return the family with ``family_code``, or ``None`` if there is none."""
        record = self._session.get(ProductFamilyRow, family_code)
        return None if record is None else _family(record)

    def families(self) -> tuple[ProductFamilyRecord, ...]:
        """Return every family, in its configured display order."""
        rows = self._session.scalars(
            select(ProductFamilyRow).order_by(
                ProductFamilyRow.sort_order, ProductFamilyRow.family_code
            )
        )
        return tuple(_family(record) for record in rows)


class PricingRepository(_ReadRepository):
    """Read price books and price entries - and never choose one.

    Every entry that exists for a product is returned, including entries whose
    window has closed, entries in a book the customer has nothing to do with, and
    quantity breaks. Selecting among them - customer contract before tier before
    list, highest quantity break, latest ``effective_from``, ``price_entry_id``
    as the total tie-break, and the ``EXPIRED``/``MISSING`` outcomes - is
    :mod:`rfq_agent.domain.pricing`'s business, and it is implemented and tested
    on its own.
    """

    def book(self, price_book_code: str) -> PriceBookRecord | None:
        """Return the price book with ``price_book_code``, or ``None``."""
        record = self._session.get(PriceBookRow, price_book_code)
        return None if record is None else _price_book(record)

    def books(self) -> tuple[PriceBookRecord, ...]:
        """Return every price book, ordered by code."""
        rows = self._session.scalars(select(PriceBookRow).order_by(PriceBookRow.price_book_code))
        return tuple(_price_book(record) for record in rows)

    def entry(self, price_entry_id: str) -> PriceEntry | None:
        """Return the price entry with ``price_entry_id``, or ``None``."""
        record = self._session.get(PriceEntryRow, price_entry_id)
        return None if record is None else _price_entry(record)

    def entries_for_products(self, product_ids: Sequence[str]) -> tuple[PriceEntry, ...]:
        """Return every price entry for ``product_ids``, deterministically ordered.

        The order - product id, then ``min_qty``, then ``effective_from``, then
        ``price_entry_id`` - ends in a unique column, so it is total. It is a
        stable reading order, not a precedence: the lookup rule that *uses* these
        entries is what decides, and it documents its own tie-breaks.

        Args:
            product_ids: Products to collect entries for. An empty sequence
                returns an empty tuple rather than every entry in the database.

        Returns:
            The matching entries as domain objects, ordered as described.
        """
        wanted = sorted(set(product_ids))
        if not wanted:
            return ()
        rows = self._session.scalars(
            select(PriceEntryRow)
            .where(PriceEntryRow.product_id.in_(wanted))
            .order_by(
                PriceEntryRow.product_id,
                PriceEntryRow.min_qty,
                PriceEntryRow.effective_from,
                PriceEntryRow.price_entry_id,
            )
        )
        return tuple(_price_entry(record) for record in rows)


class StockRepository(_ReadRepository):
    """Read warehouse stock - and never decide whether it is enough.

    ``StockLevel`` carries what the warehouse system said and when it said it.
    Comparing a requested quantity against these numbers, and turning the result
    into an availability status or a split-shipment proposal, is the stock
    policy's job.
    """

    def warehouse(self, location_code: str) -> WarehouseRecord | None:
        """Return the warehouse with ``location_code``, or ``None``."""
        record = self._session.get(WarehouseRow, location_code)
        return None if record is None else _warehouse(record)

    def warehouses(self) -> tuple[WarehouseRecord, ...]:
        """Return every warehouse, ordered by location code."""
        rows = self._session.scalars(select(WarehouseRow).order_by(WarehouseRow.location_code))
        return tuple(_warehouse(record) for record in rows)

    def level(self, location_code: str, product_id: str) -> StockLevel | None:
        """Return one warehouse's position for one product, or ``None``.

        ``None`` means this warehouse does not stock that product - the seeded
        data leaves no row in that case, rather than a row of zeros, so "not
        stocked here" and "nothing on the shelf" stay distinguishable.
        """
        record = self._session.get(
            StockLevelRow, {"location_code": location_code, "product_id": product_id}
        )
        return None if record is None else _stock_level(record)

    def levels_for_products(self, product_ids: Sequence[str]) -> tuple[StockLevel, ...]:
        """Return every stocked position for ``product_ids``.

        Args:
            product_ids: Products to collect stock for. An empty sequence returns
                an empty tuple.

        Returns:
            One :class:`~rfq_agent.domain.stock.StockLevel` per warehouse that
            stocks the product, ordered by product id and then location code.
        """
        wanted = sorted(set(product_ids))
        if not wanted:
            return ()
        rows = self._session.scalars(
            select(StockLevelRow)
            .where(StockLevelRow.product_id.in_(wanted))
            .order_by(StockLevelRow.product_id, StockLevelRow.location_code)
        )
        return tuple(_stock_level(record) for record in rows)


class DeliveryRepository(_ReadRepository):
    """Read carrier services and the holiday calendar - and never promise a date.

    Transit days, cutoffs and the days a country does not work are inputs to the
    delivery calculation. Converting them into an earliest ship date, an earliest
    delivery date or a feasibility verdict is that calculation's job.
    """

    def service(self, service_code: str) -> CarrierServiceRecord | None:
        """Return the carrier service with ``service_code``, or ``None``."""
        record = self._session.get(CarrierServiceRow, service_code)
        return None if record is None else _carrier_service(record)

    def services(self) -> tuple[CarrierServiceRecord, ...]:
        """Return every carrier service, ordered by service code."""
        rows = self._session.scalars(
            select(CarrierServiceRow).order_by(CarrierServiceRow.service_code)
        )
        return tuple(_carrier_service(record) for record in rows)

    def services_from(self, location_code: str) -> tuple[CarrierServiceRecord, ...]:
        """Return the services shipping from one origin, ordered by service code."""
        rows = self._session.scalars(
            select(CarrierServiceRow)
            .where(CarrierServiceRow.origin_location == location_code)
            .order_by(CarrierServiceRow.service_code)
        )
        return tuple(_carrier_service(record) for record in rows)

    def holidays(self, country_codes: Iterable[str] | None = None) -> tuple[HolidayRecord, ...]:
        """Return the holiday calendar for ``country_codes``, ordered by date.

        Args:
            country_codes: Countries to read; every country in the calendar when
                omitted.

        Returns:
            One record per non-working day, ordered by country and date.
        """
        statement = select(HolidayRow).order_by(HolidayRow.country_code, HolidayRow.holiday_date)
        if country_codes is not None:
            wanted = sorted(set(country_codes))
            if not wanted:
                return ()
            statement = statement.where(HolidayRow.country_code.in_(wanted))
        return tuple(_holiday(record) for record in self._session.scalars(statement))


class DiscountRepository(_ReadRepository):
    """Read discount rules - and never decide which one applies.

    Switched-off rules, expired windows and approval requirements are all
    returned as stored. Which rule a quotation earns, and what an approval
    requirement means for it, is policy: it lives with the other decisions and is
    tested there.
    """

    def rule(self, rule_id: str) -> DiscountRuleRecord | None:
        """Return the discount rule with ``rule_id``, or ``None``."""
        record = self._session.get(DiscountRuleRow, rule_id)
        return None if record is None else _discount_rule(record)

    def rules(self) -> tuple[DiscountRuleRecord, ...]:
        """Return every discount rule, ordered by rule id."""
        rows = self._session.scalars(select(DiscountRuleRow).order_by(DiscountRuleRow.rule_id))
        return tuple(_discount_rule(record) for record in rows)


@dataclass(frozen=True, slots=True)
class BusinessReader:
    """The read repositories, bound to one session.

    One entry point rather than six imports, so a caller - a test, a tool, the
    quoting core - states once which session it is reading through and gets the
    whole business-data surface back::

        reader = BusinessReader.for_session(session)
        customer = reader.customers.get("CUS_0001")
        found = reader.catalog.search("PMP-A-100")

    The reader holds the session but never exposes it, so there is no way to
    reach a row through this object.
    """

    customers: CustomerRepository
    catalog: CatalogRepository
    pricing: PricingRepository
    stock: StockRepository
    delivery: DeliveryRepository
    discounts: DiscountRepository

    @classmethod
    def for_session(cls, session: Session) -> BusinessReader:
        """Build every repository over ``session``.

        Args:
            session: Session to read through. It is not committed or closed here;
                the caller owns the unit of work.

        Returns:
            A reader whose repositories all share that session, and therefore
            that transaction snapshot.
        """
        return cls(
            customers=CustomerRepository(session),
            catalog=CatalogRepository(session),
            pricing=PricingRepository(session),
            stock=StockRepository(session),
            delivery=DeliveryRepository(session),
            discounts=DiscountRepository(session),
        )
