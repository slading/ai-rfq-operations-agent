"""Ports for the non-LLM dependencies of the workflow engine.

These are the seams that keep Phase 2 testable without a database and Phase 6
testable without a browser. Each is a :class:`~typing.Protocol`, so an
implementation is accepted on shape alone and a test double needs no base class.

Business-data repositories (catalog, stock, price books, customers) are *not*
declared here: they arrive with the SQLAlchemy models in Phase 1, where their
signatures can be derived from real queries instead of guessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from rfq_agent.contracts.llm import CompletionRequest, CompletionResult, ParseRequest
    from rfq_agent.domain.human import HumanAction
    from rfq_agent.domain.outbound import RenderedOutbound
    from rfq_agent.domain.values import Json
    from rfq_agent.domain.workflow import (
        ReasonCode,
        RunActor,
        RunState,
        TransitionEvent,
    )
    from rfq_agent.observability.ids import TraceContext

__all__ = [
    "IdempotencyStore",
    "LLMCallRecord",
    "OutboundChannel",
    "SendResult",
    "ToolCallRecord",
    "TraceRecorder",
]


@runtime_checkable
class TraceRecorder(Protocol):
    """Append-only sink for everything that happens during a run (§10).

    Implementations must be safe to call on the failure path: recording a
    failure is how the failure becomes diagnosable.
    """

    def record_state_transition(
        self,
        *,
        trace: TraceContext,
        from_state: RunState,
        to_state: RunState,
        event: TransitionEvent,
        actor: RunActor,
        reason_code: ReasonCode | None = None,
        detail: Json = None,
    ) -> None:
        """Append one state transition to the audit trail."""
        ...

    def record_llm_call(
        self,
        *,
        trace: TraceContext,
        stage: str,
        model: str,
        purpose: str,
        request: CompletionRequest | ParseRequest,
        result: CompletionResult | None = None,
        retries: int = 0,
        latency_ms: int = 0,
        error_code: str | None = None,
        output_valid: bool | None = None,
    ) -> None:
        """Record one model call, including its retries and usage."""
        ...

    def record_tool_call(self, *, trace: TraceContext, record: ToolCallRecord) -> None:
        """Record one tool invocation and its outcome."""
        ...

    def record_human_action(self, *, trace: TraceContext, action: HumanAction) -> None:
        """Record one operator action, including its before/after diff."""
        ...


@runtime_checkable
class IdempotencyStore(Protocol):
    """Guarantees that a given key is acted on at most once.

    Used both at intake (duplicate RFQ suppression) and for human actions
    (a double-clicked APPROVE must not send twice).
    """

    def claim(self, key: str, *, scope: str, ttl_seconds: int | None = None) -> bool:
        """Atomically claim ``key``; ``False`` means it was already claimed."""
        ...

    def release(self, key: str, *, scope: str) -> None:
        """Release a claim, e.g. after a failed attempt that may be retried."""
        ...

    def is_claimed(self, key: str, *, scope: str) -> bool:
        """Whether ``key`` is currently claimed."""
        ...


class SendResult(Protocol):
    """Outcome of an outbound send.

    In V1 the only implementation records the response and returns; no email is
    ever transmitted (architecture §12).
    """

    @property
    def sent(self) -> bool:
        """Whether the response was dispatched (always a simulation in V1)."""
        ...

    @property
    def reference(self) -> str:
        """Provider- or store-side reference for the dispatch."""
        ...

    @property
    def sent_at(self) -> datetime:
        """When the dispatch was recorded."""
        ...


@runtime_checkable
class OutboundChannel(Protocol):
    """Delivers an approved customer response.

    The engine only ever calls :meth:`send` after a human approval transition,
    and the implementation re-checks that precondition rather than trusting the
    caller.
    """

    def send(self, outbound: RenderedOutbound, *, approval_action_id: str) -> SendResult:
        """Dispatch an approved outbound response."""
        ...


class ToolCallRecord(Protocol):
    """Read-only view of a recorded tool call, as consumed by the trace UI."""

    @property
    def tool_name(self) -> str:
        """Name of the tool that was called."""
        ...

    @property
    def args(self) -> Json:
        """Validated arguments the tool was called with."""
        ...

    @property
    def result_status(self) -> str:
        """One of ``OK``, ``ERROR``, ``DENIED``, ``TRUNCATED``."""
        ...

    @property
    def duration_ms(self) -> int:
        """Wall-clock duration of the call."""
        ...


class LLMCallRecord(Protocol):
    """Read-only view of a recorded model call, as consumed by the trace UI."""

    @property
    def model(self) -> str:
        """Model identifier used for the call."""
        ...

    @property
    def purpose(self) -> str:
        """Why the call happened (triage, extract, resolve, repair, classify)."""
        ...

    @property
    def latency_ms(self) -> int:
        """Wall-clock duration of the call."""
        ...

    @property
    def tokens_in(self) -> int:
        """Prompt tokens reported by the provider."""
        ...

    @property
    def tokens_out(self) -> int:
        """Completion tokens reported by the provider."""
        ...
