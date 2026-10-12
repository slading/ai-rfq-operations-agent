"""The documented demo command, exercised as a subprocess (Phase 1M-C).

``make demo`` and ``python -m rfq_agent.demo`` are what a person following the
README actually runs, so they are tested the way they are run: as a process,
against a migrated and seeded scratch database, checking the exit code, the
text a person reads, and the rows a reviewer inspects.

The golden fragments below (the 46,000.00 total, the CREDIT_HOLD ledger, the
AMBIGUOUS ruling) are asserted *because the demo's promise is that it
reproduces the accepted behaviour* - the verdicts themselves belong to their
phase tests: ``test_seeded_resolution.py`` pins the chain's rulings,
``test_seeded_quote_run.py`` pins the run's money and the gate, and neither is
re-derived here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from rfq_agent.seed import reset_and_seed

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Repository root: ``python -m`` is run from here with ``src`` on the path, so
#: the command works whether or not the package is installed editable.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the demo command against ``url`` and capture its output.

    The argv list is built here, not passed through a shell: the only variable
    is a database URL pointing at this test's temporary file.
    """
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "rfq_agent.demo", "--url", url, *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def seeded_url(database_path: Path, session: Session) -> str:
    """A migrated, freshly seeded scratch database, as a CLI-ready URL."""
    reset_and_seed(session)
    session.commit()
    return f"sqlite:///{database_path}"


def _count(session: Session, table: str) -> int:
    """Row count, read in a fresh transaction so nothing is cached."""
    session.rollback()
    return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608


class TestTheDocumentedWalkthrough:
    def test_the_happy_path_persists_a_draft_awaiting_a_human(
        self, seeded_url: str, session: Session
    ) -> None:
        result = _run_cli(seeded_url, "--scenario", "SC-01")

        assert result.returncode == 0, result.stderr
        assert "EXACT identity: CUS_0001" in result.stdout
        assert "RESOLVED PMP-A-100 (PRD_0001) - stored alias match" in result.stdout
        assert "status DRAFT" in result.stdout
        assert "subtotal 46000.00 EUR - discount 0.00" in result.stdout
        assert "(no discount applies) = total 46000.00 EUR" in result.stdout
        assert "eligible for human review: YES" in result.stdout
        assert "stored quote QTE_DEMO_0001: 1 line(s), 0 blocked reason(s)" in result.stdout
        assert "outcome   : DRAFT_AWAITING_HUMAN_REVIEW" in result.stdout

        session.rollback()
        (status, policy_allowed, approved_at) = session.execute(
            text(
                "SELECT status, policy_allowed, approved_at FROM quotes "
                "WHERE quote_id = 'QTE_DEMO_0001'"
            )
        ).one()
        assert status == "DRAFT"
        assert policy_allowed is None
        assert approved_at is None

    def test_the_ambiguous_sku_stops_before_any_quote(
        self, seeded_url: str, session: Session
    ) -> None:
        result = _run_cli(seeded_url, "--scenario", "SC-02")

        assert result.returncode == 0, result.stderr
        assert "AMBIGUOUS - catalogue number matches PRD_0001, PRD_0002" in result.stdout
        assert "line 1 is not RESOLVED (AMBIGUOUS): refusing to map it" in result.stdout
        assert "outcome   : HUMAN_REQUIRED_BEFORE_QUOTE" in result.stdout
        assert _count(session, "quotes") == 0
        assert _count(session, "quote_blocked_reasons") == 0

    def test_the_credit_hold_persists_its_blocking_evidence(
        self, seeded_url: str, session: Session
    ) -> None:
        result = _run_cli(seeded_url, "--scenario", "SC-03")

        assert result.returncode == 0, result.stderr
        assert "the operator bound CUS_0007" in result.stdout
        assert "RESOLVED PMP-B-150 (PRD_0003) - exact SKU match" in result.stdout
        assert "= total 7800.00 EUR" in result.stdout
        assert "eligible for human review: NO" in result.stdout
        assert "CREDIT_HOLD - customer account is on credit hold" in result.stdout
        assert "outcome   : DRAFT_BLOCKED" in result.stdout

        session.rollback()
        rows = session.execute(
            text(
                "SELECT code, message FROM quote_blocked_reasons "
                "WHERE quote_id = 'QTE_DEMO_0003' ORDER BY seq"
            )
        ).all()
        assert rows == [("CREDIT_HOLD", "customer account is on credit hold")]
        (status,) = session.execute(
            text("SELECT status FROM quotes WHERE quote_id = 'QTE_DEMO_0003'")
        ).one()
        assert status == "DRAFT"

    def test_the_full_walkthrough_then_an_identical_free_retry(
        self, seeded_url: str, session: Session
    ) -> None:
        first = _run_cli(seeded_url)
        assert first.returncode == 0, first.stderr
        assert (
            "summary: SC-01=DRAFT_AWAITING_HUMAN_REVIEW, "
            "SC-02=HUMAN_REQUIRED_BEFORE_QUOTE, SC-03=DRAFT_BLOCKED"
        ) in first.stdout

        second = _run_cli(seeded_url)
        assert second.returncode == 0, second.stderr
        assert "identical quote QTE_DEMO_0001 was already stored - nothing new written" in (
            second.stdout
        )
        assert "identical quote QTE_DEMO_0003 was already stored - nothing new written" in (
            second.stdout
        )
        assert "summary: SC-01=DUPLICATE, SC-02=HUMAN_REQUIRED_BEFORE_QUOTE, SC-03=DUPLICATE" in (
            second.stdout
        )

        # The retry added nothing: two drafts, one blocked reason, two claims.
        assert _count(session, "quotes") == 2
        assert _count(session, "quote_lines") == 2
        assert _count(session, "quote_blocked_reasons") == 1
        assert _count(session, "idempotency_claims") == 2

    def test_nothing_is_approved_sent_or_transitioned(
        self, seeded_url: str, session: Session
    ) -> None:
        result = _run_cli(seeded_url)
        assert result.returncode == 0, result.stderr

        assert _count(session, "outbound_messages") == 0
        assert _count(session, "human_actions") == 0
        assert _count(session, "run_events") == 0
        session.rollback()
        states = session.execute(text("SELECT DISTINCT state FROM runs")).all()
        assert states == [("RECEIVED",)]
        session.rollback()
        not_draft = session.execute(
            text("SELECT COUNT(*) FROM quotes WHERE status != 'DRAFT' OR approved_at IS NOT NULL")
        ).scalar_one()
        assert not_draft == 0


class TestOperatorFailures:
    def test_a_missing_schema_is_reported_as_an_instruction(self, tmp_path: Path) -> None:
        result = _run_cli(f"sqlite:///{tmp_path / 'empty.db'}")

        assert result.returncode == 2
        assert "alembic upgrade head" in result.stderr
        assert result.stdout == ""

    def test_a_missing_dataset_is_reported_as_an_instruction(self, database_path: Path) -> None:
        result = _run_cli(f"sqlite:///{database_path}")

        assert result.returncode == 2
        assert "make seed" in result.stderr
        assert result.stdout == ""

    def test_an_unknown_scenario_lists_the_declared_ids(self, seeded_url: str) -> None:
        result = _run_cli(seeded_url, "--scenario", "SC-99")

        assert result.returncode == 2
        assert "unknown scenario 'SC-99'" in result.stderr
        assert "SC-01, SC-02, SC-03" in result.stderr
        assert result.stdout == ""

    def test_list_scenarios_needs_no_database(self) -> None:
        result = _run_cli("sqlite:///:memory:", "--list")

        assert result.returncode == 0, result.stderr
        assert "SC-01  DRAFT_AWAITING_HUMAN_REVIEW" in result.stdout
        assert "SC-02  HUMAN_REQUIRED_BEFORE_QUOTE" in result.stdout
        assert "SC-03  DRAFT_BLOCKED" in result.stdout
