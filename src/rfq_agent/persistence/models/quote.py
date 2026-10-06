"""Computed quotations (``quotes``, ``quote_lines``).

These tables record the *result* of deterministic calculation. Two columns make
that verifiable after the fact:

* ``inputs_sha256`` - a fingerprint of every input the calculation consumed
  (prices, stock, quantities, rules, ``pricing_as_of``). Re-deriving the quote
  from the same inputs must produce a byte-identical fingerprint, which is what
  makes recalculation reproducible rather than merely repeatable.
* ``calc_version`` - which version of the calculation produced this number. A
  quote from an older version stays explainable after the rules change.

One column carries a deliberate exception: ``quote_lines.price_entry_id`` is
nullable (Phase 1J', D-1). A line whose price lookup did not return ``FOUND`` has
no price row to point at, so the domain carries the sentinel ``PRICE_MISSING``
and this table stores ``NULL``; the pairing is enforced both ways by
``ck_quote_lines_price_provenance_pairing``, and the foreign key stays in place,
so a value that *is* present is still a row that exists.

Money arithmetic is *not* re-checked by a database ``CHECK``: SQLite stores
``NUMERIC`` as a float and exact float equality against a ``Decimal``-derived
total is not guaranteed at every magnitude. Rejecting a correct quotation would
be worse than the guarantee is worth. The arithmetic contract lives in
:class:`~rfq_agent.domain.quote.Quote`, in exact ``Decimal``, where it belongs.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_agent.domain.delivery import DeliveryFeasibility
from rfq_agent.domain.policy import BlockedReasonCode, DiscountScope
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import CALC_VERSION, QuoteStatus
from rfq_agent.domain.stock import StockStatus
from rfq_agent.domain.values import Json
from rfq_agent.observability.ids import utc_now
from rfq_agent.persistence.base import Base, TimestampMixin
from rfq_agent.persistence.types import (
    JSON_PAYLOAD,
    MONEY,
    PERCENT,
    UNIT_PRICE,
    UtcDateTime,
    enum_type,
)

__all__ = [
    "QuoteBlockedReasonRow",
    "QuoteLineRow",
    "QuoteRow",
]

#: ``Q-<year>-<4..8 digits>`` - the human-visible quote reference.
#:
#: ``GLOB`` is written out by hand because SQLite has no regex function; the
#: ``BETWEEN`` on length encodes the ``\\d{4,8}`` part of the domain pattern.
_QUOTE_NUMBER_CHECK = (
    "length(quote_number) BETWEEN 11 AND 15 AND quote_number GLOB 'Q-[0-9][0-9][0-9][0-9]-[0-9]*'"
)


class QuoteRow(Base, TimestampMixin):
    """One revision of a computed quotation for a run.

    ``revision`` exists because an operator EDIT can legitimately lead to a
    recalculation within the same run: the earlier revision is kept, so the
    audit trail shows what was quoted before the change, not just after.
    """

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("quote_number", name="uq_quotes_quote_number"),
        UniqueConstraint("run_id", "revision", name="uq_quotes_run_id_revision"),
        CheckConstraint(_QUOTE_NUMBER_CHECK, name="quote_number_format"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("length(currency) = 3", name="currency_len"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_amount_non_negative"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint("length(inputs_sha256) = 64", name="inputs_sha256_len"),
        #: "no character outside [0-9a-f]" - a real hex check, not a first-char one.
        CheckConstraint("inputs_sha256 NOT GLOB '*[^0-9a-f]*'", name="inputs_sha256_hex"),
    )

    quote_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quote_number: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    rfq_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("rfqs.rfq_id", ondelete="CASCADE"), nullable=False
    )
    #: A real foreign key. This is the constraint that makes "the model invented
    #: a customer" impossible rather than merely discouraged.
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[QuoteStatus] = mapped_column(
        enum_type(QuoteStatus, name="quote_status"), nullable=False, default=QuoteStatus.DRAFT
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    discount_rule_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("discount_rules.rule_id", ondelete="SET NULL"), nullable=True
    )
    discount_scope: Mapped[DiscountScope | None] = mapped_column(
        enum_type(DiscountScope, name="discount_scope"), nullable=True
    )
    discount_percent: Mapped[Decimal | None] = mapped_column(PERCENT, nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    #: The date the prices were read for. A quote without it cannot be replayed.
    pricing_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False, default=CALC_VERSION)
    inputs_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Outcome of the policy gate, stored so a reviewer can see *why* a quote
    #: needed a human rather than inferring it from the current rule set.
    policy_allowed: Mapped[bool | None] = mapped_column(nullable=True)
    policy_reason_codes_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=True)

    # --- delivery promise (all nullable: no data means "unknown", not "now") ---
    delivery_feasibility: Mapped[DeliveryFeasibility | None] = mapped_column(
        enum_type(DeliveryFeasibility, name="delivery_feasibility"), nullable=True
    )
    delivery_destination: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin_location: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("warehouses.location_code", ondelete="SET NULL"), nullable=True
    )
    carrier_service_code: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("carrier_services.service_code", ondelete="SET NULL"), nullable=True
    )
    transit_days_min: Mapped[int | None] = mapped_column(nullable=True)
    transit_days_max: Mapped[int | None] = mapped_column(nullable=True)
    earliest_ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    earliest_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    split_shipment_proposed: Mapped[bool] = mapped_column(nullable=False, default=False)

    #: Set when a human approval moves the quote to its sendable state.
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    lines: Mapped[list[QuoteLineRow]] = relationship(
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="QuoteLineRow.ordinal",
        lazy="selectin",
    )


Index("ix_quotes_run_id", QuoteRow.run_id)
Index("ix_quotes_rfq_id", QuoteRow.rfq_id)
Index("ix_quotes_customer_id", QuoteRow.customer_id)
Index("ix_quotes_status", QuoteRow.status)


class QuoteLineRow(Base):
    """One priced line of a quotation.

    The ``CHECK`` constraints mirror the domain invariants that are *not* about
    money arithmetic: a blocked line must say why, and a line whose price or
    stock is unusable must be blocked. Those are expressible exactly in SQL and
    therefore enforced even against a hand-written ``UPDATE``.
    """

    __tablename__ = "quote_lines"
    __table_args__ = (
        UniqueConstraint("quote_id", "ordinal", name="uq_quote_lines_quote_id_ordinal"),
        CheckConstraint("ordinal >= 1", name="ordinal_positive"),
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("line_extension >= 0", name="line_extension_non_negative"),
        CheckConstraint("length(currency) = 3", name="currency_len"),
        CheckConstraint(
            "(blocked = 1 AND blocked_reason IS NOT NULL) "
            "OR (blocked = 0 AND blocked_reason IS NULL)",
            name="blocked_requires_reason",
        ),
        #: D-1: status and provenance agree in both directions. A ``FOUND`` price
        #: is evidence of a real price row; a line with no usable price has none
        #: to name, and naming one anyway would be a false provenance claim.
        CheckConstraint(
            "(price_status = 'FOUND') = (price_entry_id IS NOT NULL)",
            name="price_provenance_pairing",
        ),
        CheckConstraint("price_status = 'FOUND' OR blocked = 1", name="unusable_price_blocks_line"),
        CheckConstraint("stock_status <> 'NONE' OR blocked = 1", name="no_stock_blocks_line"),
    )

    line_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quote_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("quotes.quote_id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.product_id", ondelete="RESTRICT"), nullable=False
    )
    #: SKU and description are denormalised on purpose: the quotation is a
    #: document sent to a customer and must not change if the catalog is edited
    #: afterwards.
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(UNIT_PRICE, nullable=False)
    #: Which price row the unit price came from - the evidence for the number.
    #:
    #: ``NULL`` exactly when the lookup produced no usable price (D-1): the
    #: domain's ``PRICE_MISSING`` sentinel is translated to ``NULL`` at the
    #: persistence boundary, and the foreign key is retained - so a non-``NULL``
    #: value still has to be a price entry that exists.
    price_entry_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("price_entries.price_entry_id", ondelete="RESTRICT"), nullable=True
    )
    line_extension: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    stock_status: Mapped[StockStatus] = mapped_column(
        enum_type(StockStatus, name="stock_status"), nullable=False, default=StockStatus.UNKNOWN
    )
    price_status: Mapped[PriceLookupStatus] = mapped_column(
        enum_type(PriceLookupStatus, name="price_lookup_status"),
        nullable=False,
        default=PriceLookupStatus.FOUND,
    )
    blocked: Mapped[bool] = mapped_column(nullable=False, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)

    quote: Mapped[QuoteRow] = relationship(back_populates="lines")


class QuoteBlockedReasonRow(Base):
    """One projected blocking reason, append-only (``quote_blocked_reasons``).

    Phase 1I projects the facts a calculation produced onto a
    :class:`~rfq_agent.domain.policy.QuoteBlockedLedger`; this table is where that
    projection is kept. One row per reason, in the ledger's own order, with the
    ledger's flags alongside.

    Three things it deliberately is *not*, each of which already has a home:

    * not a policy-gate outcome (that is ``quotes.policy_allowed`` and
      ``quotes.policy_reason_codes_json``, written by the gate, which this table
      never runs);
    * not a workflow transition (that is ``run_events``, whose every row must be
      a legal edge of the state machine);
    * not a human action (that is ``human_actions``) and not an observability
      record (``llm_calls``/``tool_calls``).

    ``code`` is a :class:`~rfq_agent.domain.policy.BlockedReasonCode`, the
    contract's own vocabulary, and ``seq`` is the ledger position: the ordering
    is part of the evidence, because "which reason was reported first" is a
    deterministic property of the projection rather than an artefact of storage.

    The table is append-only: the migration installs ``UPDATE``/``DELETE``
    triggers, so evidence cannot be rewritten any more than a run's history can.
    """

    __tablename__ = "quote_blocked_reasons"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("length(message) BETWEEN 1 AND 300", name="message_len"),
        CheckConstraint("line_ordinal IS NULL OR line_ordinal >= 1", name="line_ordinal_positive"),
        #: One entry per code, mirroring the gate input's refusal of duplicate
        #: codes: two rows with the same code would be two claims about one fact.
        UniqueConstraint("quote_id", "code", name="uq_quote_blocked_reasons_quote_id_code"),
    )

    quote_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("quotes.quote_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    #: Position in the ledger's deterministic order, starting at 1.
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[BlockedReasonCode] = mapped_column(
        enum_type(BlockedReasonCode, name="blocked_reason_code"), nullable=False
    )
    #: The operator-facing sentence, as projected - not re-worded on the way in.
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The offending line, when the reason is about one; ``NULL`` for quote-level
    #: reasons (a delivery promise, a discount rule, a credit hold).
    line_ordinal: Mapped[int | None] = mapped_column(nullable=True)
    #: Whether a human could clear this reason - the contract's own flag.
    resolvable_by_human: Mapped[bool] = mapped_column(nullable=False, default=True)
    #: The ledger's ``PolicyFlag`` values, as written. Always a list, never NULL:
    #: "no flags" and "flags nobody recorded" are different claims.
    flags_json: Mapped[Json] = mapped_column(JSON_PAYLOAD, nullable=False, default=list)
    #: When the row was written. An explicit column rather than ``TimestampMixin``
    #: because this table never updates - an ``updated_at`` here would be a lie,
    #: the same reason ``run_events`` carries ``occurred_at``.
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)


Index("ix_quote_blocked_reasons_run_id", QuoteBlockedReasonRow.run_id)
