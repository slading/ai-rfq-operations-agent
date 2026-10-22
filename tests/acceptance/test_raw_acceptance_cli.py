"""The raw-acceptance command, exercised as a subprocess.

The live path needs a real credential and a real network, which CI has neither
of; what is tested here is everything the command does *before* it would call a
provider - the two switches that guard a live call, the input contract, and the
promise that a refusal never echoes the body or a credential. The provider-free
success path is covered in-process by ``test_raw_acceptance.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from rfq_agent.persistence import Database
from rfq_agent.persistence.models import QuoteRow, RfqRow, RunRow
from rfq_agent.raw_acceptance import RawRfq
from rfq_agent.seed import reset_and_seed
from tests.acceptance.conftest import EXAMPLES, REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

_MARKER = "sensitive-marker-DO-NOT-PRINT"

_VALID_DELIVERY = {
    "body": (
        "Hello,\n"
        "please quote 25 pcs of 100-ABC for our standard replenishment.\n"
        "No specific delivery date is required.\n"
        "Best regards,\n"
        "Nordwind Einkauf"
    ),
    "sender_email": "einkauf@nordwind-industrie.de",
    "sender_name": "Nordwind Einkauf",
    "received_at": "2026-10-06T09:00:00Z",
    "subject": "RFQ: 25 pcs 100-ABC",
}


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the raw-acceptance command with a clean RFQ environment."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("RFQ_")}
    environment = {
        **inherited,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        **(env or {}),
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "rfq_agent.raw_acceptance", *args],
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


def _write(tmp_path: Path, payload: object) -> Path:
    """Write one input file and return its path."""
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _counts(url: str) -> dict[str, int]:
    database = Database.create(url)
    try:
        with database.session_factory() as session:
            return {
                model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
                for model in (RfqRow, RunRow, QuoteRow)
            }
    finally:
        database.dispose()


def test_the_command_refuses_to_call_a_provider_without_live_mode(tmp_path, seeded_url) -> None:
    path = _write(tmp_path, _VALID_DELIVERY)

    result = _run("--input", str(path), "--url", seeded_url, env={"RFQ_LLM_MODE": "replay"})

    assert result.returncode == 1
    assert "the live provider is not configured" in result.stderr
    assert "RFQ_LLM_MODE=live" in result.stderr
    assert _MARKER not in result.stdout + result.stderr
    assert _counts(seeded_url) == {"rfqs": 0, "runs": 0, "quotes": 0}


def test_the_command_refuses_live_mode_without_a_credential(tmp_path, seeded_url) -> None:
    path = _write(tmp_path, _VALID_DELIVERY)

    result = _run(
        "--input",
        str(path),
        "--url",
        seeded_url,
        env={"RFQ_LLM_MODE": "live", "RFQ_GROQ__API_KEY": ""},
    )

    assert result.returncode == 1
    assert "cannot support a live call" in result.stderr
    assert "RFQ_GROQ__API_KEY" in result.stderr
    # Nothing about the offending configuration value is echoed.
    assert "Traceback" not in result.stderr
    assert _counts(seeded_url) == {"rfqs": 0, "runs": 0, "quotes": 0}


def test_the_command_rejects_an_input_file_that_is_not_json(tmp_path, seeded_url) -> None:
    path = tmp_path / "delivery.json"
    path.write_text(_MARKER, encoding="utf-8")

    result = _run("--input", str(path), "--url", seeded_url, env={"RFQ_LLM_MODE": "replay"})

    assert result.returncode == 1
    assert "not valid JSON" in result.stderr
    assert _MARKER not in result.stdout + result.stderr


def test_the_command_rejects_a_delivery_without_a_sender(tmp_path, seeded_url) -> None:
    path = _write(tmp_path, {"body": f"{_MARKER} please quote 25 pcs of 100-ABC"})

    result = _run("--input", str(path), "--url", seeded_url, env={"RFQ_LLM_MODE": "replay"})

    assert result.returncode == 1
    assert "not a valid raw RFQ" in result.stderr
    assert _MARKER not in result.stdout + result.stderr


def test_the_command_rejects_unknown_arguments_without_echoing_them(tmp_path, seeded_url) -> None:
    path = _write(tmp_path, _VALID_DELIVERY)

    result = _run(
        "--input", str(path), f"--{_MARKER}", "--url", seeded_url, env={"RFQ_LLM_MODE": "replay"}
    )

    assert result.returncode == 2
    assert _MARKER not in result.stdout + result.stderr
    assert "invalid command-line arguments" in result.stderr


def test_the_documented_example_is_a_valid_raw_delivery() -> None:
    payload = json.loads((EXAMPLES / "raw-case-a.json").read_text(encoding="utf-8"))

    delivery = RawRfq.model_validate(payload)

    assert delivery.sender_email == "einkauf@nordwind-industrie.de"
    assert "100-ABC" in delivery.body
