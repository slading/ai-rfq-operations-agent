"""Concurrency, busy/lock, and rollback tests for _transition_run — V1-01a1."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from rfq_agent.config import DatabaseSettings
from rfq_agent.domain.workflow import RunActor, RunState, TransitionEvent, replay
from rfq_agent.persistence import Database
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.models.run import RunEventRow, RunRow
from rfq_agent.workflow.transitions import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    _transition_run,
)
from tests.persistence.factories import rfq_row, run_row


def _utc_now() -> datetime:
    return datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _make_db(tmp_path: Path, *, busy_timeout_ms: int = 5000) -> Database:
    db_path = tmp_path / "test.db"
    settings = DatabaseSettings(url=f"sqlite:///{db_path}", busy_timeout_ms=busy_timeout_ms)
    db = Database.create(f"sqlite:///{db_path}", database_settings=settings)
    Base.metadata.create_all(db.engine)
    return db


def _seed(db: Database, *, state: RunState = RunState.RECEIVED, version: int = 0) -> None:
    with db.session_factory() as session:
        session.add(rfq_row())
        session.add(run_row(state=state, row_version=version))
        session.commit()


class TestFileBackedConcurrency:
    def test_two_competing_transitions_one_wins(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shared.db"
        settings = DatabaseSettings(url=f"sqlite:///{db_path}", busy_timeout_ms=1000)
        db1 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        db2 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        Base.metadata.create_all(db1.engine)
        try:
            with db1.session_factory() as s:
                s.add(rfq_row())
                s.add(run_row(state=RunState.RECEIVED, row_version=0))
                s.commit()

            with db1.session_factory() as s1:
                s1.execute(text("BEGIN IMMEDIATE"))
                _transition_run(
                    s1,
                    run_id="RUN_0001",
                    event=TransitionEvent.TRIAGE_START,
                    actor=RunActor.SYSTEM,
                    expected_version=0,
                    occurred_at=_utc_now(),
                )
                s1.commit()

            with db2.session_factory() as s2:
                s2.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(ConcurrencyConflictError):
                    _transition_run(
                        s2,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                s2.rollback()

            with db1.session_factory() as s:
                run = s.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.row_version == 1
                assert run.state == RunState.TRIAGING
                cnt = s.query(RunEventRow).filter_by(run_id="RUN_0001").count()
                assert cnt == 1
        finally:
            db1.dispose()
            db2.dispose()

    def test_busy_vs_conflict_distinction(self, tmp_path: Path) -> None:
        db_path = tmp_path / "busy.db"
        settings = DatabaseSettings(url=f"sqlite:///{db_path}", busy_timeout_ms=100)
        db1 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        db2 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        Base.metadata.create_all(db1.engine)
        try:
            with db1.session_factory() as s:
                s.add(rfq_row())
                s.add(run_row())
                s.commit()

            s1 = db1.session_factory()
            s1.execute(text("BEGIN IMMEDIATE"))
            s1.execute(text("UPDATE runs SET state='TRIAGING' WHERE run_id='RUN_0001'"))

            # Second connection tries BEGIN IMMEDIATE — should get busy after timeout
            def _try_begin() -> None:
                s2 = db2.session_factory()
                try:
                    s2.execute(text("BEGIN IMMEDIATE"))
                    s2.rollback()
                finally:
                    s2.close()

            with pytest.raises(OperationalError) as excinfo:
                _try_begin()
            assert "locked" in str(excinfo.value).lower() or "busy" in str(excinfo.value).lower()

            s1.rollback()
            s1.close()

            with db1.session_factory() as s:
                run = s.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.RECEIVED
        finally:
            db1.dispose()
            db2.dispose()


class TestRollbackOnAuditFailure:
    def test_audit_failure_rolls_back_state_update(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed(db, state=RunState.RECEIVED, version=0)

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                session.execute(
                    text(
                        "UPDATE runs SET state='TRIAGING', row_version=1, "
                        "updated_at=:now WHERE run_id='RUN_0001' AND row_version=0"
                    ),
                    {"now": _utc_now()},
                )
                session.add(
                    RunEventRow(
                        run_id="RUN_0001",
                        seq=1,
                        occurred_at=_utc_now(),
                        from_state=RunState.RECEIVED,
                        to_state=RunState.SENT,
                        event=TransitionEvent.APPROVE,
                        actor=RunActor.HUMAN,
                        reason_code=None,
                        detail_json=None,
                        detail_sha256=None,
                    )
                )
                with pytest.raises(IntegrityError):
                    session.flush()
                session.rollback()

            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.RECEIVED
                assert run.row_version == 0
                cnt = session.query(RunEventRow).filter_by(run_id="RUN_0001").count()
                assert cnt == 0

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
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.TRIAGING
        finally:
            db.dispose()

    def test_seq_continuity_after_rollback(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            _seed(db)
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
                try:
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.APPROVE,
                        actor=RunActor.HUMAN,
                        expected_version=1,
                        occurred_at=_utc_now(),
                    )
                except InvalidTransitionError:
                    session.rollback()
                except Exception:
                    session.rollback()
                    raise

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                _run, event = _transition_run(
                    session,
                    run_id="RUN_0001",
                    event=TransitionEvent.CLASSIFIED_RFQ,
                    actor=RunActor.AGENT,
                    expected_version=1,
                    occurred_at=_utc_now(),
                )
                assert event.seq == 2
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
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert replayed == run.state
        finally:
            db.dispose()
