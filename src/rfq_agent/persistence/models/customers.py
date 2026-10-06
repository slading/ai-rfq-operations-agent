"""Customer master data (``customers``, ``customer_aliases``) - §5.2.

Business master data lives in the same database as the operational records so
that ``quotes.customer_id`` can be a real foreign key. That reference is the
database-level form of the project's central rule: a quotation can only name a
customer that exists in the master data, never one a model produced.

The demo dataset (``rfq_agent.seed``) populates both tables: the alias rows are
how a customer is recognised from a name, a domain or a legacy legal name. The
matching rules that consume them arrive with the repositories (Phase 1C).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.enums import AliasKind
from rfq_agent.persistence.types import MONEY, enum_type

__all__ = [
    "CustomerAliasRow",
    "CustomerRow",
]


class CustomerRow(Base, TimestampMixin):
    """A customer master-data record.

    Deliberately small. This is not a CRM: it holds exactly what the quote flow
    needs - who they are, which currency they are quoted in, how they pay, and
    whether they are on credit hold (failure case F11).
    """

    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("length(country_code) = 2", name="country_code_len"),
        CheckConstraint("length(default_currency) = 3", name="default_currency_len"),
        CheckConstraint(
            "payment_terms_days >= 0 AND payment_terms_days <= 365",
            name="payment_terms_range",
        ),
        CheckConstraint("credit_limit IS NULL OR credit_limit >= 0", name="credit_limit_positive"),
    )

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    #: Currency this customer is quoted in. A price in another currency is a
    #: blocking condition, never a conversion (§7 CURRENCY_MISMATCH).
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_terms_days: Mapped[int] = mapped_column(nullable=False, default=30)
    credit_limit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    #: When set, quotes for this customer are blocked pending a human decision.
    credit_hold: Mapped[bool] = mapped_column(nullable=False, default=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CustomerAliasRow(Base):
    """Alternate strings that may identify a customer (``customer_aliases``).

    Because a normalised alias may legitimately be ambiguous, ``normalized`` is
    *not* unique on its own: two customers sharing a trading name is exactly the
    ``AMBIGUOUS_MATCH`` case the resolver must report rather than guess.
    """

    __tablename__ = "customer_aliases"
    __table_args__ = (CheckConstraint("length(normalized) >= 1", name="normalized_not_empty"),)

    customer_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("customers.customer_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    #: Case-folded, whitespace-collapsed form used for lookup, produced by
    #: :func:`rfq_agent.seed.normalize.normalize_alias` and stored so the search
    #: is an index hit rather than a scan with a formatting guess in it.
    normalized: Mapped[str] = mapped_column(String(200), primary_key=True, autoincrement=False)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[AliasKind] = mapped_column(enum_type(AliasKind, name="alias_kind"), nullable=False)


Index("ix_customer_aliases_normalized", CustomerAliasRow.normalized)
