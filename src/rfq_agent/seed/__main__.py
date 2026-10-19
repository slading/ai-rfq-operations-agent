"""Command line entry point: ``python -m rfq_agent.seed [--reset] [--url URL]``.

The same two operations the library exposes, with the transaction and the engine
lifetime handled here:

* ``--reset`` deletes the dataset's rows first, so the result is reproducible
  regardless of what the database contained (the local-development path);
* without it, the command converges the database to the dataset and reports how
  many rows it had to insert or correct.

Exit codes: ``0`` success, ``2`` a seed failure the operator can act on (missing
schema, or data that references the dataset and blocks a reset). The message is
written to stderr in both failure cases, because the next step is a command, not
a traceback.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rfq_agent.persistence import Database
from rfq_agent.seed.dataset import DATASET_NAME, SEED_VERSION, total_rows
from rfq_agent.seed.loader import (
    SeedError,
    SeedResetBlockedError,
    SeedSchemaError,
    reset_and_seed,
    seed,
)

__all__ = ["main"]


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes attacker-controlled argv values."""

    def error(self, message: str) -> None:  # noqa: ARG002 - argparse interface
        """Print fixed diagnostics and preserve argparse's usage exit status."""
        print(f"{self.prog}: error: invalid command-line arguments", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = _SafeArgumentParser(
        prog="python -m rfq_agent.seed",
        description=(
            f"Write the {DATASET_NAME} demo dataset ({SEED_VERSION}, "
            f"{total_rows()} rows) to the configured database."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help="database URL; defaults to RFQ_DATABASE__URL (sqlite:///var/rfq_agent.db)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the dataset's rows first, so the result does not depend on the database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the seed command.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` on success, ``2`` on a seed failure.
    """
    args = _parser().parse_args(argv)
    database = None
    failure: Exception | None = None
    rendered_report: str | None = None
    try:
        database = Database.create(args.url)
        with database.session() as session:
            report = reset_and_seed(session) if args.reset else seed(session)
        rendered_report = report.format()
    except Exception as error:  # noqa: BLE001 - outer boundary sanitizes every failure
        failure = error
    finally:
        if database is not None:
            try:
                database.dispose()
            except Exception as error:  # noqa: BLE001 - cleanup is also untrusted
                if failure is None:
                    failure = error

    if failure is not None:
        if isinstance(failure, SeedSchemaError):
            print("error: database schema is missing; run `alembic upgrade head`", file=sys.stderr)
            return 2
        if isinstance(failure, SeedResetBlockedError):
            print(
                "error: reset is blocked by existing business data; use a fresh demo database",
                file=sys.stderr,
            )
            return 2
        if isinstance(failure, SeedError):
            print("error: seed operation could not be completed", file=sys.stderr)
            return 2
        print("error: unexpected seed or cleanup failure", file=sys.stderr)
        return 1

    if rendered_report is None:
        print("error: seed operation produced no report", file=sys.stderr)
        return 1
    print(rendered_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
