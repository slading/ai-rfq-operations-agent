"""Pricing master data (``price_books``, ``price_entries``, ``discount_rules``).

A price is a *dated, scoped fact*, never a column on a product: a lookup always
carries the date it is valid for, which is why
:class:`~rfq_agent.domain.quote.Quote` stores ``pricing_as_of`` and
``inputs_sha256`` and can be replayed exactly.

Overlapping price rows are deliberately not prevented by the schema - SQLite
cannot express "no overlap" declaratively, and a range predicate in a ``CHECK``
would be a lie. The lookup rule is what makes the result deterministic: among
valid entries, highest ``min_qty`` first, then latest ``effective_from``, then
``price_entry_id`` as a total tie-break. That ordering is implemented and
tested with the pricing tools (Phase 1C); the dataset provides overlapping
windows (quantity breaks, an expired entry) so the rule has something to decide.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.domain.policy import DiscountScope
from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.types import MONEY, PERCENT, UNIT_PRICE, enum_type

__all__ = [
    "DiscountRuleRow",
    "PriceBookRow",
    "PriceEntryRow",
]


class PriceBookRow(Base, TimestampMixin):
    """A named set of prices valid over a window (e.g. ``EU-2026``)."""

    __tablename__ = "price_books"
    __table_args__ = (
        CheckConstraint("length(currency) = 3", name="currency_len"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="window_ordered"
        ),
    )

    price_book_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: Optional customer tier this book applies to (``STANDARD``, ``GOLD``, ...).
    customer_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


class PriceEntryRow(Base, TimestampMixin):
    """One price for one product, optionally for one customer or tier.

    ``customer_id`` is nullable: a null value means "this is the list price, use
    it for anyone without a contract price". That is the rule that lets
    ``get_price`` be a single deterministic query instead of a chain of guesses.
    """

    __tablename__ = "price_entries"
    __table_args__ = (
        UniqueConstraint(
            "price_book_code",
            "product_id",
            "min_qty",
            "effective_from",
            name="uq_price_entries_book_product_qty_from",
        ),
        CheckConstraint("min_qty >= 1", name="min_qty_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("length(currency) = 3", name="currency_len"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="window_ordered"
        ),
    )

    price_entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    price_book_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("price_books.price_book_code", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False
    )
    #: Contract price for a specific customer; ``NULL`` means list price.
    customer_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("customers.customer_id", ondelete="CASCADE"), nullable=True
    )
    customer_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Quantity break: this price applies from ``min_qty`` units upwards.
    min_qty: Mapped[int] = mapped_column(nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(UNIT_PRICE, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


Index(
    "ix_price_entries_product_id_effective_from",
    PriceEntryRow.product_id,
    PriceEntryRow.effective_from,
)
Index("ix_price_entries_customer_id", PriceEntryRow.customer_id)


class DiscountRuleRow(Base, TimestampMixin):
    """A discount the deterministic core may apply.

    Whether a rule *requires approval* is data, not logic: the gate reads this
    column, so changing policy is a data change rather than a code change.
    """

    __tablename__ = "discount_rules"
    __table_args__ = (
        CheckConstraint("percent >= 0 AND percent <= 100", name="percent_range"),
        CheckConstraint("min_qty IS NULL OR min_qty >= 1", name="min_qty_positive"),
        CheckConstraint(
            "min_order_value IS NULL OR min_order_value >= 0", name="min_order_positive"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="window_ordered"
        ),
    )

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[DiscountScope] = mapped_column(
        enum_type(DiscountScope, name="discount_scope"), nullable=False
    )
    #: What the scope points at: a family code, product id or customer id.
    scope_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    percent: Mapped[Decimal] = mapped_column(PERCENT, nullable=False)
    min_qty: Mapped[int | None] = mapped_column(nullable=True)
    min_order_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    requires_approval: Mapped[bool] = mapped_column(nullable=False, default=False)
    priority: Mapped[int] = mapped_column(nullable=False, default=0)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


Index("ix_discount_rules_scope_scope_ref", DiscountRuleRow.scope, DiscountRuleRow.scope_ref)
