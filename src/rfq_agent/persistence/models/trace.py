"""Model and tool call traces (``llm_calls``, ``tool_calls``) - §10.

These are the persistence of the :class:`~rfq_agent.contracts.ports.TraceRecorder`
contract declared in Phase 0. They exist so that "why did the system do that?"
is answerable from the database alone, months later, without access to logs.

What is deliberately *absent*: prompt and completion text. The columns store
SHA-256 digests, and ``payload_json`` - when it is populated at all - holds the
*redacted* snapshot produced by
:mod:`rfq_agent.observability.redaction`. Storing raw prompts would put
untrusted customer content and, on a bad day, an API key into the trace table;
the redaction module is the single choke point that prevents it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.contracts.llm import ModelPurpose
from rfq_agent.domain.values import Json
from rfq_agent.observability.ids import utc_now
from rfq_agent.observability.spans import ToolResultStatus
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.types import JSON_PAYLOAD, UtcDateTime, enum_type

__all__ = [
    "LlmCallRow",
    "ToolCallRow",
]

#: Trace and span identifiers follow the W3C widths pinned in Phase 0.
_TRACE_ID_LEN = 32
_SPAN_ID_LEN = 16


class LlmCallRow(Base):
    """One model call, including its retries, usage and outcome."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(f"length(trace_id) = {_TRACE_ID_LEN}", name="trace_id_len"),
        CheckConstraint(f"span_id IS NULL OR length(span_id) = {_SPAN_ID_LEN}", name="span_id_len"),
        CheckConstraint(
            f"parent_span_id IS NULL OR length(parent_span_id) = {_SPAN_ID_LEN}",
            name="parent_span_id_len",
        ),
        CheckConstraint(
            "prompt_sha256 IS NULL OR length(prompt_sha256) = 64", name="prompt_sha256_len"
        ),
        CheckConstraint(
            "response_sha256 IS NULL OR length(response_sha256) = 64",
            name="response_sha256_len",
        ),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="tokens_non_negative"),
        CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(_TRACE_ID_LEN), nullable=False)
    span_id: Mapped[str | None] = mapped_column(String(_SPAN_ID_LEN), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(_SPAN_ID_LEN), nullable=True)

    #: Run state the call happened in (e.g. ``RESOLVING``), for stage-level cost.
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[ModelPurpose] = mapped_column(
        enum_type(ModelPurpose, name="model_purpose"), nullable=False
    )

    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    #: ``NULL`` when the call never returned a parseable result to judge.
    output_valid: Mapped[bool | None] = mapped_column(nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Provider-neutral error code from :class:`~rfq_agent.contracts.errors`.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Redacted request/response snapshot. Never raw prompt text.
    payload_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)


Index("ix_llm_calls_run_id_occurred_at", LlmCallRow.run_id, LlmCallRow.occurred_at)
Index("ix_llm_calls_model_purpose", LlmCallRow.model, LlmCallRow.purpose)


class ToolCallRow(Base):
    """One tool invocation, its arguments, and how it ended.

    ``DENIED`` is a first-class outcome: a call the agent made against a tool it
    is not permitted to use is recorded as a denial, which is what makes
    "the agent never touched a consequential tool" a checkable claim.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(f"length(trace_id) = {_TRACE_ID_LEN}", name="trace_id_len"),
        CheckConstraint(f"span_id IS NULL OR length(span_id) = {_SPAN_ID_LEN}", name="span_id_len"),
        CheckConstraint(
            f"parent_span_id IS NULL OR length(parent_span_id) = {_SPAN_ID_LEN}",
            name="parent_span_id_len",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64", name="result_sha256_len"
        ),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        CheckConstraint("step_index >= 0", name="step_index_non_negative"),
        CheckConstraint("length(tool_name) >= 2", name="tool_name_min_length"),
    )

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(_TRACE_ID_LEN), nullable=False)
    span_id: Mapped[str | None] = mapped_column(String(_SPAN_ID_LEN), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(_SPAN_ID_LEN), nullable=True)

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Position of the call within the agent's step budget (0-based).
    step_index: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Validated arguments. Arguments are identifiers, codes and quantities -
    #: never free customer text - but they are still redacted before writing.
    args_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    result_status: Mapped[ToolResultStatus] = mapped_column(
        enum_type(ToolResultStatus, name="tool_result_status"), nullable=False
    )
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)


Index("ix_tool_calls_run_id_occurred_at", ToolCallRow.run_id, ToolCallRow.occurred_at)
