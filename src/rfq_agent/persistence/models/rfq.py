"""Intake records: ``rfqs``, ``rfq_attachments``, ``intake_events`` (§5.2).

Intake is append-only and dumb by design: normalise, key, de-duplicate, store.
Nothing here decides whether something *is* an RFQ - that is triage's job.

``body_text`` holds untrusted customer content verbatim. It is stored because
the audit trail must be able to show what actually arrived, and it is never
logged: everything that leaves this table for a log, trace or prompt goes
through :mod:`rfq_agent.observability.redaction`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.domain.intake import IntakeEventKind, RfqStatus, SourceChannel
from rfq_agent.domain.values import Json
from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.types import JSON_PAYLOAD, UtcDateTime, enum_type

__all__ = [
    "IntakeEventRow",
    "RfqAttachmentRow",
    "RfqRow",
]

#: SHA-256 digests are stored as lowercase hex text.
_SHA256_LENGTH = 64


class RfqRow(Base, TimestampMixin):
    """One inbound request for quotation.

    ``idempotency_key`` is unique: re-delivering the same message (a retrying
    mail transport, a double-submitted web form) can never create a second RFQ.
    That is failure case F17 handled by the database rather than by hope.
    """

    __tablename__ = "rfqs"
    __table_args__ = (
        CheckConstraint(f"length(body_sha256) = {_SHA256_LENGTH}", name="body_sha256_len"),
        CheckConstraint(f"length(sender_email_hash) = {_SHA256_LENGTH}", name="email_hash_len"),
    )

    rfq_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_channel: Mapped[SourceChannel] = mapped_column(
        enum_type(SourceChannel, name="source_channel"), nullable=False
    )
    status: Mapped[RfqStatus] = mapped_column(
        enum_type(RfqStatus, name="rfq_status"), nullable=False, default=RfqStatus.OPEN
    )
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: Untrusted customer content, stored verbatim for audit. Never logged raw.
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)

    sender_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Stored once, at intake. Everywhere else the customer is referenced by
    #: ``customer_id`` or ``sender_email_hash`` so addresses do not propagate.
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sender_email_hash: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)

    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    #: Derived from threading headers / normalised subject. Used to *suggest*
    #: supersession when a correction arrives - never to supersede silently.
    thread_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: Set when a correction supersedes this RFQ. A human always makes that call.
    superseded_by_rfq_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("rfqs.rfq_id", ondelete="SET NULL"), nullable=True
    )


Index("ix_rfqs_status_received_at", RfqRow.status, RfqRow.received_at)
Index("ix_rfqs_sender_email_hash", RfqRow.sender_email_hash)
Index("ix_rfqs_thread_key", RfqRow.thread_key)


class RfqAttachmentRow(Base):
    """Attachment *metadata* only. V1 does not parse files (§12)."""

    __tablename__ = "rfq_attachments"
    __table_args__ = (
        CheckConstraint("byte_length >= 0", name="byte_length_non_negative"),
        CheckConstraint(f"sha256 IS NULL OR length(sha256) = {_SHA256_LENGTH}", name="sha256_len"),
    )

    attachment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rfq_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rfqs.rfq_id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(120), nullable=False, default="application/octet-stream"
    )
    byte_length: Mapped[int] = mapped_column(nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(_SHA256_LENGTH), nullable=True)
    parsed: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Untrusted preview text, subject to the same trust rules as the body.
    text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_rfq_attachments_rfq_id", RfqAttachmentRow.rfq_id)


class IntakeEventRow(Base):
    """Append-only intake audit row (``intake_events``).

    Like ``run_events``, this table has no update or delete path: the migration
    installs triggers that abort both (see the initial revision). A row here is
    the evidence that an e-mail arrived, was suppressed as a duplicate, or was
    linked to a thread.
    """

    __tablename__ = "intake_events"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(
            f"detail_sha256 IS NULL OR length(detail_sha256) = {_SHA256_LENGTH}",
            name="detail_sha256_len",
        ),
    )

    rfq_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("rfqs.rfq_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    #: Assigned by the writer as ``max(seq) + 1`` for the RFQ, inside the same
    #: transaction that inserts the row.
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    kind: Mapped[IntakeEventKind] = mapped_column(
        enum_type(IntakeEventKind, name="intake_event_kind"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    detail_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    detail_sha256: Mapped[str | None] = mapped_column(String(_SHA256_LENGTH), nullable=True)
