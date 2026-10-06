"""Outbound channel value objects (architecture §8.3 layer 7, §12).

V1 never sends email. What it does have is the *shape* of the outbound path,
including the control that matters: the customer-facing text is rendered from a
template plus allowlisted fields, so there is no code path by which
model-generated free text becomes a customer email.

``CanaryScanResult`` is the last gate before a simulated send.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rfq_agent.domain.ids import CustomerId, QuoteId
from rfq_agent.domain.values import DomainModel

__all__ = [
    "CanaryFinding",
    "CanaryFindingKind",
    "CanaryScanResult",
    "OutboundChannel",
    "OutboundStatus",
    "RenderedOutbound",
    "TemplateSlot",
]

_MAX_RENDERED = 20_000
_MAX_FINDINGS = 20


class OutboundChannel(StrEnum):
    """How the response would be delivered. V1 has exactly one value."""

    SIMULATED = "SIMULATED"


class OutboundStatus(StrEnum):
    """Outbound lifecycle."""

    DRAFTED = "DRAFTED"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    SENT = "SENT"


class TemplateSlot(StrEnum):
    """Allowlisted slots in the response template.

    Adding a slot is a deliberate act: it is the only way new content can reach
    a customer, and each slot is filled from a typed, verified source.
    """

    CUSTOMER_NAME = "customer_name"
    QUOTE_NUMBER = "quote_number"
    LINE_ITEMS_TABLE = "line_items_table"
    SUBTOTAL = "subtotal"
    DISCOUNT = "discount"
    TOTAL = "total"
    CURRENCY = "currency"
    DELIVERY_SUMMARY = "delivery_summary"
    VALID_UNTIL = "valid_until"
    PAYMENT_TERMS = "payment_terms"
    #: Operator-authored prose, rendered in a clearly separated block.
    OPERATOR_NOTE = "operator_note"


class CanaryFindingKind(StrEnum):
    """What the canary scan caught."""

    #: A planted canary string from the seed data appeared in the output.
    CANARY_LEAK = "CANARY_LEAK"
    FOREIGN_CUSTOMER_REFERENCE = "FOREIGN_CUSTOMER_REFERENCE"
    UNQUOTED_PRICE_VALUE = "UNQUOTED_PRICE_VALUE"
    SYSTEM_PROMPT_FRAGMENT = "SYSTEM_PROMPT_FRAGMENT"
    TOOL_PAYLOAD_FRAGMENT = "TOOL_PAYLOAD_FRAGMENT"
    UNALLOWLISTED_SLOT = "UNALLOWLISTED_SLOT"


class CanaryFinding(DomainModel):
    """One leak detected in rendered outbound text."""

    kind: CanaryFindingKind
    #: Deliberately vague: the finding must be actionable without reproducing
    #: the leaked value in a log or a UI.
    detail: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: Character offset of the match, when known.
    offset: Annotated[int, Field(ge=0)] | None = None


class CanaryScanResult(DomainModel):
    """Result of scanning rendered outbound text before a send."""

    passed: bool
    findings: Annotated[tuple[CanaryFinding, ...], Field(max_length=_MAX_FINDINGS)] = ()
    scanned_at: datetime

    @model_validator(mode="after")
    def _check_agreement(self) -> Self:
        """``passed`` must agree with the findings list."""
        if self.passed and self.findings:
            msg = "passed must be false when findings are present"
            raise ValueError(msg)
        if not self.passed and not self.findings:
            msg = "at least one finding is required when passed is false"
            raise ValueError(msg)
        return self


class RenderedOutbound(DomainModel):
    """A rendered, scanned customer response awaiting human approval."""

    quote_id: QuoteId
    customer_id: CustomerId
    template_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    template_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    #: Slots actually filled. Anything absent was not rendered.
    slots_used: tuple[TemplateSlot, ...] = ()
    rendered_text: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_RENDERED)]
    scan: CanaryScanResult
    status: OutboundStatus = OutboundStatus.DRAFTED
    channel: OutboundChannel = OutboundChannel.SIMULATED
    #: Set only after a human approval; ``None`` in every other case.
    sent_at: datetime | None = None
    #: Present in V1: the response is stored and shown, never emailed.
    delivery_note: Annotated[str, StringConstraints(min_length=1, max_length=200)] = (
        "simulated: recorded only, no email was sent"
    )

    @model_validator(mode="after")
    def _check_rendered_text_present(self) -> Self:
        """Refuse an empty customer response.

        Whitespace is not stripped from domain strings (see
        :class:`~rfq_agent.domain.values.DomainModel`), so the check is explicit.
        """
        if not self.rendered_text.strip():
            msg = "rendered_text must contain content"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_send_preconditions(self) -> Self:
        """A send requires a clean scan and an approval-driven status."""
        if not self.scan.passed and self.status is not OutboundStatus.BLOCKED:
            msg = "a failed canary scan must leave the outbound BLOCKED"
            raise ValueError(msg)
        if self.sent_at is not None and self.status is not OutboundStatus.SENT:
            msg = "sent_at requires status SENT"
            raise ValueError(msg)
        if self.status is OutboundStatus.SENT and self.sent_at is None:
            msg = "status SENT requires sent_at"
            raise ValueError(msg)
        if self.channel is not OutboundChannel.SIMULATED:
            msg = "V1 supports the SIMULATED channel only"
            raise ValueError(msg)
        return self
