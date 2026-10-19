"""Tests for the internal durable run-transition primitive — V1-01a1."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from rfq_agent.domain.workflow import (
    ReasonCode,
    RunActor,
    RunOutcome,
    RunState,
    TransitionEvent,
    replay,
)
from rfq_agent.persistence import Database
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.models.run import RunEventRow, RunRow
from rfq_agent.workflow.transitions import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    InvariantViolationError,
    RunNotFoundError,
    UnsupportedTransitionError,
    _transition_run,
)
from tests.persistence.factories import rfq_row, run_row


def _utc_now() -> datetime:
    return datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _make_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test.db"
    db = Database.create(f"sqlite:///{db_path}")
    Base.metadata.create_all(db.engine)
    return db


def _seed_run(db: Database, *, state: RunState = RunState.RECEIVED, version: int = 0) -> str:
    with db.session_factory() as session:
        session.add(rfq_row())
        session.add(run_row(state=state, row_version=version))
        session.commit()
    return "RUN_0001"


class TestValidTransition:
    def test_received_to_triaging(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                run, event = _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.TRIAGE_START,
                    actor=RunActor.SYSTEM,
                    expected_version=0,
                    occurred_at=_utc_now(),
                )
                assert run.state == RunState.TRIAGING
                assert run.row_version == 1
                assert event.seq == 1
                assert event.from_state == RunState.RECEIVED
                assert event.to_state == RunState.TRIAGING
                assert event.detail_json is None
                session.commit()

            with db.session_factory() as session:
                persisted = session.get(RunRow, "RUN_0001")
                assert persisted is not None
                assert persisted.state == RunState.TRIAGING
                assert persisted.row_version == 1
                ev = session.get(RunEventRow, {"run_id": "RUN_0001", "seq": 1})
                assert ev is not None
                assert ev.event == TransitionEvent.TRIAGE_START
        finally:
            db.dispose()

    def test_seq_continuity_and_replay_parity(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.TRIAGE_START,
                    actor=RunActor.SYSTEM,
                    expected_version=0,
                    occurred_at=_utc_now(),
                )
                session.commit()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.CLASSIFIED_RFQ,
                    actor=RunActor.AGENT,
                    expected_version=1,
                    occurred_at=_utc_now(),
                )
                session.commit()

            with db.session_factory() as session:
                events = (
                    session.query(RunEventRow)
                    .filter_by(run_id="RUN_0001")
                    .order_by(RunEventRow.seq)
                    .all()
                )
                assert [e.seq for e in events] == [1, 2]
                replayed = replay(tuple((e.from_state, e.event) for e in events))
                persisted = session.get(RunRow, "RUN_0001")
                assert persisted is not None
                assert replayed == persisted.state
                assert replayed == RunState.EXTRACTING
        finally:
            db.dispose()


class TestActorReasonValidation:
    def test_wrong_actor_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError, match="actor"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.HUMAN,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.RECEIVED
                assert run.row_version == 0
        finally:
            db.dispose()

    def test_missing_required_reason_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.TRIAGING, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.CLASSIFIED_NOT_RFQ,
                        actor=RunActor.AGENT,
                        expected_version=0,
                        reason_code=None,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()

    def test_wrong_required_reason_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.TRIAGING, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.CLASSIFIED_NOT_RFQ,
                        actor=RunActor.AGENT,
                        expected_version=0,
                        reason_code=ReasonCode.DUPLICATE,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()

    def test_reason_when_not_required_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        reason_code=ReasonCode.DUPLICATE,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()


class TestIllegalAndUnsupported:
    def test_illegal_transition_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvalidTransitionError):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.APPROVE,
                        actor=RunActor.HUMAN,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.RECEIVED
        finally:
            db.dispose()

    def test_retry_unsupported(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.FAILED_RETRYABLE, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(UnsupportedTransitionError, match="RETRY"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.RETRY,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()

    def test_missing_run(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(RunNotFoundError):
                    _transition_run(
                        session,
                        run_id="RUN_MISSING",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()


class TestTerminalOutcomeMapping:
    def test_terminal_requires_exact_outcome(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.AWAITING_REVIEW, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError, match="requires"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.APPROVE,
                        actor=RunActor.HUMAN,
                        expected_version=0,
                        outcome=None,
                        occurred_at=_utc_now(),
                    )
                session.rollback()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError, match="requires"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.APPROVE,
                        actor=RunActor.HUMAN,
                        expected_version=0,
                        outcome=RunOutcome.DUPLICATE,
                        occurred_at=_utc_now(),
                    )
                session.rollback()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                run, _event = _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.APPROVE,
                    actor=RunActor.HUMAN,
                    expected_version=0,
                    outcome=RunOutcome.SUCCEEDED,
                    occurred_at=_utc_now(),
                )
                assert run.state == RunState.SENT
                assert run.outcome == RunOutcome.SUCCEEDED
                session.commit()
        finally:
            db.dispose()

    def test_non_terminal_must_have_no_outcome(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError, match="must have outcome None"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        outcome=RunOutcome.SUCCEEDED,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()

    def test_failed_terminal_requires_attempts_exhausted(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.FAILED_RETRYABLE, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(InvariantViolationError, match="FAILED_TERMINAL"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.ATTEMPTS_EXHAUSTED,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        reason_code=ReasonCode.ATTEMPTS_EXHAUSTED,
                        outcome=RunOutcome.FAILED_TERMINAL,
                        failure_code=ReasonCode.DUPLICATE,
                        occurred_at=_utc_now(),
                    )
                session.rollback()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                run, _ = _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.ATTEMPTS_EXHAUSTED,
                    actor=RunActor.SYSTEM,
                    expected_version=0,
                    reason_code=ReasonCode.ATTEMPTS_EXHAUSTED,
                    outcome=RunOutcome.FAILED_TERMINAL,
                    failure_code=ReasonCode.ATTEMPTS_EXHAUSTED,
                    occurred_at=_utc_now(),
                )
                assert run.state == RunState.FAILED_TERMINAL
                session.commit()
        finally:
            db.dispose()


class TestStaleVersion:
    def test_stale_version_conflict(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed_run(db, state=RunState.RECEIVED, version=0)
            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.TRIAGE_START,
                    actor=RunActor.SYSTEM,
                    expected_version=0,
                    occurred_at=_utc_now(),
                )
                session.commit()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(ConcurrencyConflictError):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.CLASSIFIED_RFQ,
                        actor=RunActor.AGENT,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()

            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.row_version == 1
                assert run.state == RunState.TRIAGING
                count = session.query(RunEventRow).filter_by(run_id="RUN_0001").count()
                assert count == 1
        finally:
            db.dispose()
