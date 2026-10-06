"""The documented command line entry point, exercised as a subprocess.

``make seed`` and ``python -m rfq_agent.seed`` are what a reader of the README
actually runs, so they are tested the way they are run: as a process, with a
database URL, checking the exit code and the text a person would read. A CLI
that works when imported but not when executed is a documentation bug that only
surfaces for the user.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from rfq_agent.seed import DATASET_NAME, SEED_VERSION, row_counts

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Repository root: ``python -m`` is run from here with ``src`` on the path, so
#: the command works whether or not the package is installed editable.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the seed command against ``url`` and capture its output.

    The argv list is built here, not passed through a shell: the only variable is
    a database URL pointing at this test's temporary file.
    """
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "rfq_agent.seed", "--url", url, *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSeedCommand:
    def test_it_seeds_a_migrated_database(self, database_path: Path, session: Session) -> None:
        result = _run_cli(f"sqlite:///{database_path}")

        assert result.returncode == 0, result.stderr
        assert DATASET_NAME in result.stdout
        assert SEED_VERSION in result.stdout
        assert "inserted" in result.stdout
        products = session.execute(text("SELECT COUNT(*) FROM products")).scalar_one()
        assert products == row_counts()["products"]

    def test_reset_makes_the_command_reproducible(
        self, database_path: Path, session: Session
    ) -> None:
        """Running it twice with ``--reset`` leaves the same dataset both times."""
        url = f"sqlite:///{database_path}"
        first = _run_cli(url)
        second = _run_cli(url, "--reset")

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert "removed" in second.stdout
        assert f"{sum(row_counts().values())} rows" in second.stdout

        for table, expected in row_counts().items():
            count = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608
            assert count == expected, table

    def test_a_missing_schema_is_reported_as_an_instruction(self, tmp_path: Path) -> None:
        result = _run_cli(f"sqlite:///{tmp_path / 'empty.db'}")

        assert result.returncode == 2
        assert "alembic upgrade head" in result.stderr
        assert result.stdout == ""
