"""Trace-span schemas (architecture §10.2).

These mirror the ``llm_calls`` and ``tool_calls`` tables that Phase 1 will
create, so the shape of what we record is agreed before any persistence code
exists. They are value objects, not ORM models.

Two deliberate storage choices visible here:

* prompts and tool payloads are stored as ``*_sha256`` plus a size-capped
  summary, never in full;
* untrusted customer content never appears in a span at all - it is referenced
  by ``rfq_id`` and read from the immutable ``rfqs`` row when an operator needs it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import RfqId, RunId, TraceId
from rfq_agent.domain.values import DomainModel

__all__ = [
    "LLMCallSpan",
    "ToolCallSpan",
    "ToolResultStatus",
]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ToolResultStatus(StrEnum):
    """Outcome of a tool call. ``DENIED`` is a first-class, countable event."""

    OK = "OK"
    ERROR = "ERROR"
    DENIED = "DENIED"
    TRUNCATED = "TRUNCATED"


class _Span(DomainModel):
    """Fields shared by every span."""

    trace_id: TraceId
    rfq_id: RfqId | None = None
    run_id: RunId
    seq: Annotated[int, Field(ge=1)]
    occurred_at: datetime
    duration_ms: Annotated[int, Field(ge=0)] = 0


class LLMCallSpan(_Span):
    """One model call, with everything needed to explain its cost and outcome."""

    stage: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    purpose: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    #: Hash of the exact message list sent. The body itself is never stored in
    #: production; a dev-mode debug dump is the only exception.
    messages_sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
    request_bytes: Annotated[int, Field(ge=0)] = 0
    response_status: Annotated[str, StringConstraints(min_length=1, max_length=32)] = "ok"
    finish_reason: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    output_valid: bool | None = None
    validation_error_count: Annotated[int, Field(ge=0)] = 0
    #: Repair attempts consumed before this call, and transport retries within it.
    repair_attempts: Annotated[int, Field(ge=0)] = 0
    retries: Annotated[int, Field(ge=0)] = 0
    tokens_in: Annotated[int, Field(ge=0)] = 0
    tokens_out: Annotated[int, Field(ge=0)] = 0
    tokens_cached: Annotated[int, Field(ge=0)] = 0
    error_code: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _check_outcome(self) -> Self:
        """A failed call must carry an error code; a valid one must not."""
        if self.response_status != "ok" and self.error_code is None:
            msg = "error_code is required when response_status is not ok"
            raise ValueError(msg)
        if self.response_status == "ok" and self.error_code is not None:
            msg = "error_code must be None when response_status is ok"
            raise ValueError(msg)
        if self.output_valid is False and self.validation_error_count == 0:
            msg = "validation_error_count must be positive when output_valid is false"
            raise ValueError(msg)
        return self


class ToolCallSpan(_Span):
    """One tool invocation.

    ``args_sha256`` and ``result_sha256`` make a run verifiable - in particular,
    the grounding gate can prove which identifiers the model was actually shown
    - without the audit table storing every catalog search result.
    """

    tool_name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    args_sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
    args_bytes: Annotated[int, Field(ge=0)] = 0
    result_status: ToolResultStatus = ToolResultStatus.OK
    result_sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)] | None = None
    result_bytes: Annotated[int, Field(ge=0)] = 0
    error_code: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    attempt: Annotated[int, Field(ge=1)] = 1

    @model_validator(mode="after")
    def _check_outcome(self) -> Self:
        """Failures and denials must say why; successes must carry a result hash."""
        if self.result_status in {ToolResultStatus.ERROR, ToolResultStatus.DENIED}:
            if self.error_code is None:
                msg = "error_code is required for an ERROR or DENIED tool call"
                raise ValueError(msg)
        elif self.error_code is not None:
            msg = "error_code must be None for a successful tool call"
            raise ValueError(msg)
        if self.result_status is ToolResultStatus.OK and self.result_sha256 is None:
            msg = "result_sha256 is required for a successful tool call"
            raise ValueError(msg)
        if self.result_status is ToolResultStatus.DENIED and self.result_sha256 is not None:
            msg = "a denied tool call returns no result payload"
            raise ValueError(msg)
        return self
