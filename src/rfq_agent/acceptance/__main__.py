"""Command line entry point: ``python -m rfq_agent.acceptance --input FILE [--url URL]``.

One declaration file is accepted against a migrated and seeded database and the
report prints what was stored, what was reused and what was refused. The
command approves nothing, sends nothing, transitions no run and stores no gate
decision: the only quote it can produce is a ``DRAFT`` a human must still
review.

Exit codes name the outcome, so a script can branch without parsing the report:

* ``0`` - a DRAFT was persisted (awaiting review or blocked), or an identical
  request was already stored and nothing new was written;
* ``3`` - a human must act before quoting; nothing was stored beyond intake;
* ``2`` - the declaration contradicts stored history (``CONFLICT``);
* ``1`` - the declaration, the database or the run itself failed. A database
  that stayed locked for the whole bounded retry budget reports here too: it is
  not a business outcome, and nothing is claimed about what was stored.

Untrusted content is never printed. The report carries identifiers, enum
values, amounts and fixed sentences; the body, the subject and the sender
address stay in the database, and every exception is reduced to a fixed
message at this boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from rfq_agent.acceptance import (
    AcceptanceContentionError,
    AcceptanceOutcome,
    AcceptanceReason,
    AcceptanceReport,
    FreshRfq,
    accept_rfq,
)
from rfq_agent.config import Settings
from rfq_agent.intake import IntakePrerequisiteError, require_prerequisites
from rfq_agent.observability.validation_errors import summarize_validation_error
from rfq_agent.persistence import Database
from rfq_agent.seed import DATASET_NAME, SEED_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["exit_code", "format_report", "main"]

#: Label column width for the report layout (``"customer  : "``).
_LABEL = 10
_INDENT = " " * (_LABEL + 2)

#: Outcomes that mean "a draft is stored and a human still has to look at it".
_PERSISTED_OUTCOMES = frozenset(
    {
        AcceptanceOutcome.DRAFT_AWAITING_HUMAN_REVIEW,
        AcceptanceOutcome.DRAFT_BLOCKED,
        AcceptanceOutcome.DUPLICATE,
    }
)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes attacker-controlled argv values."""

    def error(self, message: str) -> None:  # noqa: ARG002 - argparse interface
        """Print fixed diagnostics and preserve argparse's usage exit status."""
        print(f"{self.prog}: error: invalid command-line arguments", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = _SafeArgumentParser(
        prog="python -m rfq_agent.acceptance",
        description=(
            "Accept one declared fresh RFQ against the "
            f"{DATASET_NAME} dataset ({SEED_VERSION}) and print what persisted."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="JSON file declaring the intake, the extraction claims, the identifiers "
        "and the instants; see docs/acceptance/ for the three worked examples.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="database URL; defaults to the application configuration "
        "(RFQ_DATABASE__URL, then .env, then sqlite:///var/rfq_agent.db) - the same "
        "resolution alembic uses. The database must already be migrated and seeded.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Accept one declaration and report the outcome.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` for a stored or duplicated draft, ``3`` when a
        human must act, ``2`` for a conflict, ``1`` for anything else.
    """
    args = _parser().parse_args(argv)

    database = None
    failure: Exception | None = None
    report: AcceptanceReport | None = None
    try:
        request = _load_declaration(args.input)
        database = Database.create(args.url or Settings.load().database.url)
        with database.session() as session:
            require_prerequisites(session)
        report = accept_rfq(request, database)
        print(f"RFQ acceptance (fresh input, persisted DRAFT): {DATASET_NAME} {SEED_VERSION}")
        print("database: configured database")
        print("input: declaration file")
        print()
        print(format_report(report))
    except Exception as error:  # noqa: BLE001 - outer boundary sanitizes all failures
        failure = error
    finally:
        if database is not None:
            try:
                database.dispose()
            except Exception as error:  # noqa: BLE001 - cleanup is untrusted too
                if failure is None:
                    failure = error

    if failure is not None:
        return _report_failure(failure)
    if report is None:  # pragma: no cover - only reachable if the block above changed
        print("error: acceptance produced no report", file=sys.stderr)
        return 1
    return exit_code(report.outcome)


def _load_declaration(path: str) -> FreshRfq:
    """Read and validate one declaration file.

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If the file is not JSON.
        ValidationError: If the document is not a valid declaration.
    """
    text = Path(path).read_text(encoding="utf-8")
    return FreshRfq.model_validate(json.loads(text))


def exit_code(outcome: AcceptanceOutcome) -> int:
    """Map an outcome onto the documented exit code."""
    if outcome in _PERSISTED_OUTCOMES:
        return 0
    if outcome is AcceptanceOutcome.HUMAN_REQUIRED_BEFORE_QUOTE:
        return 3
    return 2


def _report_failure(failure: Exception) -> int:
    """Reduce any failure to a fixed diagnostic and a status, never a traceback."""
    if isinstance(failure, IntakePrerequisiteError):
        print(
            "error: acceptance prerequisites missing; run `alembic upgrade head` "
            "if the schema is missing, or `make seed` if the dataset is missing",
            file=sys.stderr,
        )
        return 1
    if isinstance(failure, AcceptanceContentionError):
        print(
            "error: the database stayed locked; nothing is claimed about what was stored "
            "- retry the request",
            file=sys.stderr,
        )
        return 1
    if isinstance(failure, json.JSONDecodeError):
        print("error: the input file is not valid JSON", file=sys.stderr)
        return 1
    if isinstance(failure, OSError):
        print("error: the input file could not be read", file=sys.stderr)
        return 1
    if isinstance(failure, ValidationError):
        print(
            "error: the declaration is not a valid fresh RFQ request; "
            f"{summarize_validation_error(failure)}",
            file=sys.stderr,
        )
        return 1
    if isinstance(failure, ValueError):
        print(
            "error: the declaration names values the accepted contracts refuse",
            file=sys.stderr,
        )
        return 1
    print("error: unexpected acceptance failure", file=sys.stderr)
    return 1


def format_report(report: AcceptanceReport) -> str:
    """Render one acceptance report as the block the operator reads."""
    rows = [f"=== fresh RFQ {report.rfq_id}"]
    intake = "rows written" if report.created else "rows already stored, unchanged"
    rows.append(_row("intake", f"run {report.run_id} - {intake}"))
    if report.customer_status is not None:
        rows.append(_row("customer", report.customer_status.value))
    for index, status in enumerate(report.line_statuses, start=1):
        rows.append(_row(f"line {index}", status.value))
    if report.reason is not None:
        rows.append(_row("stop", _format_refusal(report)))
        # The claim refusal already prints its codes; the other refusals explain
        # themselves with one line per status that stopped the request.
        if report.reason is not AcceptanceReason.CLAIM_VERIFICATION_REFUSED:
            rows.extend(_row("", detail) for detail in report.details)
        rows.append(
            _row("persisted", "nothing - no quote was prepared, so no quote row was stored")
        )
    else:
        rows.append(
            _row(
                "quote",
                f"{report.quote_number} ({report.quote_id}) - status "
                f"{report.quote_status.value if report.quote_status is not None else 'unknown'}",
            )
        )
        for index, summary in enumerate(report.line_summaries):
            rows.append(_row("money" if index == 0 else "", summary))
        rows.append(
            _row(
                "",
                f"subtotal {report.subtotal} {report.currency} - discount "
                f"{report.discount_amount} = total {report.total} {report.currency}",
            )
        )
        rows.append(_row("gate", _format_gate(report)))
        rows.extend(_format_blocked(report))
        rows.append(_row("persisted", _format_persistence(report)))
    rows.append(_row("outcome", report.outcome.value))
    return "\n".join(rows)


def _format_refusal(report: AcceptanceReport) -> str:
    """One fixed sentence naming why nothing was quoted."""
    reason = report.reason
    if reason is None:
        return "acceptance stopped before quoting"
    if reason is AcceptanceReason.CLAIM_VERIFICATION_REFUSED:
        codes = ", ".join(report.details) if report.details else "none reported"
        return f"claim verification refused the quote (codes: {codes})"
    if reason is AcceptanceReason.CUSTOMER_NOT_EXACT:
        return "the sender is not an exactly proven customer: a human must bind it"
    if reason is AcceptanceReason.LINE_NOT_RESOLVED:
        return "at least one claimed line is not resolved: a human must decide it"
    if reason is AcceptanceReason.CUSTOMER_NOT_FOUND:
        return "the bound customer is not in the business data: nothing was priced"
    if reason is AcceptanceReason.IDENTITY_CONFLICT:
        return "the declared identity already exists as different content"
    if reason is AcceptanceReason.STORED_STATE_CONFLICT:
        return "stored intake or quotation history no longer supports this request"
    return "the adapter refused the request: a human must review it"


def _format_gate(report: AcceptanceReport) -> str:
    """The gate's answer, labelled as reported and never stored."""
    if report.eligible_for_review is None:
        return "no decision was taken"
    eligible = "YES" if report.eligible_for_review else "NO"
    awaiting = "YES" if report.approval_required else "NO"
    return (
        f"eligible for human review: {eligible} / awaiting human approval: {awaiting} "
        "(printed, never stored)"
    )


def _format_blocked(report: AcceptanceReport) -> list[str]:
    """Every blocking reason the accepted projection reported."""
    if not report.blocked_messages:
        return [_row("blocked", "none")]
    return [
        _row("blocked" if index == 0 else "", message)
        for index, message in enumerate(report.blocked_messages)
    ]


def _format_persistence(report: AcceptanceReport) -> str:
    """What the accepted write path did - including finding its own work done."""
    if report.outcome is AcceptanceOutcome.DUPLICATE:
        return (
            f"identical quote {report.quote_id} was already stored - nothing new written "
            "(an identical retry is free)"
        )
    return (
        f"stored quote {report.quote_id}: {report.line_count} line(s), "
        f"{len(report.blocked_codes)} blocked reason(s), one transaction"
    )


def _row(label: str, value: str) -> str:
    """One labelled row of the report; continuation rows carry an empty label."""
    return f"{label:<{_LABEL}}: {value}" if label else f"{_INDENT}{value}"


if __name__ == "__main__":
    raise SystemExit(main())
