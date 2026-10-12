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
* **a conflict is refused, never overwritten.** The same fixture identifier
  holding different content means the database is not the one this fixture
  describes; the demo stops rather than edit an intake record (the intake
  audit model is append-only by design).
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

    An existing pair whose content matches the fixture is reused untouched.
    An existing row with the fixture's identifier but different content is a
    contradiction the demo refuses: it exits rather than modify intake rows.

    Args:
        session: The unit of work both rows are written in. The caller commits.
        scenario: The fixture stating every identifier and instant.

    Raises:
        DemoError: When a row already exists under a fixture identifier with
            different content.
    """
    rfq = session.get(RfqRow, scenario.rfq_id)
    if rfq is None:
        session.add(_rfq_row(scenario))
        # Flush between dependency levels: the models declare no relationships,
        # so SQLAlchemy does not order the inserts by foreign key.
        session.flush()
    elif rfq.idempotency_key != _idempotency_key(scenario) or rfq.body_sha256 != sha256_text(
        scenario.body
    ):
        msg = (
            f"RFQ {scenario.rfq_id!r} already exists with different content: refusing to "
            "overwrite it. Demonstrate against a scratch database (`--url`) or remove the "
            "demo rows first"
        )
        raise DemoError(msg)

    run = session.get(RunRow, scenario.run_id)
    if run is None:
        session.add(_run_row(scenario))
        session.flush()
    elif run.rfq_id != scenario.rfq_id:
        msg = (
            f"run {scenario.run_id!r} already exists for a different RFQ ({run.rfq_id!r}): "
            "refusing to overwrite it. Demonstrate against a scratch database (`--url`) or "
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
