"""Demo-scoped provisioning: the RFQ and run rows a persisted quote needs (Phase 1M-C).

``QuoteWriter`` stores a quote only against an existing run, and a run only
against an existing RFQ ("a quote cannot be stored without one"). No accepted
phase owns an intake write path yet - the persistence tests create these rows
with factories in ``tests/``, which production code must not import. This shim
is the smallest demo-scoped stand-in: it writes exactly the two rows the
fixture states, or recognises them when they are already there.

Three rules keep it honest:

* **get-or-create, never create-or-fail.** Re-running the demo on the same
  database finds its own rows and continues - which is also what lets the
  ``DUPLICATE`` outcome be demonstrated at the quote layer.
* **identical means identical.** A stored row is reused only when *every*
  fixture-owned field matches: for the RFQ the intake content (sender identity,
  subject, body and its hash, channel, status, received instant, key, metadata),
  for the run its entire initial lifecycle (parent RFQ, attempt number,
  trigger, state, instants and the untouched lifecycle fields). A row under a
  fixture identifier that differs anywhere is refused, field names stated -
  never overwritten (the intake audit model is append-only by design, and a
  run the demo did not leave in ``RECEIVED`` is not the demo's run).
* **everything is stated by the fixture.** The RFQ's stored hashes are pure
  functions of the fixture's untrusted text (:func:`sha256_text`), and both
  rows take their instants from the fixture - no clock is read here.

This is scaffolding for the demonstration only: the phase that owns real
intake persistence will replace it. It transitions nothing - the run stays
``RECEIVED``, exactly as the accepted write path leaves it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import DemoScenario
from rfq_agent.domain.intake import RfqStatus, SourceChannel
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
    """Raise :class:`DemoError` unless the database is ready to demonstrate on.

    The demo requires an already migrated and seeded database - it never runs
    migrations or writes business data itself. Both failures are reported as
    the command that fixes them.

    Raises:
        DemoError: When the schema has not been migrated (run
            ``alembic upgrade head``) or the demo dataset has not been written
            (run ``make seed``).
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


def provision_run(session: Session, scenario: DemoScenario) -> None:
    """Get-or-create the RFQ and run rows ``scenario`` states.

    An existing pair is reused only when every fixture-owned field is
    identical to what the fixture states. A row under a fixture identifier
    with any difference - another sender, another subject, a lifecycle that
    moved past ``RECEIVED`` - is a contradiction the demo refuses rather than
    modifies.

    Args:
        session: The unit of work both rows are written in. The caller commits.
        scenario: The fixture stating every identifier and instant.

    Raises:
        DemoError: When a stored row under a fixture identifier differs from
            the fixture in any owned field. The differing fields are named.
    """
    rfq = session.get(RfqRow, scenario.rfq_id)
    if rfq is None:
        session.add(_rfq_row(scenario))
        # Flush between dependency levels: the models declare no relationships,
        # so SQLAlchemy does not order the inserts by foreign key.
        session.flush()
    else:
        _require_identical(rfq, _rfq_row(scenario), _RFQ_CONTENT_FIELDS, "RFQ", scenario.rfq_id)

    run = session.get(RunRow, scenario.run_id)
    if run is None:
        session.add(_run_row(scenario))
        session.flush()
    else:
        _require_identical(run, _run_row(scenario), _RUN_CONTENT_FIELDS, "run", scenario.run_id)


def _require_identical(
    stored: RfqRow | RunRow,
    expected: RfqRow | RunRow,
    fields: tuple[str, ...],
    kind: str,
    identifier: str,
) -> None:
    """Refuse when the stored row is not the row the fixture states.

    Not a lookup rule: the demo's identifiers pin *content*, so a stored row
    that is not field-for-field the fixture's own is a different history the
    demonstration must not silently join - and must never rewrite.
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


def _rfq_row(scenario: DemoScenario) -> RfqRow:
    """The inbound record, channel ``FIXTURE``: stated, hashed, never parsed."""
    return RfqRow(
        rfq_id=scenario.rfq_id,
        source_channel=SourceChannel.FIXTURE,
        status=RfqStatus.OPEN,
        received_at=scenario.received_at,
        subject=scenario.subject,
        body_text=scenario.body,
        body_sha256=sha256_text(scenario.body),
        sender_name=scenario.sender_name,
        sender_email=scenario.sender_email,
        sender_email_hash=sha256_text(scenario.sender_email),
        idempotency_key=_idempotency_key(scenario),
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
