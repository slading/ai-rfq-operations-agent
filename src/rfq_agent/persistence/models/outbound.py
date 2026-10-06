"""Prepared and (simulated) sent customer responses (``outbound_messages``).

V1 never transmits an e-mail (§12). What it does do is record, immutably, the
exact text that *would* have been sent, the canary scan that cleared it, and the
human approval that unlocked it.

Three database-level guarantees, all of them tested:

* ``approval_action_id`` is a ``NOT NULL`` foreign key to ``human_actions`` -
  a message cannot exist without a recorded human action;
* a trigger on insert and update refuses the row unless that action is an
  ``APPROVE``, so referencing a REJECT record does not get you a send either;
* ``status = 'SENT'`` requires ``sent_at``, so a sent message is always
  timestamped.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.domain.outbound import OutboundChannel, OutboundStatus
from rfq_agent.domain.values import Json
from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.types import JSON_PAYLOAD, UtcDateTime, enum_type

__all__ = [
    "OutboundMessageRow",
]


class OutboundMessageRow(Base, TimestampMixin):
    """One rendered customer response and its approval provenance."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint("status <> 'SENT' OR sent_at IS NOT NULL", name="sent_requires_timestamp"),
        CheckConstraint(
            "canary_passed = 1 OR status <> 'SENT'",
            name="canary_must_pass_before_sending",
        ),
        CheckConstraint("length(rendered_sha256) = 64", name="rendered_sha256_len"),
        CheckConstraint("length(rendered_text) >= 1", name="rendered_text_present"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    quote_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("quotes.quote_id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    #: The APPROVE record that unlocked this message. Enforced by trigger.
    approval_action_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_actions.action_id", ondelete="RESTRICT"),
        nullable=False,
    )

    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    slots_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    channel: Mapped[OutboundChannel] = mapped_column(
        enum_type(OutboundChannel, name="outbound_channel"),
        nullable=False,
        default=OutboundChannel.SIMULATED,
    )
    status: Mapped[OutboundStatus] = mapped_column(
        enum_type(OutboundStatus, name="outbound_status"),
        nullable=False,
        default=OutboundStatus.DRAFTED,
    )

    #: The rendered body is stored in full (it is our own text, not customer
    #: content) so the audit trail can show exactly what a customer would see.
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canary_passed: Mapped[bool] = mapped_column(nullable=False, default=True)
    canary_findings_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    delivery_note: Mapped[str | None] = mapped_column(String(200), nullable=True)


Index("ix_outbound_messages_run_id", OutboundMessageRow.run_id)
Index("ix_outbound_messages_quote_id", OutboundMessageRow.quote_id)
Index("ix_outbound_messages_status", OutboundMessageRow.status)
