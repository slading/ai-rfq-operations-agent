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
from rfq_agent.seed.loader import SeedError, reset_and_seed, seed

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
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
    database = Database.create(args.url)
    try:
        with database.session() as session:
            report = reset_and_seed(session) if args.reset else seed(session)
    except SeedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        database.dispose()

    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
