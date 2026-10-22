"""A raw RFQ body in; a persisted DRAFT, a blocked DRAFT or a typed refusal out.

This is Project 04's vertical slice, and it is a composition, not a new engine.
It joins three accepted pieces in the only order that keeps their guarantees:

1. **metadata is the caller's, never the model's.** :class:`RawRfq` carries the
   body exactly as received plus the trusted fields intake needs - the sender,
   the arrival instant, the subject and the channel. Everything the downstream
   contracts need and no human typed - the RFQ, run, quote and line identifiers,
   the quote number, the pricing/stock/delivery instants - is *derived here*, by
   trusted code, from that metadata and the body digest. A model never sees those
   fields and cannot name, change or invent one of them.
2. **one bounded model call produces claims, or nothing happens.** The body goes
   to :func:`rfq_agent.llm_extraction.extract_claims` as untrusted content in an
   accepted envelope. No intake row is written until the model has returned a
   schema-valid claim that attempts to set no system-owned field. A timeout, a
   provider error, invalid JSON, a schema violation or an empty extraction is a
   typed refusal that stores **nothing**: there is no half-created request to
   retry against.
3. **the accepted pipeline decides everything else, unchanged.**
   :func:`rfq_agent.acceptance.accept_rfq` stores the declaration, reads the
   source back from the database, binds the customer, resolves every line against
   the catalogue, verifies the claims against the stored source, prices, gates and
   persists a quote - or refuses. This module adds no rule to any of that: it does
   not price, stock, discount, approve, send, transition a run or write a row.

The outcomes are therefore exactly the accepted vocabulary plus the extraction
stage's own: an :class:`~rfq_agent.acceptance.AcceptanceOutcome` when the engine
ran, or an :class:`~rfq_agent.llm_extraction.ExtractionRefusalKind` when no claim
ever reached it. Both are typed, both are printable, and neither carries customer
content.

A repeated delivery of the same raw RFQ - same body, sender, instant, subject and
channel - derives the same identifiers and therefore reports ``DUPLICATE``, writes
nothing new and is not a new quote revision. That is the accepted Case D property,
inherited rather than re-implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import StringConstraints, model_validator

from rfq_agent.acceptance import AcceptanceOutcome, AcceptanceReport, FreshRfq, accept_rfq
from rfq_agent.domain.extraction import ExtractionResult
from rfq_agent.domain.intake import SourceChannel
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import DomainModel, fingerprint
from rfq_agent.intake import IntakeDeclaration
from rfq_agent.llm_extraction import (
    ExtractionRefusal,
    ExtractionRefusalKind,
    ExtractionSummary,
    extract_claims,
)

if TYPE_CHECKING:
    from rfq_agent.contracts.llm import LLMProvider
    from rfq_agent.persistence.engine import Database

__all__ = [
    "RawAcceptanceReport",
    "RawRfq",
    "accept_raw_rfq",
    "derive_identifiers",
]

#: Largest body this path accepts, in characters. The same bound the accepted
#: intake declaration enforces, restated here so an oversized delivery is refused
#: before a model call is paid for.
_MAX_BODY_CHARS = 65_536

#: Width of the ordinal suffix on a derived line identifier. ``ExtractionResult``
#: allows at most 50 lines, so two digits covers every supported claim.
_MIN_LINE_ID_ORDINAL_WIDTH = 2


class RawRfq(DomainModel):
    """One raw RFQ delivery, as a trusted caller states it.

    ``body`` is the untrusted customer content, carried verbatim and hashed - never
    parsed here. Everything else is metadata the surrounding system holds: who
    sent it, when it arrived, what subject it carried and which channel delivered
    it. Nothing in this model is a business fact and nothing in it comes from a
    model.

    ``SourceChannel.FIXTURE`` is refused for the same reason the accepted intake
    declaration refuses it: the production path must not be able to claim the
    demonstration's channel.
    """

    body: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_BODY_CHARS)]
    sender_email: Annotated[
        str,
        StringConstraints(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ]
    sender_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    received_at: datetime
    subject: Annotated[str, StringConstraints(max_length=500)] = ""
    source_channel: SourceChannel = SourceChannel.EMAIL

    @model_validator(mode="after")
    def _check_the_delivery(self) -> Self:
        """Refuse the demo channel and a timestamp with no timezone."""
        if self.source_channel is SourceChannel.FIXTURE:
            msg = (
                "source_channel FIXTURE belongs to the demonstration: a raw delivery "
                "must name EMAIL, WEB_FORM or API"
            )
            raise ValueError(msg)
        if self.received_at.utcoffset() is None:
            msg = "received_at must be timezone-aware: history is ordered in UTC"
            raise ValueError(msg)
        return self

    @property
    def content(self) -> UntrustedText:
        """The body as the untrusted value object the rest of the system uses."""
        return UntrustedText.from_text(self.body)


@dataclass(frozen=True, slots=True)
class RawAcceptanceReport:
    """What one raw acceptance established, ready for a caller to print.

    ``outcome`` is either an accepted :class:`AcceptanceOutcome` (the engine ran
    and this is what it stored or refused) or an
    :class:`ExtractionRefusalKind` (no claim reached the engine, and nothing was
    stored). Exactly one of ``acceptance`` and ``refusal`` is set.

    No field carries customer content: identifiers, enum values, counts, amounts
    and fixed sentences only.
    """

    outcome: AcceptanceOutcome | ExtractionRefusalKind
    rfq_id: str
    run_id: str
    #: The call's accounting, when a call produced a claim.
    extraction: ExtractionSummary | None = None
    #: The accepted engine's full report, when the engine ran.
    acceptance: AcceptanceReport | None = None
    #: Why no claim reached the engine, when none did.
    refusal: ExtractionRefusal | None = None

    @property
    def engine_ran(self) -> bool:
        """Whether the accepted engine ran - whatever it then decided."""
        return self.acceptance is not None


@dataclass(frozen=True, slots=True)
class _Identifiers:
    """The downstream identities and instants, derived from trusted metadata.

    Derivation, never generation: there is no clock and no randomness, so the same
    delivery always produces the same identities and a replay is decided by the
    accepted idempotency rules rather than by chance.
    """

    rfq_id: str
    run_id: str
    quote_id: str
    quote_number: str
    idempotency_key: str
    line_prefix: str
    pricing_as_of: date
    asked_at: datetime

    def line_item_ids(self, count: int) -> tuple[str, ...]:
        """One stated identifier per claimed line, unique by ordinal."""
        return tuple(
            f"{self.line_prefix}{ordinal:0{_MIN_LINE_ID_ORDINAL_WIDTH}d}"
            for ordinal in range(1, count + 1)
        )


def derive_identifiers(raw: RawRfq) -> tuple[str, str, str, str, str]:
    """Return the identities a raw delivery implies, as plain strings.

    Exposed so an operator can compute the identifiers a delivery will use without
    calling a provider, and so tests can assert derivation is stable. The basis is
    the trusted metadata plus the body digest - never the model's output, which
    does not exist yet at this point.

    Returns:
        ``(rfq_id, run_id, quote_id, quote_number, idempotency_key)``.
    """
    identities = _derive(raw)
    return (
        identities.rfq_id,
        identities.run_id,
        identities.quote_id,
        identities.quote_number,
        identities.idempotency_key,
    )


def accept_raw_rfq(
    raw: RawRfq,
    *,
    provider: LLMProvider,
    database: Database,
    model: str,
    temperature: Decimal = Decimal("0"),
    seed: int | None = None,
    max_output_tokens: int | None = None,
) -> RawAcceptanceReport:
    """Extract one model's claims from ``raw`` and pass them to the accepted engine.

    Steps, in order:

    1. derive the identities from trusted metadata;
    2. make one bounded structured-output call for the body;
    3. on a typed extraction refusal, return it and store nothing;
    4. otherwise build the accepted :class:`~rfq_agent.acceptance.FreshRfq`
       declaration - model claims, trusted metadata, derived identities - and let
       :func:`~rfq_agent.acceptance.accept_rfq` store, verify, price and persist,
       or refuse, exactly as it does for a hand-written declaration.

    Args:
        raw: The delivery: body and trusted metadata.
        provider: Any implementation of the provider-neutral port.
        database: A migrated and seeded database.
        model: The configured model identifier, never hardcoded here.
        temperature: Sampling temperature, from configuration.
        seed: Optional sampling seed, from configuration.
        max_output_tokens: Completion-token ceiling for the call.

    Returns:
        A report whose outcome is either an accepted
        :class:`~rfq_agent.acceptance.AcceptanceOutcome` or an
        :class:`~rfq_agent.llm_extraction.ExtractionRefusalKind`.

    Raises:
        rfq_agent.acceptance.AcceptanceContentionError: When the database stayed
            locked for the accepted path's whole retry budget.
        OperationalError: A database failure the accepted path does not classify.
    """
    identities = _derive(raw)
    extraction = extract_claims(
        raw.content,
        provider,
        model=model,
        temperature=temperature,
        seed=seed,
        max_output_tokens=max_output_tokens,
    )
    if isinstance(extraction, ExtractionRefusal):
        return RawAcceptanceReport(
            outcome=extraction.kind,
            rfq_id=identities.rfq_id,
            run_id=identities.run_id,
            refusal=extraction,
        )
    request = _fresh_request(raw, identities, extraction.claims)
    acceptance = accept_rfq(request, database)
    return RawAcceptanceReport(
        outcome=acceptance.outcome,
        rfq_id=identities.rfq_id,
        run_id=identities.run_id,
        extraction=extraction.summary,
        acceptance=acceptance,
    )


def _fresh_request(raw: RawRfq, identities: _Identifiers, claims: ExtractionResult) -> FreshRfq:
    """Build the accepted declaration from trusted metadata and model claims.

    The claim object is passed through whole - delivery, open questions and all -
    because omitting any part of it would verify a smaller request than the model
    made. Only the identities, the instants and the source metadata come from
    here.
    """
    return FreshRfq(
        intake=IntakeDeclaration(
            rfq_id=identities.rfq_id,
            run_id=identities.run_id,
            idempotency_key=identities.idempotency_key,
            source_channel=raw.source_channel,
            received_at=raw.received_at,
            subject=raw.subject,
            body=raw.body,
            sender_name=raw.sender_name,
            sender_email=raw.sender_email,
        ),
        quote_id=identities.quote_id,
        quote_number=identities.quote_number,
        line_item_ids=identities.line_item_ids(len(claims.lines)),
        pricing_as_of=identities.pricing_as_of,
        stock_as_of=identities.asked_at,
        delivery_as_of=identities.asked_at,
        extraction=claims,
    )


def _derive(raw: RawRfq) -> _Identifiers:
    """Derive every downstream identity from the delivery's trusted metadata.

    The digest is taken over canonical JSON of the metadata and the body digest, so
    it is stable, contains no customer text itself and is reproducible without a
    provider call. The ``received_at`` used for the pricing date and the asked-at
    instants is the delivery's own instant in UTC: no clock is read.
    """
    received_utc = raw.received_at.astimezone(UTC)
    digest = fingerprint(
        {
            "body_sha256": raw.content.sha256,
            "received_at": received_utc.isoformat(),
            "sender_email": raw.sender_email,
            "sender_name": raw.sender_name,
            "source_channel": raw.source_channel.value,
            "subject": raw.subject,
        }
    )
    return _Identifiers(
        rfq_id=f"RFQ_{digest[:16].upper()}",
        run_id=f"RUN_{digest[:16].upper()}",
        quote_id=f"QTE_{digest[:16].upper()}",
        # ``QuoteNumber`` is ``Q-<year>-<4..8 digits>``; the trailing digits come
        # from the same digest so a replay is byte-identical.
        quote_number=f"Q-{received_utc.year}-{int(digest[16:24], 16) % 100_000_000:08d}",
        idempotency_key=f"raw:{digest}",
        line_prefix=f"LI_{digest[24:34].upper()}",
        pricing_as_of=received_utc.date(),
        asked_at=raw.received_at,
    )
