"""Regression tests for flush classification and __all__ leakage — V1-01a1 P2 fixes."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from rfq_agent.domain.workflow import RunActor, RunState, TransitionEvent
from rfq_agent.persistence import Database
from rfq_agent.persistence.base import Base
from rfq_agent.workflow import transitions as trans_mod
from rfq_agent.workflow.transitions import AuditConstraintError, _transition_run
from tests.persistence.factories import rfq_row, run_row


def _utc_now() -> datetime:
    return datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _make_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test.db"
    db = Database.create(f"sqlite:///{db_path}")
    Base.metadata.create_all(db.engine)
    return db


class TestAllDoesNotExposeInternal:
    def test_transition_run_absent_from_all(self) -> None:
        assert "_transition_run" not in trans_mod.__all__
        assert "AuditConstraintError" in trans_mod.__all__


class TestFlushClassification:
    def test_integrity_error_maps_to_audit_constraint(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            with db.session_factory() as s:
                s.add(rfq_row())
                s.add(run_row(state=RunState.RECEIVED, row_version=0))
                s.commit()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))

                marker = "SECRET_DIAGNOSTIC_SHOULD_NOT_LEAK_12345"

                def _raise_integrity(*_a, **_kw):
                    raise IntegrityError(
                        statement="INSERT INTO run_events ...",
                        params={"run_id": "RUN_0001"},
                        orig=Exception(marker),
                    )

                session.flush = MagicMock(side_effect=_raise_integrity)  # type: ignore[method-assign]

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
                assert marker not in msg
                assert "INSERT INTO" not in msg
                assert "IntegrityError" not in msg
                assert excinfo.value.__cause__ is None

                tb_text = "".join(
                    traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb)
                )
                assert marker not in tb_text
                assert "SECRET_DIAGNOSTIC" not in tb_text

                session.rollback()
        finally:
            db.dispose()

    def test_operational_error_propagates_unchanged(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            with db.session_factory() as s:
                s.add(rfq_row())
                s.add(run_row(state=RunState.RECEIVED, row_version=0))
                s.commit()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))

                def _raise_operational(*_a, **_kw):
                    raise OperationalError(
                        statement="UPDATE runs ...",
                        params={},
                        orig=Exception("database is locked"),
                    )

                session.flush = MagicMock(side_effect=_raise_operational)  # type: ignore[method-assign]

                with pytest.raises(OperationalError) as excinfo:
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                assert "locked" in str(excinfo.value).lower()
                session.rollback()
        finally:
            db.dispose()

    def test_unexpected_exception_propagates_unchanged(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            with db.session_factory() as s:
                s.add(rfq_row())
                s.add(run_row(state=RunState.RECEIVED, row_version=0))
                s.commit()

            with db.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))

                def _raise_value(*_a, **_kw):
                    raise ValueError("unexpected bug")

                session.flush = MagicMock(side_effect=_raise_value)  # type: ignore[method-assign]

                with pytest.raises(ValueError, match="unexpected bug"):
                    _transition_run(
                        session,
                        run_id="RUN_0001",
                        event=TransitionEvent.TRIAGE_START,
                        actor=RunActor.SYSTEM,
                        expected_version=0,
                        occurred_at=_utc_now(),
                    )
                session.rollback()
        finally:
            db.dispose()
