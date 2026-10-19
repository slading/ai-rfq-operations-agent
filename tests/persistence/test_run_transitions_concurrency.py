"""Concurrency, busy/lock, and rollback tests for _transition_run — V1-01a1 P2 fixes.

Uses Alembic-migrated file-backed SQLite with isolated tmp_path databases.
Implements threaded busy vs conflict distinction and trigger-based audit failure.
"""

from __future__ import annotations

import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from rfq_agent.config import DatabaseSettings
from rfq_agent.domain.workflow import RunActor, RunState, TransitionEvent, replay
from rfq_agent.persistence import Database
from rfq_agent.persistence.models.run import RunEventRow, RunRow
from rfq_agent.workflow.transitions import (
    AuditConstraintError,
    ConcurrencyConflictError,
    _transition_run,
)
from tests.persistence.factories import rfq_row, run_row

REPO_ROOT = Path(__file__).resolve().parents[2]


def _utc_now() -> datetime:
    return datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def make_alembic_config(database_path: Path | str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _migrate_db_file(db_path: Path) -> None:
    cfg = make_alembic_config(db_path)
    command.upgrade(cfg, "head")


def _make_db(file_path: Path, *, busy_timeout_ms: int = 5000) -> Database:
    settings = DatabaseSettings(url=f"sqlite:///{file_path}", busy_timeout_ms=busy_timeout_ms)
    return Database.create(f"sqlite:///{file_path}", database_settings=settings)


def _seed(db: Database, *, state: RunState = RunState.RECEIVED, version: int = 0) -> None:
    with db.session_factory() as session:
        session.add(rfq_row())
        session.add(run_row(state=state, row_version=version))
        session.commit()


class TestFileBackedConcurrency:
    def test_two_competing_transitions_one_wins(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shared.db"
        _migrate_db_file(db_path)
        settings = DatabaseSettings(url=f"sqlite:///{db_path}", busy_timeout_ms=1000)
        db1 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        db2 = Database.create(f"sqlite:///{db_path}", database_settings=settings)
        try:
            _seed(db1, state=RunState.RECEIVED, version=0)

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

    def test_busy_vs_conflict_with_threads(self, tmp_path: Path) -> None:  # noqa: PLR0915
        """Deterministic threaded test: A holds lock, B gets busy, then conflict."""
        db_path = tmp_path / "threaded.db"
        _migrate_db_file(db_path)

        db_a = _make_db(db_path, busy_timeout_ms=5000)
        db_b = _make_db(db_path, busy_timeout_ms=100)
        try:
            _seed(db_a, state=RunState.RECEIVED, version=0)

            evt_a_locked = threading.Event()
            evt_b_attempted_busy = threading.Event()
            evt_a_committed = threading.Event()
            evt_b_done = threading.Event()
            errors: dict[str, Exception] = {}

            def run_a():
                try:
                    sess = db_a.session_factory()
                    sess.execute(text("BEGIN IMMEDIATE"))
                    _transition_run(
                        sess,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                    evt_a_locked.set()
                    if not evt_b_attempted_busy.wait(timeout=5):
                        errors["a_timeout"] = TimeoutError("B never attempted busy")
                        sess.rollback()
                        sess.close()
                        evt_a_committed.set()
                        return
                    sess.commit()
                    sess.close()
                    evt_a_committed.set()
                except Exception as exc:  # noqa: BLE001
                    errors["a"] = exc
                    evt_a_locked.set()
                    evt_a_committed.set()
                    evt_b_attempted_busy.set()

            def run_b():
                try:
                    if not evt_a_locked.wait(timeout=5):
                        errors["b_timeout_a"] = TimeoutError("A never locked")
                        evt_b_attempted_busy.set()
                        return
                    # Attempt while A holds RESERVED
                    busy_ok = False
                    busy_msg = ""
                    try:
                        with db_b.session_factory() as s2:
                            s2.execute(text("BEGIN IMMEDIATE"))
                            _transition_run(
                                s2,
                                run_id="RUN_0001",
                                event=TransitionEvent.TRIAGE_START,
                                actor=RunActor.SYSTEM,
                                expected_version=0,
                                occurred_at=_utc_now(),
                            )
                            s2.commit()
                            errors["b_busy"] = AssertionError("expected OperationalError")
                    except OperationalError as oe:
                        busy_msg = str(oe).lower()
                        busy_ok = "locked" in busy_msg or "busy" in busy_msg
                    except Exception as exc:  # noqa: BLE001
                        errors["b_busy_unexpected"] = exc
                    else:
                        busy_ok = False
                    if not busy_ok and "b_busy" not in errors and "b_busy_unexpected" not in errors:
                        errors["b_busy_check"] = AssertionError(f"busy not detected: {busy_msg}")
                    evt_b_attempted_busy.set()

                    if not evt_a_committed.wait(timeout=5):
                        errors["b_timeout_commit"] = TimeoutError("A never committed")
                        evt_b_done.set()
                        return

                    # Retry with stale expected_version -> conflict
                    try:
                        with db_b.session_factory() as s3:
                            s3.execute(text("BEGIN IMMEDIATE"))
                            _transition_run(
                                s3,
                                run_id="RUN_0001",
                                event=TransitionEvent.TRIAGE_START,
                                actor=RunActor.SYSTEM,
                                expected_version=0,
                                occurred_at=_utc_now(),
                            )
                            s3.commit()
                            errors["b_conflict"] = AssertionError(
                                "expected ConcurrencyConflictError"
                            )
                    except ConcurrencyConflictError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        errors["b_conflict_unexpected"] = exc
                    finally:
                        evt_b_done.set()
                except Exception as exc:  # noqa: BLE001
                    errors["b"] = exc
                    evt_b_attempted_busy.set()
                    evt_b_done.set()

            t_a = threading.Thread(target=run_a, name="writer-a")
            t_b = threading.Thread(target=run_b, name="writer-b")
            t_a.start()
            t_b.start()

            t_a.join(timeout=10)
            t_b.join(timeout=10)

            assert not t_a.is_alive(), "writer A thread did not finish"
            assert not t_b.is_alive(), "writer B thread did not finish"
            assert not errors, f"thread errors: {errors}"

            with db_a.session_factory() as s:
                run = s.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.row_version == 1
                assert run.state == RunState.TRIAGING
                events = (
                    s.query(RunEventRow)
                    .filter_by(run_id="RUN_0001")
                    .order_by(RunEventRow.seq)
                    .all()
                )
                assert len(events) == 1
                assert events[0].seq == 1
                replayed = replay(tuple((e.from_state, e.event) for e in events))
                assert replayed == run.state
        finally:
            db_a.dispose()
            db_b.dispose()


class TestRollbackOnAuditFailure:
    def test_audit_failure_via_trigger_with_real_primitive(self, tmp_path: Path) -> None:  # noqa: PLR0915
        """Trigger-based audit INSERT failure using real _transition_run on migrated DB."""
        db_path = tmp_path / "audit_fail.db"
        _migrate_db_file(db_path)
        db = _make_db(db_path)
        fault_marker = "TEST_FAULT_DIAGNOSTIC_SECRET_999"
        try:
            _seed(db, state=RunState.RECEIVED, version=0)

            with db.session_factory() as session:
                session.execute(
                    text(
                        f"CREATE TRIGGER trg_test_audit_fail "
                        f"BEFORE INSERT ON run_events FOR EACH ROW "
                        f"BEGIN SELECT RAISE(ABORT, '{fault_marker}'); END"
                    )
                )
                session.commit()

            with db.engine.connect() as conn:
                sql = (
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='run_events'"
                )
                triggers = [row[0] for row in conn.execute(text(sql)).fetchall()]
            assert "trg_run_events_no_update" in triggers
            assert "trg_run_events_no_delete" in triggers
            assert "trg_test_audit_fail" in triggers

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                with pytest.raises(AuditConstraintError) as excinfo:
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                msg = str(excinfo.value)
                assert fault_marker not in msg
                assert "TEST_FAULT" not in msg
                assert excinfo.value.__cause__ is None
                tb_text = "".join(
                    traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb)
                )
                assert fault_marker not in tb_text

                session.rollback()

            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.RECEIVED
                assert run.row_version == 0
                cnt = session.query(RunEventRow).filter_by(run_id="RUN_0001").count()
                assert cnt == 0

            with db.session_factory() as session:
                session.execute(text("DROP TRIGGER IF EXISTS trg_test_audit_fail"))
                session.commit()

            with db.engine.connect() as conn:
                sql = (
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='run_events'"
                )
                triggers_after = [row[0] for row in conn.execute(text(sql)).fetchall()]
            assert "trg_run_events_no_update" in triggers_after
            assert "trg_run_events_no_delete" in triggers_after
            assert "trg_test_audit_fail" not in triggers_after

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
                assert event.seq == 1
                session.commit()

            with db.session_factory() as session:
                run = session.get(RunRow, "RUN_0001")
                assert run is not None
                assert run.state == RunState.TRIAGING
                assert run.row_version == 1
                events = (
                    session.query(RunEventRow)
                    .filter_by(run_id="RUN_0001")
                    .order_by(RunEventRow.seq)
                    .all()
                )
                assert [e.seq for e in events] == [1]
                replayed = replay(tuple((e.from_state, e.event) for e in events))
                assert replayed == run.state
        finally:
            db.dispose()

    def test_seq_continuity_after_rollback(self, tmp_path: Path) -> None:
        db_path = tmp_path / "seq.db"
        _migrate_db_file(db_path)
        db = _make_db(db_path)
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
                except Exception:  # noqa: BLE001
                    session.rollback()

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
