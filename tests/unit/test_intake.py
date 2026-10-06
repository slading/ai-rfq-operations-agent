"""Tests for intake schemas."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from rfq_agent.domain.intake import (
    TERMINAL_RFQ_STATUSES,
    AttachmentMetadata,
    InboundRfq,
    IntakeEventKind,
    QuoteValidityWindow,
    RfqStatus,
    SourceChannel,
)
from rfq_agent.domain.trust import UntrustedText
from rfq_agent.domain.values import sha256_text
from tests.conftest import make_untrusted, utc


def make_inbound(**overrides: object) -> InboundRfq:
    payload: dict[str, object] = {
        "source_channel": SourceChannel.EMAIL,
        "received_at": utc(),
        "subject": "Request for quotation",
        "body": make_untrusted(),
        "sender_name": "Jan Kowalski",
        "sender_email": "jan.kowalski@example.com",
        "sender_email_hash": sha256_text("jan.kowalski@example.com"),
        "idempotency_key": "k" * 40,
        "thread_key": "thread-abc123",
    }
    payload.update(overrides)
    return InboundRfq.model_validate(payload)


class TestInboundRfq:
    def test_valid_request_is_accepted(self) -> None:
        rfq = make_inbound()
        assert rfq.source_channel is SourceChannel.EMAIL
        assert rfq.body.text.startswith("Hi, we need 40 units")
        assert rfq.attachments == ()

    def test_empty_body_is_rejected(self) -> None:
        body = UntrustedText(text="   ", sha256=sha256_text("   "), byte_length=3)
        with pytest.raises(ValidationError, match="body must not be empty"):
            make_inbound(body=body)

    def test_untrusted_text_preserves_whitespace_verbatim(self) -> None:
        # Stripping would break the digest invariant and the evidence spans.
        body = UntrustedText.from_text("  40   units  ")
        assert body.text == "  40   units  "
        assert body.sha256 == sha256_text("  40   units  ")

    @pytest.mark.parametrize(
        "email",
        ["not-an-email", "missing@tld", "@example.com", "spaces in@example.com"],
    )
    def test_invalid_sender_email_is_rejected(self, email: str) -> None:
        with pytest.raises(ValidationError):
            make_inbound(sender_email=email)

    def test_short_idempotency_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_inbound(idempotency_key="too-short")

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_inbound(discount_percent=100)

    def test_attachments_are_metadata_only(self) -> None:
        rfq = make_inbound(
            attachments=(
                AttachmentMetadata(
                    filename="spec.pdf",
                    content_type="application/pdf",
                    byte_length=20_480,
                    parsed=False,
                ),
            )
        )
        assert rfq.attachments[0].parsed is False
        assert rfq.attachments[0].text_preview is None


class TestRfqStatus:
    def test_terminal_statuses(self) -> None:
        assert (
            frozenset(
                {
                    RfqStatus.SENT,
                    RfqStatus.CLOSED,
                    RfqStatus.SUPERSEDED,
                    RfqStatus.DUPLICATE_SUPPRESSED,
                }
            )
            == TERMINAL_RFQ_STATUSES
        )

    def test_open_is_not_terminal(self) -> None:
        assert RfqStatus.OPEN not in TERMINAL_RFQ_STATUSES
        assert RfqStatus.AWAITING_HUMAN not in TERMINAL_RFQ_STATUSES

    def test_intake_event_kinds_cover_duplicate_and_thread_cases(self) -> None:
        assert IntakeEventKind.DUPLICATE_SUPPRESSED.value == "DUPLICATE_SUPPRESSED"
        assert IntakeEventKind.THREAD_LINKED.value == "THREAD_LINKED"


class TestQuoteValidityWindow:
    def test_ordered_window_is_accepted(self) -> None:
        window = QuoteValidityWindow(valid_from=date(2026, 10, 6), valid_until=date(2026, 11, 6))
        assert window.valid_until is not None

    def test_inverted_window_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid_until"):
            QuoteValidityWindow(valid_from=date(2026, 11, 6), valid_until=date(2026, 10, 6))
