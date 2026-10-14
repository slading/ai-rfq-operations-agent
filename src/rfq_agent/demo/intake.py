"""Provisioning shim: the RFQ and run rows a persisted quote needs (Phase 1M-C).

``QuoteWriter`` stores a quote only against an existing run, and a run only
against an existing RFQ. No accepted phase owns an intake write path yet, and
production code must not import the test factories - this shim is the smallest
demo-scoped stand-in, to be replaced by the phase that owns real intake.

The rules it enforces:

* **get-or-create.** A re-run finds its own rows and continues - which is also
  what lets the ``DUPLICATE`` outcome be demonstrated at the quote layer.
* **identical means identical.** A stored row is reused only when *every*
  fixture-owned field matches; any difference under a fixture identifier is
  refused, differing fields named, never overwritten (intake history is
  append-only, and a run the demo did not leave in ``RECEIVED`` is not the
  demo's run).
* **nothing is invented.** Stored hashes are :func:`sha256_text` of the
  fixture's untrusted text; every instant comes from the fixture - no clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import DemoScenario
from rfq_agent.domain.intake import InboundRfq, RfqStatus, SourceChannel
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import sha256_text
from rfq_agent.domain.workflow import RunState, RunTrigger
from rfq_agent.persistence.models import CustomerRow, RfqRow, RunRow
from rfq_agent.seed.loader import SeedSchemaError, require_schema

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["provision_run", "require_prerequisites"]

#: The customer whose presence proves the demo dataset has been written.
_DATASET_SENTINEL = "CUS_0001"

#: Every RFQ field the fixture owns. The primary key is excluded - it is how
#: the row is found - and so are ``created_at``/``updated_at``, which the
#: database stamps and the fixture does not.
_RFQ_CONTENT_FIELDS: tuple[str, ...] = (
    "source_channel",
    "status",
    "received_at",
    "subject",
    "body_text",
    "body_sha256",
    "sender_name",
    "sender_email",
    "sender_email_hash",
    "idempotency_key",
    "thread_key",
    "superseded_by_rfq_id",
)

#: Every run field the fixture owns: the whole initial lifecycle.
_RUN_CONTENT_FIELDS: tuple[str, ...] = (
    "rfq_id",
    "attempt_no",
    "trigger",
    "state",
    "prior_run_id",
    "outcome",
    "failure_code",
    "retry_count",
    "checkpoint_stage",
    "row_version",
    "started_at",
    "finished_at",
)


def require_prerequisites(session: Session) -> None:
    """Refuse unless the database is migrated and seeded, naming the fix command.

    The demo never runs migrations or writes the business dataset itself.

    Raises:
        DemoError: When the schema is missing (run ``alembic upgrade head``)
            or the dataset is missing (run ``make seed``).
    """
    try:
        require_schema(session)
    except SeedSchemaError as error:
        raise DemoError(str(error)) from error
    if session.get(CustomerRow, _DATASET_SENTINEL) is None:
        msg = (
            "the Northwind Components demo dataset is not loaded: write it first with "
            "`make seed` (or `python -m rfq_agent.seed`)"
        )
        raise DemoError(msg)


def provision_run(session: Session, scenario: DemoScenario) -> InboundRfq:
    """Get-or-create the RFQ and run rows ``scenario`` states.

    A stored row is reused only when every fixture-owned field is identical;
    a fixture identifier holding different content is a refusal, never an
    overwrite.

    Args:
        session: The unit of work both rows are written in. The caller commits.
        scenario: The fixture stating every identifier and instant.

    Returns:
        The exact immutable source snapshot used to provision/check the RFQ.
        Callers use it for verification only after this transaction commits.

    Raises:
        DemoError: On any differing field in a stored row; the fields are named.
    """
    source = InboundRfq(
        source_channel=SourceChannel.FIXTURE,
        received_at=scenario.received_at,
        subject=scenario.subject,
        body=UntrustedText.from_text(scenario.body),
        sender_name=scenario.sender_name,
        sender_email=scenario.sender_email,
        sender_email_hash=sha256_text(scenario.sender_email),
        idempotency_key=_idempotency_key(scenario),
    )
    rfq = session.get(RfqRow, scenario.rfq_id)
    if rfq is None:
        session.add(_rfq_row(scenario, source))
        # Flush between dependency levels: the models declare no relationships,
        # so SQLAlchemy does not order the inserts by foreign key.
        session.flush()
    else:
        _require_identical(
            rfq, _rfq_row(scenario, source), _RFQ_CONTENT_FIELDS, "RFQ", scenario.rfq_id
        )

    run = session.get(RunRow, scenario.run_id)
    if run is None:
        session.add(_run_row(scenario))
        session.flush()
    else:
        _require_identical(run, _run_row(scenario), _RUN_CONTENT_FIELDS, "run", scenario.run_id)
    return source


def _require_identical(
    stored: RfqRow | RunRow,
    expected: RfqRow | RunRow,
    fields: tuple[str, ...],
    kind: str,
    identifier: str,
) -> None:
    """Refuse a stored row that is not field-for-field the fixture's own.

    The demo's identifiers pin *content*: a differing row is a different
    history the demonstration must not silently join - and must never rewrite.
    """
    mismatches = [name for name in fields if getattr(stored, name) != getattr(expected, name)]
    if mismatches:
        msg = (
            f"{kind} {identifier!r} already exists as different content "
            f"({', '.join(mismatches)}): refusing to overwrite it. The demo only reuses rows "
            "identical to its fixture - demonstrate against a scratch database (`--url`) or "
            "remove the demo rows first"
        )
        raise DemoError(msg)


def _idempotency_key(scenario: DemoScenario) -> str:
    """The intake key the fixture's identity pins. Stable across re-runs."""
    return f"demo:intake:{scenario.scenario_id}"


def _rfq_row(scenario: DemoScenario, source: InboundRfq) -> RfqRow:
    """The inbound record, channel ``FIXTURE``: stated, hashed, never parsed."""
    return RfqRow(
        rfq_id=scenario.rfq_id,
        source_channel=source.source_channel,
        status=RfqStatus.OPEN,
        received_at=source.received_at,
        subject=source.subject,
        body_text=source.body.text,
        body_sha256=source.body.sha256,
        sender_name=source.sender_name,
        sender_email=source.sender_email,
        sender_email_hash=source.sender_email_hash,
        idempotency_key=source.idempotency_key,
        thread_key=None,
        superseded_by_rfq_id=None,
    )


def _run_row(scenario: DemoScenario) -> RunRow:
    """The run the quote will belong to: initial, and left in ``RECEIVED``."""
    return RunRow(
        run_id=scenario.run_id,
        rfq_id=scenario.rfq_id,
        attempt_no=1,
        trigger=RunTrigger.INITIAL,
        state=RunState.RECEIVED,
        prior_run_id=None,
        outcome=None,
        failure_code=None,
        retry_count=0,
        checkpoint_stage=None,
        row_version=0,
        started_at=scenario.received_at,
        finished_at=None,
    )
