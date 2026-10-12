"""Command line entry point: ``python -m rfq_agent.demo [--url URL] [--scenario SC-NN]``.

Each fixture scenario is walked through the accepted RFQ-to-DRAFT chain
against an already migrated and seeded database; the report prints the
binding, the line rulings, the calculated quote, the gate's (never stored)
verdict, and what the write path persisted. The command writes only through
the accepted ``QuoteWriter``: it approves nothing, sends nothing, transitions
no run.

Exit codes: ``0`` every selected scenario reached its declared outcome, ``2``
anything else - reported on stderr as an instruction, not a traceback.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

from rfq_agent.config import Settings
from rfq_agent.demo import (
    SCENARIOS,
    DemoError,
    ScenarioReport,
    require_prerequisites,
    run_scenario,
    scenario_by_id,
)
from rfq_agent.domain.policy import PolicyGateDecision, QuoteBlockedLedger
from rfq_agent.domain.quote import Quote
from rfq_agent.persistence import Database
from rfq_agent.persistence.writers import QuoteWriteResult
from rfq_agent.quoting import QuoteRunResult
from rfq_agent.seed import DATASET_NAME, SEED_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]

#: Label column width for the report layout (``"sender    : "``).
_LABEL = 10
_INDENT = " " * (_LABEL + 2)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m rfq_agent.demo",
        description=(
            "Walk fixture scenarios through the accepted RFQ-to-DRAFT chain against the "
            f"{DATASET_NAME} dataset ({SEED_VERSION}) and print what persisted."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help="database URL; defaults to the application configuration "
        "(RFQ_DATABASE__URL, then .env, then sqlite:///var/rfq_agent.db) - the same "
        "resolution alembic uses. The database must already be migrated and seeded.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="SC-NN",
        help="run only this scenario; repeat for several. Default: all of them, in order.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the declared scenarios and their declared outcomes, then exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demonstration.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` on success, ``2`` on a failure the operator
        can act on.
    """
    args = _parser().parse_args(argv)
    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.scenario_id}  {scenario.expected.value}")
            print(f"      {scenario.title}")
        return 0

    try:
        scenarios = (
            [scenario_by_id(scenario_id) for scenario_id in args.scenario]
            if args.scenario
            else list(SCENARIOS)
        )
        # The default URL resolves exactly as alembic's does (Settings.load
        # reads RFQ_* and .env): migration and demo always target one database.
        database = Database.create(args.url or Settings.load().database.url)
        try:
            with database.session() as session:
                require_prerequisites(session)
            _print_header(database.url.render_as_string(hide_password=True))
            outcomes: list[str] = []
            for index, scenario in enumerate(scenarios):
                if index:
                    print()
                report = run_scenario(scenario, database)
                print(_format_report(report))
                outcomes.append(f"{scenario.scenario_id}={report.outcome.value}")
        finally:
            database.dispose()
    except DemoError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ValidationError as error:
        print(
            f"error: invalid configuration (RFQ_* environment or .env):\n{error}",
            file=sys.stderr,
        )
        return 2
    except ValueError as error:
        print(
            f"error: the database holds facts the accepted contracts refuse: {error}",
            file=sys.stderr,
        )
        return 2

    print(f"\nsummary: {', '.join(outcomes)}")
    print(
        "Nothing was approved or sent, no run changed state, and no gate decision was "
        "stored: every persisted quote is a DRAFT a human must still review."
    )
    return 0


def _print_header(url: str) -> None:
    """Print what this run is, over which dataset and database."""
    print("RFQ-to-DRAFT demonstration (Phase 1M-C): fixture claims in, persisted drafts out.")
    print(f"dataset: {DATASET_NAME} {SEED_VERSION}; database: {url}")
    print()


def _format_report(report: ScenarioReport) -> str:
    """Render one scenario report as the block the operator reads."""
    scenario = report.scenario
    rows = [f"=== {scenario.scenario_id} · {scenario.title}"]
    rows.append(_row("sender", f"{scenario.sender_email} (untrusted fixture intake)"))
    rows.append(_row("customer", report.customer_summary))
    for index, summary in enumerate(report.line_summaries, start=1):
        rows.append(_row(f"line {index}", summary))
    if report.stop_reason is not None:
        rows.append(_row("stop", report.stop_reason))
        rows.append(_row("persisted", "nothing - no quote was prepared, so nothing was stored"))
    else:
        _result, write, quote, decision, ledger = _require_artefacts(report)
        rows.append(
            _row(
                "quote",
                f"{quote.quote_number} ({quote.quote_id}) for {quote.customer_id} on run "
                f"{quote.run_id} - status {quote.status}",
            )
        )
        rows.extend(_format_money(quote))
        rows.append(_row("delivery", _format_delivery(quote)))
        rows.append(_row("inputs", f"sha256 {_inputs_digest(quote)} - same facts, same digest"))
        eligible = "YES" if decision.eligible_for_human_review else "NO"
        awaiting = "YES" if decision.requires_human_approval else "NO"
        rows.append(
            _row(
                "gate",
                f"eligible for human review: {eligible} / awaiting human approval: {awaiting} "
                "(printed, never stored)",
            )
        )
        rows.extend(_format_blocked(ledger))
        rows.append(_row("persisted", _format_write(write)))
    rows.append(_row("outcome", report.outcome.value))
    return "\n".join(rows)


def _require_artefacts(
    report: ScenarioReport,
) -> tuple[QuoteRunResult, QuoteWriteResult, Quote, PolicyGateDecision, QuoteBlockedLedger]:
    """The completed run's artefacts; a ``COMPLETED`` run guarantees each one."""
    result = report.result
    write = report.write
    quote = result.quote if result is not None else None
    decision = result.decision if result is not None else None
    ledger = result.ledger if result is not None else None
    if result is None or write is None or quote is None or decision is None or ledger is None:
        msg = "a scenario that was not stopped must carry its run, write, quote and ledger"
        raise DemoError(msg)
    return result, write, quote, decision, ledger


def _format_money(quote: Quote) -> list[str]:
    """The money the calculator produced, one row per line plus the totals."""
    rows: list[tuple[str, str]] = []
    for line in quote.lines:
        rendered = (
            f"{line.quantity} x {line.sku} @ {line.unit_price} {line.currency} "
            f"= {line.line_extension}"
        )
        if line.blocked:
            rendered += f"  BLOCKED ({line.price_status}): {line.blocked_reason}"
        rows.append(("money" if not rows else "", rendered))
    discount = quote.discount
    note = "no discount applies" if discount is None else f"rule {discount.rule_id}"
    rows.append(
        (
            "",
            f"subtotal {quote.subtotal} {quote.currency} - discount {quote.discount_amount} "
            f"({note}) = total {quote.total} {quote.currency}",
        )
    )
    return [_row(label, rendered) for label, rendered in rows]


def _format_delivery(quote: Quote) -> str:
    """The delivery promise, or the fact that nobody asked for one."""
    delivery = quote.delivery
    if delivery is None:
        return "none - the request stated no delivery facts, so none were computed"
    promise = delivery.promise
    rendered = (
        f"{promise.feasibility} - ship {promise.earliest_ship_date}, deliver "
        f"{promise.earliest_delivery_date} via {promise.carrier_service_code} "
        f"from {promise.origin_location}"
    )
    if promise.requested_date is not None:
        rendered += f" (requested {promise.requested_date})"
    return rendered


def _format_blocked(ledger: QuoteBlockedLedger) -> list[str]:
    """Every stored blocking reason, verbatim as the projection worded it."""
    if not ledger.reasons:
        return [_row("blocked", "none")]
    return [
        _row(
            "blocked" if index == 0 else "",
            f"{reason.code} - {reason.message}"
            + (f" (line {reason.line_ordinal})" if reason.line_ordinal is not None else ""),
        )
        for index, reason in enumerate(ledger.reasons)
    ]


def _format_write(write: QuoteWriteResult) -> str:
    """What the write path did - including finding its own work already done."""
    if write.duplicate:
        return (
            f"identical quote {write.quote_id} was already stored - nothing new written "
            "(an identical retry is free)"
        )
    return (
        f"stored quote {write.quote_id}: {len(write.line_ids)} line(s), "
        f"{len(write.ledger_seqs)} blocked reason(s), one transaction"
    )


def _inputs_digest(quote: Quote) -> str:
    """The fingerprint the calculation carries (computed if not stored on it)."""
    return quote.inputs_sha256 or quote.inputs_fingerprint()


def _row(label: str, value: str) -> str:
    """One labelled row of the report; continuation rows carry an empty label."""
    return f"{label:<{_LABEL}}: {value}" if label else f"{_INDENT}{value}"


if __name__ == "__main__":
    raise SystemExit(main())
