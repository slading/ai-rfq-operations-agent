"""Record-level vocabulary used by the persistence layer only.

These enums describe *records*, not business documents, which is why they live
here rather than in :mod:`rfq_agent.domain`: the domain has no concept of a
"queued" row or a "legacy code" alias. Keeping them separate means the domain
model can never accidentally depend on storage detail.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AliasKind",
    "QueueEntryStatus",
]


class AliasKind(StrEnum):
    """Why an alias exists. Drives how the resolver scores a match (Phase 1C)."""

    #: Alternate human-readable name (abbreviation, trading name, misspelling).
    NAME = "NAME"
    #: Alternate or legacy SKU / article number.
    SKU = "SKU"
    #: Full e-mail address belonging to the customer.
    EMAIL = "EMAIL"
    #: E-mail domain owned by the customer.
    EMAIL_DOMAIN = "EMAIL_DOMAIN"
    #: The customer's own part number for a product.
    CUSTOMER_PART = "CUSTOMER_PART"


class QueueEntryStatus(StrEnum):
    """Lifecycle of one ``run_queue`` row.

    Deliberately not a :class:`~rfq_agent.domain.workflow.RunState`: a run can be
    paused in ``AWAITING_REVIEW`` while its queue entry is simply ``COMPLETED``
    (the worker's job for that run is finished). Conflating the two would make
    "the queue is empty but the run is not finished" unrepresentable.
    """

    QUEUED = "QUEUED"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
