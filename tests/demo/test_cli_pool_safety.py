"""Real DBAPI pool faults must not leak through either CLI's output streams."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rfq_agent.seed import reset_and_seed

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_ROOT = Path(__file__).resolve().parents[2]
_MARKER = "pool-fault-sensitive-marker"
_CAUSE = "pool-fault-sensitive-cause"
_SETUP = """import logging
import sqlite3
from sqlalchemy import event, Engine
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('sqlalchemy').setLevel(LOG_LEVEL)
class FaultConnection(sqlite3.Connection):
    armed = False
    def commit(self):
        super().commit()
        self.armed = True
    def rollback(self):
        if STAGE == 'reset' and self.armed:
            fail()
        return super().rollback()
    def close(self):
        super().close()
        if STAGE == 'close':
            fail()
def fail():
    try:
        raise RuntimeError(CAUSE)
    except RuntimeError as cause:
        raise RuntimeError(MARKER) from cause
def connect(dialect, record, cargs, cparams):
    if STAGE == 'connect':
        fail()
    return sqlite3.connect(*cargs, **cparams, factory=FaultConnection)
event.listen(Engine, 'do_connect', connect)
import runpy
sys.argv = [MODULE, '--url', URL]
runpy.run_module(MODULE, run_name='__main__')
"""


@pytest.mark.parametrize("module", ["demo", "seed"])
@pytest.mark.parametrize("stage", ["reset", "close", "connect"])
@pytest.mark.parametrize("log_level", [logging.DEBUG, logging.CRITICAL])
def test_cli_driver_faults_are_nonzero_and_sanitized(
    *,
    database_path: Path,
    session: Session,
    tmp_path: Path,
    module: str,
    stage: str,
    log_level: int,
) -> None:
    """Exercise real commit/reset/close with enabled and disabled pool logs."""
    reset_and_seed(session)
    session.commit()
    source = (
        "import sys\n"
        f"MODULE = {'rfq_agent.' + module!r}\n"
        f"URL = {'sqlite:///' + str(database_path)!r}\n"
        f"MARKER = {_MARKER!r}\nCAUSE = {_CAUSE!r}\n"
        f"STAGE = {stage!r}\nLOG_LEVEL = {log_level!r}\n" + _SETUP
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("RFQ_")}
    env.update(PYTHONPATH=str(_ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(  # noqa: S603 - constant script with synthetic fault data
        [sys.executable, "-B", "-c", source],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    (tmp_path / "pool-fault-capture.json").write_text(
        json.dumps(
            {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
    )
    assert result.returncode == 1, result.stderr
    assert _MARKER not in result.stdout + result.stderr
    assert _CAUSE not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    if stage == "reset" and module == "demo":
        assert "\nsummary:" not in result.stdout
