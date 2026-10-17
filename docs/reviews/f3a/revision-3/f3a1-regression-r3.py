"""F3-A1 Revision 3 regression tests — C-8, C-9, C-10, C-12.

Standalone: stdlib + pytest only. Run against the module under test with the
prototype shim installed (``f3a1-test-shim.py`` renamed to ``sitecustomize.py``,
``f3a1-prototype-r3.py`` imported as ``f3a1_prototype``)::

    PYTHONPATH=src:<dir with sitecustomize.py + f3a1_prototype.py> \\
        .venv/bin/python -m pytest f3a1-regression-r3.py -q

These cases are deliberately separate from the 89-test adversarial gate
(Session B): that gate predates C-8/C-9/C-10 and cannot catch them. Adopt this
module alongside it as ``tests/unit/test_redaction_regression.py`` so the
regressions stay visible in CI.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from rfq_agent.observability.redaction import (
    REDACTED,
    email_hash,
    redact_mapping,
    redact_payload,
    redact_text,
    summarize_payload,
)

#: Synthetic, non-provider-shaped secret. It has no ``gsk_``/``sk-`` prefix, so
#: only key-aware rules can catch it - which is exactly what C-9 and C-10 need.
SECRET = "hunter2024"  # noqa: S105
SHORT = "abc123"  # noqa: S105
EMAIL = "jan.kowalski@example.com"

#: The complete sensitive-key vocabulary, spelled as normalised names. Kept in
#: the test rather than imported so a silently narrowed vocabulary fails here.
SENSITIVE_KEYS = (
    "accesskey accesskeyid accesstoken apikey apikeyid apikeys auth "
    "authentication authorization authorizationcode awssecretaccesskey bearer "
    "clientsecret cookie credential credentials customeremail email "
    "emailaddress encryptionkey groqapikey input inputvalue jwt key oauth "
    "operatortoken passphrase passwd password passwords privatekey "
    "proxyauthorization refreshtoken secret secretaccesskey secretkey secrets "
    "senderemail sessionid sessiontoken setcookie signingkey token tokens "
    "useremail"
).split()

#: Compound spellings that must resolve through the suffix policy.
SUFFIXED_KEYS = (
    "x-api-key",
    "X-API-KEY",
    "db_password",
    "operator-token",
    "accessToken",
    "clientSecret",
    "senderEmail",
    "customer_email",
    "aws_secret_access_key",
    "proxy-authorization",
    "set-cookie",
    "session_id",
    "api key",
    "db.passwd",
)


def leaks(value: object, needle: str = SECRET) -> bool:
    """True when an intact 8-character fragment of ``needle`` survives."""
    text = str(value)
    return any(needle[i : i + 8] in text for i in range(len(needle) - 7))


# ---------------------------------------------------------------------------
# C-8 — PEM scanning must be linear, in both the text and summary paths
# ---------------------------------------------------------------------------
class TestPemLinearScanner:
    @staticmethod
    def _begins(n_chars: int) -> str:
        return "-----BEGIN A-----" * (n_chars // 17)

    def test_48kb_of_begin_markers_is_fast(self) -> None:
        text = self._begins(48_000)
        start = time.perf_counter()
        out = redact_text(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.2, f"{elapsed:.2f}s for {len(text)} chars"
        assert out == REDACTED

    def test_1mb_of_begin_markers_is_fast(self) -> None:
        text = self._begins(1_000_000)
        start = time.perf_counter()
        out = redact_text(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"{elapsed:.2f}s for {len(text)} chars"
        assert out == REDACTED

    def test_repeated_begin_and_end_pairs_are_fast(self) -> None:
        block = "-----BEGIN KEY-----abc-----END KEY-----"
        text = block * 2_000
        start = time.perf_counter()
        out = redact_text(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"{elapsed:.2f}s for {len(text)} chars"
        assert "abc" not in out
        assert out.count(REDACTED) == 2_000

    def test_real_pem_block_is_removed(self) -> None:
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n"
            "-----END PRIVATE KEY-----"
        )
        out = redact_text(f"key material: {pem} trailing")
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw" not in out
        assert "trailing" in out

    def test_unterminated_block_fails_closed(self) -> None:
        out = redact_text("before -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw")
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw" not in out
        assert out.startswith("before ")

    def test_markers_without_a_body_are_still_bounded(self) -> None:
        text = "-----BEGIN A----- xxx " * 5_000
        start = time.perf_counter()
        redact_text(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5

    def test_summarize_path_48kb(self) -> None:
        payload = {"body": self._begins(48_000), "api_key": SECRET}
        start = time.perf_counter()
        summary = summarize_payload(payload, max_bytes=512)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"{elapsed:.2f}s"
        assert not leaks(summary)

    def test_summarize_path_1mb(self) -> None:
        payload = {"body": self._begins(1_000_000)}
        start = time.perf_counter()
        summary = summarize_payload(payload, max_bytes=512)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"{elapsed:.2f}s"
        assert "BEGIN A" not in str(summary) or summary["payload_status"] == "WITHHELD"

    def test_summarize_path_preserves_hash_and_status(self) -> None:
        payload = {"body": self._begins(48_000), "api_key": SECRET}
        summary = summarize_payload(payload, max_bytes=512)
        assert summary["payload_status"] == "TRUNCATED"
        assert len(summary["payload_sha256"]) == 64


# ---------------------------------------------------------------------------
# C-9 — the text path must know every key the structural path knows
# ---------------------------------------------------------------------------
class TestTextKeyPolicy:
    @pytest.mark.parametrize("key", SENSITIVE_KEYS)
    def test_every_exact_key_redacts_a_non_provider_secret(self, key: str) -> None:
        out = redact_text(f"{key}={SECRET}")
        assert not leaks(out), f"{key!r} leaked: {out!r}"
        assert REDACTED in out

    @pytest.mark.parametrize("key", SENSITIVE_KEYS)
    def test_every_exact_key_redacts_structurally(self, key: str) -> None:
        assert redact_mapping({key: SECRET})[key] == REDACTED

    @pytest.mark.parametrize("key", SUFFIXED_KEYS)
    def test_suffixed_and_header_spellings_redact(self, key: str) -> None:
        out = redact_text(f"{key}={SECRET}")
        assert not leaks(out), f"{key!r} leaked: {out!r}"
        assert redact_mapping({key: SECRET})[key] == REDACTED

    @pytest.mark.parametrize("key", ["passphrase", "auth", "authentication", "sessionid", "email"])
    def test_keys_named_in_c_minus_9(self, key: str) -> None:
        assert SECRET not in redact_text(f"{key}={SECRET}")
        assert SECRET not in redact_text(f"{key}: {SECRET}")

    def test_short_values_under_strong_keys_are_redacted(self) -> None:
        for key in ("password", "passphrase", "auth", "sessionid", "secret", "token"):
            assert SHORT not in redact_text(f"{key}={SHORT}"), key

    def test_bare_key_stays_shape_gated(self) -> None:
        # ``key`` is the one ambiguous key: prose must survive, credentials must not.
        assert redact_text("the key: Warsaw") == "the key: Warsaw"
        assert SHORT not in redact_text(f"key={SHORT}")
        # A mixed-case multi-digit identifier looks like a credential, and is
        # therefore redacted: documented over-redaction.
        assert "Warsaw2024" not in redact_text("key: Warsaw2024")

    @pytest.mark.parametrize(
        "text",
        [
            "idempotency_key=ORD-2026-000123",
            "primary_key=RFQ-2026-000042",
            "Keyboard=KB-2026-BLACK-US",
            "Turkey: ANKARA-WAREHOUSE-01",
            "token_count: 1234567890123",
            "Bearer Instrument-2026-A",
            "the key: Warsaw",
            "monkey: bananas!",
            "max_tokens: 7000",
            "tokenizer: abcdefghijkl",
            "secretary: abcdefghijkl",
            "public_key: abcdefghijkl",
            "hockey: abcdefghijkl",
        ],
    )
    def test_business_identifiers_survive(self, text: str) -> None:
        assert redact_text(text) == text

    def test_text_and_structure_agree(self) -> None:
        """Whatever the structural walk redacts, the text rule must redact too."""
        for key in SENSITIVE_KEYS:
            assert redact_mapping({key: SECRET})[key] == REDACTED
            assert SECRET not in redact_text(f"{key}={SECRET}"), key


# ---------------------------------------------------------------------------
# C-10 — nested / multiply encoded JSON must not hide a keyed secret
# ---------------------------------------------------------------------------
class TestNestedEncodedJson:
    @staticmethod
    def _encode(value: object, levels: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value)
        for _ in range(levels):
            text = json.dumps(text)
        return text

    @pytest.mark.parametrize("key", ["api_key", "password", "passphrase", "session_id", "token"])
    def test_double_encoded(self, key: str) -> None:
        text = self._encode({key: SECRET}, 2)
        assert not leaks(redact_text(text)), redact_text(text)

    @pytest.mark.parametrize("levels", [1, 2, 3, 4])
    def test_multiple_levels(self, levels: int) -> None:
        text = self._encode({"api_key": SECRET}, levels)
        assert not leaks(redact_text(text)), redact_text(text)

    def test_nested_in_innocuous_field(self) -> None:
        text = json.dumps({"note": json.dumps({"api_key": SECRET})})
        assert not leaks(redact_text(text))
        assert not leaks(redact_mapping({"raw": text}))

    def test_nested_escaped_key(self) -> None:
        inner = '{"api\\u005fkey": "%s"}' % SECRET
        text = json.dumps({"note": inner})
        assert not leaks(redact_text(text)), redact_text(text)

    def test_escaped_key_flat(self) -> None:
        assert not leaks(redact_text('{"api\\u005fkey": "%s"}' % SECRET))
        assert not leaks(redact_text('{"pass\\u0077ord": "%s"}' % SECRET))

    def test_deep_encoding_fails_closed(self) -> None:
        # Nine levels is beyond the scanner's eight-level window: the string
        # must be withheld rather than returned with the secret inside.
        text = self._encode({"api_key": SECRET}, 9)
        out = redact_text(text)
        assert not leaks(out), out[:120]

    def test_benign_nested_text_is_untouched(self) -> None:
        text = json.dumps({"note": 'say "hello" to the team'})
        assert redact_text(text) == text

    def test_idempotent_on_nested_payloads(self) -> None:
        for levels in (1, 2, 3):
            once = redact_text(self._encode({"password": SECRET}, levels))
            assert redact_text(once) == once

    def test_summary_of_nested_payload(self) -> None:
        payload = {"raw": self._encode({"api_key": SECRET}, 2)}
        summary = summarize_payload(payload, max_bytes=4096)
        assert not leaks(summary), summary

    def test_oversized_escaped_payload_fails_closed(self) -> None:
        text = '\\"' + ("x" * 300_000) + '\\" api_key=\\"' + SECRET
        out = redact_text(text)
        assert not leaks(out)


# ---------------------------------------------------------------------------
# C-12 — email_hash and the summary contract are unchanged
# ---------------------------------------------------------------------------
class TestPublicApiRetained:
    def test_email_hash_is_exported(self) -> None:
        import rfq_agent.observability.redaction as module

        assert "email_hash" in module.__all__

    def test_email_hash_normalises_case_and_whitespace(self) -> None:
        assert email_hash(f" {EMAIL.upper()} ") == email_hash(EMAIL)

    def test_email_hash_is_sha256_of_the_normalised_address(self) -> None:
        import hashlib

        assert email_hash(EMAIL) == hashlib.sha256(EMAIL.lower().encode()).hexdigest()

    def test_email_hash_differs_between_addresses(self) -> None:
        assert email_hash(EMAIL) != email_hash("someone.else@example.com")

    def test_emails_are_still_redacted(self) -> None:
        assert EMAIL not in redact_text(f"sender was {EMAIL}, thanks")

    def test_max_bytes_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            summarize_payload({"a": 1}, max_bytes=0)

    def test_max_bytes_negative_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            summarize_payload({"a": 1}, max_bytes=-1)

    def test_redact_payload_is_exported(self) -> None:
        import rfq_agent.observability.redaction as module

        assert "redact_payload" in module.__all__
        assert hasattr(module, "redact_payload")

    def test_summary_status_vocabulary_is_closed(self) -> None:
        for payload, cap in (({"a": 1}, 1024), ({"a": "x" * 500}, 32)):
            status = summarize_payload(payload, max_bytes=cap)["payload_status"]
            assert status in {"INLINE", "TRUNCATED", "WITHHELD"}
