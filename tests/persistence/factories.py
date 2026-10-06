"""Row factories for persistence tests.

Defaults are chosen so that a test only has to state what it is actually about:
every factory returns a row that satisfies every constraint unless the caller
overrides exactly the field under test.

One trap worth knowing: with no ORM relationships between the models, SQLAlchemy
flushes pending inserts in *mapper registration order*, not in foreign-key order.
Anything that inserts across dependency levels must flush between them - either
with explicit ``session.flush()`` calls (as :func:`seed_core` does) or by
committing each level. ``tests/persistence/test_sqlite_config.py`` pins this
behaviour so it stays visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from rfq_agent.domain.delivery import DeliveryFeasibility
from rfq_agent.domain.human import HumanActionKind
from rfq_agent.domain.intake import RfqStatus, SourceChannel
from rfq_agent.domain.outbound import OutboundChannel, OutboundStatus
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import QuoteStatus
from rfq_agent.domain.stock import StockStatus
from rfq_agent.domain.workflow import (
    RunActor,
    RunOutcome,
    RunState,
    RunTrigger,
    TransitionEvent,
)
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.models import (
    CarrierServiceRow,
    CustomerRow,
    HumanActionRow,
    OutboundMessageRow,
    PriceBookRow,
    PriceEntryRow,
    ProductFamilyRow,
    ProductRow,
    QuoteLineRow,
    QuoteRow,
    RfqRow,
    RunEventRow,
    RunRow,
    StockLevelRow,
    WarehouseRow,
)
from rfq_agent.seed import TIER_STANDARD

#: A fixed instant, so failures are reproducible rather than "at 3pm it passed".
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)

#: A syntactically valid SHA-256 digest used wherever a hash is required.
SHA256 = "a" * 64


@dataclass(frozen=True, slots=True)
class Core:
    """Identifiers of the seeded master-data graph."""

    customer_id: str = "CUS_0001"
    family_code: str = "FAM_PUMPS"
    product_id: str = "PRD_0001"
    sku: str = "PMP-A-100"
    price_book_code: str = "BK-EU-2026"
    price_entry_id: str = "PE_0001"
    location_code: str = "WAW"
    service_code: str = "DHL-EXP"
    rfq_id: str = "RFQ_0001"
    run_id: str = "RUN_0001"
    quote_id: str = "QTE_0001"
    quote_number: str = "Q-2026-0001"
    line_id: str = "QLI_0001"
    action_id: str = "HAC_0001"


def core_rows(ids: Core | None = None) -> list[list[Base]]:
    """The master-data rows, grouped by dependency level.

    Split out from :func:`seed_core` so that a test which needs to inspect the
    rows *before* they are committed - a round-trip test comparing what was
    written against what comes back - can add them itself. Returning levels
    rather than one flat list keeps the foreign-key ordering visible: SQLAlchemy
    flushes pending inserts in mapper-registration order, not foreign-key order,
    so ``products`` must not be pending in the same flush as ``product_families``.
    """
    ids = ids or Core()
    return [
        [
            CustomerRow(
                customer_id=ids.customer_id,
                legal_name="Nordwind Industrie GmbH",
                display_name="Nordwind",
                country_code="DE",
                default_currency="EUR",
                payment_terms_days=30,
                credit_limit=Decimal("50000.00"),
                credit_hold=False,
                active=True,
            ),
            ProductFamilyRow(
                family_code=ids.family_code,
                name="Centrifugal pumps",
                description="Industrial centrifugal pumps",
                sort_order=1,
            ),
            ProductRow(
                product_id=ids.product_id,
                sku=ids.sku,
                family_code=ids.family_code,
                name="Centrifugal pump PMP-A-100",
                description="Cast-iron centrifugal pump, 100 mm flange",
                uom="EA",
                active=True,
            ),
            PriceBookRow(
                price_book_code=ids.price_book_code,
                name="EU list 2026",
                currency="EUR",
                # The public list tier, as in the demo dataset: an entry scoped to
                # neither a customer nor a tier is a price nobody can defend, and
                # rfq_agent.domain.pricing refuses to load one.
                customer_tier=TIER_STANDARD,
                effective_from=date(2026, 1, 1),
                effective_to=None,
                active=True,
            ),
            WarehouseRow(
                location_code=ids.location_code,
                name="Warsaw DC",
                city="Warsaw",
                country_code="PL",
                active=True,
            ),
        ],
        [
            PriceEntryRow(
                price_entry_id=ids.price_entry_id,
                price_book_code=ids.price_book_code,
                product_id=ids.product_id,
                customer_id=None,
                customer_tier=TIER_STANDARD,
                min_qty=1,
                unit_price=Decimal("1234.5600"),
                currency="EUR",
                effective_from=date(2026, 1, 1),
                effective_to=None,
            ),
            StockLevelRow(
                location_code=ids.location_code,
                product_id=ids.product_id,
                on_hand_qty=120,
                reserved_qty=20,
                inbound_qty=0,
                inbound_eta=None,
                as_of=NOW,
            ),
            CarrierServiceRow(
                service_code=ids.service_code,
                carrier="DHL",
                name="Express 24",
                origin_location=ids.location_code,
                transit_days_min=1,
                transit_days_max=2,
                cutoff_hour_utc=12,
                runs_on_weekends=False,
                active=True,
            ),
        ],
    ]


def operational_rows(ids: Core | None = None) -> list[Base]:
    """The RFQ and its first run - the parents of everything operational.

    The run is created here rather than left to each test because almost every
    row worth inserting (events, quotations, approvals, outbound messages)
    hangs off it, and a test that had to build its own would be testing
    ``seed_core`` instead of the thing it is about.
    """
    ids = ids or Core()
    return [rfq_row(ids.rfq_id), run_row(ids.run_id, ids.rfq_id)]


def seed_core(session: Session, core: Core | None = None) -> Core:
    """Insert the reference graph: master data, one RFQ and one run.

    This is a *scaffold*, not the demo dataset (:mod:`rfq_agent.seed`): it inserts
    unconditionally and holds only the minimum a constraint or round-trip test
    needs. It shares identifiers with the dataset's first rows (``CUS_0001``,
    ``PRD_0001``, ``PE_0001``, ``WAW``) so that both describe the same business,
    and a test that needs the demo data should seed the dataset instead - in one
    database, use one of the two, not both.

    Returns the identifiers so a test can build quotations against known-good
    customers, products and prices without repeating ten inserts. Inserts are
    flushed level by level - see the note in :func:`core_rows`.
    """
    ids = core or Core()
    for level in [*core_rows(ids), operational_rows(ids)]:
        session.add_all(level)
        session.flush()
    session.commit()
    return ids


def rfq_row(rfq_id: str = "RFQ_0001", **overrides: object) -> RfqRow:
    """An inbound RFQ that satisfies every constraint."""
    values: dict[str, object] = {
        "rfq_id": rfq_id,
        "source_channel": SourceChannel.EMAIL,
        "status": RfqStatus.OPEN,
        "received_at": NOW,
        "subject": "RFQ: 40 centrifugal pumps",
        "body_text": "Please quote 40 pcs PMP-A-100, delivery to Warsaw.",
        "body_sha256": SHA256,
        "sender_name": "Anna Kowalska",
        "sender_email": "anna.kowalska@nordwind.example",
        "sender_email_hash": SHA256,
        "idempotency_key": f"intake:{rfq_id}",
        "thread_key": None,
        "superseded_by_rfq_id": None,
    }
    values.update(overrides)
    return RfqRow(**values)  # type: ignore[arg-type]


def run_row(run_id: str = "RUN_0001", rfq_id: str = "RFQ_0001", **overrides: object) -> RunRow:
    """A run in a non-terminal state (the state most tests want)."""
    values: dict[str, object] = {
        "run_id": run_id,
        "rfq_id": rfq_id,
        "attempt_no": 1,
        "trigger": RunTrigger.INITIAL,
        "state": RunState.RECEIVED,
        "prior_run_id": None,
        "outcome": None,
        "failure_code": None,
        "retry_count": 0,
        "checkpoint_stage": None,
        "row_version": 0,
        "started_at": NOW,
        "finished_at": None,
    }
    values.update(overrides)
    return RunRow(**values)  # type: ignore[arg-type]


def terminal_run_row(
    run_id: str = "RUN_0001",
    rfq_id: str = "RFQ_0001",
    state: RunState = RunState.SENT,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    **overrides: object,
) -> RunRow:
    """A run in a terminal state, with the outcome the state machine demands."""
    return run_row(
        run_id,
        rfq_id,
        state=state,
        outcome=outcome,
        finished_at=NOW,
        **overrides,
    )


def run_event_row(
    run_id: str = "RUN_0001",
    seq: int = 1,
    **overrides: object,
) -> RunEventRow:
    """An audit row for a legal edge (``RECEIVED --triage_start--> TRIAGING``)."""
    values: dict[str, object] = {
        "run_id": run_id,
        "seq": seq,
        "occurred_at": NOW,
        "from_state": RunState.RECEIVED,
        "to_state": RunState.TRIAGING,
        "event": TransitionEvent.TRIAGE_START,
        "actor": RunActor.SYSTEM,
        "reason_code": None,
        "detail_json": None,
        "detail_sha256": None,
    }
    values.update(overrides)
    return RunEventRow(**values)  # type: ignore[arg-type]


def quote_row(core: Core, **overrides: object) -> QuoteRow:
    """A clean, sendable quotation for the seeded core graph."""
    values: dict[str, object] = {
        "quote_id": core.quote_id,
        "quote_number": core.quote_number,
        "run_id": core.run_id,
        "rfq_id": core.rfq_id,
        "customer_id": core.customer_id,
        "revision": 1,
        "status": QuoteStatus.READY,
        "currency": "EUR",
        "subtotal": Decimal("49382.40"),
        "discount_rule_id": None,
        "discount_scope": None,
        "discount_percent": None,
        "discount_amount": Decimal("0.00"),
        "total": Decimal("49382.40"),
        "pricing_as_of": TODAY,
        "calc_version": "calc-v1",
        "inputs_sha256": SHA256,
        "policy_allowed": True,
        "policy_reason_codes_json": None,
        "delivery_feasibility": DeliveryFeasibility.FEASIBLE,
        "delivery_destination": "Warsaw, PL",
        "origin_location": core.location_code,
        "carrier_service_code": core.service_code,
        "transit_days_min": 1,
        "transit_days_max": 2,
        "earliest_ship_date": TODAY,
        "earliest_delivery_date": date(2026, 10, 7),
        "requested_date": None,
        "split_shipment_proposed": False,
        "approved_at": None,
    }
    values.update(overrides)
    return QuoteRow(**values)  # type: ignore[arg-type]


def quote_line_row(core: Core, **overrides: object) -> QuoteLineRow:
    """One priced, unblocked line."""
    values: dict[str, object] = {
        "line_id": core.line_id,
        "quote_id": core.quote_id,
        "ordinal": 1,
        "product_id": core.product_id,
        "sku": core.sku,
        "description": "Centrifugal pump PMP-A-100",
        "quantity": 40,
        "unit_price": Decimal("1234.5600"),
        "price_entry_id": core.price_entry_id,
        "line_extension": Decimal("49382.40"),
        "currency": "EUR",
        "stock_status": StockStatus.SUFFICIENT,
        "price_status": PriceLookupStatus.FOUND,
        "blocked": False,
        "blocked_reason": None,
        "notes": None,
    }
    values.update(overrides)
    return QuoteLineRow(**values)  # type: ignore[arg-type]


def human_action_row(run_id: str = "RUN_0001", rfq_id: str = "RFQ_0001", **overrides: object):
    """An APPROVE action - the action that unlocks outbound."""
    values: dict[str, object] = {
        "action_id": "HAC_0001",
        "run_id": run_id,
        "rfq_id": rfq_id,
        "actor": "operator@example",
        "action": HumanActionKind.APPROVE,
        "occurred_at": NOW,
        "idempotency_key": "action:approve:0001",
        "before_json": None,
        "after_json": None,
        "reason_code": None,
        "note": None,
        "row_version": 0,
    }
    values.update(overrides)
    return HumanActionRow(**values)  # type: ignore[arg-type]


def outbound_row(core: Core, **overrides: object) -> OutboundMessageRow:
    """A simulated send, unlocked by ``core.action_id``."""
    values: dict[str, object] = {
        "message_id": "MSG_0001",
        "run_id": core.run_id,
        "quote_id": core.quote_id,
        "customer_id": core.customer_id,
        "approval_action_id": core.action_id,
        "template_id": "quote_response",
        "template_version": "v1",
        "slots_json": {"greeting": "Hello"},
        "channel": OutboundChannel.SIMULATED,
        "status": OutboundStatus.SENT,
        "rendered_text": "Dear Nordwind, please find our quotation Q-2026-0001.",
        "rendered_sha256": SHA256,
        "canary_passed": True,
        "canary_findings_json": None,
        "sent_at": NOW,
        "delivery_note": "simulated delivery",
    }
    values.update(overrides)
    return OutboundMessageRow(**values)  # type: ignore[arg-type]


__all__ = [
    "NOW",
    "SHA256",
    "TODAY",
    "Core",
    "human_action_row",
    "outbound_row",
    "quote_line_row",
    "quote_row",
    "rfq_row",
    "run_event_row",
    "run_row",
    "seed_core",
    "terminal_run_row",
]
