"""Adversarial regression tests for rfq_agent.observability.redaction (Session B).

Stdlib + pytest only. No hypothesis: property-style cases use a deterministic
``random.Random(seed)`` fuzzer so failures are reproducible by seed.

Run against baseline d5611eef::

    PYTHONPATH=src pytest /home/user/qa-session-b/test_redaction_adversarial.py -q

Every test encodes the *expected safe behaviour*. Tests that fail on baseline are
confirmed findings; the IDs (B-01 ...) map to the QA report.
"""

from __future__ import annotations

import json
import random
import time

import pytest
from pydantic import BaseModel, ValidationError

from rfq_agent.observability.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    summarize_payload,
)

SECRET = "gsk_FAKEsupersecret0123456789"  # noqa: S105 - synthetic
SHORT_SECRET = "hunter2"  # noqa: S105 - synthetic
EMAIL = "jan.kowalski@example.com"

SENSITIVE_WORDS = ("api_key", "token", "password", "secret", "authorization")


def _leaks(value: object, needle: str = SECRET) -> bool:
    return needle in str(value)


# --------------------------------------------------------------------------
# B-01  summarize_payload: non-mapping / TRUNCATED path goes through redact_text
#       on canonical JSON, where the quoted-key syntax defeats every rule.
# --------------------------------------------------------------------------
class TestSummarizePayloadSerializationPaths:
    @pytest.mark.parametrize("key", SENSITIVE_WORDS)
    def test_truncated_mapping_never_leaks_sensitive_value(self, key: str) -> None:
        payload = {key: SECRET, "zz_body": "x" * 4_000}
        summary = summarize_payload(payload, max_bytes=200)
        assert summary["payload_status"] == "TRUNCATED"
        assert not _leaks(summary)

    def test_truncated_never_leaks_a_secret_prefix(self) -> None:
        # Cutting inside the value must not leave a usable prefix behind.
        payload = {"a_token": SECRET, "zz_body": "x" * 4_000}
        cut = len('{"a_token":"') + 16
        summary = summarize_payload(payload, max_bytes=cut)
        assert SECRET[:8] not in str(summary)

    def test_list_payload_is_structurally_redacted(self) -> None:
        summary = summarize_payload([{"api_key": SECRET}], max_bytes=4_096)
        assert not _leaks(summary)

    def test_truncated_email_is_removed(self) -> None:
        payload = {"customer_email": EMAIL, "zz_body": "x" * 4_000}
        summary = summarize_payload(payload, max_bytes=200)
        assert not _leaks(summary, EMAIL)


# --------------------------------------------------------------------------
# B-02  Serialised structures (JSON / repr) with quoted keys are not redacted.
# --------------------------------------------------------------------------
class TestQuotedKeySyntaxes:
    @pytest.mark.parametrize("key", SENSITIVE_WORDS)
    def test_json_string_with_sensitive_key(self, key: str) -> None:
        assert not _leaks(redact_text(json.dumps({key: SECRET})))

    @pytest.mark.parametrize("key", SENSITIVE_WORDS)
    def test_python_repr_with_sensitive_key(self, key: str) -> None:
        assert not _leaks(redact_text(repr({key: SECRET})))

    def test_json_embedded_in_an_innocuous_field(self) -> None:
        assert not _leaks(redact_mapping({"raw": json.dumps({"password": SECRET})}))

    def test_quoted_dotenv_value(self) -> None:
        assert not _leaks(redact_text(f'GROQ_API_KEY="{SECRET}"'))


# --------------------------------------------------------------------------
# B-03  Key set vs. regex set drift; header-style key spellings.
# --------------------------------------------------------------------------
class TestSensitiveKeyNormalisation:
    @pytest.mark.parametrize(
        "key",
        [
            "x-api-key",
            "X-API-KEY",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "secret_key",
            "private_key",
            "aws_secret_access_key",
            "db_password",
            "api key",
            "api_key:",
        ],
    )
    def test_header_style_and_suffixed_keys_are_wholesale_redacted(self, key: str) -> None:
        assert redact_mapping({key: SHORT_SECRET})[key] == REDACTED

    @pytest.mark.parametrize("key", [1, None, 2.5, ("a",)])
    def test_non_string_keys_do_not_crash_the_choke_point(self, key: object) -> None:
        # A crash in the redactor means the caller either loses the log line or,
        # worse, falls back to logging the raw record.
        out = redact_mapping({key: f"api_key={SECRET}"})  # type: ignore[dict-item]
        assert not _leaks(out)

    def test_secret_or_email_used_as_a_key_is_redacted(self) -> None:
        out = redact_mapping({f"api_key={SECRET}": 1, EMAIL: 2})
        assert not _leaks(out)
        assert not _leaks(out, EMAIL)


# --------------------------------------------------------------------------
# B-04  Pair arrays / name-value header lists (httpx, requests, OpenAI SDK dumps).
# --------------------------------------------------------------------------
class TestPairArrays:
    def test_list_of_pairs_is_treated_like_a_mapping(self) -> None:
        out = redact_mapping({"headers": [["api_key", SHORT_SECRET], ["Authorization", "Basic dXNlcjpwYXNz"]]})
        assert not _leaks(out, SHORT_SECRET)
        assert not _leaks(out, "dXNlcjpwYXNz")

    def test_name_value_dicts(self) -> None:
        out = redact_mapping({"headers": [{"name": "api_key", "value": SHORT_SECRET}]})
        assert not _leaks(out, SHORT_SECRET)

    @pytest.mark.parametrize("container", [set, frozenset])
    def test_sets_are_recursed(self, container: type) -> None:
        assert not _leaks(redact_mapping({"s": container({f"token={SECRET}"})}))

    @pytest.mark.parametrize("container", [bytes, bytearray])
    def test_bytes_are_not_passed_through_verbatim(self, container: type) -> None:
        assert not _leaks(redact_mapping({"b": container(f"token={SECRET}".encode())}))


# --------------------------------------------------------------------------
# B-05  Authorization schemes other than Bearer; short credentials under a
#       recognised key.
# --------------------------------------------------------------------------
class TestValueShapes:
    @pytest.mark.parametrize(
        "text",
        [
            f"password={SHORT_SECRET}",
            f"password: {SHORT_SECRET}",
            f"passwd={SHORT_SECRET}",
            "api_key=abc12345",
            f"Bearer {SHORT_SECRET}",
        ],
    )
    def test_short_values_under_an_explicit_secret_key_are_redacted(self, text: str) -> None:
        out = redact_text(text)
        assert SHORT_SECRET not in out
        assert "abc12345" not in out

    def test_basic_auth_scheme(self) -> None:
        assert "dXNlcjpwYXNz" not in redact_text("Authorization: Basic dXNlcjpwYXNz")

    def test_pem_block(self) -> None:
        pem = "private_key=-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n-----END PRIVATE KEY-----"
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw" not in redact_text(pem)

    def test_csv_separator(self) -> None:
        assert not _leaks(redact_text(f"api_key,{SECRET}"))


# --------------------------------------------------------------------------
# B-06  URL-embedded credentials.
# --------------------------------------------------------------------------
class TestUrlCredentials:
    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql://rfq:{SECRET}@db:5432/rfq",
            f"https://user:{SECRET}@host/path",  # baseline passes only via email regex
            f"redis://:{SECRET}@cache:6379/0",
            f"amqp://guest:{SECRET}@mq",
        ],
    )
    def test_userinfo_password_is_removed(self, url: str) -> None:
        assert not _leaks(redact_text(url))

    def test_userinfo_password_short(self) -> None:
        assert SHORT_SECRET not in redact_text(f"postgresql://rfq:{SHORT_SECRET}@db/rfq")

    def test_query_string_key_param_short_value(self) -> None:
        assert "abc123" not in redact_text("https://x.example/?key=abc123&x=1")


# --------------------------------------------------------------------------
# B-07  Unicode / normalisation evasions and email coverage.
# --------------------------------------------------------------------------
class TestUnicode:
    @pytest.mark.parametrize(
        "text",
        [
            f"api_key\uff1d{SECRET}",  # fullwidth equals
            f"api_key\uff1a{SECRET}",  # fullwidth colon
            f"\uff41\uff50\uff49_\uff4b\uff45\uff59={SECRET}",  # fullwidth 'api_key'
        ],
    )
    def test_fullwidth_punctuation_and_letters(self, text: str) -> None:
        assert not _leaks(redact_text(text))

    @pytest.mark.parametrize(
        "addr",
        [
            "kontakt@przyk\u0142ad.pl",
            "\u017c\u00f3\u0142w@example.com",
            '"john doe"@example.com',
        ],
    )
    def test_internationalised_and_quoted_emails(self, addr: str) -> None:
        assert addr not in redact_text(f"from {addr} today")

    @pytest.mark.parametrize("suffix", ["_", "\u017c", "0"])
    def test_email_followed_by_word_char_is_still_redacted(self, suffix: str) -> None:
        # ``\b`` after the TLD fails when a word character follows.
        assert EMAIL not in redact_text(f"{EMAIL}{suffix}")

    def test_lone_surrogate_does_not_crash_summarize(self) -> None:
        # str from a decoded-with-surrogateescape mail body. Must not raise:
        # an exception here drops the audit row.
        out = summarize_payload({"body": "ab\udcffcd", "token": SECRET}, max_bytes=100)
        assert not _leaks(out)


# --------------------------------------------------------------------------
# B-08  Truncation is measured in bytes but sliced in characters.
# --------------------------------------------------------------------------
class TestTruncationBoundary:
    @pytest.mark.parametrize("char", ["\u017c", "\U0001f600"])
    def test_stored_truncated_copy_respects_max_bytes(self, char: str) -> None:
        summary = summarize_payload({"body": char * 500}, max_bytes=64)
        stored = summary["payload"]
        assert isinstance(stored, str)
        assert len(stored.encode("utf-8")) <= 64

    def test_max_bytes_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            summarize_payload({"a": 1}, max_bytes=0)


# --------------------------------------------------------------------------
# B-09  Regex complexity: a hostile customer email must not stall the writer.
# --------------------------------------------------------------------------
class TestRegexPerformance:
    @pytest.mark.parametrize(
        "label",
        ["key-run", "dash-key", "email-dots", "email-digits"],
    )
    def test_48kb_hostile_input_redacts_in_under_200ms(self, label: str) -> None:
        text = {
            "key-run": "key" * 16_000 + "=",
            "dash-key": "a-" * 16_000 + "key",
            "email-dots": "a.b" * 16_000,
            "email-digits": "a1." * 16_000 + "@b.",
        }[label]
        start = time.perf_counter()
        redact_text(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.2, f"{label}: {elapsed:.2f}s for {len(text)} chars"

    def test_scaling_is_roughly_linear(self) -> None:
        # 4x the input should cost well under 16x the time.
        def cost(n: int) -> float:
            text = "a-" * n + "key"
            t0 = time.perf_counter()
            redact_text(text)
            return time.perf_counter() - t0

        small, large = cost(2_000), cost(8_000)
        assert large < small * 8


# --------------------------------------------------------------------------
# B-10  Exception rendering: pydantic ValidationError carries the input value.
# --------------------------------------------------------------------------
class _IntOnly(BaseModel):
    requests_per_minute_budget: int


class TestExceptionRendering:
    def test_validation_error_text_through_redact_text(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _IntOnly(requests_per_minute_budget=SECRET)
        assert not _leaks(redact_text(str(exc.value)))

    def test_validation_error_errors_through_redact_mapping(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _IntOnly(requests_per_minute_budget=SECRET)
        assert not _leaks(redact_mapping({"errors": exc.value.errors()}))


# --------------------------------------------------------------------------
# B-11  False positives: over-redaction of business identifiers.
# --------------------------------------------------------------------------
class TestFalsePositives:
    @pytest.mark.parametrize(
        "text",
        [
            "idempotency_key=ORD-2026-000123",
            "primary_key=RFQ-2026-000042",
            "Keyboard=KB-2026-BLACK-US",
            "Turkey: ANKARA-WAREHOUSE-01",
            "token_count: 1234567890123",
            "Bearer Instrument-2026-A",
        ],
    )
    def test_business_identifiers_survive(self, text: str) -> None:
        assert redact_text(text) == text

    def test_header_rule_does_not_eat_the_rest_of_the_line(self) -> None:
        text = "token: 40 units of X-120 to Warsaw by Friday, PO-2026-00123"
        assert "PO-2026-00123" in redact_text(text)


# --------------------------------------------------------------------------
# Deterministic property-style fuzz (no hypothesis). Invariant: any string of
# the form <sensitive-key><sep><secret> has the secret removed, regardless of
# surrounding punctuation/quoting; and the redactor is idempotent.
# --------------------------------------------------------------------------
_KEYS = ["api_key", "API_KEY", "token", "password", "secret", "x-api-key", "authorization"]
_SEPS = ["=", ":", ": ", " = ", '": "', "': '", "=\"", ","]
_WRAP = [("", ""), ('"', '"'), ("'", "'"), ("{", "}"), ("[", "]"), ("<", ">")]
_PRE = ["", " ", "\n", "\t", "{", "(", "export ", "-e ", '{"', "{'"]


def _fuzz_cases(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        key = rng.choice(_KEYS)
        sep = rng.choice(_SEPS)
        pre = rng.choice(_PRE)
        wl, wr = rng.choice(_WRAP)
        out.append(f"{pre}{key}{sep}{wl}{SECRET}{wr}")
    return out


class TestDeterministicFuzz:
    @pytest.mark.parametrize("seed", [0, 1, 2, 3])
    def test_key_sep_secret_is_always_removed(self, seed: int) -> None:
        failures = [c for c in _fuzz_cases(seed, 200) if _leaks(redact_text(c))]
        assert not failures, f"{len(failures)} leaks, e.g. {failures[:5]!r}"

    @pytest.mark.parametrize("seed", [0, 1])
    def test_redaction_is_idempotent(self, seed: int) -> None:
        for case in _fuzz_cases(seed, 200):
            once = redact_text(case)
            assert redact_text(once) == once
