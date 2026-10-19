"""Durable run-transition persistence primitive — V1-01a1.

Internal, caller-transaction-owned operation that atomically persists a legal
run-state transition and its append-only audit event.

This is persistence correctness only, not business authorization.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rfq_agent.domain.workflow import (
    ReasonCode,
    RunActor,
    RunOutcome,
    RunState,
    TransitionError,
    TransitionEvent,
    get_transition,
    terminal_states,
)
from rfq_agent.persistence.models.run import RunEventRow, RunRow

__all__ = [
    "AuditConstraintError",
    "ConcurrencyConflictError",
    "InvalidTransitionError",
    "InvariantViolationError",
    "RunNotFoundError",
    "UnsupportedTransitionError",
]

_MAX_RUN_ID_LEN: Final[int] = 64


class RunNotFoundError(RuntimeError):
    """Run does not exist."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} not found")
        self.run_id = run_id


class InvalidTransitionError(RuntimeError):
    """Illegal source/event edge per TRANSITIONS table."""

    def __init__(self, run_id: str, from_state: RunState, event: TransitionEvent) -> None:
        msg = f"illegal {from_state.value} --{event.value}--> for run {run_id!r}"
        super().__init__(msg)
        self.run_id = run_id
        self.from_state = from_state
        self.event = event


class UnsupportedTransitionError(RuntimeError):
    """Syntactically legal but deferred in V1-01a1."""

    def __init__(self, run_id: str, event: TransitionEvent, reason: str) -> None:
        super().__init__(f"unsupported {event.value} for {run_id!r}: {reason}")
        self.run_id = run_id
        self.event = event
        self.reason = reason


class InvariantViolationError(RuntimeError):
    """Actor, reason, or outcome mapping violation."""

    def __init__(self, run_id: str, detail: str) -> None:
        super().__init__(f"invariant violation for {run_id!r}: {detail}")
        self.run_id = run_id
        self.detail = detail


class ConcurrencyConflictError(RuntimeError):
    """Optimistic lock conflict — stale expected_version."""

    def __init__(self, run_id: str, expected_version: int) -> None:
        super().__init__(f"conflict for {run_id!r}: expected {expected_version}")
        self.run_id = run_id
        self.expected_version = expected_version


class AuditConstraintError(RuntimeError):
    """Database constraint violation on audit append or run update."""

    def __init__(self, run_id: str, detail: str = "audit constraint violation") -> None:
        super().__init__(f"{detail} for {run_id!r}")
        self.run_id = run_id


_TERMINAL_OUTCOME_MAP: Final[dict[RunState, RunOutcome]] = {
    RunState.SENT: RunOutcome.SUCCEEDED,
    RunState.REJECTED: RunOutcome.REJECTED,
    RunState.ESCALATED: RunOutcome.ESCALATED,
    RunState.NOT_AN_RFQ: RunOutcome.NOT_AN_RFQ,
    RunState.DUPLICATE: RunOutcome.DUPLICATE,
    RunState.SUPERSEDED: RunOutcome.SUPERSEDED,
    RunState.FAILED_TERMINAL: RunOutcome.FAILED_TERMINAL,
}


def _validate_outcome_mapping(
    target_state: RunState,
    outcome: RunOutcome | None,
    failure_code: ReasonCode | None,
    required_reason: ReasonCode | None,
) -> None:
    """Enforce explicit terminal outcome mapping."""
    is_terminal = target_state in terminal_states()

    if is_terminal:
        expected_outcome = _TERMINAL_OUTCOME_MAP.get(target_state)
        if expected_outcome is None:
            raise InvariantViolationError(
                "<unknown>",
                f"no mapping for terminal {target_state.value}",
            )
        if outcome != expected_outcome:
            raise InvariantViolationError(
                "<unknown>",
                f"{target_state.value} requires {expected_outcome.value}, got {outcome}",
            )
        if (
            target_state is RunState.FAILED_TERMINAL
            and failure_code != ReasonCode.ATTEMPTS_EXHAUSTED
        ):
            raise InvariantViolationError(
                "<unknown>",
                f"FAILED_TERMINAL needs ATTEMPTS_EXHAUSTED, got {failure_code}",
            )
        if (
            required_reason is not None
            and failure_code is not None
            and failure_code != required_reason
        ):
            raise InvariantViolationError(
                "<unknown>",
                f"failure_code {failure_code} != required {required_reason}",
            )
    elif outcome is not None:
        raise InvariantViolationError(
            "<unknown>",
            f"non-terminal {target_state.value} must have outcome None, got {outcome}",
        )


def _transition_run(  # noqa: PLR0912
    session: Session,
    *,
    run_id: str,
    event: TransitionEvent,
    actor: RunActor,
    expected_version: int,
    reason_code: ReasonCode | None = None,
    outcome: RunOutcome | None = None,
    failure_code: ReasonCode | None = None,
    occurred_at: datetime,
) -> tuple[RunRow, RunEventRow]:
    """Atomically persist transition and audit event in caller's transaction.

    Caller must have started BEGIN IMMEDIATE before first read for SQLite
    write-write handling. This function never begins, commits, or rolls back.
    """
    if not run_id or len(run_id) > _MAX_RUN_ID_LEN:
        raise InvariantViolationError(run_id, "invalid run_id length")
    if expected_version < 0:
        raise InvariantViolationError(run_id, "expected_version must be >=0")
    if occurred_at.tzinfo is None or occurred_at.tzinfo.utcoffset(occurred_at) is None:
        raise InvariantViolationError(run_id, "occurred_at must be timezone-aware")

    run: RunRow | None = session.get(RunRow, run_id)
    if run is None:
        raise RunNotFoundError(run_id)

    if run.row_version != expected_version:
        raise ConcurrencyConflictError(run_id, expected_version)

    try:
        trans = get_transition(run.state, event)
    except TransitionError as exc:
        raise InvalidTransitionError(run_id, exc.source, exc.event) from None

    if event is TransitionEvent.RETRY:
        raise UnsupportedTransitionError(run_id, event, "RETRY excluded from V1-01a1")

    if actor != trans.actor:
        raise InvariantViolationError(
            run_id,
            f"actor {actor.value} != required {trans.actor.value} for {event.value}",
        )

    if trans.required_reason is not None:
        if reason_code != trans.required_reason:
            raise InvariantViolationError(
                run_id,
                f"{event.value} needs {trans.required_reason.value}, got {reason_code}",
            )
    elif reason_code is not None:
        raise InvariantViolationError(
            run_id,
            f"{event.value} needs no reason, got {reason_code}",
        )

    try:
        _validate_outcome_mapping(trans.target, outcome, failure_code, trans.required_reason)
    except InvariantViolationError as exc:
        raise InvariantViolationError(run_id, exc.detail) from None

    max_seq = session.scalar(
        select(func.coalesce(func.max(RunEventRow.seq), 0)).where(RunEventRow.run_id == run_id)
    )
    new_seq = int(max_seq or 0) + 1
    if new_seq < 1:
        raise AuditConstraintError(run_id, "invalid seq allocation")

    stmt = (
        update(RunRow)
        .where(RunRow.run_id == run_id, RunRow.row_version == expected_version)
        .values(
            state=trans.target,
            row_version=expected_version + 1,
            outcome=outcome,
            failure_code=failure_code,
            updated_at=occurred_at,
            finished_at=occurred_at if trans.target in terminal_states() else None,
        )
    )
    result = session.execute(stmt)
    if result.rowcount != 1:
        raise ConcurrencyConflictError(run_id, expected_version)

    run.state = trans.target
    run.row_version = expected_version + 1
    run.outcome = outcome
    run.failure_code = failure_code
    run.updated_at = occurred_at
    run.finished_at = occurred_at if trans.target in terminal_states() else None

    event_row = RunEventRow(
        run_id=run_id,
        seq=new_seq,
        occurred_at=occurred_at,
        from_state=trans.source,
        to_state=trans.target,
        event=event,
        actor=actor,
        reason_code=reason_code,
        detail_json=None,
        detail_sha256=None,
    )
    session.add(event_row)

    try:
        session.flush()
    except IntegrityError:
        raise AuditConstraintError(run_id) from None

    return run, event_row
