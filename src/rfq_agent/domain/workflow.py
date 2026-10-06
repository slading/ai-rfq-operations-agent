"""Workflow state machine contract (architecture §6).

Phase 0 defines the states, the events, the allowed transitions and the
invariants. Phase 2 implements the executor that actually moves a run, persists
events and enforces guards against the database.

The transition table below is *data*, which means it can be tested exhaustively
before a single line of execution code exists: every legal transition must be
accepted, every illegal one refused, and the invariants that make the audit
trail trustworthy must hold.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import RfqId, RunId
from rfq_agent.domain.values import DomainModel

__all__ = [
    "INITIAL_RUN_STATE",
    "TRANSITIONS",
    "ReasonCode",
    "RunActor",
    "RunOutcome",
    "RunState",
    "RunTrigger",
    "StateTransition",
    "TransitionError",
    "TransitionEvent",
    "allowed_transitions",
    "is_allowed",
    "replay",
    "terminal_states",
    "validate_transition_table",
]


class RunState(StrEnum):
    """Execution states of one run (architecture §6.2)."""

    RECEIVED = "RECEIVED"
    TRIAGING = "TRIAGING"
    EXTRACTING = "EXTRACTING"
    RESOLVING = "RESOLVING"
    VALIDATING = "VALIDATING"
    CALCULATING = "CALCULATING"
    PREPARING = "PREPARING"
    READY_TO_SEND = "READY_TO_SEND"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    NEEDS_HUMAN_INPUT = "NEEDS_HUMAN_INPUT"
    SENT = "SENT"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    NOT_AN_RFQ = "NOT_AN_RFQ"
    DUPLICATE = "DUPLICATE"
    SUPERSEDED = "SUPERSEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"


#: Where every run starts.
INITIAL_RUN_STATE = RunState.RECEIVED


class TransitionEvent(StrEnum):
    """Events that drive state transitions."""

    TRIAGE_START = "triage_start"
    CLASSIFIED_RFQ = "classified_rfq"
    CLASSIFIED_NOT_RFQ = "classified_not_rfq"
    CLASSIFIED_UNCERTAIN = "classified_uncertain"
    DUPLICATE_DETECTED = "duplicate_detected"
    EXTRACTED = "extracted"
    EXTRACT_INVALID = "extract_invalid"
    DRAFT_VALID = "draft_valid"
    DRAFT_INVALID = "draft_invalid"
    STEP_BUDGET_EXCEEDED = "step_budget_exceeded"
    NO_LINE_ITEMS = "no_line_items"
    COMPLETE_AND_RESOLVED = "complete_and_resolved"
    AMBIGUITY_OR_GAP = "ambiguity_or_gap"
    QUOTE_COMPUTED = "quote_computed"
    PRICE_MISSING = "price_missing"
    DRAFT_READY = "draft_ready"
    AUTO_ADVANCE = "auto_advance"
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    REQUEST_INFO = "request_info"
    HUMAN_RESOLVED = "human_resolved"
    HUMAN_ESCALATES = "human_escalates"
    PROVIDER_RETRY_EXHAUSTED = "provider_retryable_exhausted"
    TOOL_UNAVAILABLE = "tool_unavailable"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"
    NEW_RUN_STARTED = "new_run_started"
    RETRY = "retry"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class ReasonCode(StrEnum):
    """Machine-readable reasons attached to transitions and failures (§7)."""

    NOT_AN_RFQ = "NOT_AN_RFQ"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    DUPLICATE = "DUPLICATE"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    NO_ITEMS_EXTRACTED = "NO_ITEMS_EXTRACTED"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    UNKNOWN_SKU = "UNKNOWN_SKU"
    MISSING_QTY = "MISSING_QTY"
    CUSTOMER_UNRESOLVED = "CUSTOMER_UNRESOLVED"
    CUSTOMER_AMBIGUOUS = "CUSTOMER_AMBIGUOUS"
    PRICE_MISSING = "PRICE_MISSING"
    STOCK_INSUFFICIENT = "STOCK_INSUFFICIENT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TOOL_FAILURE = "TOOL_FAILURE"
    TIMEOUT = "TIMEOUT"
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_ESCALATED = "HUMAN_ESCALATED"
    SUPERSEDED = "SUPERSEDED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"


class RunActor(StrEnum):
    """Who caused a transition. Recorded on every audit event."""

    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    TOOL = "TOOL"
    HUMAN = "HUMAN"


class RunTrigger(StrEnum):
    """Why a run exists."""

    INITIAL = "INITIAL"
    HUMAN_RERUN = "HUMAN_RERUN"
    SUPERSEDED_BY_CORRECTION = "SUPERSEDED_BY_CORRECTION"
    RETRY = "RETRY"


class RunOutcome(StrEnum):
    """Terminal outcome of a run."""

    SUCCEEDED = "SUCCEEDED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    NOT_AN_RFQ = "NOT_AN_RFQ"
    DUPLICATE = "DUPLICATE"
    ESCALATED = "ESCALATED"


class StateTransition(DomainModel):
    """One edge of the state machine.

    ``retry_target`` is set for the single non-linear edge in the graph: a retry
    resumes at the last successful checkpoint rather than restarting the run.
    """

    source: RunState
    event: TransitionEvent
    target: RunState
    actor: RunActor = RunActor.SYSTEM
    #: Required reason code for this edge, where the architecture names one.
    required_reason: ReasonCode | None = None
    #: Human-readable guard, mirrored by an executable check in Phase 2.
    guard: Annotated[str, StringConstraints(min_length=1, max_length=200)] = "none"
    #: For ``RETRY``: resume here instead of ``target``.
    retry_target: RunState | None = None

    @model_validator(mode="after")
    def _check_retry_target(self) -> Self:
        """Only the retry edge may redirect to a checkpoint."""
        if self.retry_target is not None and self.event is not TransitionEvent.RETRY:
            msg = "retry_target is only valid on the retry edge"
            raise ValueError(msg)
        if self.event is TransitionEvent.RETRY and self.retry_target is None:
            msg = "the retry edge must declare a retry_target"
            raise ValueError(msg)
        return self

    @property
    def key(self) -> tuple[RunState, TransitionEvent]:
        """Lookup key for this edge."""
        return (self.source, self.event)


#: The complete V1 transition table (architecture §6.3).
TRANSITIONS: tuple[StateTransition, ...] = (
    # --- intake / triage ---------------------------------------------------
    StateTransition(
        source=RunState.RECEIVED,
        event=TransitionEvent.TRIAGE_START,
        target=RunState.TRIAGING,
        guard="run claimed by the worker",
    ),
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.CLASSIFIED_RFQ,
        target=RunState.EXTRACTING,
        actor=RunActor.AGENT,
        guard="is_rfq is true and no INJECTION_BLOCKED",
    ),
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.CLASSIFIED_NOT_RFQ,
        target=RunState.NOT_AN_RFQ,
        actor=RunActor.AGENT,
        required_reason=ReasonCode.NOT_AN_RFQ,
        guard="is_rfq is false with confidence >= threshold",
    ),
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.CLASSIFIED_UNCERTAIN,
        target=RunState.NEEDS_HUMAN_INPUT,
        actor=RunActor.AGENT,
        required_reason=ReasonCode.LOW_CONFIDENCE,
        guard="triage confidence below threshold",
    ),
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.DUPLICATE_DETECTED,
        target=RunState.DUPLICATE,
        required_reason=ReasonCode.DUPLICATE,
        guard="idempotency key already exists",
    ),
    # --- extraction --------------------------------------------------------
    StateTransition(
        source=RunState.EXTRACTING,
        event=TransitionEvent.EXTRACTED,
        target=RunState.RESOLVING,
        actor=RunActor.AGENT,
        guard="triage result and raw lines persisted",
    ),
    StateTransition(
        source=RunState.EXTRACTING,
        event=TransitionEvent.EXTRACT_INVALID,
        target=RunState.NEEDS_HUMAN_INPUT,
        required_reason=ReasonCode.MALFORMED_MODEL_OUTPUT,
        guard="schema repair retries exhausted",
    ),
    StateTransition(
        source=RunState.EXTRACTING,
        event=TransitionEvent.NO_LINE_ITEMS,
        target=RunState.NEEDS_HUMAN_INPUT,
        actor=RunActor.AGENT,
        required_reason=ReasonCode.NO_ITEMS_EXTRACTED,
        guard="extraction produced no line items",
    ),
    # --- resolution --------------------------------------------------------
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.DRAFT_VALID,
        target=RunState.VALIDATING,
        actor=RunActor.AGENT,
        guard="all ids grounded and all evidence spans verbatim",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.DRAFT_INVALID,
        target=RunState.NEEDS_HUMAN_INPUT,
        required_reason=ReasonCode.MALFORMED_MODEL_OUTPUT,
        guard="two repair attempts failed",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.STEP_BUDGET_EXCEEDED,
        target=RunState.NEEDS_HUMAN_INPUT,
        required_reason=ReasonCode.STEP_BUDGET_EXCEEDED,
        guard="agent step budget exhausted",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.AMBIGUITY_OR_GAP,
        target=RunState.NEEDS_HUMAN_INPUT,
        actor=RunActor.AGENT,
        guard="unresolved ambiguity, unknown SKU, missing qty or customer",
    ),
    # --- validation / calculation / preparation ----------------------------
    StateTransition(
        source=RunState.VALIDATING,
        event=TransitionEvent.COMPLETE_AND_RESOLVED,
        target=RunState.CALCULATING,
        guard="completeness check reports no blockers",
    ),
    StateTransition(
        source=RunState.VALIDATING,
        event=TransitionEvent.AMBIGUITY_OR_GAP,
        target=RunState.NEEDS_HUMAN_INPUT,
        guard="completeness check reports blockers",
    ),
    StateTransition(
        source=RunState.CALCULATING,
        event=TransitionEvent.QUOTE_COMPUTED,
        target=RunState.PREPARING,
        guard="calculator returned and inputs_sha256 persisted",
    ),
    StateTransition(
        source=RunState.CALCULATING,
        event=TransitionEvent.PRICE_MISSING,
        target=RunState.NEEDS_HUMAN_INPUT,
        required_reason=ReasonCode.PRICE_MISSING,
        guard="a required price could not be found",
    ),
    StateTransition(
        source=RunState.PREPARING,
        event=TransitionEvent.DRAFT_READY,
        target=RunState.READY_TO_SEND,
        guard="template rendered and canary scan clean",
    ),
    StateTransition(
        source=RunState.READY_TO_SEND,
        event=TransitionEvent.AUTO_ADVANCE,
        target=RunState.AWAITING_REVIEW,
        guard="always taken in V1 - there is no auto-send path",
    ),
    # --- human review ------------------------------------------------------
    StateTransition(
        source=RunState.AWAITING_REVIEW,
        event=TransitionEvent.APPROVE,
        target=RunState.SENT,
        actor=RunActor.HUMAN,
        guard="state guard, fresh row_version, idempotency key, actor present",
    ),
    StateTransition(
        source=RunState.AWAITING_REVIEW,
        event=TransitionEvent.EDIT,
        target=RunState.CALCULATING,
        actor=RunActor.HUMAN,
        guard="edited inputs differ, diff recorded, quote recomputed",
    ),
    StateTransition(
        source=RunState.AWAITING_REVIEW,
        event=TransitionEvent.REJECT,
        target=RunState.REJECTED,
        actor=RunActor.HUMAN,
        required_reason=ReasonCode.HUMAN_REJECTED,
        guard="reason code mandatory",
    ),
    StateTransition(
        source=RunState.AWAITING_REVIEW,
        event=TransitionEvent.REQUEST_INFO,
        target=RunState.NEEDS_HUMAN_INPUT,
        actor=RunActor.HUMAN,
        guard="clarification draft attached",
    ),
    StateTransition(
        source=RunState.NEEDS_HUMAN_INPUT,
        event=TransitionEvent.HUMAN_RESOLVED,
        target=RunState.EXTRACTING,
        actor=RunActor.HUMAN,
        guard="creates a new run (attempt_no+1); prior run becomes terminal",
    ),
    StateTransition(
        source=RunState.NEEDS_HUMAN_INPUT,
        event=TransitionEvent.HUMAN_ESCALATES,
        target=RunState.ESCALATED,
        actor=RunActor.HUMAN,
        required_reason=ReasonCode.HUMAN_ESCALATED,
        guard="human decision to escalate",
    ),
    # --- failure handling --------------------------------------------------
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.PROVIDER_RETRY_EXHAUSTED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.RATE_LIMITED,
        guard="provider retries exhausted",
    ),
    StateTransition(
        source=RunState.EXTRACTING,
        event=TransitionEvent.PROVIDER_RETRY_EXHAUSTED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.RATE_LIMITED,
        guard="provider retries exhausted",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.PROVIDER_RETRY_EXHAUSTED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.RATE_LIMITED,
        guard="provider retries exhausted",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.TOOL_UNAVAILABLE,
        target=RunState.FAILED_RETRYABLE,
        actor=RunActor.TOOL,
        required_reason=ReasonCode.TOOL_FAILURE,
        guard="essential tool still failing after retries",
    ),
    StateTransition(
        source=RunState.CALCULATING,
        event=TransitionEvent.TOOL_UNAVAILABLE,
        target=RunState.FAILED_RETRYABLE,
        actor=RunActor.TOOL,
        required_reason=ReasonCode.TOOL_FAILURE,
        guard="essential tool still failing after retries",
    ),
    StateTransition(
        source=RunState.TRIAGING,
        event=TransitionEvent.RUN_BUDGET_EXCEEDED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.TIMEOUT,
        guard="run budget exceeded",
    ),
    StateTransition(
        source=RunState.EXTRACTING,
        event=TransitionEvent.RUN_BUDGET_EXCEEDED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.TIMEOUT,
        guard="run budget exceeded",
    ),
    StateTransition(
        source=RunState.RESOLVING,
        event=TransitionEvent.RUN_BUDGET_EXCEEDED,
        target=RunState.FAILED_RETRYABLE,
        required_reason=ReasonCode.TIMEOUT,
        guard="run budget exceeded",
    ),
    StateTransition(
        source=RunState.FAILED_RETRYABLE,
        event=TransitionEvent.RETRY,
        target=RunState.FAILED_RETRYABLE,
        retry_target=RunState.TRIAGING,
        guard="attempts < max; resumes at the last checkpoint stage",
    ),
    StateTransition(
        source=RunState.FAILED_RETRYABLE,
        event=TransitionEvent.ATTEMPTS_EXHAUSTED,
        target=RunState.FAILED_TERMINAL,
        required_reason=ReasonCode.ATTEMPTS_EXHAUSTED,
        guard="retry attempts exhausted",
    ),
    # --- supersession ------------------------------------------------------
    *[
        StateTransition(
            source=state,
            event=TransitionEvent.NEW_RUN_STARTED,
            target=RunState.SUPERSEDED,
            required_reason=ReasonCode.SUPERSEDED,
            guard="a newer run was started for the same RFQ",
        )
        for state in (
            RunState.RECEIVED,
            RunState.TRIAGING,
            RunState.EXTRACTING,
            RunState.RESOLVING,
            RunState.VALIDATING,
            RunState.CALCULATING,
            RunState.PREPARING,
            RunState.READY_TO_SEND,
            RunState.AWAITING_REVIEW,
            RunState.NEEDS_HUMAN_INPUT,
            RunState.FAILED_RETRYABLE,
        )
    ],
)

_INDEX: dict[tuple[RunState, TransitionEvent], StateTransition] = {
    transition.key: transition for transition in TRANSITIONS
}


def terminal_states() -> frozenset[RunState]:
    """States with no outgoing transition. Derived, never declared twice."""
    sources = {transition.source for transition in TRANSITIONS}
    return frozenset(state for state in RunState if state not in sources)


def allowed_transitions(source: RunState) -> tuple[StateTransition, ...]:
    """Every transition available from ``source``."""
    return tuple(t for t in TRANSITIONS if t.source is source)


def is_allowed(source: RunState, event: TransitionEvent) -> bool:
    """Whether ``event`` may fire in ``source``."""
    return (source, event) in _INDEX


def get_transition(source: RunState, event: TransitionEvent) -> StateTransition:
    """Return the transition for an edge, or raise :class:`TransitionError`."""
    try:
        return _INDEX[(source, event)]
    except KeyError:
        msg = f"illegal transition: {event} is not allowed in state {source}"
        raise TransitionError(msg, source=source, event=event) from None


class TransitionError(RuntimeError):
    """Raised when a transition is not permitted."""

    def __init__(self, message: str, *, source: RunState, event: TransitionEvent) -> None:
        super().__init__(message)
        self.source = source
        self.event = event


def replay(events: tuple[tuple[RunState, TransitionEvent], ...]) -> RunState:
    """Fold a sequence of transitions into a final state.

    This is the executable form of invariant 4 (§6.3): a run's state must be
    recoverable from its event log alone. Phase 2 asserts this against the
    persisted ``runs.state`` column for every completed run.
    """
    state = INITIAL_RUN_STATE
    for source, event in events:
        if source is not state:
            msg = f"event log gap: expected source {state}, got {source}"
            raise TransitionError(msg, source=source, event=event)
        state = get_transition(source, event).target
    return state


def validate_transition_table() -> tuple[str, ...]:
    """Check the transition table's structural invariants.

    Returns a tuple of violation descriptions; an empty tuple means the table
    is consistent. Run as a unit test in Phase 0 and again at engine startup in
    Phase 2, so the contract cannot silently drift.
    """
    problems: list[str] = []

    if INITIAL_RUN_STATE in terminal_states():
        problems.append(f"initial state {INITIAL_RUN_STATE} must not be terminal")

    expected_sent = {(RunState.AWAITING_REVIEW, TransitionEvent.APPROVE)}
    actual_sent = {t.key for t in TRANSITIONS if t.target is RunState.SENT}
    if actual_sent != expected_sent:
        problems.append(f"SENT must be reachable only via approve, got {sorted(actual_sent)}")

    expected_rejected = {(RunState.AWAITING_REVIEW, TransitionEvent.REJECT)}
    actual_rejected = {t.key for t in TRANSITIONS if t.target is RunState.REJECTED}
    if actual_rejected != expected_rejected:
        problems.append(
            f"REJECTED must be reachable only via reject, got {sorted(actual_rejected)}"
        )

    keys = [t.key for t in TRANSITIONS]
    duplicates: set[tuple[RunState, TransitionEvent]] = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        problems.append(f"duplicate transition keys: {sorted(duplicates)}")

    for transition in TRANSITIONS:
        if transition.source in terminal_states():
            problems.append(f"{transition.source} is terminal but has an outgoing transition")
        if transition.target is transition.source and transition.retry_target is None:
            problems.append(f"self-loop without retry_target: {transition.key}")

    # Every state must be reachable from the initial state.
    reachable = {INITIAL_RUN_STATE}
    frontier = {INITIAL_RUN_STATE}
    while frontier:
        current = frontier.pop()
        for transition in allowed_transitions(current):
            for target in (transition.target, transition.retry_target):
                if target is not None and target not in reachable:
                    reachable.add(target)
                    frontier.add(target)
    unreachable = set(RunState) - reachable
    if unreachable:
        problems.append(f"unreachable states: {sorted(unreachable)}")

    return tuple(problems)


class RunRecord(DomainModel):
    """In-memory shape of a run (persisted in Phase 1/2).

    Declared here so that the state machine has something to operate on and so
    the Phase 1 SQLAlchemy model has an unambiguous target schema.
    """

    run_id: RunId
    rfq_id: RfqId
    attempt_no: Annotated[int, Field(ge=1)] = 1
    trigger: RunTrigger = RunTrigger.INITIAL
    state: RunState = INITIAL_RUN_STATE
    prior_run_id: RunId | None = None
    outcome: RunOutcome | None = None
    failure_code: ReasonCode | None = None
    retry_count: Annotated[int, Field(ge=0)] = 0
    checkpoint_stage: RunState | None = None
    row_version: Annotated[int, Field(ge=0)] = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _check_outcome_consistency(self) -> Self:
        """An outcome may only exist for a terminal state."""
        if self.outcome is not None and self.state not in terminal_states():
            msg = f"outcome is only valid in a terminal state, not {self.state}"
            raise ValueError(msg)
        if self.state in terminal_states() and self.outcome is None:
            msg = f"a terminal state ({self.state}) requires an outcome"
            raise ValueError(msg)
        if self.retry_count > 0 and self.checkpoint_stage is None:
            msg = "checkpoint_stage is required once a run has been retried"
            raise ValueError(msg)
        return self


class RunEventRecord(DomainModel):
    """One append-only audit row (``run_events``).

    There is no update or delete path for these records anywhere in the codebase.
    """

    run_id: RunId
    seq: Annotated[int, Field(ge=1)]
    occurred_at: datetime
    from_state: RunState
    to_state: RunState
    event: TransitionEvent
    actor: RunActor = RunActor.SYSTEM
    reason_code: ReasonCode | None = None
    detail_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def _check_edge_is_legal(self) -> Self:
        """An audit row must correspond to a real edge in the table."""
        if not is_allowed(self.from_state, self.event):
            msg = f"{self.event} is not allowed from {self.from_state}"
            raise ValueError(msg)
        if get_transition(self.from_state, self.event).target is not self.to_state:
            msg = f"{self.from_state} --{self.event}--> {self.to_state} is not a legal edge"
            raise ValueError(msg)
        required = get_transition(self.from_state, self.event).required_reason
        if required is not None and self.reason_code is None:
            msg = f"reason_code {required} is required for this transition"
            raise ValueError(msg)
        return self
