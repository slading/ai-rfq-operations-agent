"""Extraction schemas: what the model is allowed to claim (architecture §4.6).

Every schema in this module is a *claim* (trust tier T3). Nothing here is a
business fact until :mod:`rfq_agent.domain.resolution` grounding checks and the
deterministic core have re-derived it.

Two rules are encoded structurally rather than in the prompt:

1. ``quantity=None`` is legal and *requires* a ``missing_reason`` - "I don't
   know" is a first-class, encouraged output (§4.5 rule 4).
2. Every field carries ``evidence``: a verbatim span from the untrusted input,
   verified by :meth:`UntrustedEnvelope.contains_evidence`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.values import DomainModel

__all__ = [
    "Confidence",
    "DateResolution",
    "ExtractedLine",
    "LineExtractionStatus",
    "MissingFieldReason",
    "RequestedDelivery",
]

#: Model self-reported confidence in [0, 1]. Advisory only: routing decisions
#: use deterministic thresholds and policy, never this number on its own.
Confidence = Annotated[Decimal, Field(ge=0, le=1)]

_MAX_LINES = 50
_MAX_EVIDENCE = 500
_MAX_LINES_RAW = 2_000


class LineExtractionStatus(StrEnum):
    """Per-line extraction outcome (architecture §5.2, §7)."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"
    MISSING_QTY = "MISSING_QTY"
    DISCONTINUED = "DISCONTINUED"
    REJECTED = "REJECTED"


class MissingFieldReason(StrEnum):
    """Why a required field is absent. Closed set - no free-text excuses."""

    NOT_STATED = "NOT_STATED"
    IMPLICIT_REFERENCE = "IMPLICIT_REFERENCE"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    UNREADABLE = "UNREADABLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class DateResolution(StrEnum):
    """How a requested delivery date was obtained.

    ``INFERRED`` (e.g. "by Friday" resolved against the intake timestamp) is
    always surfaced to the operator - the model never *states* a delivery date
    as a promise; it only records what the customer asked for.
    """

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"
    ABSENT = "ABSENT"


class ExtractedLine(DomainModel):
    """One requested line item, as claimed by the model."""

    ordinal: Annotated[int, Field(ge=1)]
    #: Verbatim span of the request that produced this line.
    raw_text: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_LINES_RAW)]
    #: What the customer wrote as an identifier. Never assumed to be a real SKU.
    requested_sku: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    quantity: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    #: Required whenever ``quantity`` is ``None``.
    missing_reason: MissingFieldReason | None = None
    #: Unit of measure as written by the customer (normalised in Phase 1).
    uom: Annotated[str, StringConstraints(min_length=1, max_length=16)] | None = None
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_EVIDENCE)]
    confidence: Confidence = Decimal("0")
    status: LineExtractionStatus = LineExtractionStatus.PENDING
    #: Set by the grounding gate, never by the model.
    rejection_reason: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_quantity_contract(self) -> Self:
        """Enforce the null-quantity contract and identifier/description presence."""
        if self.quantity is None and self.missing_reason is None:
            msg = "missing_reason is required when quantity is None"
            raise ValueError(msg)
        if self.quantity is not None and self.missing_reason is not None:
            msg = "missing_reason must be None when quantity is present"
            raise ValueError(msg)
        if self.requested_sku is None and self.description is None:
            msg = "at least one of requested_sku or description is required"
            raise ValueError(msg)
        if self.status is LineExtractionStatus.REJECTED and self.rejection_reason is None:
            msg = "rejection_reason is required when status is REJECTED"
            raise ValueError(msg)
        return self


class RequestedDelivery(DomainModel):
    """What the customer asked for regarding delivery - not a promise."""

    #: Verbatim phrase, e.g. ``"by Friday"``.
    raw: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    resolution: DateResolution = DateResolution.ABSENT
    #: Only meaningful when ``resolution is EXPLICIT``.
    requested_delivery_date: date | None = None
    destination: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_resolution_contract(self) -> Self:
        """A date may only be present when the customer stated one explicitly."""
        if self.resolution is DateResolution.EXPLICIT and self.requested_delivery_date is None:
            msg = "requested_delivery_date is required when resolution is EXPLICIT"
            raise ValueError(msg)
        if (
            self.resolution is not DateResolution.EXPLICIT
            and self.requested_delivery_date is not None
        ):
            msg = "requested_delivery_date must be None unless resolution is EXPLICIT"
            raise ValueError(msg)
        if self.resolution is DateResolution.ABSENT and self.raw is not None:
            msg = "raw must be None when resolution is ABSENT"
            raise ValueError(msg)
        if self.resolution is not DateResolution.ABSENT and self.raw is None:
            msg = "raw is required when a delivery request was detected"
            raise ValueError(msg)
        return self


class ExtractionResult(DomainModel):
    """The full extraction claim for one run, before resolution."""

    lines: Annotated[tuple[ExtractedLine, ...], Field(min_length=0, max_length=_MAX_LINES)] = ()
    requested_delivery: RequestedDelivery = Field(default_factory=RequestedDelivery)
    #: Questions the agent could not answer from the content. Surfaced to the
    #: operator; never auto-answered.
    open_questions: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=300)], ...],
        Field(max_length=10),
    ] = ()

    @model_validator(mode="after")
    def _check_ordinals(self) -> Self:
        """Ordinals must be unique and dense starting at 1."""
        seen = {line.ordinal for line in self.lines}
        if len(seen) != len(self.lines):
            msg = "line ordinals must be unique"
            raise ValueError(msg)
        if seen and seen != set(range(1, len(self.lines) + 1)):
            msg = "line ordinals must be 1..N without gaps"
            raise ValueError(msg)
        return self
