"""Observability primitives: identifiers, clocks and structured log fields.

Phase 0 defines the *shape* of what later phases must record. Nothing here
performs I/O; the writer that persists trace rows arrives with the workflow
engine in Phase 2.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Protocol

from pydantic import Field, StringConstraints

from rfq_agent.domain.ids import RfqId, RunId, TraceId
from rfq_agent.domain.values import DomainModel

__all__ = [
    "Clock",
    "IdGenerator",
    "SpanId",
    "SystemClock",
    "SystemIdGenerator",
    "TraceContext",
    "utc_now",
]

#: W3C-compatible span identifier: 16 lowercase hex characters.
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


class Clock(Protocol):
    """Source of truth for "now". Injectable so tests are deterministic."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Wall-clock :class:`Clock` implementation."""

    def now(self) -> datetime:
        """Return :func:`utc_now`."""
        return utc_now()


class IdGenerator(Protocol):
    """Source of identifiers and trace identifiers. Injectable for tests."""

    def trace_id(self) -> TraceId:
        """Return a fresh trace identifier."""
        ...

    def span_id(self) -> SpanId:
        """Return a fresh span identifier."""
        ...

    def opaque_id(self, prefix: str) -> str:
        """Return a fresh identifier prefixed with ``prefix``."""
        ...


class SystemIdGenerator:
    """Cryptographically random :class:`IdGenerator` implementation."""

    def trace_id(self) -> TraceId:
        """Return a fresh 32-hex-character trace identifier."""
        return _hex(16)

    def span_id(self) -> SpanId:
        """Return a fresh 16-hex-character span identifier."""
        return _hex(8)

    def opaque_id(self, prefix: str) -> str:
        """Return ``prefix`` joined to 16 random hex characters."""
        return f"{prefix}_{_hex(8)}"


class TraceContext(DomainModel):
    """Correlation identifiers attached to every log line and trace row.

    ``run_id`` is the primary correlation key for the whole project: every
    state transition, model call, tool call, retry, error and human action is
    recorded against it, which is what makes a single run fully replayable by
    a human reading the trace.
    """

    trace_id: TraceId
    rfq_id: RfqId | None = None
    run_id: RunId | None = None
    span_id: SpanId | None = None
    parent_span_id: SpanId | None = None

    def child(self, span_id: SpanId) -> TraceContext:
        """Return a copy nested under ``span_id``."""
        return TraceContext(
            trace_id=self.trace_id,
            rfq_id=self.rfq_id,
            run_id=self.run_id,
            span_id=span_id,
            parent_span_id=self.span_id,
        )


class LogFields(DomainModel):
    """Structured log payload.

    Kept deliberately flat and string-ish: it is serialised to JSON lines and
    must survive a redaction pass without structure-dependent special cases.
    """

    level: Annotated[str, StringConstraints(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")] = (
        "INFO"
    )
    event: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    message: str = ""
    trace: TraceContext | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
