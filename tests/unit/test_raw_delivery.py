"""Tests for the raw delivery contract and the identities derived from it.

Nothing here calls a provider or a database: this is the trusted metadata a raw
RFQ arrives with, and the derivation that turns it into the identifiers the
accepted contracts demand. Both must be closed and deterministic, because
everything downstream treats them as facts.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from rfq_agent.domain.ids import IdempotencyKey, QuoteId, QuoteNumber, RfqId, RunId
from rfq_agent.domain.values import sha256_text
from rfq_agent.raw_acceptance import RawRfq, derive_identifiers

_BODY = (
    "Hello,\n"
    "please quote 25 pcs of 100-ABC for our standard replenishment.\n"
    "No specific delivery date is required.\n"
    "Best regards,\n"
    "Nordwind Einkauf"
)


def _payload(**overrides: object) -> dict[str, object]:
    """A valid raw delivery, ready to validate."""
    payload: dict[str, object] = {
        "body": _BODY,
        "sender_email": "einkauf@nordwind-industrie.de",
        "sender_name": "Nordwind Einkauf",
        "received_at": "2026-10-06T09:00:00Z",
        "subject": "RFQ: 25 pcs 100-ABC",
        "source_channel": "EMAIL",
    }
    payload.update(overrides)
    return payload


def test_derived_identities_satisfy_the_accepted_identifier_contracts() -> None:
    rfq_id, run_id, quote_id, quote_number, idempotency_key = derive_identifiers(
        RawRfq.model_validate(_payload())
    )

    TypeAdapter(RfqId).validate_python(rfq_id)
    TypeAdapter(RunId).validate_python(run_id)
    TypeAdapter(QuoteId).validate_python(quote_id)
    TypeAdapter(QuoteNumber).validate_python(quote_number)
    TypeAdapter(IdempotencyKey).validate_python(idempotency_key)


def test_the_body_digest_is_the_untrusted_snapshot_digest() -> None:
    delivery = RawRfq.model_validate(_payload())

    assert delivery.content.text == _BODY
    assert delivery.content.sha256 == sha256_text(_BODY)


def test_derivation_is_stable_and_sensitive_to_trusted_metadata() -> None:
    base = derive_identifiers(RawRfq.model_validate(_payload()))

    assert base == derive_identifiers(RawRfq.model_validate(_payload()))
    assert (
        base[0]
        != derive_identifiers(RawRfq.model_validate(_payload(subject="RFQ: a different subject")))[
            0
        ]
    )
    assert (
        base[0]
        != derive_identifiers(RawRfq.model_validate(_payload(body=_BODY.replace("25", "26"))))[0]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"received_at": "2026-10-06T09:00:00"},  # naive: history is ordered in UTC
        {"source_channel": "FIXTURE"},  # the demonstration's channel
        {"body": ""},
        {"sender_email": "not-an-address"},
        {"subject": "x" * 501},
        {"unexpected_field": "value"},
    ],
)
def test_a_delivery_that_breaks_the_contract_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RawRfq.model_validate(_payload(**overrides))
