"""The demo provisioning shim's contract (Phase 1M-C).

The shim is get-or-create over fixture-stated rows, and *identical means
identical*: it writes the RFQ and its run exactly once, reuses them only when
every fixture-owned field matches, and refuses - field names stated, never an
overwrite - a row that holds the fixture's identifier with different content.
The prerequisite check reports a missing schema or dataset as the command
that fixes it, because the demo prepares neither.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from rfq_agent.demo.errors import DemoError
from rfq_agent.demo.fixtures import SCENARIOS
from rfq_agent.demo.intake import provision_run, require_prerequisites
from rfq_agent.domain.intake import RfqStatus, SourceChannel
from rfq_agent.domain.values import sha256_text
from rfq_agent.domain.workflow import RunState, RunTrigger
from rfq_agent.persistence import Database
from rfq_agent.persistence.models import RfqRow, RunRow
from rfq_agent.seed import reset_and_seed
from tests.persistence.factories import rfq_row, run_row

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

SCENARIO = SCENARIOS[0]
OTHER = SCENARIOS[2]


def fixture_rfq_row(**overrides: object) -> RfqRow:
    """The RFQ exactly as the shim would store it, with one field overridden."""
    values: dict[str, object] = {
        "rfq_id": SCENARIO.rfq_id,
        "source_channel": SourceChannel.FIXTURE,
        "status": RfqStatus.OPEN,
        "received_at": SCENARIO.received_at,
        "subject": SCENARIO.subject,
        "body_text": SCENARIO.body,
        "body_sha256": sha256_text(SCENARIO.body),
        "sender_name": SCENARIO.sender_name,
        "sender_email": SCENARIO.sender_email,
        "sender_email_hash": sha256_text(SCENARIO.sender_email),
        "idempotency_key": "demo:intake:SC-01",
        "thread_key": None,
        "superseded_by_rfq_id": None,
    }
    values.update(overrides)
    return rfq_row(**values)


def fixture_run_row(**overrides: object) -> RunRow:
    """The run exactly as the shim would store it, with one field overridden."""
    values: dict[str, object] = {
        "run_id": SCENARIO.run_id,
        "rfq_id": SCENARIO.rfq_id,
        "attempt_no": 1,
        "trigger": RunTrigger.INITIAL,
        "state": RunState.RECEIVED,
        "started_at": SCENARIO.received_at,
    }
    values.update(overrides)
    return run_row(**values)


class TestProvisionRun:
    def test_it_writes_the_fixture_rows_verbatim(self, session: Session) -> None:
        provision_run(session, SCENARIO)

        rfq = session.get(RfqRow, SCENARIO.rfq_id)
        assert rfq is not None
        assert rfq.source_channel is SourceChannel.FIXTURE
        assert rfq.status is RfqStatus.OPEN
        assert rfq.received_at == SCENARIO.received_at
        assert rfq.sender_email == SCENARIO.sender_email
        assert rfq.body_text == SCENARIO.body
        assert rfq.body_sha256 == sha256_text(SCENARIO.body)
        assert rfq.sender_email_hash == sha256_text(SCENARIO.sender_email)
        assert rfq.idempotency_key == "demo:intake:SC-01"

        run = session.get(RunRow, SCENARIO.run_id)
        assert run is not None
        assert run.rfq_id == SCENARIO.rfq_id
        assert run.attempt_no == 1
        assert run.trigger is RunTrigger.INITIAL
        assert run.state is RunState.RECEIVED
        assert run.outcome is None
        assert run.started_at == SCENARIO.received_at

    def test_reprovisioning_recognises_its_own_rows(self, session: Session) -> None:
        provision_run(session, SCENARIO)
        provision_run(session, SCENARIO)
        session.flush()

        assert session.scalar(select(func.count()).select_from(RfqRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunRow)) == 1

    def test_reprovisioning_after_a_committed_run_still_recognises_them(
        self, session: Session
    ) -> None:
        """Row content round-trips: a fresh read still matches the fixture exactly."""
        provision_run(session, SCENARIO)
        session.commit()
        session.rollback()

        provision_run(session, SCENARIO)
        provision_run(session, SCENARIO)
        assert session.scalar(select(func.count()).select_from(RfqRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunRow)) == 1

    def test_rows_survive_for_the_writer(self, session: Session) -> None:
        """The provisioned run is exactly the parent ``QuoteWriter`` looks up."""
        provision_run(session, SCENARIO)
        session.commit()
        session.rollback()

        run = session.get(RunRow, SCENARIO.run_id)
        assert run is not None
        assert run.state is RunState.RECEIVED


class TestConflictingRowsAreRefused:
    """get-or-create enforces *identical*: any field's difference is a refusal."""

    def test_a_different_idempotency_key_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row(idempotency_key="intake:other"))
        session.commit()

        with pytest.raises(DemoError, match=r"different content \(idempotency_key\)"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RfqRow, SCENARIO.rfq_id).idempotency_key == "intake:other"

    def test_a_different_body_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row(body_sha256="b" * 64, body_text="different request text"))
        session.commit()

        with pytest.raises(DemoError, match=r"already exists as different content"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RfqRow, SCENARIO.rfq_id).body_sha256 == "b" * 64

    def test_a_different_rfq_subject_is_rejected_even_with_matching_key_and_hash(
        self, session: Session
    ) -> None:
        session.add(fixture_rfq_row(subject="RFQ: a quietly changed subject"))
        session.commit()

        with pytest.raises(DemoError, match=r"\(subject\)"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RfqRow, SCENARIO.rfq_id).subject == "RFQ: a quietly changed subject"

    def test_a_different_sender_is_rejected_even_with_matching_key_and_hash(
        self, session: Session
    ) -> None:
        another = "vertrieb@nordwind-industrie.de"
        session.add(fixture_rfq_row(sender_email=another, sender_email_hash=sha256_text(another)))
        session.commit()

        with pytest.raises(DemoError, match=r"sender_email"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RfqRow, SCENARIO.rfq_id).sender_email == another

    def test_a_different_received_at_is_rejected(self, session: Session) -> None:
        earlier = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
        session.add(fixture_rfq_row(received_at=earlier))
        session.commit()

        with pytest.raises(DemoError, match=r"\(received_at\)"):
            provision_run(session, SCENARIO)

    def test_a_different_channel_or_status_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row(source_channel=SourceChannel.EMAIL))
        session.commit()

        with pytest.raises(DemoError, match=r"source_channel"):
            provision_run(session, SCENARIO)

    def test_a_run_of_a_different_rfq_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row())
        session.add(rfq_row(OTHER.rfq_id, idempotency_key="intake:other-rfq"))
        session.flush()
        session.add(fixture_run_row(rfq_id=OTHER.rfq_id))
        session.commit()

        with pytest.raises(DemoError, match=rf"run {SCENARIO.run_id!r} already exists.*\(rfq_id\)"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RunRow, SCENARIO.run_id).rfq_id == OTHER.rfq_id

    def test_a_run_that_moved_on_is_rejected(self, session: Session) -> None:
        """Same RFQ, but a lifecycle past RECEIVED is not the demo's run."""
        session.add(fixture_rfq_row())
        session.flush()
        session.add(fixture_run_row(state=RunState.CALCULATING))
        session.commit()

        with pytest.raises(DemoError, match=r"state"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RunRow, SCENARIO.run_id).state is RunState.CALCULATING

    def test_a_run_with_a_different_trigger_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row())
        session.flush()
        session.add(fixture_run_row(trigger=RunTrigger.HUMAN_RERUN))
        session.commit()

        with pytest.raises(DemoError, match=r"\(trigger\)"):
            provision_run(session, SCENARIO)
        session.rollback()

        assert session.get(RunRow, SCENARIO.run_id).trigger is RunTrigger.HUMAN_RERUN

    def test_a_run_with_a_different_attempt_or_start_is_rejected(self, session: Session) -> None:
        session.add(fixture_rfq_row())
        session.flush()
        session.add(
            fixture_run_row(attempt_no=2, started_at=datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
        )
        session.commit()

        with pytest.raises(DemoError, match=r"attempt_no, started_at"):
            provision_run(session, SCENARIO)


class TestRequirePrerequisites:
    def test_a_missing_schema_names_the_migration_command(self, tmp_path: Path) -> None:
        empty = Database.create(f"sqlite:///{tmp_path / 'empty.db'}")
        try:
            with empty.session() as session, pytest.raises(DemoError, match="alembic upgrade head"):
                require_prerequisites(session)
        finally:
            empty.dispose()

    def test_a_migrated_but_unseeded_database_names_the_seed_command(
        self, session: Session
    ) -> None:
        with pytest.raises(DemoError, match="make seed"):
            require_prerequisites(session)

    def test_a_seeded_database_passes(self, session: Session) -> None:
        reset_and_seed(session)
        session.commit()

        require_prerequisites(session)

    def test_the_sentinel_check_does_not_confuse_scaffold_with_dataset(
        self, session: Session
    ) -> None:
        """A database holding only the demo's own RFQ rows is still unseeded."""
        provision_run(session, SCENARIO)
        session.commit()

        with pytest.raises(DemoError, match="make seed"):
            require_prerequisites(session)


def test_provisioned_instants_come_from_the_fixture(session: Session) -> None:
    """No clock is read: every stored instant is a fixture literal."""
    assert SCENARIO.received_at == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    provision_run(session, SCENARIO)
    assert session.get(RfqRow, SCENARIO.rfq_id).received_at == SCENARIO.received_at
