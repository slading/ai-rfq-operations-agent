"""Human-in-the-loop value objects (architecture §3.6, §6.3).

Every consequential action is idempotency-keyed, diffed and audited. The
``before``/``after`` payloads are hashes-or-values, never secrets, and the diff
is what makes "a human changed this" reviewable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import IdempotencyKey, RfqId, RunId
from rfq_agent.domain.values import DomainModel, Json
from rfq_agent.domain.workflow import ReasonCode

__all__ = [
    "HUMAN_APPROVAL_ACTIONS",
    "REASON_REQUIRED_ACTIONS",
    "HumanAction",
    "HumanActionKind",
]


class HumanActionKind(StrEnum):
    """Actions an operator may take."""

    APPROVE = "APPROVE"
    EDIT = "EDIT"
    REJECT = "REJECT"
    REQUEST_INFO = "REQUEST_INFO"
    RERUN = "RERUN"
    BIND_CUSTOMER = "BIND_CUSTOMER"
    SELECT_PRODUCT = "SELECT_PRODUCT"
    SET_QUANTITY = "SET_QUANTITY"
    ADD_NOTE = "ADD_NOTE"
    MARK_INJECTION_REVIEWED = "MARK_INJECTION_REVIEWED"


#: Actions that unlock the outbound channel. Only ``APPROVE`` does.
HUMAN_APPROVAL_ACTIONS: frozenset[HumanActionKind] = frozenset({HumanActionKind.APPROVE})

#: Actions that must carry a reason code.
REASON_REQUIRED_ACTIONS: frozenset[HumanActionKind] = frozenset({HumanActionKind.REJECT})

_MAX_NOTE = 2_000


class HumanAction(DomainModel):
    """A recorded operator action."""

    run_id: RunId
    rfq_id: RfqId
    #: Identity of the operator. V1 uses a single static token subject; a real
    #: user system is explicitly out of scope (architecture §12).
    actor: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    action: HumanActionKind
    occurred_at: datetime
    #: Prevents double-submits and makes retries of the same click safe.
    idempotency_key: IdempotencyKey
    #: Serialised "before" state of whatever changed. ``None`` for pure commands.
    before: Json = None
    after: Json = None
    reason_code: ReasonCode | None = None
    note: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_NOTE)] | None = None
    #: ``runs.row_version`` the action was applied against (optimistic lock).
    row_version: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _check_action_contract(self) -> Self:
        """Reasons, diffs and notes must be present exactly when required."""
        if self.action in REASON_REQUIRED_ACTIONS and self.reason_code is None:
            msg = f"reason_code is required for {self.action}"
            raise ValueError(msg)
        if self.action is HumanActionKind.EDIT and self.before is None:
            msg = "an EDIT action must record the before state"
            raise ValueError(msg)
        if self.action is HumanActionKind.EDIT and self.after is None:
            msg = "an EDIT action must record the after state"
            raise ValueError(msg)
        if self.action is not HumanActionKind.EDIT and (
            self.before is not None or self.after is not None
        ):
            msg = "only EDIT actions carry a before/after diff"
            raise ValueError(msg)
        if self.action is HumanActionKind.ADD_NOTE and self.note is None:
            msg = "ADD_NOTE requires a note"
            raise ValueError(msg)
        return self

    @property
    def unlocks_outbound(self) -> bool:
        """Whether this action is the human approval that gates sending."""
        return self.action in HUMAN_APPROVAL_ACTIONS
