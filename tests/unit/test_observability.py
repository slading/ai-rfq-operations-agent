"""Tests for the redaction choke point and trace spans (architecture §10).

Phase 0 ships the rules; Phase 5 adds a test that plants a secret in a real log
record and asserts it never reaches the sink.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rfq_agent.domain.values import canonical_json, fingerprint, sha256_text
from rfq_agent.observability.redaction import (
    REDACTED,
    email_hash,
    redact_mapping,
    redact_text,
    summarize_payload,
)
from rfq_agent.observability.spans import LLMCallSpan, ToolCallSpan, ToolResultStatus
from tests.conftest import utc

#: A fake credential used to prove the redaction rules fire. Not a real secret.
SECRET = "gsk_abc123supersecret"  # noqa: S105
EMAIL = "jan.kowalski@example.com"


class TestRedactText:
    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {SECRET}",
            f"api_key={SECRET}",
            f"GROQ_API_KEY: {SECRET}",
            f"x-api-key: {SECRET}",
            f"token = {SECRET}",
            f"the api_key: {SECRET}",
            f"key: {SECRET}",
        ],
    )
    def test_assignment_style_secrets_are_removed(self, text: str) -> None:
        assert SECRET not in redact_text(text)
        assert REDACTED in redact_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            f"the api key is {SECRET}",
            f"someone pasted {SECRET} into a sentence",
        ],
    )
    def test_prose_secrets_are_out_of_scope_for_the_pattern_rules(self, text: str) -> None:
        """Documented limitation, not a bug.

        The rules target assignment-style secrets. A secret embedded in prose is
        prevented by construction instead: keys and tokens are held in
        ``SecretStr`` config fields and are never placed into a log record, so
        there is no prose for them to leak through. Redaction is the second line
        of defence, not the only one.
        """
        assert SECRET in redact_text(text)

    def test_bearer_token_is_removed(self) -> None:
        assert SECRET not in redact_text(f"used bearer {SECRET} for the call")

    def test_email_addresses_are_removed(self) -> None:
        assert EMAIL not in redact_text(f"sender was {EMAIL}, thanks")

    def test_clean_text_is_untouched(self) -> None:
        text = "40 units of X-120 to Warsaw by Friday"
        assert redact_text(text) == text

    def test_a_header_rule_redacts_to_the_end_of_the_line(self) -> None:
        # Deliberate: a header value runs to the newline, so anything trailing it
        # on the same line is redacted too. Over-redaction is the safe failure.
        redacted = redact_text(f"api_key={SECRET} and also token={SECRET}")
        assert SECRET not in redacted
        assert redacted == f"api_key={REDACTED}"

    def test_secrets_on_separate_lines_are_each_removed(self) -> None:
        redacted = redact_text(f"api_key={SECRET}\ntoken={SECRET}")
        assert SECRET not in redacted
        assert redacted.count(REDACTED) == 2


class TestRedactMapping:
    def test_sensitive_keys_are_replaced_wholesale(self) -> None:
        redacted = redact_mapping({"api_key": SECRET, "model": "openai/gpt-oss-120b"})
        assert redacted["api_key"] == REDACTED
        assert redacted["model"] == "openai/gpt-oss-120b"

    def test_sensitive_keys_are_matched_case_insensitively(self) -> None:
        assert redact_mapping({"API_KEY": SECRET})["API_KEY"] == REDACTED
        assert redact_mapping({"Operator_Token": SECRET})["Operator_Token"] == REDACTED

    def test_nested_structures_are_redacted(self) -> None:
        redacted = redact_mapping(
            {
                "request": {"headers": {"authorization": f"Bearer {SECRET}"}},
                "recipients": [EMAIL, "ops@example.com"],
            }
        )
        assert SECRET not in str(redacted)
        assert EMAIL not in str(redacted)

    def test_secret_in_an_innocuous_field_is_still_caught(self) -> None:
        redacted = redact_mapping({"note": f"the api_key: {SECRET}"})
        assert SECRET not in str(redacted)

    def test_short_prose_after_the_word_key_is_not_eaten(self) -> None:
        # The value must look like a credential, or ordinary text disappears.
        assert redact_text("the key: Warsaw") == "the key: Warsaw"

    def test_non_string_values_pass_through(self) -> None:
        redacted = redact_mapping({"count": 3, "ok": True, "nothing": None})
        assert redacted == {"count": 3, "ok": True, "nothing": None}


class TestSummarizePayload:
    def test_small_payload_is_inlined(self) -> None:
        summary = summarize_payload({"query": "X-120"}, max_bytes=1024)
        assert summary["payload_status"] == "INLINE"
        assert "X-120" in str(summary["payload"])
        assert summary["payload_sha256"] == sha256_text(canonical_json({"query": "X-120"}))

    def test_large_payload_is_truncated_but_still_verifiable(self) -> None:
        payload = {"body": "x" * 5_000}
        summary = summarize_payload(payload, max_bytes=128)
        assert summary["payload_status"] == "TRUNCATED"
        assert summary["payload_bytes"] > 128
        assert len(str(summary["payload"])) <= 128
        assert summary["payload_sha256"] == sha256_text(canonical_json(payload))

    def test_summarized_payload_is_redacted(self) -> None:
        summary = summarize_payload({"api_key": SECRET}, max_bytes=1024)
        assert SECRET not in str(summary)

    def test_hash_is_stable_even_though_the_copy_is_redacted(self) -> None:
        first = summarize_payload({"api_key": SECRET}, max_bytes=1024)
        second = summarize_payload({"api_key": "a different secret value"}, max_bytes=1024)
        assert first["payload_sha256"] != second["payload_sha256"]
        assert SECRET not in str(first)


class TestEmailHash:
    def test_is_stable_under_case_and_whitespace(self) -> None:
        assert email_hash(f" {EMAIL.upper()} ") == email_hash(EMAIL)

    def test_differs_for_different_addresses(self) -> None:
        assert email_hash(EMAIL) != email_hash("someone.else@example.com")


class TestFingerprint:
    def test_key_order_does_not_matter(self) -> None:
        assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})

    def test_value_change_changes_the_fingerprint(self) -> None:
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})


class TestLLMCallSpan:
    def span(self, **overrides: object) -> LLMCallSpan:
        payload: dict[str, object] = {
            "trace_id": "a" * 32,
            "run_id": "RUN-0001",
            "seq": 1,
            "occurred_at": utc(),
            "stage": "resolve",
            "model": "openai/gpt-oss-120b",
            "purpose": "resolve",
            "messages_sha256": "b" * 64,
            "tokens_in": 120,
            "tokens_out": 40,
        }
        payload.update(overrides)
        return LLMCallSpan.model_validate(payload)

    def test_valid_span(self) -> None:
        assert self.span().response_status == "ok"

    def test_failure_requires_an_error_code(self) -> None:
        with pytest.raises(ValidationError, match="error_code is required"):
            self.span(response_status="error")

    def test_success_must_not_carry_an_error_code(self) -> None:
        with pytest.raises(ValidationError, match="must be None"):
            self.span(error_code="RATE_LIMITED")

    def test_invalid_output_requires_a_count(self) -> None:
        with pytest.raises(ValidationError, match="validation_error_count"):
            self.span(output_valid=False)

    def test_invalid_output_with_count_is_valid(self) -> None:
        span = self.span(output_valid=False, validation_error_count=2)
        assert span.repair_attempts == 0


class TestToolCallSpan:
    def span(self, **overrides: object) -> ToolCallSpan:
        payload: dict[str, object] = {
            "trace_id": "a" * 32,
            "run_id": "RUN-0001",
            "seq": 1,
            "occurred_at": utc(),
            "tool_name": "search_catalog",
            "args_sha256": "c" * 64,
            "args_bytes": 32,
            "result_status": ToolResultStatus.OK,
            "result_sha256": "d" * 64,
            "result_bytes": 512,
        }
        payload.update(overrides)
        return ToolCallSpan.model_validate(payload)

    def test_valid_span(self) -> None:
        assert self.span().tool_name == "search_catalog"

    def test_error_requires_a_code(self) -> None:
        with pytest.raises(ValidationError, match="error_code is required"):
            self.span(result_status=ToolResultStatus.ERROR)

    def test_denied_call_returns_no_payload(self) -> None:
        with pytest.raises(ValidationError, match="denied tool call returns no result"):
            self.span(
                result_status=ToolResultStatus.DENIED,
                error_code="TOOL_DENIED",
            )

    def test_denied_call_without_payload_is_valid(self) -> None:
        span = self.span(
            result_status=ToolResultStatus.DENIED,
            error_code="TOOL_DENIED",
            result_sha256=None,
            result_bytes=0,
        )
        assert span.result_sha256 is None

    def test_success_requires_a_result_hash(self) -> None:
        with pytest.raises(ValidationError, match="result_sha256 is required"):
            self.span(result_sha256=None)

    def test_tool_name_pattern_is_enforced(self) -> None:
        with pytest.raises(ValidationError, match="tool_name"):
            self.span(tool_name="SearchCatalog")
