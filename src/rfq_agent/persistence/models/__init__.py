"""Persistence models - the SQLAlchemy side of the schema (architecture §5).

Importing this package registers every table on
:attr:`rfq_agent.persistence.base.Base.metadata`. Alembic uses that metadata as
its autogenerate target, and the drift test in ``tests/persistence`` asserts it
matches what the migrations actually built.

The import order below is meaningful and must stay dependency-ordered: with no
ORM relationships between the models, SQLAlchemy flushes pending inserts in
*mapper registration order*, not in foreign-key order. Registering ``products``
after ``warehouses`` means a transaction that inserts both writes products
first - and fails on the foreign key. The Phase 1C repositories will still flush in
explicit batches; this ordering removes the most common way to trip over it.

These classes are intentionally *not* the domain models. A domain object
(:class:`~rfq_agent.domain.quote.Quote`) is a frozen value object with the
invariants of a business document; a ``*Row`` class is a mutable record with the
constraints of a table. Repositories (Phase 1C) are the only place the two meet,
converting explicitly in both directions.
"""

from __future__ import annotations

from rfq_agent.persistence.models.catalog import (
    ProductAliasRow,
    ProductFamilyRow,
    ProductRow,
)
from rfq_agent.persistence.models.customers import CustomerAliasRow, CustomerRow
from rfq_agent.persistence.models.human import HumanActionRow
from rfq_agent.persistence.models.logistics import (
    CarrierServiceRow,
    HolidayRow,
    StockLevelRow,
    WarehouseRow,
)
from rfq_agent.persistence.models.outbound import OutboundMessageRow
from rfq_agent.persistence.models.pricing import (
    DiscountRuleRow,
    PriceBookRow,
    PriceEntryRow,
)
from rfq_agent.persistence.models.quote import (
    QuoteBlockedReasonRow,
    QuoteLineRow,
    QuoteRow,
)
from rfq_agent.persistence.models.rfq import (
    IntakeEventRow,
    RfqAttachmentRow,
    RfqRow,
)
from rfq_agent.persistence.models.run import (
    IdempotencyClaimRow,
    RunEventRow,
    RunQueueEntryRow,
    RunRow,
)
from rfq_agent.persistence.models.trace import LlmCallRow, ToolCallRow

__all__ = [
    "CarrierServiceRow",
    "CustomerAliasRow",
    "CustomerRow",
    "DiscountRuleRow",
    "HolidayRow",
    "HumanActionRow",
    "IdempotencyClaimRow",
    "IntakeEventRow",
    "LlmCallRow",
    "OutboundMessageRow",
    "PriceBookRow",
    "PriceEntryRow",
    "ProductAliasRow",
    "ProductFamilyRow",
    "ProductRow",
    "QuoteBlockedReasonRow",
    "QuoteLineRow",
    "QuoteRow",
    "RfqAttachmentRow",
    "RfqRow",
    "RunEventRow",
    "RunQueueEntryRow",
    "RunRow",
    "StockLevelRow",
    "ToolCallRow",
    "WarehouseRow",
]
