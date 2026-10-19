"""The demonstration fixtures: versioned literals, never generated (Phase 1M-C).

A scenario states everything the deterministic pipeline needs outright: the
untrusted intake, the *extraction claims* an LLM would have produced from it
(trust tier T3 - the resolution core re-derives everything, exactly like
model output), the identifiers and instants only a caller may hold, and the
outcome the run must produce. Following :mod:`rfq_agent.seed.dataset`'s
discipline there is no clock and no randomness, so a scenario plus
:data:`rfq_agent.seed.SEED_VERSION` is a reproducible artefact.

Two fields carry self-contained guarantees: ``human_binding`` is the one
caller-state a fixture may make beyond the claims - the record that an
operator bound a sender to a customer, which the 1M-A adapter's contract
already accepts - and ``expected`` is the demonstration's self-check: a run
that drifts from the accepted behaviour must fail, not narrate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.demo.errors import UnknownScenarioError
from rfq_agent.domain.extraction import ExtractedLine, RequestedDelivery
from rfq_agent.domain.ids import CustomerId, LineItemId, QuoteId, QuoteNumber, RfqId, RunId
from rfq_agent.domain.values import DomainModel
from rfq_agent.seed import STOCK_AS_OF

__all__ = [
    "SCENARIOS",
    "DemoOutcome",
    "DemoScenario",
    "scenario_by_id",
]

#: The date every scenario is priced for: inside every seeded validity window.
PRICING_AS_OF = date(2026, 10, 6)

#: When every demonstration RFQ "arrived" - after :data:`STOCK_AS_OF`, before
#: the seeded carrier cut-offs, fixed so a re-run reproduces identically.
RECEIVED_AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


class DemoOutcome(StrEnum):
    """What a scenario run reports.

    The first three are the outcomes a fixture may *declare*. ``DUPLICATE`` is
    never declared: it is what a persisted outcome becomes on an identical
    re-run, when the write path finds its own work already stored.
    """

    #: Resolution or the adapter refused: no quote was prepared or stored.
    HUMAN_REQUIRED_BEFORE_QUOTE = "HUMAN_REQUIRED_BEFORE_QUOTE"
    #: A clean calculation persisted as a DRAFT; human approval still required.
    DRAFT_AWAITING_HUMAN_REVIEW = "DRAFT_AWAITING_HUMAN_REVIEW"
    #: A calculation with blocking evidence persisted as a DRAFT; not eligible.
    DRAFT_BLOCKED = "DRAFT_BLOCKED"
    #: An identical write already existed; the retry stored nothing new.
    DUPLICATE = "DUPLICATE"


class DemoScenario(DomainModel):
    """One fixture scenario: intake, claims, identifiers and outcome stated outright."""

    scenario_id: Annotated[str, StringConstraints(pattern=r"^SC-[0-9]{2}$")]
    #: One sentence shown as the scenario's heading.
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    #: The declared outcome; ``DUPLICATE`` is a re-run state, never a fixture.
    expected: DemoOutcome

    # --- intake (stored verbatim by the provisioning shim) -----------------
    sender_email: Annotated[
        str,
        StringConstraints(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ]
    sender_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    subject: Annotated[str, StringConstraints(max_length=500)] = ""
    #: The untrusted request text, stored verbatim and never parsed here.
    body: Annotated[str, StringConstraints(min_length=1)]
    received_at: datetime = RECEIVED_AT

    # --- extraction claims (trust tier T3; the core re-derives everything) --
    lines: Annotated[tuple[ExtractedLine, ...], Field(min_length=1)]
    requested_delivery: RequestedDelivery | None = None

    # --- caller-stated identifiers and instants ----------------------------
    rfq_id: RfqId
    run_id: RunId
    quote_id: QuoteId
    quote_number: QuoteNumber
    line_item_ids: Annotated[tuple[LineItemId, ...], Field(min_length=1)]
    pricing_as_of: date = PRICING_AS_OF
    stock_as_of: datetime = STOCK_AS_OF
    delivery_as_of: datetime | None = None

    # --- caller-stated human decision --------------------------------------
    #: The customer an operator bound the sender to, when the data proves no
    #: identity. ``None`` means no human decision was made.
    human_binding: CustomerId | None = None

    @model_validator(mode="after")
    def _check_the_fixture_is_sane(self) -> Self:
        """Refuse fixtures that could silently lie about what they exercise."""
        if self.expected is DemoOutcome.DUPLICATE:
            msg = "expected must be a first-run outcome: DUPLICATE only ever reports a retry"
            raise ValueError(msg)
        if len(self.line_item_ids) != len(self.lines):
            msg = (
                "line_item_ids must pair one-to-one with the claimed lines: "
                f"{len(self.line_item_ids)} identifiers for {len(self.lines)} lines"
            )
            raise ValueError(msg)
        if len(set(self.line_item_ids)) != len(self.line_item_ids):
            msg = "line_item_ids must be unique: identity is stated, never generated"
            raise ValueError(msg)
        for name, instant in (
            ("received_at", self.received_at),
            ("stock_as_of", self.stock_as_of),
            ("delivery_as_of", self.delivery_as_of),
        ):
            if instant is not None and instant.utcoffset() is None:
                msg = f"{name} must be timezone-aware: cut-offs and stock ages are UTC"
                raise ValueError(msg)
        if (
            self.requested_delivery is not None
            and self.requested_delivery.destination is not None
            and self.delivery_as_of is None
        ):
            msg = "a stated delivery destination needs the instant it is asked at"
            raise ValueError(msg)
        if (
            self.human_binding is not None
            and self.expected is DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE
        ):
            msg = (
                "a fixture-stated human binding exists to let a scenario reach quoting: "
                "an expected HUMAN_REQUIRED_BEFORE_QUOTE may not carry one"
            )
            raise ValueError(msg)
        return self


def scenario_by_id(scenario_id: str) -> DemoScenario:
    """Return the named scenario; unknown ids fail with the declared list."""
    for scenario in SCENARIOS:
        if scenario.scenario_id == scenario_id:
            return scenario
    known = ", ".join(scenario.scenario_id for scenario in SCENARIOS)
    msg = f"unknown scenario {scenario_id!r}: the declared scenarios are {known}"
    raise UnknownScenarioError(msg)


# ---------------------------------------------------------------------------
# The three approved scenarios
# ---------------------------------------------------------------------------

SCENARIOS: tuple[DemoScenario, ...] = (
    # SC-01 - the happy path: a stored e-mail alias proves Nordwind (CUS_0001),
    # one catalogue line resolves, 40 pumps price at the contract rate. No
    # delivery facts were stated, so none are computed or invented.
    DemoScenario(
        scenario_id="SC-01",
        title="Nordwind orders 40 pumps: identity proven, line resolved, draft persists",
        expected=DemoOutcome.DRAFT_AWAITING_HUMAN_REVIEW,
        sender_email="einkauf@nordwind-industrie.de",
        sender_name="Nordwind Einkauf",
        subject="RFQ: 40 pcs 100-ABC",
        body=(
            "Hello,\n"
            "please quote 40 pcs of 100-ABC for our standard replenishment.\n"
            "No specific delivery date is required.\n"
            "Best regards,\n"
            "Nordwind Einkauf"
        ),
        lines=(
            ExtractedLine(
                ordinal=1,
                raw_text="please quote 40 pcs of 100-ABC for our standard replenishment.",
                requested_sku="100-ABC",
                description="Centrifugal pump PMP-A-100",
                quantity=40,
                evidence="40 pcs of 100-ABC",
            ),
        ),
        rfq_id="RFQ_DEMO_0001",
        run_id="RUN_DEMO_0001",
        quote_id="QTE_DEMO_0001",
        quote_number="Q-2026-8001",
        line_item_ids=("LI_DEMO_0001",),
    ),
    # SC-02 - the seeded collision: "PMP-A-100" is one product's SKU and the
    # stainless variant's stored name, so resolution returns AMBIGUOUS and the
    # adapter refuses the line: a human must choose before any quote exists.
    DemoScenario(
        scenario_id="SC-02",
        title="The PMP-A-100 collision: an ambiguous line stops before any quote",
        expected=DemoOutcome.HUMAN_REQUIRED_BEFORE_QUOTE,
        sender_email="einkauf@nordwind-industrie.de",
        sender_name="Nordwind Einkauf",
        subject="RFQ: PMP-A-100 pumps",
        body=(
            "Hello,\n"
            "we need 10 pcs of PMP-A-100 for the Gdansk site.\n"
            "Please send pricing.\n"
            "Nordwind Einkauf"
        ),
        lines=(
            ExtractedLine(
                ordinal=1,
                raw_text="we need 10 pcs of PMP-A-100 for the Gdansk site.",
                requested_sku="PMP-A-100",
                description="Cast-iron centrifugal pump, 100 mm flange",
                quantity=10,
                evidence="10 pcs of PMP-A-100",
            ),
        ),
        rfq_id="RFQ_DEMO_0002",
        run_id="RUN_DEMO_0002",
        quote_id="QTE_DEMO_0002",
        quote_number="Q-2026-8002",
        line_item_ids=("LI_DEMO_0002",),
    ),
    # SC-03 - the sender's domain points at Nordic Kraft (CUS_0007) but no
    # full-address identity proves them, so the fixture states the operator's
    # binding. The line prices at the public list and the account's credit
    # hold becomes stored blocking evidence.
    DemoScenario(
        scenario_id="SC-03",
        title="Nordic Kraft on credit hold: the blocked draft persists as evidence",
        expected=DemoOutcome.DRAFT_BLOCKED,
        sender_email="inkop@nordickraft.se",
        sender_name="Nordic Kraft Inköp",
        subject="RFQ: 5 pcs PMP-B-150",
        body=("Hej,\nplease quote 5 pcs of PMP-B-150.\nRegards,\nNordic Kraft, inköp"),
        lines=(
            ExtractedLine(
                ordinal=1,
                raw_text="please quote 5 pcs of PMP-B-150.",
                requested_sku="PMP-B-150",
                description="Centrifugal pump PMP-B-150",
                quantity=5,
                evidence="5 pcs of PMP-B-150",
            ),
        ),
        rfq_id="RFQ_DEMO_0003",
        run_id="RUN_DEMO_0003",
        quote_id="QTE_DEMO_0003",
        quote_number="Q-2026-8003",
        line_item_ids=("LI_DEMO_0003",),
        human_binding="CUS_0007",
    ),
)
