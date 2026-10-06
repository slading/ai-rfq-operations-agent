"""Recorded operator actions (``human_actions``) - §3.6, §6.3.

The human-in-the-loop guarantee is only as strong as its storage. Two things
make it real here:

* ``idempotency_key`` is unique, so a double-clicked APPROVE cannot be applied
  twice - the second insert fails instead of sending a second quotation.
* the ``CHECK`` constraints mirror
  :meth:`~rfq_agent.domain.human.HumanAction._check_action_contract`: an EDIT
  carries a before/after diff, a REJECT carries a reason, and nothing else
  carries a diff. A domain rule enforced only in Python is a rule any future
  caller can bypass; here it is a property of the table.

``outbound_messages.approval_action_id`` points at a row in this table, which is
how "no message leaves without a recorded human approval" becomes a foreign key
rather than a convention.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.domain.human import HumanActionKind
from rfq_agent.domain.values import Json
from rfq_agent.domain.workflow import ReasonCode
from rfq_agent.persistence.base import Base
from rfq_agent.persistence.types import JSON_PAYLOAD, UtcDateTime, enum_type

__all__ = [
    "HumanActionRow",
]

_EDIT = HumanActionKind.EDIT.value
_REJECT = HumanActionKind.REJECT.value
_ADD_NOTE = HumanActionKind.ADD_NOTE.value


class HumanActionRow(Base):
    """One operator action, immutable once written.

    The migration installs a trigger that aborts ``UPDATE`` and ``DELETE``: an
    approval record that can be edited after the fact is not an approval record.
    """

    __tablename__ = "human_actions"
    __table_args__ = (
        CheckConstraint(
            f"(action = '{_EDIT}' AND before_json IS NOT NULL AND after_json IS NOT NULL) "
            f"OR (action <> '{_EDIT}' AND before_json IS NULL AND after_json IS NULL)",
            name="edit_carries_diff",
        ),
        CheckConstraint(
            f"action <> '{_REJECT}' OR reason_code IS NOT NULL", name="reject_requires_reason"
        ),
        CheckConstraint(
            f"action <> '{_ADD_NOTE}' OR note IS NOT NULL", name="add_note_requires_note"
        ),
        CheckConstraint("row_version >= 0", name="row_version_non_negative"),
    )

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    rfq_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rfqs.rfq_id", ondelete="CASCADE"), nullable=False
    )
    #: Identity of the operator. V1 has a single static token subject; a real
    #: user system is explicitly out of scope (§12).
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[HumanActionKind] = mapped_column(
        enum_type(HumanActionKind, name="human_action_kind"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    before_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    after_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)
    reason_code: Mapped[ReasonCode | None] = mapped_column(
        enum_type(ReasonCode, name="reason_code"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(2_000), nullable=True)
    #: ``runs.row_version`` this action was applied against (optimistic lock).
    row_version: Mapped[int] = mapped_column(nullable=False, default=0)


Index("ix_human_actions_run_id_occurred_at", HumanActionRow.run_id, HumanActionRow.occurred_at)
