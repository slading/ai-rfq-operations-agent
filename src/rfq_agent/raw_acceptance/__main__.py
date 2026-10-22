"""Command line entry point: ``python -m rfq_agent.raw_acceptance --input FILE``.

One raw delivery file is extracted by the configured live provider and pushed
through the accepted engine against a migrated and seeded database. The command
approves nothing, sends nothing, transitions no run and stores no gate decision:
the only quote it can produce is a ``DRAFT`` a human must still review.

Live calls are refused unless ``RFQ_LLM_MODE=live`` **and**
``RFQ_GROQ__API_KEY`` are set. That is deliberate: the default configuration can
never reach the network, so a mistyped command is a safe refusal rather than a
billed call.

Exit codes name the outcome, so a script can branch without parsing the report:

* ``0`` - a DRAFT was persisted (awaiting review or blocked), or an identical
  delivery was already accepted and nothing new was written;
* ``3`` - a human must act before quoting; the accepted engine stored the intake
  rows and no quotation;
* ``2`` - the delivery contradicts stored history (``CONFLICT``), or the accepted
  engine refused the request as unusable;
* ``1`` - no claim reached the engine, or the declaration, the configuration or
  the database failed. Nothing is claimed about what was stored, and the request
  may be retried once the cause is fixed.

Untrusted content is never printed. The report carries identifiers, enum values,
counts, amounts and fixed sentences; the body, the subject, the sender address,
the prompt and the model's raw output stay out of stdout and stderr, and every
exception is reduced to a fixed message at this boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from rfq_agent.acceptance import AcceptanceContentionError, AcceptanceOutcome
from rfq_agent.acceptance.__main__ import exit_code, format_report
from rfq_agent.config import LLMMode, Settings
from rfq_agent.contracts.errors import ProviderNotConfiguredError
from rfq_agent.intake import IntakePrerequisiteError, require_prerequisites
from rfq_agent.llm_extraction import ExtractionRefusalKind, ExtractionSummary
from rfq_agent.observability.validation_errors import summarize_validation_error
from rfq_agent.persistence import Database
from rfq_agent.providers import GroqProvider
from rfq_agent.raw_acceptance import RawAcceptanceReport, RawRfq, accept_raw_rfq
from rfq_agent.seed import DATASET_NAME, SEED_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]

#: Label column width, matching the accepted acceptance report layout.
_LABEL = 10
_INDENT = " " * (_LABEL + 2)


class _ConfigurationError(Exception):
    """The application settings cannot support a live call.

    A fixed message: configuration errors are where a credential would otherwise
    reach stderr, so nothing about the offending value is echoed.
    """


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never echoes attacker-controlled argv values."""

    def error(self, message: str) -> None:  # noqa: ARG002 - argparse interface
        """Print fixed diagnostics and preserve argparse's usage exit status."""
        print(f"{self.prog}: error: invalid command-line arguments", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = _SafeArgumentParser(
        prog="python -m rfq_agent.raw_acceptance",
        description=(
            "Extract claims from one raw RFQ with the configured live provider and "
            f"accept them against the {DATASET_NAME} dataset ({SEED_VERSION})."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="JSON file stating the raw body and the trusted metadata (sender, "
        "received_at, subject, source_channel); see docs/acceptance/raw-input.md.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="database URL; defaults to the application configuration "
        "(RFQ_DATABASE__URL, then .env, then sqlite:///var/rfq_agent.db). The "
        "database must already be migrated and seeded.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Extract one raw delivery and report what persisted.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: ``0`` for a stored or duplicated draft, ``3`` when a
        human must act, ``2`` for a conflict or an unusable request, ``1`` when no
        claim reached the engine or the run failed.
    """
    args = _parser().parse_args(argv)

    database = None
    failure: Exception | None = None
    report: RawAcceptanceReport | None = None
    try:
        settings = _settings()
        raw = _load_delivery(args.input)
        provider = _live_provider(settings)
        database = Database.create(args.url or settings.database.url)
        with database.session() as session:
            require_prerequisites(session)
        report = accept_raw_rfq(
            raw,
            provider=provider,
            database=database,
            model=settings.groq.agent_model,
            temperature=settings.agent.temperature,
            seed=settings.agent.random_seed,
            max_output_tokens=settings.groq.max_output_tokens,
        )
        print(_header(settings.llm_mode))
        print(_format_extraction(report.extraction))
        if report.acceptance is not None:
            print()
            print(format_report(report.acceptance))
        else:
            print()
            print(_format_refusal(report))
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
        print("error: raw acceptance produced no report", file=sys.stderr)
        return 1
    if isinstance(report.outcome, AcceptanceOutcome):
        return exit_code(report.outcome)
    return 1


def _settings() -> Settings:
    """Load application settings, refusing an invalid live configuration safely.

    Raises:
        _ConfigurationError: When the settings do not validate. The message is
            fixed because a settings dump is exactly where an API key would leak.
    """
    try:
        return Settings.load()
    except ValidationError as error:
        raise _ConfigurationError(
            "the configuration cannot support a live call: set RFQ_LLM_MODE=live "
            "together with RFQ_GROQ__API_KEY"
        ) from error


def _live_provider(settings: Settings) -> GroqProvider:
    """Build the live provider, or refuse before anything is read or called.

    Two independent switches must be on: the mode, and a credential. Neither has
    a default that reaches the network.

    Raises:
        ProviderNotConfiguredError: When live calls are disabled or no credential
            is configured.
    """
    if settings.llm_mode is not LLMMode.LIVE:
        msg = (
            "live provider calls are disabled: set RFQ_LLM_MODE=live and "
            "RFQ_GROQ__API_KEY to call the real provider"
        )
        raise ProviderNotConfiguredError(msg)
    try:
        api_key = settings.groq.require_credentials()
    except RuntimeError as error:
        msg = "RFQ_GROQ__API_KEY is not set: live provider calls are unavailable"
        raise ProviderNotConfiguredError(msg) from error
    retry = settings.groq.retry
    return GroqProvider(
        api_key=api_key,
        model=settings.groq.agent_model,
        base_url=settings.groq.base_url,
        timeout_seconds=float(settings.groq.request_timeout_seconds),
        max_response_bytes=settings.groq.max_response_bytes,
        max_output_tokens=settings.groq.max_output_tokens,
        max_attempts=retry.max_attempts,
        base_delay_seconds=float(retry.base_delay_seconds),
        max_delay_seconds=float(retry.max_delay_seconds),
        jitter_ratio=float(retry.jitter_ratio),
    )


def _load_delivery(path: str) -> RawRfq:
    """Read and validate one raw delivery file.

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If the file is not JSON.
        ValidationError: If the document is not a valid raw delivery.
    """
    text = Path(path).read_text(encoding="utf-8")
    return RawRfq.model_validate(json.loads(text))


def _header(mode: LLMMode) -> str:
    """The first line: what this command is and what it is not."""
    return (
        f"Raw RFQ acceptance (live extraction -> accepted engine): "
        f"{DATASET_NAME} {SEED_VERSION}\n"
        f"database: configured database\n"
        f"input: raw delivery file\n"
        f"provider: {mode.value} mode, credentials from the environment"
    )


def _format_extraction(summary: ExtractionSummary | None) -> str:
    """One line of accounting for the model call. No content."""
    if summary is None:
        return _row("extraction", "no claim was produced")
    strict = "yes" if summary.strict_mode_used else "no"
    tokens = summary.prompt_tokens + summary.completion_tokens
    return _row(
        "extraction",
        f"{summary.model} - {summary.line_count} line(s), "
        f"{summary.open_questions} open question(s), {tokens} token(s), "
        f"{summary.latency_ms} ms, provider-constrained schema: {strict}",
    )


def _format_refusal(report: RawAcceptanceReport) -> str:
    """The block printed when no claim reached the accepted engine."""
    refusal = report.refusal
    kind = refusal.kind if refusal is not None else ExtractionRefusalKind.PROVIDER_FAILURE
    detail = refusal.detail if refusal is not None else ""
    retryable = "YES" if refusal is not None and refusal.retryable else "NO"
    rows = [
        f"=== raw RFQ {report.rfq_id}",
        _row("intake", "nothing written - no claim reached the accepted pipeline"),
        _row(
            "stop",
            f"extraction refused ({kind.value}: {detail})"
            if detail
            else f"extraction refused ({kind.value})",
        ),
        _row("retryable", retryable),
        _row("persisted", "nothing - no intake row and no quote row"),
        _row("outcome", kind.value),
    ]
    return "\n".join(rows)


def _row(label: str, value: str) -> str:
    """One labelled row of the report; continuation rows carry an empty label."""
    return f"{label:<{_LABEL}}: {value}" if label else f"{_INDENT}{value}"


def _report_failure(failure: Exception) -> int:
    """Reduce any failure to a fixed diagnostic and a status, never a traceback."""
    if isinstance(failure, _ConfigurationError):
        print(
            "error: the configuration cannot support a live call; set "
            "RFQ_LLM_MODE=live together with RFQ_GROQ__API_KEY",
            file=sys.stderr,
        )
        return 1
    if isinstance(failure, ProviderNotConfiguredError):
        print(
            "error: the live provider is not configured; set RFQ_LLM_MODE=live and "
            "RFQ_GROQ__API_KEY in the environment (credentials are never taken from "
            "arguments)",
            file=sys.stderr,
        )
        return 1
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
            f"error: the delivery is not a valid raw RFQ; {summarize_validation_error(failure)}",
            file=sys.stderr,
        )
        return 1
    if isinstance(failure, ValueError):
        print(
            "error: the delivery names values the accepted contracts refuse",
            file=sys.stderr,
        )
        return 1
    print("error: unexpected raw acceptance failure", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
