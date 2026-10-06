"""The Northwind Components demo dataset (architecture §5.2, Phase 1B).

Northwind Components is a fictional European industrial-components distributor:
three product families, eighteen catalogue items, eight customers, two
warehouses and the pricing, stock, carrier and calendar data a quote needs. It
is the business *content* of the system - the tables the deterministic core
reads and the only place a price, a stock figure or a delivery promise may come
from.

Design rules this module follows, in order of importance:

#. **Versioned, not generated.** Every value is a literal in this file, and
   :data:`SEED_VERSION` changes whenever any of them does. Nothing is random,
   nothing is derived from the clock or the environment, so "the demo database"
   is a reproducible artefact rather than a description of one.
#. **Explicit, with no reliance on column defaults.** Every managed column of
   every row is stated, including ``active=True`` and ``min_qty=1``. The loader
   compares what is here against what is stored, so a value that only exists as
   a default would make that comparison a guess.
#. **EUR only, no tax.** V1 is single-currency and price-display-only for tax
   (§12); a second currency would be a conversion engine, which is out of scope.
#. **The awkward cases are data too.** A discontinued product, an expired price
   window, a switched-off discount rule, a customer on credit hold, a
   deactivated account and a zero-availability stock row are all present on
   purpose: they are the conditions the later phases must handle, and having
   them in the dataset means they are handled against real rows rather than
   invented ones.
#. **One deliberate ambiguity.** Exactly one product string (``PMP-A-100``)
   resolves to two catalogue items (a cast-iron pump and its stainless variant).
   That is a legitimate catalog condition, and it is what makes the resolver's
   ``AMBIGUOUS_MATCH`` path demonstrable without adversarial input.

Nothing here performs arithmetic. This module builds rows; deciding which price
applies, whether stock is sufficient or when a shipment arrives is deterministic
business logic and lands with the repositories and the quoting core (Phase 1C
onwards).

Two identifiers deliberately match the minimal fixtures in
``tests/persistence/factories.py`` (customer ``CUS_0001``, product ``PRD_0001``,
price entry ``PE_0001``, warehouse ``WAW``, family ``FAM_PUMPS``, carrier
``DHL-EXP``), so the scaffold a persistence test builds and the demo dataset
describe the same business. Those fixtures stay minimal; this file is the
dataset.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import NamedTuple

from rfq_agent.domain.policy import DiscountScope
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.enums import AliasKind
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
from rfq_agent.seed.normalize import normalize_alias

__all__ = [
    "CALENDAR_YEAR",
    "CONTRACT_END",
    "CURRENCY",
    "DATASET_NAME",
    "LIST_START",
    "PRICE_BOOK_CONTRACT",
    "PRICE_BOOK_LIST",
    "SEED_VERSION",
    "STOCK_AS_OF",
    "TIER_STANDARD",
    "levels",
    "row_counts",
    "table_names",
    "total_rows",
]

#: Name of the dataset, printed by the CLI and shown in the operator console.
DATASET_NAME = "northwind-components"

#: Bump on **any** change to the literals below. The dataset is a test fixture
#: in the widest sense: an evaluation run recorded against one version and
#: replayed against another is not the same experiment.
SEED_VERSION = "2026.10.1"

#: V1 is single-currency (architecture §12). A price in another currency is a
#: blocking condition, never a conversion.
CURRENCY = "EUR"

#: The tier that carries the public list prices.
#:
#: ``price_entries`` allows both ``customer_id`` and ``customer_tier`` to be
#: NULL, but the domain's :class:`~rfq_agent.domain.pricing.PriceEntry` requires
#: every entry to be scoped to a customer *or* a tier. Every list price in this
#: dataset is therefore scoped to ``STANDARD`` - "the price anyone without a
#: negotiated contract is quoted" - so each row can be loaded as a domain object
#: without special-casing. Which tier a given customer falls back to is a lookup
#: rule, and it is not implemented yet.
TIER_STANDARD = "STANDARD"

#: Price books: one public list, one book of negotiated customer contracts.
PRICE_BOOK_LIST = "BK-EU-2026"
PRICE_BOOK_CONTRACT = "BK-EU-CONTRACT-2026"

#: Validity window of the public list and the contract book.
LIST_START = date(2026, 1, 1)
CONTRACT_END = date(2026, 12, 31)

#: The instant the stock rows describe - a nightly snapshot, not "now".
#:
#: A stock figure without a timestamp cannot be defended later, and a figure
#: taken from the clock would make the dataset non-reproducible; the value is
#: therefore a fixed part of the dataset.
STOCK_AS_OF = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

#: The calendar year the holiday table describes. A calendar is a dated fact:
#: the next year is a new set of rows and a new dataset version.
CALENDAR_YEAR = 2026


# ---------------------------------------------------------------------------
# Specs: the dataset as literal tables
# ---------------------------------------------------------------------------


class _Family(NamedTuple):
    """``(code, name, description, sort_order)`` for one product family."""

    family_code: str
    name: str
    description: str
    sort_order: int


class _Product(NamedTuple):
    """``(id, sku, family, name, description, uom, active)`` for one product."""

    product_id: str
    sku: str
    family_code: str
    name: str
    description: str
    uom: str
    active: bool


class _ProductAlias(NamedTuple):
    """``(product_id, alias, kind)`` - an alternate way a product is written."""

    product_id: str
    alias: str
    kind: AliasKind


class _Customer(NamedTuple):
    """One customer master-data record (currency is always :data:`CURRENCY`)."""

    customer_id: str
    legal_name: str
    display_name: str
    country_code: str
    payment_terms_days: int
    credit_limit: str
    credit_hold: bool
    active: bool
    notes: str | None = None


class _CustomerAlias(NamedTuple):
    """``(customer_id, alias, kind)`` - a string that may identify a customer."""

    customer_id: str
    alias: str
    kind: AliasKind


class _Book(NamedTuple):
    """One price book."""

    price_book_code: str
    name: str
    customer_tier: str | None
    effective_from: date
    effective_to: date | None


class _Price(NamedTuple):
    """One price entry.

    ``unit_price`` is a string so the literal keeps the column's scale
    (``"1234.5600"``, not ``1234.56``): ``Decimal`` is constructed from it, and
    a float literal here would quietly reintroduce the rounding this project
    spends effort avoiding.
    """

    price_entry_id: str
    product_id: str
    min_qty: int
    unit_price: str
    effective_from: date
    effective_to: date | None
    customer_id: str | None


class _Discount(NamedTuple):
    """One discount rule."""

    rule_id: str
    scope: DiscountScope
    scope_ref: str | None
    percent: str
    min_qty: int | None
    min_order_value: str | None
    requires_approval: bool
    priority: int
    active: bool
    effective_from: date
    effective_to: date | None


class _Warehouse(NamedTuple):
    """One stocking location."""

    location_code: str
    name: str
    city: str
    country_code: str


class _Stock(NamedTuple):
    """One warehouse position. ``reserved`` may not exceed ``on_hand``."""

    location_code: str
    product_id: str
    on_hand: int = 0
    reserved: int = 0
    inbound: int = 0
    inbound_eta: date | None = None


class _Carrier(NamedTuple):
    """One shipping service from one origin."""

    service_code: str
    carrier: str
    name: str
    origin_location: str
    transit_days_min: int
    transit_days_max: int
    cutoff_hour_utc: int
    runs_on_weekends: bool = False


# ---------------------------------------------------------------------------
# Product families and catalogue
# ---------------------------------------------------------------------------

_FAMILIES: tuple[_Family, ...] = (
    _Family("FAM_PUMPS", "Centrifugal pumps", "Industrial centrifugal pumps", 1),
    _Family("FAM_VALVES", "Valves and actuators", "Shut-off, control and actuated valves", 2),
    _Family("FAM_INSTR", "Instrumentation", "Pressure, flow, temperature and level instruments", 3),
)

_PRODUCTS: tuple[_Product, ...] = (
    # Pumps - PRD_0001 and PRD_0002 are the deliberate ambiguity: the customer
    # part number is the same string for the cast-iron and the stainless pump.
    _Product(
        "PRD_0001",
        "PMP-A-100",
        "FAM_PUMPS",
        "Centrifugal pump PMP-A-100",
        "Cast-iron centrifugal pump, 100 mm flange",
        "EA",
        True,
    ),
    _Product(
        "PRD_0002",
        "PMP-A-100-SS",
        "FAM_PUMPS",
        "Centrifugal pump PMP-A-100-SS",
        "Stainless-steel centrifugal pump, 100 mm flange",
        "EA",
        True,
    ),
    _Product(
        "PRD_0003",
        "PMP-B-150",
        "FAM_PUMPS",
        "Centrifugal pump PMP-B-150",
        "Cast-iron centrifugal pump, 150 mm flange",
        "EA",
        True,
    ),
    _Product(
        "PRD_0004",
        "PMP-B-200",
        "FAM_PUMPS",
        "Centrifugal pump PMP-B-200",
        "Cast-iron centrifugal pump, 200 mm flange",
        "EA",
        True,
    ),
    _Product(
        "PRD_0005",
        "PMP-C-250",
        "FAM_PUMPS",
        "Multistage pump PMP-C-250",
        "Four-stage stainless-steel pump for boiler feed duty",
        "EA",
        True,
    ),
    # Discontinued 2026-06-30: kept in the catalogue with active=False so a
    # request for it resolves to a product rather than to UNKNOWN_SKU, and the
    # quote is blocked by policy instead of by a missing record.
    _Product(
        "PRD_0006",
        "PMP-D-300",
        "FAM_PUMPS",
        "End-suction pump PMP-D-300",
        "End-suction pump, 300 mm flange - discontinued 2026-06-30",
        "EA",
        False,
    ),
    # Valves and actuators.
    _Product(
        "PRD_0007",
        "VLV-BF-050",
        "FAM_VALVES",
        "Butterfly valve VLV-BF-050",
        "Wafer butterfly valve, DN50, EPDM seat, cast iron",
        "EA",
        True,
    ),
    _Product(
        "PRD_0008",
        "VLV-BF-080",
        "FAM_VALVES",
        "Butterfly valve VLV-BF-080",
        "Wafer butterfly valve, DN80, EPDM seat, cast iron",
        "EA",
        True,
    ),
    _Product(
        "PRD_0009",
        "VLV-GT-025",
        "FAM_VALVES",
        "Gate valve VLV-GT-025",
        "Rising-stem gate valve, DN25, brass",
        "EA",
        True,
    ),
    _Product(
        "PRD_0010",
        "VLV-GT-040",
        "FAM_VALVES",
        "Gate valve VLV-GT-040",
        "Rising-stem gate valve, DN40, brass",
        "EA",
        True,
    ),
    _Product(
        "PRD_0011",
        "VLV-BL-020",
        "FAM_VALVES",
        "Ball valve VLV-BL-020",
        "Two-piece ball valve, DN20, stainless steel",
        "EA",
        True,
    ),
    # Contract-only item: no list price, so a general enquiry for it is the
    # PRICE MISSING case (F13) rather than a zero or an invented number.
    _Product(
        "PRD_0012",
        "VLV-AC-063",
        "FAM_VALVES",
        "Pneumatic actuator VLV-AC-063",
        "Double-acting pneumatic actuator, 63 mm stroke",
        "EA",
        True,
    ),
    # Instrumentation.
    _Product(
        "PRD_0013",
        "INS-PG-063",
        "FAM_INSTR",
        "Pressure gauge INS-PG-063",
        "Bourdon-tube pressure gauge, 63 mm, 0-16 bar",
        "EA",
        True,
    ),
    _Product(
        "PRD_0014",
        "INS-PG-100",
        "FAM_INSTR",
        "Pressure gauge INS-PG-100",
        "Bourdon-tube pressure gauge, 100 mm, 0-25 bar",
        "EA",
        True,
    ),
    _Product(
        "PRD_0015",
        "INS-FM-025",
        "FAM_INSTR",
        "Flow meter INS-FM-025",
        "Electromagnetic flow meter, DN25, 4-20 mA",
        "EA",
        True,
    ),
    _Product(
        "PRD_0016",
        "INS-FM-050",
        "FAM_INSTR",
        "Flow meter INS-FM-050",
        "Electromagnetic flow meter, DN50, 4-20 mA",
        "EA",
        True,
    ),
    _Product(
        "PRD_0017",
        "INS-TT-200",
        "FAM_INSTR",
        "Temperature transmitter INS-TT-200",
        "Pt100 transmitter, 4-20 mA, -50 to 200 C",
        "EA",
        True,
    ),
    _Product(
        "PRD_0018",
        "INS-LS-300",
        "FAM_INSTR",
        "Level switch INS-LS-300",
        "Vibrating-fork level switch, stainless steel",
        "EA",
        True,
    ),
)

_PRODUCT_ALIASES: tuple[_ProductAlias, ...] = (
    # Customer part numbers - the customer's own world, which is why they are
    # stored rather than guessed at. ``100-ABC`` is Nordwind's number for the
    # cast-iron pump; the stainless variant carries its own.
    _ProductAlias("PRD_0001", "100-ABC", AliasKind.CUSTOMER_PART),
    _ProductAlias("PRD_0002", "100-ABC-SS", AliasKind.CUSTOMER_PART),
    # THE deliberate collision (see the module docstring): customers routinely
    # say "PMP-A-100" when they mean either the cast-iron pump (its SKU) or the
    # stainless one. Two candidates, no defensible automatic choice - the
    # resolver must report AMBIGUOUS_MATCH and let a human pick.
    _ProductAlias("PRD_0002", "PMP-A-100", AliasKind.NAME),
    # How the rest get written in prose, on drawings or in legacy systems.
    _ProductAlias("PRD_0001", "PMP A 100", AliasKind.NAME),
    _ProductAlias("PRD_0003", "PMP B 150", AliasKind.NAME),
    _ProductAlias("PRD_0005", "boiler feed pump", AliasKind.NAME),
    _ProductAlias("PRD_0007", "BF-50", AliasKind.SKU),
    _ProductAlias("PRD_0008", "BF-80", AliasKind.SKU),
    _ProductAlias("PRD_0009", "gate 25", AliasKind.NAME),
    # Legacy SKU without the zero padding it was given in 2024.
    _ProductAlias("PRD_0011", "VLV-BL-20", AliasKind.SKU),
    _ProductAlias("PRD_0012", "AC-63", AliasKind.SKU),
    _ProductAlias("PRD_0013", "PG-63", AliasKind.SKU),
    _ProductAlias("PRD_0014", "Manometer 100", AliasKind.NAME),
    _ProductAlias("PRD_0015", "FM-25", AliasKind.SKU),
    _ProductAlias("PRD_0017", "TT-200", AliasKind.SKU),
    _ProductAlias("PRD_0018", "vibrating fork switch", AliasKind.NAME),
)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

_CUSTOMERS: tuple[_Customer, ...] = (
    _Customer(
        "CUS_0001",
        "Nordwind Industrie GmbH",
        "Nordwind",
        "DE",
        30,
        "50000.00",
        False,
        True,
        "Key account. Contract prices agreed for pumps and butterfly valves.",
    ),
    _Customer(
        "CUS_0002",
        "Vistula Machinery Sp. z o.o.",
        "Vistula Machinery",
        "PL",
        14,
        "25000.00",
        False,
        True,
        None,
    ),
    _Customer(
        "CUS_0003",
        "Bohemia Process s.r.o.",
        "Bohemia Process",
        "CZ",
        30,
        "15000.00",
        False,
        True,
        None,
    ),
    _Customer(
        "CUS_0004",
        "Alpen Technik AG",
        "Alpen Technik",
        "AT",
        45,
        "75000.00",
        False,
        True,
        "Contract prices for multistage pumps and pneumatic actuators.",
    ),
    _Customer(
        "CUS_0005",
        "Delta Pompen B.V.",
        "Delta Pompen",
        "NL",
        30,
        "30000.00",
        False,
        True,
        None,
    ),
    _Customer(
        "CUS_0006",
        "Compagnie Fluides SAS",
        "Compagnie Fluides",
        "FR",
        60,
        "120000.00",
        False,
        True,
        "Contract price for electromagnetic flow meters, annual window.",
    ),
    _Customer(
        "CUS_0007",
        "Nordic Kraft AB",
        "Nordic Kraft",
        "SE",
        30,
        "20000.00",
        # Credit hold: quotes may be prepared but must not be sent without a
        # finance decision (failure case F11).
        True,
        True,
        "Credit hold since 2026-09-15: quotes need finance sign-off before sending.",
    ),
    _Customer(
        "CUS_0008",
        "Rheinland Anlagenbau GmbH",
        "Rheinland Anlagenbau",
        "DE",
        14,
        "10000.00",
        False,
        # Deactivated account: the record must still resolve, so the operator is
        # told the account is closed rather than that the customer is unknown.
        False,
        "Account deactivated 2026-08-01. Route new enquiries to sales.",
    ),
)

_CUSTOMER_ALIASES: tuple[_CustomerAlias, ...] = (
    _CustomerAlias("CUS_0001", "Nordwind", AliasKind.NAME),
    _CustomerAlias("CUS_0001", "nordwind-industrie.de", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0001", "einkauf@nordwind-industrie.de", AliasKind.EMAIL),
    _CustomerAlias("CUS_0002", "Vistula", AliasKind.NAME),
    _CustomerAlias("CUS_0002", "vistula.pl", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0003", "Bohemia", AliasKind.NAME),
    _CustomerAlias("CUS_0003", "bohemia-process.cz", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0004", "Alpen", AliasKind.NAME),
    _CustomerAlias("CUS_0004", "alpen-technik.at", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0005", "Delta Pompen BV", AliasKind.NAME),
    _CustomerAlias("CUS_0005", "deltapompen.nl", AliasKind.EMAIL_DOMAIN),
    # The French customer is known internally by its initials.
    _CustomerAlias("CUS_0006", "CFS", AliasKind.NAME),
    _CustomerAlias("CUS_0006", "compagnie-fluides.fr", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0007", "Nordic", AliasKind.NAME),
    _CustomerAlias("CUS_0007", "nordickraft.se", AliasKind.EMAIL_DOMAIN),
    _CustomerAlias("CUS_0008", "Rheinland", AliasKind.NAME),
    _CustomerAlias("CUS_0008", "rheinland-anlagenbau.de", AliasKind.EMAIL_DOMAIN),
    # A trading name the company stopped using in 2023 - mail still arrives
    # addressed this way, and it must resolve to the customer that exists today.
    _CustomerAlias("CUS_0008", "Altbau Rheinland GmbH", AliasKind.NAME),
)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

_PRICE_BOOKS: tuple[_Book, ...] = (
    _Book(PRICE_BOOK_LIST, "EU list 2026", TIER_STANDARD, LIST_START, None),
    _Book(PRICE_BOOK_CONTRACT, "EU contract prices 2026", None, LIST_START, CONTRACT_END),
)

_PRICES: tuple[_Price, ...] = (
    # --- Public list (tier STANDARD) -------------------------------------
    _Price("PE_0001", "PRD_0001", 1, "1234.5600", LIST_START, None, None),
    # Quantity break: 25 units or more at roughly 4% below the single-unit price.
    _Price("PE_0002", "PRD_0001", 25, "1185.1700", LIST_START, None, None),
    _Price("PE_0003", "PRD_0002", 1, "1789.0000", LIST_START, None, None),
    _Price("PE_0004", "PRD_0003", 1, "1560.0000", LIST_START, None, None),
    _Price("PE_0005", "PRD_0004", 1, "1980.0000", LIST_START, None, None),
    _Price("PE_0006", "PRD_0005", 1, "3420.5000", LIST_START, None, None),
    _Price("PE_0007", "PRD_0005", 10, "3283.6800", LIST_START, None, None),
    _Price("PE_0008", "PRD_0007", 1, "142.7500", LIST_START, None, None),
    _Price("PE_0009", "PRD_0007", 50, "134.1900", LIST_START, None, None),
    _Price("PE_0010", "PRD_0008", 1, "178.4000", LIST_START, None, None),
    _Price("PE_0011", "PRD_0009", 1, "38.9000", LIST_START, None, None),
    _Price("PE_0012", "PRD_0010", 1, "52.6000", LIST_START, None, None),
    _Price("PE_0013", "PRD_0011", 1, "61.2000", LIST_START, None, None),
    _Price("PE_0014", "PRD_0013", 1, "84.5000", LIST_START, None, None),
    _Price("PE_0015", "PRD_0014", 1, "139.9000", LIST_START, None, None),
    _Price("PE_0016", "PRD_0015", 1, "1245.0000", LIST_START, None, None),
    _Price("PE_0017", "PRD_0016", 1, "1580.0000", LIST_START, None, None),
    _Price("PE_0018", "PRD_0017", 1, "268.4000", LIST_START, None, None),
    _Price("PE_0019", "PRD_0018", 1, "176.3000", LIST_START, None, None),
    # The discontinued pump: the only entry that exists for it is out of
    # validity, which is the EXPIRED price case (F14). It is not deleted,
    # because a quote sent in May still has to be explainable in October.
    _Price("PE_0020", "PRD_0006", 1, "2140.0000", LIST_START, date(2026, 6, 30), None),
    # --- Negotiated customer contracts -----------------------------------
    _Price("PE_0021", "PRD_0001", 1, "1150.0000", LIST_START, CONTRACT_END, "CUS_0001"),
    _Price("PE_0022", "PRD_0002", 1, "1690.0000", LIST_START, CONTRACT_END, "CUS_0001"),
    _Price("PE_0023", "PRD_0003", 1, "1480.0000", LIST_START, CONTRACT_END, "CUS_0001"),
    _Price("PE_0024", "PRD_0005", 1, "3195.0000", LIST_START, CONTRACT_END, "CUS_0004"),
    _Price("PE_0025", "PRD_0012", 1, "612.0000", LIST_START, CONTRACT_END, "CUS_0004"),
    _Price("PE_0026", "PRD_0015", 1, "1180.0000", LIST_START, CONTRACT_END, "CUS_0006"),
    _Price("PE_0027", "PRD_0009", 10, "36.4000", LIST_START, CONTRACT_END, "CUS_0002"),
)

#: Rules are data, so a policy change is a data change (see the model docstring).
#: ``requires_approval`` is what routes a quote to a human: nothing else here
#: decides that, and nothing here decides *which* rule applies - the lookup
#: (highest priority, narrowest scope, active, in window) is Phase 1C.
_DISCOUNTS: tuple[_Discount, ...] = (
    # Standard order-value discount, delegated to the system.
    _Discount(
        "DSC_0001",
        DiscountScope.GLOBAL,
        None,
        "2.00",
        None,
        "5000.00",
        False,
        10,
        True,
        LIST_START,
        None,
    ),
    # Volume discount beyond the delegated limit: needs sign-off.
    _Discount(
        "DSC_0002",
        DiscountScope.GLOBAL,
        None,
        "5.00",
        None,
        "25000.00",
        True,
        20,
        True,
        LIST_START,
        None,
    ),
    # Nordwind's negotiated 3% for 2026.
    _Discount(
        "DSC_0003",
        DiscountScope.CUSTOMER,
        "CUS_0001",
        "3.00",
        None,
        None,
        False,
        30,
        True,
        LIST_START,
        CONTRACT_END,
    ),
    # Alpen Technik's 4.5%, which always needs a human.
    _Discount(
        "DSC_0004",
        DiscountScope.CUSTOMER,
        "CUS_0004",
        "4.50",
        None,
        None,
        True,
        30,
        True,
        LIST_START,
        CONTRACT_END,
    ),
    # Expired: the window closed on 2026-06-30 and the rule must not apply.
    _Discount(
        "DSC_0005",
        DiscountScope.CUSTOMER,
        "CUS_0006",
        "6.00",
        None,
        None,
        True,
        30,
        True,
        LIST_START,
        date(2026, 6, 30),
    ),
    # Switched off, window open: proof that ``active`` is consulted before the
    # window, otherwise a suspended campaign would still discount.
    _Discount(
        "DSC_0006",
        DiscountScope.GLOBAL,
        None,
        "7.50",
        None,
        None,
        True,
        5,
        False,
        LIST_START,
        None,
    ),
)


# ---------------------------------------------------------------------------
# Warehouses, stock and delivery
# ---------------------------------------------------------------------------

_WAREHOUSES: tuple[_Warehouse, ...] = (
    _Warehouse("WAW", "Warsaw DC", "Warsaw", "PL"),
    _Warehouse("BER", "Berlin DC", "Berlin", "DE"),
)

#: Warsaw holds everything that is still sold; Berlin holds a subset, and the
#: products it does not stock have no row at all - "not stocked here" is the
#: absence of a row, not a zero, so the two cases stay distinguishable.
_STOCK: tuple[_Stock, ...] = (
    # --- Warsaw ----------------------------------------------------------
    _Stock("WAW", "PRD_0001", on_hand=120, reserved=20),
    _Stock("WAW", "PRD_0002", on_hand=40, inbound=60, inbound_eta=date(2026, 10, 20)),
    _Stock("WAW", "PRD_0003", on_hand=65, reserved=5),
    _Stock("WAW", "PRD_0004", on_hand=18, inbound=24, inbound_eta=date(2026, 10, 27)),
    _Stock("WAW", "PRD_0005", on_hand=12, reserved=2),
    _Stock("WAW", "PRD_0007", on_hand=400, reserved=150),
    _Stock("WAW", "PRD_0008", on_hand=260),
    _Stock("WAW", "PRD_0009", on_hand=900, reserved=100),
    _Stock("WAW", "PRD_0010", on_hand=620),
    _Stock("WAW", "PRD_0011", on_hand=750),
    _Stock("WAW", "PRD_0012", on_hand=35),
    _Stock("WAW", "PRD_0013", on_hand=1500, reserved=200),
    _Stock("WAW", "PRD_0014", on_hand=980),
    _Stock("WAW", "PRD_0015", on_hand=22, reserved=4),
    _Stock("WAW", "PRD_0016", on_hand=14),
    _Stock("WAW", "PRD_0017", on_hand=310),
    _Stock("WAW", "PRD_0018", on_hand=205),
    # --- Berlin ----------------------------------------------------------
    _Stock("BER", "PRD_0001", on_hand=55, reserved=10),
    _Stock("BER", "PRD_0003", on_hand=30),
    _Stock("BER", "PRD_0007", on_hand=180, reserved=20),
    _Stock("BER", "PRD_0009", on_hand=450),
    # Nothing on the shelf and 120 units on the water: the PARTIAL case, with
    # the ETA that makes a split shipment a proposal rather than a guess.
    _Stock("BER", "PRD_0011", inbound=120, inbound_eta=date(2026, 10, 13)),
    _Stock("BER", "PRD_0013", on_hand=640),
    _Stock("BER", "PRD_0014", on_hand=420),
    _Stock("BER", "PRD_0015", on_hand=6),
    _Stock("BER", "PRD_0017", on_hand=140),
    _Stock("BER", "PRD_0018", on_hand=88),
)

_CARRIERS: tuple[_Carrier, ...] = (
    _Carrier("DHL-EXP", "DHL", "Express 24", "WAW", 1, 2, 12),
    _Carrier("DHL-ECO", "DHL", "Economy Select", "WAW", 2, 4, 15),
    # The one weekend-capable service, so the calendar rule has a case where it
    # does not apply.
    _Carrier("DPD-CLS", "DPD", "Classic", "BER", 2, 3, 15, runs_on_weekends=True),
    _Carrier("GLS-EUR", "GLS", "Euro Business", "BER", 3, 5, 16),
)

#: Public holidays for :data:`CALENDAR_YEAR`, per country, as ``("MM-DD", name)``.
#:
#: Movable feasts are computed from Easter Sunday, which in 2026 is **5 April**:
#: Good Friday 3 April, Easter Monday 6 April, Ascension 14 May, Whit Monday
#: 25 May, Corpus Christi 4 June. Sweden's Midsummer Day is the Saturday between
#: 20 and 26 June, hence 20 June.
#:
#: This is a demo calendar, not legal advice; it covers the countries that appear
#: in this dataset (warehouses in PL/DE, customers in DE/PL/CZ/AT/NL/FR/SE), and
#: it exists because a delivery promise is counted in *working* days.
_HOLIDAYS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "PL": (
        ("01-01", "New Year's Day"),
        ("01-06", "Epiphany"),
        ("04-06", "Easter Monday"),
        ("05-01", "Labour Day"),
        ("05-03", "Constitution Day"),
        ("06-04", "Corpus Christi"),
        ("08-15", "Assumption Day"),
        ("11-01", "All Saints' Day"),
        ("11-11", "Independence Day"),
        ("12-25", "Christmas Day"),
        ("12-26", "Second Day of Christmas"),
    ),
    "DE": (
        ("01-01", "New Year's Day"),
        ("04-03", "Good Friday"),
        ("04-06", "Easter Monday"),
        ("05-01", "Labour Day"),
        ("05-14", "Ascension Day"),
        ("05-25", "Whit Monday"),
        ("10-03", "German Unity Day"),
        ("12-25", "Christmas Day"),
        ("12-26", "Boxing Day"),
    ),
    "CZ": (
        ("01-01", "New Year's Day"),
        ("04-03", "Good Friday"),
        ("04-06", "Easter Monday"),
        ("05-01", "Labour Day"),
        ("05-08", "Victory Day"),
        ("07-05", "Saints Cyril and Methodius Day"),
        ("07-06", "Jan Hus Day"),
        ("09-28", "Czech Statehood Day"),
        ("10-28", "Independent Czechoslovak State Day"),
        ("11-17", "Struggle for Freedom and Democracy Day"),
        ("12-24", "Christmas Eve"),
        ("12-25", "Christmas Day"),
        ("12-26", "St Stephen's Day"),
    ),
    "AT": (
        ("01-01", "New Year's Day"),
        ("01-06", "Epiphany"),
        ("04-06", "Easter Monday"),
        ("05-01", "National Holiday"),
        ("05-14", "Ascension Day"),
        ("05-25", "Whit Monday"),
        ("06-04", "Corpus Christi"),
        ("08-15", "Assumption Day"),
        ("10-26", "National Day"),
        ("11-01", "All Saints' Day"),
        ("12-08", "Immaculate Conception"),
        ("12-25", "Christmas Day"),
        ("12-26", "St Stephen's Day"),
    ),
    "NL": (
        ("01-01", "New Year's Day"),
        ("04-03", "Good Friday"),
        ("04-06", "Easter Monday"),
        ("04-27", "King's Day"),
        ("05-14", "Ascension Day"),
        ("05-25", "Whit Monday"),
        ("12-25", "Christmas Day"),
        ("12-26", "Boxing Day"),
    ),
    "FR": (
        ("01-01", "New Year's Day"),
        ("04-06", "Easter Monday"),
        ("05-01", "Labour Day"),
        ("05-08", "Victory in Europe Day"),
        ("05-14", "Ascension Day"),
        ("05-25", "Whit Monday"),
        ("07-14", "Bastille Day"),
        ("08-15", "Assumption Day"),
        ("11-01", "All Saints' Day"),
        ("11-11", "Armistice Day"),
        ("12-25", "Christmas Day"),
    ),
    "SE": (
        ("01-01", "New Year's Day"),
        ("01-06", "Epiphany"),
        ("04-03", "Good Friday"),
        ("04-06", "Easter Monday"),
        ("05-01", "Labour Day"),
        ("05-14", "Ascension Day"),
        ("06-06", "National Day"),
        ("06-20", "Midsummer Day"),
        ("12-25", "Christmas Day"),
        ("12-26", "Boxing Day"),
    ),
}


def _calendar_date(month_day: str) -> date:
    """Turn a ``"MM-DD"`` calendar key into a date in :data:`CALENDAR_YEAR`."""
    month, day = month_day.split("-")
    return date(CALENDAR_YEAR, int(month), int(day))


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _family_rows() -> tuple[Base, ...]:
    """Build the product-family rows."""
    return tuple(
        ProductFamilyRow(
            family_code=spec.family_code,
            name=spec.name,
            description=spec.description,
            sort_order=spec.sort_order,
        )
        for spec in _FAMILIES
    )


def _product_rows() -> tuple[Base, ...]:
    """Build the product rows."""
    return tuple(
        ProductRow(
            product_id=spec.product_id,
            sku=spec.sku,
            family_code=spec.family_code,
            name=spec.name,
            description=spec.description,
            uom=spec.uom,
            active=spec.active,
        )
        for spec in _PRODUCTS
    )


def _product_alias_rows() -> tuple[Base, ...]:
    """Build the product-alias rows, normalising each alias on the way in."""
    return tuple(
        ProductAliasRow(
            product_id=spec.product_id,
            normalized=normalize_alias(spec.alias),
            alias=spec.alias,
            kind=spec.kind,
        )
        for spec in _PRODUCT_ALIASES
    )


def _customer_rows() -> tuple[Base, ...]:
    """Build the customer rows (all quoted in :data:`CURRENCY`)."""
    return tuple(
        CustomerRow(
            customer_id=spec.customer_id,
            legal_name=spec.legal_name,
            display_name=spec.display_name,
            country_code=spec.country_code,
            default_currency=CURRENCY,
            payment_terms_days=spec.payment_terms_days,
            credit_limit=Decimal(spec.credit_limit),
            credit_hold=spec.credit_hold,
            active=spec.active,
            notes=spec.notes,
        )
        for spec in _CUSTOMERS
    )


def _customer_alias_rows() -> tuple[Base, ...]:
    """Build the customer-alias rows, normalising each alias on the way in."""
    return tuple(
        CustomerAliasRow(
            customer_id=spec.customer_id,
            normalized=normalize_alias(spec.alias),
            alias=spec.alias,
            kind=spec.kind,
        )
        for spec in _CUSTOMER_ALIASES
    )


def _price_book_rows() -> tuple[Base, ...]:
    """Build the price-book rows."""
    return tuple(
        PriceBookRow(
            price_book_code=spec.price_book_code,
            name=spec.name,
            currency=CURRENCY,
            customer_tier=spec.customer_tier,
            effective_from=spec.effective_from,
            effective_to=spec.effective_to,
            active=True,
        )
        for spec in _PRICE_BOOKS
    )


def _price_entry_rows() -> tuple[Base, ...]:
    """Build the price-entry rows.

    The book a contract price belongs to is decided by whether the entry names a
    customer: list prices carry the :data:`TIER_STANDARD` tier, contract prices
    carry a ``customer_id`` and no tier.
    """
    return tuple(
        PriceEntryRow(
            price_entry_id=spec.price_entry_id,
            price_book_code=PRICE_BOOK_LIST if spec.customer_id is None else PRICE_BOOK_CONTRACT,
            product_id=spec.product_id,
            customer_id=spec.customer_id,
            customer_tier=None if spec.customer_id is not None else TIER_STANDARD,
            min_qty=spec.min_qty,
            unit_price=Decimal(spec.unit_price),
            currency=CURRENCY,
            effective_from=spec.effective_from,
            effective_to=spec.effective_to,
        )
        for spec in _PRICES
    )


def _discount_rows() -> tuple[Base, ...]:
    """Build the discount-rule rows."""
    return tuple(
        DiscountRuleRow(
            rule_id=spec.rule_id,
            scope=spec.scope,
            scope_ref=spec.scope_ref,
            percent=Decimal(spec.percent),
            min_qty=spec.min_qty,
            min_order_value=None if spec.min_order_value is None else Decimal(spec.min_order_value),
            requires_approval=spec.requires_approval,
            priority=spec.priority,
            active=spec.active,
            effective_from=spec.effective_from,
            effective_to=spec.effective_to,
        )
        for spec in _DISCOUNTS
    )


def _warehouse_rows() -> tuple[Base, ...]:
    """Build the warehouse rows."""
    return tuple(
        WarehouseRow(
            location_code=spec.location_code,
            name=spec.name,
            city=spec.city,
            country_code=spec.country_code,
            active=True,
        )
        for spec in _WAREHOUSES
    )


def _stock_rows() -> tuple[Base, ...]:
    """Build the stock rows, all describing :data:`STOCK_AS_OF`."""
    return tuple(
        StockLevelRow(
            location_code=spec.location_code,
            product_id=spec.product_id,
            on_hand_qty=spec.on_hand,
            reserved_qty=spec.reserved,
            inbound_qty=spec.inbound,
            inbound_eta=spec.inbound_eta,
            as_of=STOCK_AS_OF,
        )
        for spec in _STOCK
    )


def _carrier_rows() -> tuple[Base, ...]:
    """Build the carrier-service rows."""
    return tuple(
        CarrierServiceRow(
            service_code=spec.service_code,
            carrier=spec.carrier,
            name=spec.name,
            origin_location=spec.origin_location,
            transit_days_min=spec.transit_days_min,
            transit_days_max=spec.transit_days_max,
            cutoff_hour_utc=spec.cutoff_hour_utc,
            runs_on_weekends=spec.runs_on_weekends,
            active=True,
        )
        for spec in _CARRIERS
    )


def _holiday_rows() -> tuple[Base, ...]:
    """Build the holiday rows for every country in the calendar."""
    return tuple(
        HolidayRow(
            country_code=country_code,
            holiday_date=_calendar_date(month_day),
            name=name,
        )
        for country_code, holidays in _HOLIDAYS.items()
        for month_day, name in holidays
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def levels() -> tuple[tuple[Base, ...], ...]:
    """Return the dataset as fresh rows, grouped into dependency levels.

    Every call builds new row objects: a row instance belongs to one session and
    one flush, so handing out shared instances would make a second seeding run
    depend on what the first one left behind.

    The grouping is what makes the insert order safe. SQLAlchemy flushes pending
    inserts in mapper-registration order, not foreign-key order, so parents and
    children must not be pending in the same flush; within a level the tables
    have no foreign keys between them.

    Returns:
        Three levels: master data, then catalogue and customer aliases, then
        everything that points at them. Reversed, the same sequence is a valid
        deletion order.
    """
    return (
        (*_family_rows(), *_customer_rows(), *_warehouse_rows(), *_price_book_rows()),
        (*_product_rows(), *_customer_alias_rows()),
        (
            *_product_alias_rows(),
            *_price_entry_rows(),
            *_discount_rows(),
            *_stock_rows(),
            *_carrier_rows(),
            *_holiday_rows(),
        ),
    )


def row_counts() -> dict[str, int]:
    """Return how many rows the dataset holds per table."""
    counts: Counter[str] = Counter(type(row).__tablename__ for level in levels() for row in level)
    return dict(sorted(counts.items()))


def table_names() -> tuple[str, ...]:
    """Return the names of every table the dataset writes, sorted."""
    return tuple(row_counts())


def total_rows() -> int:
    """Return the total number of rows in the dataset."""
    return sum(row_counts().values())
