"""Intake schemas (architecture §3.3, §5.2).

Intake is deliberately dumb: normalise, key, de-duplicate, store immutably.
Deciding whether something *is* an RFQ belongs to triage, not to intake.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import IdempotencyKey, RfqId
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import DomainModel

__all__ = [
    "AttachmentMetadata",
    "InboundRfq",
    "IntakeEventKind",
    "RfqStatus",
    "SourceChannel",
]


class SourceChannel(StrEnum):
    """Where the request entered the system."""

    EMAIL = "EMAIL"
    WEB_FORM = "WEB_FORM"
    API = "API"
    FIXTURE = "FIXTURE"


class RfqStatus(StrEnum):
    """RFQ lifecycle state (architecture §6.1).

    Deliberately separate from the run state machine: an RFQ can be superseded
    by a correction while its runs have their own, finer-grained lifecycle.
    """

    OPEN = "OPEN"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    QUOTED = "QUOTED"
    SENT = "SENT"
    CLOSED = "CLOSED"
    SUPERSEDED = "SUPERSEDED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"


#: RFQ statuses that no further intake event may leave.
TERMINAL_RFQ_STATUSES: frozenset[RfqStatus] = frozenset(
    {RfqStatus.SENT, RfqStatus.CLOSED, RfqStatus.SUPERSEDED, RfqStatus.DUPLICATE_SUPPRESSED}
)


class IntakeEventKind(StrEnum):
    """Append-only intake audit event kinds (``intake_events`` table, Phase 1)."""

    RECEIVED = "RECEIVED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    THREAD_LINKED = "THREAD_LINKED"
    ATTACHMENT_REJECTED = "ATTACHMENT_REJECTED"


class AttachmentMetadata(DomainModel):
    """Attachment *metadata* only - V1 does not parse files (architecture §12).

    The untrusted text of an attachment, when a caller supplies it, travels in
    :attr:`text_preview` and is subject to exactly the same trust rules as the
    email body.
    """

    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_type: Annotated[str, StringConstraints(min_length=1, max_length=120)] = (
        "application/octet-stream"
    )
    byte_length: Annotated[int, Field(ge=0)] = 0
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    parsed: bool = False
    text_preview: str | None = None


class InboundRfq(DomainModel):
    """A normalised inbound request, prior to any model involvement.

    ``sender_email`` is stored once here. Everywhere else in the system the
    customer is referenced by ``customer_id`` or by ``sender_email_hash``, so
    raw addresses do not propagate into logs and traces (§10.4).
    """

    source_channel: SourceChannel
    received_at: datetime
    subject: str = ""
    body: UntrustedText
    sender_name: str | None = None
    sender_email: Annotated[
        str,
        StringConstraints(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ]
    sender_email_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    #: Stable per-message key; the basis of duplicate suppression (§7 F17).
    idempotency_key: IdempotencyKey
    #: Derived from threading headers / normalised subject; used to *suggest*
    #: supersession when a correction arrives (§7 F18). Never used to silently
    #: supersede - a human always decides.
    thread_key: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    attachments: tuple[AttachmentMetadata, ...] = ()

    @model_validator(mode="after")
    def _check_body_present(self) -> Self:
        """Refuse an empty request body: there is nothing to interpret."""
        if not self.body.text.strip():
            msg = "body must not be empty"
            raise ValueError(msg)
        return self


class QuoteValidityWindow(DomainModel):
    """Optional validity window a customer asked for (rare, but explicit)."""

    valid_from: date | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        """``valid_until`` must not precede ``valid_from``."""
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            msg = "valid_until must be on or after valid_from"
            raise ValueError(msg)
        return self


#: Type alias kept for readability at call sites.
RfqIdentifier = RfqId
