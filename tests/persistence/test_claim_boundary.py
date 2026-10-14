"""Real seeded composition: failed claim verification never reaches quote writes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from rfq_agent.demo import runner
from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import SCENARIOS, DemoOutcome, DemoScenario
from rfq_agent.demo.intake import provision_run
from rfq_agent.demo.runner import run_scenario
from rfq_agent.domain.extraction import ExtractedLine
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.models import ProductRow
from rfq_agent.seed import reset_and_seed


@pytest.fixture
def seeded(db):
    with db.session() as session:
        reset_and_seed(session)
    return db


def snapshot(db):
    """Compare all operational and business rows, not only the quote count."""
    with db.session_factory() as session:
        return {
            table.name: tuple(sorted(session.execute(select(table)).all(), key=repr))
            for table in Base.metadata.sorted_tables
        }


def replace(scenario, **updates):
    return DemoScenario.model_validate(scenario.model_dump() | updates)


def changed_scenario(mutation):
    scenario = SCENARIOS[2] if mutation == "human-quantity" else SCENARIOS[0]
    claim_updates = {
        "quantity": {"quantity": 9},
        "human-quantity": {"quantity": 9},
        "claim-unit": {"uom": "cartons"},
        "fabricated-raw": {"raw_text": "Please quote 9 pcs of 100-ABC"},
        "fabricated-evidence": {"evidence": "9 pcs of 100-ABC"},
        "narrow-evidence": {"evidence": "100-ABC"},
    }
    body = scenario.body
    source_updates = {
        "source-unit": {"body": body.replace("40 pcs", "40 boxes")},
        "missing-source-unit": {"body": body.replace("40 pcs", "40")},
        "subject": {"subject": "RFQ: 9 pcs 100-ABC"},
        "specification": {"body": body + "\nATEX certified parts required."},
        "hidden-delivery": {"body": body + "\nDeliver to Hamburg on 2026-10-12."},
        "correction": {"body": body + "\nCorrection: only 9 pcs please."},
        "additional-source-line": {"body": body + "\nPlease quote 5 each of PMP-B-150."},
        "missing-source-line": {"body": "Hello,\nPlease send pricing."},
        "duplicate-source-line": {
            "body": body.replace(
                "No specific delivery date is required.",
                scenario.lines[0].raw_text,
            )
        },
    }
    values = scenario.lines[0].model_dump() | claim_updates.get(mutation, {})
    return replace(scenario, lines=(ExtractedLine(**values),), **source_updates.get(mutation, {}))


@pytest.mark.parametrize(
    "mutation",
    [
        "quantity",
        "claim-unit",
        "fabricated-raw",
        "fabricated-evidence",
        "narrow-evidence",
        "source-unit",
        "missing-source-unit",
        "subject",
        "specification",
        "hidden-delivery",
        "correction",
        "additional-source-line",
        "missing-source-line",
        "duplicate-source-line",
        "catalog-unit",
        "human-quantity",
    ],
)
def test_failed_verification_changes_no_rows_and_never_calls_engine_or_writer(seeded, mutation):
    scenario = changed_scenario(mutation)
    with seeded.session() as session:
        if mutation == "catalog-unit":
            session.get(ProductRow, "PRD_0001").uom = "BOX"
        authoritative = provision_run(session, scenario)
        assert authoritative.body.text == scenario.body
        assert authoritative.subject == scenario.subject
    before = snapshot(seeded)
    with (
        patch.object(runner, "run_quote", wraps=runner.run_quote) as engine,
        patch.object(runner.QuoteWriter, "persist", autospec=True) as persist,
        pytest.raises(DemoError, match=r"SOURCE_|EXTRACTION_|QUANTITY_|UNIT_|EVIDENCE_"),
    ):
        run_scenario(scenario, seeded)
    engine.assert_not_called()
    persist.assert_not_called()
    # Includes quotes, quote_lines, blocked ledger, write claims, approvals,
    # outbound messages, RFQ/run lifecycle state and transition/event rows.
    assert snapshot(seeded) == before


def test_only_catalog_descriptions_persist_and_identical_retry_is_free(seeded):
    original = SCENARIOS[0]
    forged = original.lines[0].model_copy(
        update={
            "description": "ATEX certified titanium pump, warranty 10 years, 50 bar",
        }
    )
    scenario = replace(original, lines=(forged,))
    report = run_scenario(scenario, seeded)
    assert report.outcome is DemoOutcome.DRAFT_AWAITING_HUMAN_REVIEW
    assert report.write is not None
    assert not report.write.duplicate
    with seeded.session_factory() as session:
        description, quantity = session.execute(
            text("SELECT description, quantity FROM quote_lines WHERE quote_id = 'QTE_DEMO_0001'")
        ).one()
        assert description == session.get(ProductRow, "PRD_0001").name
        assert "ATEX" not in description
        assert quantity == 40
        assert session.execute(
            text("SELECT status, approved_at, policy_allowed FROM quotes")
        ).one() == ("DRAFT", None, None)
    before = snapshot(seeded)
    retry = run_scenario(scenario, seeded)
    assert retry.outcome is DemoOutcome.DUPLICATE
    assert retry.write is not None
    assert retry.write.duplicate
    assert snapshot(seeded) == before
    # Changing just the discarded model prose does not change quotation facts.
    assert run_scenario(original, seeded).outcome is DemoOutcome.DUPLICATE
    assert snapshot(seeded) == before


def test_credit_hold_is_still_blocked_and_human_binding_does_not_approve(seeded):
    scenario = SCENARIOS[2]
    forged = scenario.lines[0].model_copy(update={"description": "Unsupported warranty"})
    report = run_scenario(replace(scenario, lines=(forged,)), seeded)
    assert report.outcome is DemoOutcome.DRAFT_BLOCKED
    assert report.result.quote.total == 7800
    assert report.result.quote.status == "DRAFT"
    assert not report.result.eligible_for_human_review
    with seeded.session_factory() as session:
        assert session.execute(text("SELECT code FROM quote_blocked_reasons")).scalars().all() == [
            "CREDIT_HOLD"
        ]
        assert (
            session.execute(text("SELECT description FROM quote_lines")).scalar_one()
            == session.get(ProductRow, "PRD_0003").name
        )
        assert session.execute(text("SELECT count(*) FROM human_actions")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM outbound_messages")).scalar_one() == 0
