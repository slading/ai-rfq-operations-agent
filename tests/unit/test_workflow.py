"""Tests for the workflow state machine contract (architecture §6).

The state machine is data, so it can be verified exhaustively before any
execution code exists. These tests are the Phase 2 acceptance criteria written
down early.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rfq_agent.domain.workflow import (
    INITIAL_RUN_STATE,
    TRANSITIONS,
    ReasonCode,
    RunActor,
    RunEventRecord,
    RunOutcome,
    RunRecord,
    RunState,
    RunTrigger,
    StateTransition,
    TransitionError,
    TransitionEvent,
    allowed_transitions,
    get_transition,
    is_allowed,
    replay,
    terminal_states,
    validate_transition_table,
)
from tests.conftest import utc

HAPPY_PATH: tuple[tuple[RunState, TransitionEvent], ...] = (
    (RunState.RECEIVED, TransitionEvent.TRIAGE_START),
    (RunState.TRIAGING, TransitionEvent.CLASSIFIED_RFQ),
    (RunState.EXTRACTING, TransitionEvent.EXTRACTED),
    (RunState.RESOLVING, TransitionEvent.DRAFT_VALID),
    (RunState.VALIDATING, TransitionEvent.COMPLETE_AND_RESOLVED),
    (RunState.CALCULATING, TransitionEvent.QUOTE_COMPUTED),
    (RunState.PREPARING, TransitionEvent.DRAFT_READY),
    (RunState.READY_TO_SEND, TransitionEvent.AUTO_ADVANCE),
    (RunState.AWAITING_REVIEW, TransitionEvent.APPROVE),
)


class TestTableInvariants:
    def test_table_is_self_consistent(self) -> None:
        assert validate_transition_table() == ()

    def test_initial_state_is_received(self) -> None:
        assert INITIAL_RUN_STATE is RunState.RECEIVED

    def test_terminal_states(self) -> None:
        assert terminal_states() == frozenset(
            {
                RunState.SENT,
                RunState.REJECTED,
                RunState.ESCALATED,
                RunState.NOT_AN_RFQ,
                RunState.DUPLICATE,
                RunState.SUPERSEDED,
                RunState.FAILED_TERMINAL,
            }
        )

    def test_awaiting_review_is_not_terminal(self) -> None:
        assert RunState.AWAITING_REVIEW not in terminal_states()
        assert RunState.NEEDS_HUMAN_INPUT not in terminal_states()

    def test_every_state_is_either_terminal_or_has_exits(self) -> None:
        for state in RunState:
            assert state in terminal_states() or allowed_transitions(state), state

    def test_transition_keys_are_unique(self) -> None:
        keys = [transition.key for transition in TRANSITIONS]
        assert len(keys) == len(set(keys))

    def test_every_transition_declares_a_guard(self) -> None:
        for transition in TRANSITIONS:
            assert transition.guard.strip(), transition.key

    def test_retry_target_only_on_the_retry_edge(self) -> None:
        for transition in TRANSITIONS:
            if transition.retry_target is not None:
                assert transition.event is TransitionEvent.RETRY


class TestTheHumanGate:
    def test_sent_is_reachable_only_via_approve(self) -> None:
        entries = [t for t in TRANSITIONS if t.target is RunState.SENT]
        assert len(entries) == 1
        assert entries[0].source is RunState.AWAITING_REVIEW
        assert entries[0].event is TransitionEvent.APPROVE
        assert entries[0].actor is RunActor.HUMAN

    def test_rejected_is_reachable_only_via_reject(self) -> None:
        entries = [t for t in TRANSITIONS if t.target is RunState.REJECTED]
        assert len(entries) == 1
        assert entries[0].event is TransitionEvent.REJECT
        assert entries[0].required_reason is ReasonCode.HUMAN_REJECTED

    def test_no_model_or_system_actor_can_reach_sent(self) -> None:
        for transition in TRANSITIONS:
            if transition.target is RunState.SENT:
                assert transition.actor is RunActor.HUMAN

    def test_ready_to_send_always_goes_to_review(self) -> None:
        exits = allowed_transitions(RunState.READY_TO_SEND)
        assert {(t.event, t.target) for t in exits} == {
            (TransitionEvent.AUTO_ADVANCE, RunState.AWAITING_REVIEW),
            (TransitionEvent.NEW_RUN_STARTED, RunState.SUPERSEDED),
        }
        advance = get_transition(RunState.READY_TO_SEND, TransitionEvent.AUTO_ADVANCE)
        assert advance.target is RunState.AWAITING_REVIEW

    def test_review_exits(self) -> None:
        events = {t.event for t in allowed_transitions(RunState.AWAITING_REVIEW)}
        assert events == {
            TransitionEvent.APPROVE,
            TransitionEvent.EDIT,
            TransitionEvent.REJECT,
            TransitionEvent.REQUEST_INFO,
            TransitionEvent.NEW_RUN_STARTED,
        }

    def test_edit_returns_to_calculation_not_to_send(self) -> None:
        transition = get_transition(RunState.AWAITING_REVIEW, TransitionEvent.EDIT)
        assert transition.target is RunState.CALCULATING


class TestLookupAndGuards:
    def test_is_allowed(self) -> None:
        assert is_allowed(RunState.AWAITING_REVIEW, TransitionEvent.APPROVE) is True
        assert is_allowed(RunState.TRIAGING, TransitionEvent.APPROVE) is False
        assert is_allowed(RunState.SENT, TransitionEvent.APPROVE) is False

    def test_illegal_transition_raises(self) -> None:
        with pytest.raises(TransitionError) as excinfo:
            get_transition(RunState.SENT, TransitionEvent.APPROVE)
        assert excinfo.value.source is RunState.SENT
        assert excinfo.value.event is TransitionEvent.APPROVE

    def test_retry_resumes_at_the_checkpoint(self) -> None:
        transition = get_transition(RunState.FAILED_RETRYABLE, TransitionEvent.RETRY)
        assert transition.retry_target is RunState.TRIAGING

    def test_attempts_exhausted_is_terminal(self) -> None:
        transition = get_transition(RunState.FAILED_RETRYABLE, TransitionEvent.ATTEMPTS_EXHAUSTED)
        assert transition.target is RunState.FAILED_TERMINAL

    @pytest.mark.parametrize(
        "state",
        [
            RunState.TRIAGING,
            RunState.EXTRACTING,
            RunState.RESOLVING,
            RunState.VALIDATING,
            RunState.CALCULATING,
            RunState.AWAITING_REVIEW,
            RunState.NEEDS_HUMAN_INPUT,
        ],
    )
    def test_supersession_is_available_from_active_states(self, state: RunState) -> None:
        assert is_allowed(state, TransitionEvent.NEW_RUN_STARTED) is True
        transition = get_transition(state, TransitionEvent.NEW_RUN_STARTED)
        assert transition.target is RunState.SUPERSEDED

    def test_supersession_is_not_available_from_terminal_states(self) -> None:
        for state in terminal_states():
            assert is_allowed(state, TransitionEvent.NEW_RUN_STARTED) is False

    def test_self_loop_is_declared_as_a_retry(self) -> None:
        for transition in TRANSITIONS:
            if transition.target is transition.source:
                assert transition.event is TransitionEvent.RETRY


class TestReplay:
    def test_happy_path_reaches_sent(self) -> None:
        assert replay(HAPPY_PATH) is RunState.SENT

    def test_rejection_path(self) -> None:
        path = (*HAPPY_PATH[:-1], (RunState.AWAITING_REVIEW, TransitionEvent.REJECT))
        assert replay(path) is RunState.REJECTED

    def test_escalation_path(self) -> None:
        path: tuple[tuple[RunState, TransitionEvent], ...] = (
            (RunState.RECEIVED, TransitionEvent.TRIAGE_START),
            (RunState.TRIAGING, TransitionEvent.CLASSIFIED_RFQ),
            (RunState.EXTRACTING, TransitionEvent.EXTRACTED),
            (RunState.RESOLVING, TransitionEvent.DRAFT_INVALID),
            (RunState.NEEDS_HUMAN_INPUT, TransitionEvent.HUMAN_ESCALATES),
        )
        assert replay(path) is RunState.ESCALATED

    def test_duplicate_path(self) -> None:
        path: tuple[tuple[RunState, TransitionEvent], ...] = (
            (RunState.RECEIVED, TransitionEvent.TRIAGE_START),
            (RunState.TRIAGING, TransitionEvent.DUPLICATE_DETECTED),
        )
        assert replay(path) is RunState.DUPLICATE

    def test_gap_in_the_log_is_detected(self) -> None:
        with pytest.raises(TransitionError, match="event log gap"):
            replay(
                (
                    (RunState.RECEIVED, TransitionEvent.TRIAGE_START),
                    (RunState.EXTRACTING, TransitionEvent.EXTRACTED),
                )
            )

    def test_illegal_event_is_detected(self) -> None:
        with pytest.raises(TransitionError, match="not allowed"):
            replay(((RunState.RECEIVED, TransitionEvent.APPROVE),))

    def test_empty_log_stays_at_the_initial_state(self) -> None:
        assert replay(()) is INITIAL_RUN_STATE


class TestRunEventRecord:
    def make_event(self, **overrides: object) -> RunEventRecord:
        payload: dict[str, object] = {
            "run_id": "RUN-0001",
            "seq": 1,
            "occurred_at": utc(),
            "from_state": RunState.AWAITING_REVIEW,
            "to_state": RunState.SENT,
            "event": TransitionEvent.APPROVE,
            "actor": RunActor.HUMAN,
        }
        payload.update(overrides)
        return RunEventRecord.model_validate(payload)

    def test_legal_edge_is_recorded(self) -> None:
        assert self.make_event().to_state is RunState.SENT

    def test_illegal_edge_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not allowed"):
            self.make_event(from_state=RunState.TRIAGING)

    def test_wrong_target_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a legal edge"):
            self.make_event(to_state=RunState.REJECTED)

    def test_required_reason_must_be_present(self) -> None:
        with pytest.raises(ValidationError, match="reason_code"):
            self.make_event(
                from_state=RunState.AWAITING_REVIEW,
                to_state=RunState.REJECTED,
                event=TransitionEvent.REJECT,
            )

    def test_required_reason_is_accepted(self) -> None:
        event = self.make_event(
            from_state=RunState.AWAITING_REVIEW,
            to_state=RunState.REJECTED,
            event=TransitionEvent.REJECT,
            reason_code=ReasonCode.HUMAN_REJECTED,
        )
        assert event.reason_code is ReasonCode.HUMAN_REJECTED


class TestRunRecord:
    def make_run(self, **overrides: object) -> RunRecord:
        payload: dict[str, object] = {
            "run_id": "RUN-0001",
            "rfq_id": "RFQ-0001",
            "state": RunState.AWAITING_REVIEW,
        }
        payload.update(overrides)
        return RunRecord.model_validate(payload)

    def test_active_run_needs_no_outcome(self) -> None:
        assert self.make_run().outcome is None

    def test_outcome_requires_a_terminal_state(self) -> None:
        with pytest.raises(ValidationError, match="terminal state"):
            self.make_run(outcome=RunOutcome.SUCCEEDED)

    def test_terminal_state_requires_an_outcome(self) -> None:
        with pytest.raises(ValidationError, match="requires an outcome"):
            self.make_run(state=RunState.SENT)

    def test_sent_run_with_outcome_is_valid(self) -> None:
        run = self.make_run(state=RunState.SENT, outcome=RunOutcome.SUCCEEDED)
        assert run.outcome is RunOutcome.SUCCEEDED

    def test_retry_requires_a_checkpoint(self) -> None:
        with pytest.raises(ValidationError, match="checkpoint_stage is required"):
            self.make_run(retry_count=1)

    def test_retry_with_checkpoint_is_valid(self) -> None:
        run = self.make_run(retry_count=2, checkpoint_stage=RunState.RESOLVING)
        assert run.trigger is RunTrigger.INITIAL

    def test_attempt_number_starts_at_one(self) -> None:
        with pytest.raises(ValidationError):
            self.make_run(attempt_no=0)


class TestTransitionConstruction:
    def test_retry_target_requires_the_retry_event(self) -> None:
        with pytest.raises(ValidationError, match="only valid on the retry edge"):
            StateTransition(
                source=RunState.TRIAGING,
                event=TransitionEvent.CLASSIFIED_RFQ,
                target=RunState.EXTRACTING,
                retry_target=RunState.TRIAGING,
            )

    def test_retry_edge_requires_a_target(self) -> None:
        with pytest.raises(ValidationError, match="must declare a retry_target"):
            StateTransition(
                source=RunState.FAILED_RETRYABLE,
                event=TransitionEvent.RETRY,
                target=RunState.FAILED_RETRYABLE,
            )
