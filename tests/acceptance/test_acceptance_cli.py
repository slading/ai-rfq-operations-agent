"""The documented acceptance command, exercised as a subprocess.

``python -m rfq_agent.acceptance --input FILE`` is what a reviewer runs, so it
is tested the way it is run: as a process against a migrated and seeded scratch
database, checking the exit code, the text a person reads, and the rows that
were actually written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from rfq_agent.acceptance import __main__ as cli
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import (
    IntakeEventRow,
    QuoteLineRow,
    QuoteRow,
    RfqRow,
    RunRow,
)
from rfq_agent.seed import reset_and_seed
from tests.acceptance.conftest import EXAMPLES, REPO_ROOT

_MARKER = "sensitive-marker-DO-NOT-PRINT"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the acceptance command with ``args`` and capture its output."""
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), **(env or {})}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "rfq_agent.acceptance", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def seeded_url(database_path: Path, session) -> str:
    """A migrated, freshly seeded scratch database, as a CLI-ready URL."""
    reset_and_seed(session)
    session.commit()
    return f"sqlite:///{database_path}"


def _counts(url: str) -> dict[str, int]:
    database = Database.create(url)
    try:
        with database.session_factory() as session:
            return {
                model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
                for model in (RfqRow, RunRow, IntakeEventRow, QuoteRow, QuoteLineRow)
            }
    finally:
        database.dispose()


def test_case_a_prints_the_persisted_draft_without_echoing_the_source(seeded_url) -> None:
    result = _run("--input", str(EXAMPLES / "case-a-clean.json"), "--url", seeded_url)

    assert result.returncode == 0
    assert "DRAFT_AWAITING_HUMAN_REVIEW" in result.stdout
    assert "28750.00" in result.stdout
    assert "customer  : EXACT" in result.stdout
    assert "database: configured database" in result.stdout
    assert "Hello," not in result.stdout
    assert "nordwind-industrie.de" not in result.stdout
    assert result.stderr == ""

    counts = _counts(seeded_url)
    assert counts["quotes"] == 1
    assert counts["quote_lines"] == 1
    assert counts["intake_events"] == 1


def test_case_b_prints_the_blocked_draft_and_its_reason(seeded_url) -> None:
    result = _run("--input", str(EXAMPLES / "case-b-stock.json"), "--url", seeded_url)

    assert result.returncode == 0
    assert "DRAFT_BLOCKED" in result.stdout
    assert "230000.00" in result.stdout
    assert "STOCK_INSUFFICIENT" in result.stdout
    assert "eligible for human review: NO" in result.stdout


def test_case_c_refuses_the_claim_and_stores_no_quote(seeded_url) -> None:
    result = _run("--input", str(EXAMPLES / "case-c-tampered.json"), "--url", seeded_url)

    assert result.returncode == 3
    assert "HUMAN_REQUIRED_BEFORE_QUOTE" in result.stdout
    assert "EVIDENCE_NOT_VERBATIM" in result.stdout
    assert "QUANTITY_UNSUPPORTED" in result.stdout
    assert "claim verification refused" in result.stdout
    assert "no quote was prepared" in result.stdout
    assert "Hello," not in result.stdout

    counts = _counts(seeded_url)
    assert counts["rfqs"] == 1
    assert counts["runs"] == 1
    assert counts["intake_events"] == 1
    assert counts["quotes"] == 0
    assert counts["quote_lines"] == 0


def test_case_d_replaying_case_a_reports_a_duplicate(seeded_url) -> None:
    first = _run("--input", str(EXAMPLES / "case-a-clean.json"), "--url", seeded_url)
    second = _run("--input", str(EXAMPLES / "case-a-clean.json"), "--url", seeded_url)

    assert first.returncode == 0
    assert second.returncode == 0
    assert "DUPLICATE" in second.stdout
    assert "nothing new written" in second.stdout

    counts = _counts(seeded_url)
    assert counts["rfqs"] == 1
    assert counts["runs"] == 1
    assert counts["intake_events"] == 1
    assert counts["quotes"] == 1
    assert counts["quote_lines"] == 1


def test_a_conflicting_declaration_exits_two(seeded_url, tmp_path: Path) -> None:
    assert _run("--input", str(EXAMPLES / "case-a-clean.json"), "--url", seeded_url).returncode == 0
    payload = json.loads((EXAMPLES / "case-a-clean.json").read_text(encoding="utf-8"))
    payload["intake"]["body"] = (
        "Hello,\nplease quote 26 pcs of 100-ABC.\nBest regards,\nNordwind Einkauf"
    )
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")

    result = _run("--input", str(changed), "--url", seeded_url)

    assert result.returncode == 2
    assert "CONFLICT" in result.stdout
    assert "different content" in result.stdout


def test_a_missing_input_file_fails_without_a_traceback(seeded_url, tmp_path: Path) -> None:
    result = _run("--input", str(tmp_path / "absent.json"), "--url", seeded_url)

    assert result.returncode == 1
    assert "could not be read" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_invalid_json_is_reported_without_echoing_it(seeded_url, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text(f'{{"body": "{_MARKER}"', encoding="utf-8")

    result = _run("--input", str(broken), "--url", seeded_url)

    assert result.returncode == 1
    assert "not valid JSON" in result.stderr
    assert _MARKER not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_an_invalid_declaration_withholds_every_input_value(seeded_url, tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"intake": {"body": _MARKER}}), encoding="utf-8")

    result = _run("--input", str(invalid), "--url", seeded_url)

    assert result.returncode == 1
    assert "not a valid fresh RFQ request" in result.stderr
    assert _MARKER not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_an_unseeded_database_names_the_fix(database_path: Path) -> None:
    result = _run(
        "--input", str(EXAMPLES / "case-a-clean.json"), "--url", f"sqlite:///{database_path}"
    )

    assert result.returncode == 1
    assert "make seed" in result.stderr


def test_the_parser_does_not_echo_unknown_arguments(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([f"--unknown-{_MARKER}"])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert _MARKER not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_help_remains_available(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    assert "usage:" in capsys.readouterr().out
