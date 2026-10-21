"""Fixtures for the acceptance tests.

The database fixtures are imported from the persistence package rather than
redefined: "a database built by the real migrations" is one idea in this
suite, and so is "seeded with the Northwind dataset".

Declarations are built programmatically here, not read from ``docs/``: a test
that changes one fact must be able to see exactly which fact it changed. The
worked examples under ``docs/acceptance/`` are exercised separately, by the
command tests, so a broken example fails the suite rather than rotting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rfq_agent.acceptance import FreshRfq
from rfq_agent.domain.extraction import ExtractedLine, ExtractionResult, RequestedDelivery
from rfq_agent.seed import reset_and_seed
from tests.persistence.conftest import (  # noqa: F401  (imported as fixtures)
    database_path,
    db,
    migrated_template,
    session,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from rfq_agent.persistence import Database

#: Repository root - the worked examples live under ``docs/acceptance``.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "docs" / "acceptance"

#: The sender whose address is a stored EMAIL alias of ``CUS_0001``.
SEEDED_SENDER = "einkauf@nordwind-industrie.de"
SEEDED_SENDER_NAME = "Nordwind Einkauf"


@pytest.fixture
def seeded(request: pytest.FixtureRequest) -> Database:
    """The migrated test database with the business dataset written to it.

    The database and its session are requested by name rather than bound as
    parameters: this module re-exports the persistence fixtures, and a parameter
    of the same name would shadow them before pytest could resolve it.
    """
    database: Database = request.getfixturevalue("db")
    active: Session = request.getfixturevalue("session")
    reset_and_seed(active)
    active.commit()
    return database


@pytest.fixture
def seeded_url(request: pytest.FixtureRequest) -> str:
    """The seeded database's URL, for tests that open their own connections.

    Requested by name for the same reason :func:`seeded` is: this module
    re-exports the persistence fixtures, so a parameter of the same name would
    shadow them before pytest could resolve it.
    """
    request.getfixturevalue("seeded")
    path: Path = request.getfixturevalue("database_path")
    return f"sqlite:///{path}"


def make_declaration(
    *,
    number: int = 1,
    quantity: int = 25,
    claim_quantity: int | None = None,
    claim_evidence: str | None = None,
    sender_email: str = SEEDED_SENDER,
    sender_name: str | None = SEEDED_SENDER_NAME,
    body: str | None = None,
    subject: str | None = None,
    idempotency_key: str | None = None,
) -> FreshRfq:
    """Build one declaration whose claims may deliberately disagree with its body.

    ``quantity`` is what the customer wrote; ``claim_quantity`` is what the
    extraction claims (defaults to the same). Passing a different value produces
    exactly the tampered-claim case: the source says one thing and the claim
    says another.
    """
    claimed = quantity if claim_quantity is None else claim_quantity
    statement = f"please quote {quantity} pcs of 100-ABC for our standard replenishment."
    source_body = (
        body
        if body is not None
        else (
            "Hello,\n"
            f"{statement}\n"
            "No specific delivery date is required.\n"
            "Best regards,\n"
            f"{sender_name}"
        )
    )
    return FreshRfq.model_validate(
        {
            "intake": {
                "rfq_id": f"RFQ_TEST_000{number}",
                "run_id": f"RUN_TEST_000{number}",
                "idempotency_key": idempotency_key or f"acceptance:test:{number:04d}",
                "source_channel": "EMAIL",
                "received_at": "2026-10-06T09:00:00Z",
                "subject": subject if subject is not None else f"RFQ: {quantity} pcs 100-ABC",
                "body": source_body,
                "sender_name": sender_name,
                "sender_email": sender_email,
            },
            "quote_id": f"QTE_TEST_000{number}",
            "quote_number": f"Q-2026-900{number}",
            "line_item_ids": [f"LI_TEST_000{number}"],
            "pricing_as_of": "2026-10-06",
            "stock_as_of": "2026-10-01T06:00:00Z",
            "extraction": ExtractionResult(
                lines=(
                    ExtractedLine(
                        ordinal=1,
                        raw_text=statement,
                        requested_sku="100-ABC",
                        description="Centrifugal pump PMP-A-100",
                        quantity=claimed,
                        uom="pcs",
                        evidence=claim_evidence or f"{claimed} pcs of 100-ABC",
                        confidence="1",
                    ),
                ),
                requested_delivery=RequestedDelivery(),
            ).model_dump(mode="json"),
        }
    )


def load_example(name: str) -> FreshRfq:
    """Load one worked example from ``docs/acceptance`` as a declaration."""
    return FreshRfq.model_validate(json.loads((EXAMPLES / name).read_text(encoding="utf-8")))
