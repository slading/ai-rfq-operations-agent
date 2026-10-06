"""The deterministic write path: accepted output in, rows out (Phase 1J').

Two accepted decisions meet here. Phase 1H produced a :class:`Quote` in exact
``Decimal`` and Phase 1I projected the facts it found into a
:class:`QuoteBlockedLedger`; this module stores both, and does nothing else. It
recalculates nothing, reinterprets nothing, gates nothing and transitions
nothing: every value it writes is a value some earlier phase already decided.

Three properties are deliberate.

**One transaction per quote.** The header, its lines and the ledger evidence are
written inside a single :meth:`~rfq_agent.persistence.engine.Database.session`
unit of work, whose contract is "commit on success, roll back on any exception".
A failure anywhere - a constraint the database refuses, a missing run - leaves no
partial quotation behind, not even a claim that one was attempted.

**A retry is not a revision.** Repeating the *same* write stores nothing new: the
operation's key is a pure function of what would be written (the quote and the
ledger), so the second attempt finds its own claim already taken and reports the
rows that are already there. A *different* calculation for the same run is
refused by ``uq_quotes_run_id_revision`` rather than quietly becoming revision 2 -
inventing a revision policy is a later phase's decision, not this module's.

**The domain's sentinel stops here.** ``PRICE_MISSING`` is how Phase 1H says "this
line has no price row". ``quote_lines.price_entry_id`` is nullable and foreign -
keyed (D-1), so the sentinel is translated to ``NULL`` at this boundary, and the
pairing is enforced by ``ck_quote_lines_price_provenance_pairing`` in both
directions. The domain object is never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from rfq_agent.domain.policy import QuoteBlockedLedger
from rfq_agent.domain.pricing import PriceLookupStatus
from rfq_agent.domain.quote import MISSING_PRICE_ENTRY_ID, Quote, QuoteCalculation, QuoteLine
from rfq_agent.domain.values import canonical_json, sha256_text
from rfq_agent.observability.ids import Clock, SystemClock
from rfq_agent.persistence.models import (
    IdempotencyClaimRow,
    QuoteBlockedReasonRow,
    QuoteLineRow,
    QuoteRow,
    RunRow,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from rfq_agent.persistence.engine import Database

__all__ = [
    "CLAIM_SCOPE",
    "QuoteWriteResult",
    "QuoteWriter",
    "SqlIdempotencyStore",
    "quote_line_id",
]

#: The scope every quote-persistence claim is filed under. One key may mean
#: different things in different scopes, which is why the claim table is keyed by
#: the pair.
CLAIM_SCOPE = "quote_persist"

#: Identifier columns hold at most this many characters.
_MAX_IDENTIFIER = 64

#: How much of a digest a hashed identifier keeps: enough to be collision-free,
#: short enough to leave room for a prefix.
_DIGEST_PREFIX = 40


@dataclass(frozen=True, slots=True)
class QuoteWriteResult:
    """What a write stored - whether it wrote it or found it already there."""

    quote_id: str
    #: The lines' identifiers, in ordinal order.
    line_ids: tuple[str, ...]
    #: The ledger positions stored, in report order.
    ledger_seqs: tuple[int, ...]
    #: ``True`` when this call found the operation already stored and wrote nothing.
    duplicate: bool


def quote_line_id(quote_id: str, ordinal: int) -> str:
    """Return the deterministic identifier for a quote line.

    The domain's :class:`~rfq_agent.domain.quote.QuoteLine` carries no identifier
    of its own - it is identified by its quote and its ordinal - so the writer
    mints one, and mints the same one every time: ``<quote_id>-L<ordinal>``.

    The readable form is used whenever it fits the 64-character column. A quote
    id long enough to push it over gets a hashed id instead, because a silently
    truncated identifier would eventually collide with a different line.
    """
    readable = f"{quote_id}-L{ordinal:02d}"
    if len(readable) <= _MAX_IDENTIFIER:
        return readable
    return f"L{sha256_text(f'{quote_id}:{ordinal}')[:_DIGEST_PREFIX]}"


class SqlIdempotencyStore:
    """``IdempotencyStore`` over ``idempotency_claims``.

    The claim is taken with ``INSERT ... ON CONFLICT DO NOTHING``, so an
    already-claimed key is a *row count* rather than an exception that would
    poison the caller's transaction. Nothing is committed here: the store
    participates in whatever unit of work its session is in, which is what lets
    the writer claim and write atomically - if the write fails, the rollback
    releases the claim with it, and the retry is free to try again.
    """

    def __init__(self, session: Session, *, clock: Clock | None = None) -> None:
        """Bind the store to ``session``, taking "now" from ``clock``."""
        self._session = session
        self._clock = clock if clock is not None else SystemClock()

    def claim(self, key: str, *, scope: str, ttl_seconds: int | None = None) -> bool:
        """Atomically claim ``key``; ``False`` means it was already claimed."""
        claimed_at = self._clock.now()
        expires_at = None if ttl_seconds is None else claimed_at + timedelta(seconds=ttl_seconds)
        statement = (
            sqlite_insert(IdempotencyClaimRow)
            .values(
                scope=scope,
                claim_key=key,
                claimed_at=claimed_at,
                expires_at=expires_at,
                released_at=None,
            )
            .on_conflict_do_nothing(index_elements=["scope", "claim_key"])
        )
        return bool(self._session.execute(statement).rowcount)

    def release(self, key: str, *, scope: str) -> None:
        """Release a claim, e.g. after a failed attempt that may be retried."""
        self._session.execute(
            delete(IdempotencyClaimRow).where(
                IdempotencyClaimRow.scope == scope,
                IdempotencyClaimRow.claim_key == key,
            )
        )

    def is_claimed(self, key: str, *, scope: str) -> bool:
        """Whether ``key`` is currently claimed."""
        return (
            self._session.scalar(
                select(IdempotencyClaimRow.claim_key).where(
                    IdempotencyClaimRow.scope == scope,
                    IdempotencyClaimRow.claim_key == key,
                )
            )
            is not None
        )


class QuoteWriter:
    """Store one accepted calculation and its projected ledger, atomically.

    The caller supplies facts that are already decided; the database decides
    whether they are representable. A quotation whose price lookup failed is
    representable (the line is stored blocked, with no price entry id), and a
    quotation whose ledger is empty is representable (no ledger rows).
    """

    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        """Write to ``database``, stamping ledger rows from ``clock``."""
        self._database = database
        self._clock = clock if clock is not None else SystemClock()

    def persist(
        self,
        calculation: QuoteCalculation,
        ledger: QuoteBlockedLedger,
        *,
        claim_ttl_seconds: int | None = None,
    ) -> QuoteWriteResult:
        """Store ``calculation`` and ``ledger``; write nothing on a retry.

        Args:
            calculation: The accepted arithmetic outcome. Its lines are written in
                ordinal order, and a non-``FOUND`` line's ``PRICE_MISSING``
                sentinel becomes ``NULL``.
            ledger: The projection for exactly this quote and run. Its reasons are
                written in the order it reports them, numbered from 1.
            claim_ttl_seconds: Optional lifetime for the idempotency claim; the
                claim is permanent when omitted.

        Returns:
            What was stored, with ``duplicate`` true when this call found the same
            operation already stored.

        Raises:
            ValueError: If the ledger does not belong to this quote and run, if
                the run does not exist, or if a line's provenance contradicts its
                price status.
            RuntimeError: If a claim exists without the rows it claims (a defect
                that cannot be produced by a commit, and so is never expected).
        """
        quote = calculation.quote
        _check_the_ledger_belongs_to_the_quote(quote, ledger)
        operation_key = _operation_key(calculation, ledger)

        with self._database.session() as session:
            claims = SqlIdempotencyStore(session, clock=self._clock)
            if not claims.claim(operation_key, scope=CLAIM_SCOPE, ttl_seconds=claim_ttl_seconds):
                return _stored_result(session, quote.quote_id)

            rfq_id = _rfq_id_for(session, quote.run_id)
            lines = _line_rows(quote)
            written_at = self._clock.now()
            # Level by level, then flush: the models declare no relationships, so
            # the unit of work has no dependency order to sort by - and the
            # foreign keys are enforced.
            session.add(_quote_row(quote, rfq_id=rfq_id))
            session.flush()
            session.add_all(lines)
            session.add_all(_ledger_rows(ledger, written_at=written_at))
            session.flush()

            return QuoteWriteResult(
                quote_id=quote.quote_id,
                line_ids=tuple(row.line_id for row in lines),
                ledger_seqs=tuple(range(1, len(ledger.reasons) + 1)),
                duplicate=False,
            )


def _check_the_ledger_belongs_to_the_quote(quote: Quote, ledger: QuoteBlockedLedger) -> None:
    """Refuse evidence attributed to a different quote or run.

    Not a business rule: the ledger is *about* one quotation, and storing it
    against another would be a false attribution the schema cannot detect.
    """
    if ledger.quote_id != quote.quote_id:
        msg = f"ledger is for quote {ledger.quote_id!r}, not {quote.quote_id!r}"
        raise ValueError(msg)
    if ledger.run_id != quote.run_id:
        msg = f"ledger is for run {ledger.run_id!r}, but the quote is {quote.run_id!r}"
        raise ValueError(msg)


def _operation_key(calculation: QuoteCalculation, ledger: QuoteBlockedLedger) -> str:
    """Return the key that identifies this write, as a pure function of its content.

    Everything that would be stored is in the payload, so two calls that would
    produce identical rows produce identical keys - and a call that would produce
    *different* rows for an operation already claimed is a contradiction the
    database refuses rather than a retry the writer mistakes for one.
    """
    return sha256_text(
        canonical_json(
            {
                "quote": calculation.quote.model_dump(mode="json"),
                "ledger": ledger.model_dump(mode="json"),
            }
        )
    )


def _stored_result(session: Session, quote_id: str) -> QuoteWriteResult:
    """Read back what an earlier, identical write stored."""
    line_ids = tuple(
        session.scalars(
            select(QuoteLineRow.line_id)
            .where(QuoteLineRow.quote_id == quote_id)
            .order_by(QuoteLineRow.ordinal)
        )
    )
    ledger_seqs = tuple(
        session.scalars(
            select(QuoteBlockedReasonRow.seq)
            .where(QuoteBlockedReasonRow.quote_id == quote_id)
            .order_by(QuoteBlockedReasonRow.seq)
        )
    )
    if not line_ids:
        msg = (
            f"claim exists for quote {quote_id!r} but no lines were stored: "
            "claims and rows are written in one transaction, so the database is inconsistent"
        )
        raise RuntimeError(msg)
    return QuoteWriteResult(
        quote_id=quote_id,
        line_ids=line_ids,
        ledger_seqs=ledger_seqs,
        duplicate=True,
    )


def _rfq_id_for(session: Session, run_id: str) -> str:
    """Read the RFQ a run belongs to; a quote cannot be stored without one."""
    rfq_id = session.scalar(select(RunRow.rfq_id).where(RunRow.run_id == run_id))
    if rfq_id is None:
        msg = f"run {run_id!r} does not exist, so its quote cannot be stored"
        raise ValueError(msg)
    return rfq_id


def _quote_row(quote: Quote, *, rfq_id: str) -> QuoteRow:
    """Map the accepted header onto its row. No value is recomputed.

    ``revision`` is left at the schema's default (1): this layer stores a
    calculation, it does not decide that a second one is a new revision. The
    policy columns are left ``NULL`` - a gate that never ran has no outcome to
    record - and ``approved_at`` with them.

    The transit range is a documented choice rather than a mapping.
    ``promise.transit_days`` *is* the service's ``transit_days_min`` - the
    accepted delivery contract counts the advertised range at its minimum and
    quotes the range in prose - so the minimum is stored and the maximum is left
    ``NULL``: the promise never states it, and a ``NULL`` column means "unknown"
    where a copied minimum would have meant "one day".
    """
    delivery = quote.delivery
    promise = delivery.promise if delivery is not None else None
    discount = quote.discount
    return QuoteRow(
        quote_id=quote.quote_id,
        quote_number=quote.quote_number,
        run_id=quote.run_id,
        rfq_id=rfq_id,
        customer_id=quote.customer_id,
        status=quote.status,
        currency=quote.currency,
        subtotal=quote.subtotal,
        discount_rule_id=discount.rule_id if discount is not None else None,
        discount_scope=discount.scope if discount is not None else None,
        discount_percent=discount.percent if discount is not None else None,
        discount_amount=quote.discount_amount,
        total=quote.total,
        pricing_as_of=quote.pricing_as_of,
        calc_version=quote.calc_version,
        inputs_sha256=quote.inputs_sha256 or quote.inputs_fingerprint(),
        delivery_feasibility=promise.feasibility if promise is not None else None,
        delivery_destination=promise.destination if promise is not None else None,
        origin_location=promise.origin_location if promise is not None else None,
        carrier_service_code=promise.carrier_service_code if promise is not None else None,
        transit_days_min=promise.transit_days if promise is not None else None,
        earliest_ship_date=promise.earliest_ship_date if promise is not None else None,
        earliest_delivery_date=promise.earliest_delivery_date if promise is not None else None,
        requested_date=promise.requested_date if promise is not None else None,
        split_shipment_proposed=delivery.split_shipment_proposed if delivery is not None else False,
    )


def _line_rows(quote: Quote) -> list[QuoteLineRow]:
    """Map every line, in ordinal order, which is also the order they are written."""
    ordered = sorted(quote.lines, key=lambda line: line.ordinal)
    return [_line_row(quote.quote_id, line) for line in ordered]


def _line_row(quote_id: str, line: QuoteLine) -> QuoteLineRow:
    """Map one accepted line onto its row, provenance included."""
    return QuoteLineRow(
        line_id=quote_line_id(quote_id, line.ordinal),
        quote_id=quote_id,
        ordinal=line.ordinal,
        product_id=line.product_id,
        sku=line.sku,
        description=line.description,
        quantity=line.quantity,
        unit_price=line.unit_price,
        price_entry_id=_price_entry_id(line),
        line_extension=line.line_extension,
        currency=line.currency,
        stock_status=line.stock_status,
        price_status=line.price_status,
        blocked=line.blocked,
        blocked_reason=line.blocked_reason,
        notes=line.notes,
    )


def _price_entry_id(line: QuoteLine) -> str | None:
    """Translate the domain's price provenance, sentinel and all (D-1).

    The only reinterpretation in the module, and it is not a business decision:
    a ``FOUND`` line names the price row it used, and a line with no usable price
    names nothing. A line that claims otherwise is refused here rather than
    stored, because either way it would be evidence of a price that does not
    exist.
    """
    if line.price_status is PriceLookupStatus.FOUND:
        if line.price_entry_id == MISSING_PRICE_ENTRY_ID:
            msg = f"line {line.ordinal} is FOUND but carries no price entry id"
            raise ValueError(msg)
        return line.price_entry_id
    if line.price_entry_id != MISSING_PRICE_ENTRY_ID:
        msg = (
            f"line {line.ordinal} has price status {line.price_status} but names "
            f"price entry {line.price_entry_id!r}"
        )
        raise ValueError(msg)
    return None


def _ledger_rows(
    ledger: QuoteBlockedLedger, *, written_at: datetime
) -> list[QuoteBlockedReasonRow]:
    """Map the projected ledger onto its rows, in the order it reported them.

    ``seq`` is that order: "which reason came first" is part of the evidence the
    projection produces, so it is stored rather than recomputed on read. The
    ledger's flags are written on every row, because the flags belong to the
    ledger rather than to any one reason, and an empty list is written as an
    empty list - "no flags" is a claim, and a ``NULL`` would not be one.
    """
    flags = [flag.value for flag in ledger.flags]
    return [
        QuoteBlockedReasonRow(
            quote_id=ledger.quote_id,
            seq=seq,
            run_id=ledger.run_id,
            code=reason.code,
            message=reason.message,
            line_ordinal=reason.line_ordinal,
            resolvable_by_human=reason.resolvable_by_human,
            flags_json=list(flags),
            created_at=written_at,
        )
        for seq, reason in enumerate(ledger.reasons, start=1)
    ]
