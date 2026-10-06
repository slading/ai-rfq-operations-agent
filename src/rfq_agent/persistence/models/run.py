"""Run, audit-trail, queue and idempotency records (§5.3, §6).

The database is the arbiter of three guarantees that the Python layer can only
*intend*:

* **A run is in exactly one state, and its history is append-only.**
  ``runs.state`` is the current position; ``run_events`` is the history. The
  migration installs triggers that abort ``UPDATE`` and ``DELETE`` on
  ``run_events``, so even a hand-written SQL statement cannot rewrite history.
* **A terminal run has an outcome.** The checkpoint in
  :class:`~rfq_agent.domain.workflow.RunRecord` is mirrored as a ``CHECK``
  constraint, so a run cannot be marked ``SENT`` without saying how it ended.
* **A key is acted on at most once.** ``idempotency_claims`` backs the
  :class:`~rfq_agent.contracts.ports.IdempotencyStore` port; a double-clicked
  APPROVE and a re-delivered e-mail both fail to claim a key they already used.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.domain.values import Json
from rfq_agent.domain.workflow import (
    TRANSITIONS,
    ReasonCode,
    RunActor,
    RunOutcome,
    RunState,
    RunTrigger,
    TransitionEvent,
    terminal_states,
)
from rfq_agent.observability.ids import utc_now
from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.enums import QueueEntryStatus
from rfq_agent.persistence.types import JSON_PAYLOAD, UtcDateTime, enum_type

# Foreign-key columns carry an explicit type as well as a ``ForeignKey``: the
# target table may not be registered in the metadata when this module is
# imported on its own, and an unresolvable target would otherwise leave the
# column typed ``NullType``.

__all__ = [
    "IdempotencyClaimRow",
    "RunEventRow",
    "RunQueueEntryRow",
    "RunRow",
]

#: Terminal states, rendered as SQL literals **derived from the transition
#: table** so the constraint and the state machine cannot disagree by accident.
#:
#: The migration carries this list as frozen SQL text. If a future phase adds or
#: renames a terminal state, ``tests/persistence/test_constraints.py`` fails -
#: that test walks :func:`~rfq_agent.domain.workflow.terminal_states` and asserts
#: the database agrees - which is the prompt to write a migration.
_TERMINAL_STATES_SQL = ", ".join(
    f"'{state.value}'" for state in sorted(terminal_states(), key=lambda item: item.value)
)

_TERMINAL_REQUIRES_OUTCOME = (
    f"(state IN ({_TERMINAL_STATES_SQL}) AND outcome IS NOT NULL) "
    f"OR (state NOT IN ({_TERMINAL_STATES_SQL}) AND outcome IS NULL)"
)


def _legal_edge_check_sql() -> str:
    """Build the ``CHECK`` text that restricts ``run_events`` to legal edges.

    Derived from :data:`~rfq_agent.domain.workflow.TRANSITIONS`, so the audit
    trail cannot contain an edge the state machine does not define - not even if
    a repair script or a buggy executor writes it by hand. Expressed as an
    ``OR`` chain over the 45 edges because SQLite forbids subqueries in a
    ``CHECK``; the alternative, a trigger, would have to re-derive the table and
    hide the rule from the schema.
    """
    clauses = [
        f"(from_state = '{transition.source.value}'"
        f" AND event = '{transition.event.value}'"
        f" AND to_state = '{transition.target.value}')"
        for transition in TRANSITIONS
    ]
    return " OR ".join(clauses)


#: The state machine, as a database constraint. A later phase that edits the
#: transition table without writing a migration fails
#: ``test_audit_row_must_describe_a_real_edge`` rather than corrupting history.
_LEGAL_EDGE_CHECK = _legal_edge_check_sql()


class RunRow(Base, TimestampMixin):
    """One execution attempt against an RFQ (``runs``)."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("rfq_id", "attempt_no", name="uq_runs_rfq_id_attempt_no"),
        CheckConstraint(_TERMINAL_REQUIRES_OUTCOME, name="terminal_requires_outcome"),
        CheckConstraint(
            "retry_count = 0 OR checkpoint_stage IS NOT NULL",
            name="retry_requires_checkpoint",
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        CheckConstraint("row_version >= 0", name="row_version_non_negative"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rfq_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rfqs.rfq_id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(nullable=False, default=1)
    trigger: Mapped[RunTrigger] = mapped_column(
        enum_type(RunTrigger, name="run_trigger"), nullable=False, default=RunTrigger.INITIAL
    )
    state: Mapped[RunState] = mapped_column(
        enum_type(RunState, name="run_state"), nullable=False, default=RunState.RECEIVED
    )
    prior_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[RunOutcome | None] = mapped_column(
        enum_type(RunOutcome, name="run_outcome"), nullable=True
    )
    failure_code: Mapped[ReasonCode | None] = mapped_column(
        enum_type(ReasonCode, name="reason_code"), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    checkpoint_stage: Mapped[RunState | None] = mapped_column(
        enum_type(RunState, name="run_state"), nullable=True
    )
    #: Optimistic-lock counter. The executor updates with
    #: ``WHERE run_id = :id AND row_version = :expected`` and treats a zero row
    #: count as "someone else moved this run" rather than as success.
    row_version: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index("ix_runs_rfq_id", RunRow.rfq_id)
Index("ix_runs_state", RunRow.state)
Index("ix_runs_state_created_at", RunRow.state, RunRow.created_at)


class RunEventRow(Base):
    """One append-only audit row (``run_events``).

    There is no update or delete path for these records anywhere in the
    codebase, and the database enforces that rather than trusting the codebase.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(_LEGAL_EDGE_CHECK, name="legal_edge"),
        CheckConstraint(
            "detail_sha256 IS NULL OR length(detail_sha256) = 64", name="detail_sha256_len"
        ),
    )

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    from_state: Mapped[RunState] = mapped_column(
        enum_type(RunState, name="run_state"), nullable=False
    )
    to_state: Mapped[RunState] = mapped_column(
        enum_type(RunState, name="run_state"), nullable=False
    )
    event: Mapped[TransitionEvent] = mapped_column(
        enum_type(TransitionEvent, name="transition_event"), nullable=False
    )
    actor: Mapped[RunActor] = mapped_column(
        enum_type(RunActor, name="run_actor"), nullable=False, default=RunActor.SYSTEM
    )
    reason_code: Mapped[ReasonCode | None] = mapped_column(
        enum_type(ReasonCode, name="reason_code"), nullable=True
    )
    #: Redacted detail. Anything that could carry customer text is hashed.
    detail_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    detail_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunQueueEntryRow(Base):
    """The single in-process worker's work queue (``run_queue``).

    One row per run, keyed by ``run_id``: ``INSERT ... ON CONFLICT DO NOTHING``
    makes enqueueing idempotent, so a retry of the API call that enqueued a run
    cannot double-process it. The lease columns exist so that a worker that
    crashes mid-run does not strand the run forever.
    """

    __tablename__ = "run_queue"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "(status = 'LEASED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'LEASED' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_consistency",
        ),
        CheckConstraint(
            "status <> 'COMPLETED' OR completed_at IS NOT NULL",
            name="completed_at_required",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    status: Mapped[QueueEntryStatus] = mapped_column(
        enum_type(QueueEntryStatus, name="queue_entry_status"),
        nullable=False,
        default=QueueEntryStatus.QUEUED,
    )
    #: Higher runs first; equal priorities are served oldest-first.
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    enqueued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)
    #: Retry backoff target: a row is only eligible once ``available_at`` passes.
    available_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index("ix_run_queue_status_available_at", RunQueueEntryRow.status, RunQueueEntryRow.available_at)


class IdempotencyClaimRow(Base):
    """Backing store for the :class:`~rfq_agent.contracts.ports.IdempotencyStore` port.

    The primary key is the pair ``(scope, key)``: the same opaque key may
    legitimately mean different things in different scopes (an intake key and a
    human-action key are generated from different inputs), but within a scope a
    key is claimed exactly once.
    """

    __tablename__ = "idempotency_claims"
    __table_args__ = (
        CheckConstraint(
            "expires_at IS NULL OR expires_at >= claimed_at", name="expiry_after_claim"
        ),
        CheckConstraint("length(claim_key) >= 8", name="claim_key_min_length"),
    )

    scope: Mapped[str] = mapped_column(String(64), primary_key=True, autoincrement=False)
    claim_key: Mapped[str] = mapped_column(String(256), primary_key=True, autoincrement=False)
    claimed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
