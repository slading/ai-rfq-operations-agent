"""Product catalog (``product_families``, ``products``, ``product_aliases``).

``product_aliases`` is what makes ``search_catalog`` deterministic rather than
fuzzy: a customer's own part number, a legacy SKU or a colloquial name is a
*stored fact*, so a match can cite the alias that produced it. Free-text
similarity still happens (the resolver scores candidates), but the evidence is a
row rather than a guess.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.persistence.types import enum_type

__all__ = [
    "ProductAliasRow",
    "ProductFamilyRow",
    "ProductRow",
]


class ProductFamilyRow(Base, TimestampMixin):
    """A coarse grouping of products ("3 families" in the demo dataset)."""

    __tablename__ = "product_families"

    family_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)


class ProductRow(Base, TimestampMixin):
    """One sellable catalog item.

    There is no price column here on purpose. Prices live in
    :mod:`rfq_agent.persistence.models.pricing` with validity windows and
    quantity breaks, because a price is a dated fact, not an attribute of a
    product - and a "current price" column is the classic way a stale price
    reaches a customer.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("sku", name="uq_products_sku"),
        CheckConstraint("length(sku) >= 2", name="sku_min_length"),
    )

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    family_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("product_families.family_code", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Unit of measure, e.g. ``EA``, ``BOX``, ``PAL``.
    uom: Mapped[str] = mapped_column(String(16), nullable=False, default="EA")
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


Index("ix_products_family_code", ProductRow.family_code)


class ProductAliasRow(Base):
    """Alternate identifiers for a product.

    ``normalized`` is indexed but not unique: an alias that resolves to two
    products is a legitimate catalog condition (a genuine ambiguity) and must
    surface as ``AMBIGUOUS_MATCH`` for a human, not be prevented at write time.
    """

    __tablename__ = "product_aliases"
    __table_args__ = (CheckConstraint("length(normalized) >= 1", name="normalized_not_empty"),)

    product_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    normalized: Mapped[str] = mapped_column(String(200), primary_key=True, autoincrement=False)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[AliasKind] = mapped_column(enum_type(AliasKind, name="alias_kind"), nullable=False)


Index("ix_product_aliases_normalized", ProductAliasRow.normalized)
