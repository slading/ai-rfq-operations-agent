"""Stock and delivery feasibility data (§5.2, §7 F15).

Nothing here is computed: ``stock_levels`` records what the warehouse system
says, ``carrier_services`` records how long a service takes, ``holidays``
records which days do not count. The delivery promise is derived from these
rows by the deterministic core (Phase 1C) - never by a model, and never by
adding days to a date the customer asked for.

``available_qty`` is deliberately *absent*: availability is ``on_hand_qty -
reserved_qty``, and a stored derived column is a second source of truth that
can disagree with its inputs.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.types import UtcDateTime

__all__ = [
    "CarrierServiceRow",
    "HolidayRow",
    "StockLevelRow",
    "WarehouseRow",
]


class WarehouseRow(Base, TimestampMixin):
    """A stocking location. ``location_code`` is a three-letter code (§4.2)."""

    __tablename__ = "warehouses"
    __table_args__ = (
        CheckConstraint("length(location_code) = 3", name="location_code_len"),
        CheckConstraint("length(country_code) = 2", name="country_code_len"),
    )

    location_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


class StockLevelRow(Base, TimestampMixin):
    """Stock of one product at one location, as of a stated instant.

    ``as_of`` is required: a stock figure without a timestamp cannot be
    defended later when a customer asks why a quantity was promised.
    ``reserved_qty <= on_hand_qty`` is enforced because a negative available
    quantity is a data error, not a business condition.
    """

    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint("location_code", "product_id", name="uq_stock_levels_location_product"),
        CheckConstraint("on_hand_qty >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved_qty >= 0", name="reserved_non_negative"),
        CheckConstraint("inbound_qty >= 0", name="inbound_non_negative"),
        CheckConstraint("reserved_qty <= on_hand_qty", name="reserved_within_on_hand"),
    )

    location_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("warehouses.location_code", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    product_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    on_hand_qty: Mapped[int] = mapped_column(nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Stock on its way, with an ETA. Used for ``earliest_full_availability``.
    inbound_qty: Mapped[int] = mapped_column(nullable=False, default=0)
    inbound_eta: Mapped[date | None] = mapped_column(Date, nullable=True)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class CarrierServiceRow(Base, TimestampMixin):
    """A shipping service from one origin, with its transit time and cutoff."""

    __tablename__ = "carrier_services"
    __table_args__ = (
        CheckConstraint("transit_days_min >= 0", name="transit_days_min_non_negative"),
        CheckConstraint("transit_days_max >= 0", name="transit_days_max_non_negative"),
        CheckConstraint("transit_days_max >= transit_days_min", name="transit_range_ordered"),
        CheckConstraint("cutoff_hour_utc >= 0 AND cutoff_hour_utc <= 23", name="cutoff_hour_range"),
    )

    service_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    carrier: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    origin_location: Mapped[str] = mapped_column(
        String(3), ForeignKey("warehouses.location_code", ondelete="RESTRICT"), nullable=False
    )
    transit_days_min: Mapped[int] = mapped_column(nullable=False)
    transit_days_max: Mapped[int] = mapped_column(nullable=False)
    #: Latest hour (UTC) an order can ship same-day on this service.
    cutoff_hour_utc: Mapped[int] = mapped_column(nullable=False, default=12)
    runs_on_weekends: Mapped[bool] = mapped_column(nullable=False, default=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


Index("ix_carrier_services_origin_location", CarrierServiceRow.origin_location)


class HolidayRow(Base):
    """A non-working day per country, subtracted by the delivery calculation."""

    __tablename__ = "holidays"
    __table_args__ = (CheckConstraint("length(country_code) = 2", name="country_code_len"),)

    country_code: Mapped[str] = mapped_column(String(2), primary_key=True, autoincrement=False)
    holiday_date: Mapped[date] = mapped_column(Date, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
