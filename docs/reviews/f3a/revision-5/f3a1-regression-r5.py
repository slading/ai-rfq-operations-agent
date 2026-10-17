"""F3-A1 Revision 5 regression tests — C-8..C-12, F-1, F-2, D-1, D-2, D-3.

Standalone: stdlib + pytest only. Run against the module under test with the
prototype shim installed (``f3a1-test-shim.py`` renamed to ``sitecustomize.py``,
``f3a1-prototype-r5.py`` imported as ``f3a1_prototype``)::

    PYTHONPATH=src:<dir with sitecustomize.py + f3a1_prototype.py> \\
        .venv/bin/python -m pytest f3a1-regression-r4.py -q

These cases are deliberately separate from the 89-test adversarial gate
(Session B): that gate predates C-8/C-9/C-10 and cannot catch them, and it has
no container or deep-escape coverage (F-1/F-2 in revision 4). Adopt this module
alongside it as ``tests/unit/test_redaction_regression.py`` so the regressions
stay visible in CI.

Revision 5 appends three classes: ``TestWrapperContainers`` (D-1),
``TestCredentialRecords`` (D-2) and ``TestDecoratedKeyLimits`` (D-3). The 219
inherited tests are unchanged.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from pydantic import BaseModel, ValidationError

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
        # Seventeen levels is beyond the scanner's sixteen-level window: the
        # string must be withheld rather than returned with the secret inside.
        # (Nine levels are now read in place; see TestEscapedKeyBoundaries.)
        text = self._encode({"api_key": SECRET}, 17)
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


class TestContainerValuedKeys:
    """F-1: a strong key followed by a container must never leak a leaf value."""

    @pytest.mark.parametrize("key", ["credentials", "api_key", "secret", "token"])
    def test_json_object_value(self, key: str) -> None:
        text = '{"%s": {"value": "%s"}}' % (key, SECRET)
        out = redact_text(text)
        assert not leaks(out), out
        assert REDACTED in out
        assert redact_text(out) == out

    def test_json_array_value(self) -> None:
        out = redact_text('{"token": ["x", "%s"]}' % SECRET)
        assert not leaks(out), out
        assert out == '{"token": [REDACTED]}'

    def test_deeply_nested_container(self) -> None:
        text = '{"secret": {"a": {"b": {"c": ["%s"]}}}}' % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == '{"secret": [REDACTED]}'

    def test_python_repr_dict(self) -> None:
        text = "{'credentials': {'value': '%s'}}" % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == "{'credentials': [REDACTED]}"

    def test_nested_json_string(self) -> None:
        text = json.dumps(json.dumps({"credentials": {"value": SECRET}}))
        out = redact_text(text)
        assert not leaks(out), out
        assert REDACTED in out
        assert redact_text(out) == out

    def test_quoted_container_value(self) -> None:
        text = '{"credentials": "{\\"value\\": \\"%s\\"}"}' % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == '{"credentials": "[REDACTED]"}'

    def test_yaml_block(self) -> None:
        text = "credentials:\n  value: %s" % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == f"credentials: {REDACTED}"

    def test_yaml_list(self) -> None:
        text = "token:\n  - x\n  - %s" % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == f"token: {REDACTED}"

    def test_yaml_block_scalar(self) -> None:
        text = "secret: |\n  %s" % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == f"secret: {REDACTED}"

    def test_yaml_deep_block_preserves_following_line(self) -> None:
        text = "credentials:\n  value:\n    nested: %s\nnext: ok" % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert out == f"credentials: {REDACTED}\nnext: ok"

    def test_multiple_containers(self) -> None:
        text = '{"token": ["a", "%s"], "password": {"v": "%s"}, "note": "ok"}' % (SECRET, SECRET)
        out = redact_text(text)
        assert not leaks(out), out
        assert out.count(REDACTED) == 2
        assert '"note": "ok"' in out

    @pytest.mark.parametrize(
        "text",
        [
            '{"api_key": {"user": "%s"' % SECRET,          # unterminated
            '{"api_key": [1, "%s"}' % SECRET,              # mismatched closer
            '{"api_key": {"a": {"b": "%s"' % SECRET,       # nested, unterminated
        ],
    )
    def test_malformed_containers_fail_closed(self, text: str) -> None:
        out = redact_text(text)
        assert not leaks(out), out
        assert out.endswith(REDACTED), out
        assert len(out) < len(text) + 20

    def test_oversized_container_is_withheld(self) -> None:
        text = '{"api_key": {"pad": "' + "x" * 70_000 + '", "v": "%s"}}' % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert len(out) < 100

    def test_deep_container_is_withheld(self) -> None:
        text = '{"api_key": ' + '{"a":' * 5_000 + '"%s"' % SECRET + "}" * 5_000 + "}"
        out = redact_text(text)
        assert not leaks(out), out
        assert len(out) < 100

    def test_container_redaction_is_idempotent(self) -> None:
        corpus = [
            '{"credentials": {"value": "%s"}}' % SECRET,
            "{'credentials': {'value': '%s'}}" % SECRET,
            "credentials:\n  value: %s" % SECRET,
            json.dumps(json.dumps({"token": [SECRET]})),
        ]
        for text in corpus:
            once = redact_text(text)
            assert redact_text(once) == once, (text, once)

    def test_benign_containers_are_untouched(self) -> None:
        corpus = [
            '{"payload": {"value": "Warsaw"}}',
            "{'order': {'status': 'READY', 'qty': 3}}",
            "note:\n  value: ordinary text",
            '{"items": ["a", "b"]}',
        ]
        for text in corpus:
            assert redact_text(text) == text, text

    def test_inner_sensitive_key_is_still_redacted(self) -> None:
        text = '{"payload": {"api_key": "%s"}, "n": 1}' % SECRET
        out = redact_text(text)
        assert not leaks(out), out
        assert '"n": 1' in out

    def test_pydantic_dict_input_value(self) -> None:
        class Model(BaseModel):
            value: int

        with pytest.raises(ValidationError) as excinfo:
            Model(value={"value": SECRET})
        out = redact_text(str(excinfo.value))
        assert not leaks(out), out
        assert "input_value=[REDACTED" in out

    def test_pydantic_nested_dict_input_value(self) -> None:
        class Model(BaseModel):
            value: int

        with pytest.raises(ValidationError) as excinfo:
            Model(value={"a": {"b": SECRET}})
        assert not leaks(redact_text(str(excinfo.value)))

    def test_pydantic_list_input_value(self) -> None:
        class Model(BaseModel):
            value: int

        with pytest.raises(ValidationError) as excinfo:
            Model(value=["x", SECRET])
        out = redact_text(str(excinfo.value))
        assert not leaks(out), out
        assert "input_value=[REDACTED" in out

    def test_pydantic_errors_are_safe_through_redact_mapping(self) -> None:
        class Model(BaseModel):
            value: int

        with pytest.raises(ValidationError) as excinfo:
            Model(value={"value": SECRET})
        errors = excinfo.value.errors()
        assert leaks(errors)  # raw by design (F-3)
        safe = redact_mapping({"errors": errors})
        assert not leaks(safe), safe

    def test_summary_of_container_payload(self) -> None:
        summary = summarize_payload(
            {"body": '{"api_key": {"user": "%s"}}' % SECRET, "n": 1}, max_bytes=512
        )
        assert not leaks(summary), summary
        assert summary["payload_status"] in {"INLINE", "TRUNCATED"}

    @pytest.mark.parametrize(
        "payload",
        [
            {"credentials": {"value": SECRET}},
            {"api_key": {"user": SECRET}},
            {"token": ["x", SECRET]},
            {"secret": {"a": {"b": SECRET}}},
        ],
    )
    def test_structural_container_values(self, payload: dict[str, object]) -> None:
        out = redact_mapping(payload)
        assert not leaks(out), out
        assert list(out.values()) == [REDACTED]


class TestEscapedKeyBoundaries:
    """F-2: escape runs are read up to sixteen levels; deeper is withheld."""

    @staticmethod
    def _keyed(run: int, key: str = "api") -> str:
        return '{"%s%su005fkey": "%s"}' % (key, "\\" * run, SECRET)

    @pytest.mark.parametrize("run", list(range(1, 17)))
    def test_escape_runs_one_to_sixteen_are_redacted(self, run: int) -> None:
        text = self._keyed(run)
        out = redact_text(text)
        assert not leaks(out), out
        assert "[REDACTED]" in out
        assert redact_text(out) == out

    @pytest.mark.parametrize("run", [17, 24, 32, 64, 1_000])
    def test_deeper_escape_runs_are_contained(self, run: int) -> None:
        out = redact_text(self._keyed(run))
        assert not leaks(out), out
        assert REDACTED in out

    @pytest.mark.parametrize("run", [1, 8, 16])
    def test_benign_escaped_key_survives(self, run: int) -> None:
        text = self._keyed(run, key="note")
        assert redact_text(text) == text

    def test_deep_escape_without_quotes_is_caught(self) -> None:
        # F-2: the probe must trigger on the deep run itself, not only on an
        # escaped quote.
        text = "api" + "\\" * 20 + "u005fkey: %s" % SECRET
        out = redact_text(text)
        assert not leaks(out), out

    def test_escape_boundary_is_sixteen(self) -> None:
        # Sixteen levels are read in place; seventeen cannot be read directly
        # and must be probed or withheld. Both must be leak-free.
        sixteen = redact_text(self._keyed(16))
        seventeen = redact_text(self._keyed(17))
        assert not leaks(sixteen) and not leaks(seventeen)
        assert "[REDACTED]" in sixteen
        assert REDACTED in seventeen


# --------------------------------------------------------------------------
# D-1  wrapper-decorated values (revision 5).
# --------------------------------------------------------------------------
class TestWrapperContainers:
    """A strong key's decorated value is consumed whole, never tokenised."""

    @pytest.mark.parametrize(
        "text",
        [
            'credentials=<{"value": "hunter2024"}>',
            'credentials=({"value": "hunter2024"})',
            "credentials=({'value': 'hunter2024'},)",
            "credentials=('value', 'hunter2024')",
            "credentials=({'a': 1}, {'b': 'hunter2024'})",
            'credentials=<<{"value": "hunter2024"}>>',
            'credentials=<[{"value": "hunter2024"}]>',
            'credentials=[{"value": "hunter2024"}]',
        ],
    )
    def test_decorated_containers_are_replaced_whole(self, text: str) -> None:
        redacted = redact_text(text)
        assert not leaks(redacted), redacted
        assert redacted == "credentials=[REDACTED]"

    def test_the_review_example(self) -> None:
        # D-1, verbatim: revision 4 returned
        # ``credentials=[REDACTED]: "hunter2024"}>``.
        out = redact_text('credentials=<{"value": "hunter2024"}>')
        assert out == "credentials=[REDACTED]"
        assert SECRET not in out

    def test_quoted_wrapper(self) -> None:
        out = redact_text('credentials="<{\\"value\\": \\"hunter2024\\"}>"')
        assert not leaks(out), out
        assert out == 'credentials="[REDACTED]"'

    def test_inline_yaml_sequence_item(self) -> None:
        assert redact_text("credentials: - hunter2024") == "credentials: [REDACTED]"

    def test_same_indent_yaml_sequence(self) -> None:
        assert redact_text("token:\n- x: hunter2024") == "token: [REDACTED]"

    def test_same_indent_yaml_sequence_keeps_following_line(self) -> None:
        text = "token:\n- x: hunter2024\nnext: ok"
        assert redact_text(text) == "token: [REDACTED]\nnext: ok"

    @pytest.mark.parametrize(
        "text",
        [
            'credentials=<{"value": "hunter2024"}',
            'credentials=({"value": "hunter2024"}',
            'credentials=<{"value": "hunter2024"})',
        ],
    )
    def test_malformed_wrappers_withhold_the_tail(self, text: str) -> None:
        out = redact_text(text)
        assert not leaks(out), out
        assert out.endswith("[REDACTED]")

    def test_mismatched_wrapper_with_a_nested_key_is_withheld(self) -> None:
        # A mis-nested group whose content starts with ``key:``/``key=`` cannot
        # be consumed as a scalar without leaving the secret behind, so it is
        # withheld to the end of the line.
        for opener in "<(":
            for closer in "})]":
                text = f"credentials={opener}value: hunter2024{closer}"
                out = redact_text(text)
                assert not leaks(out), text
                assert out == "credentials=[REDACTED]", text

    def test_mismatched_wrapper_keeps_the_following_line(self) -> None:
        text = "credentials=<value: hunter2024}\nnext: ok"
        assert redact_text(text) == "credentials=[REDACTED]\nnext: ok"

    def test_mismatched_wrapper_all_entry_points(self) -> None:
        text = "credentials=<value: hunter2024}"
        assert SECRET not in redact_text(text)
        assert SECRET not in json.dumps(redact_mapping({"log": text}))
        assert SECRET not in json.dumps(redact_payload({"log": text}))
        assert SECRET not in json.dumps(summarize_payload({"log": text}, max_bytes=512))

    def test_wrapped_scalar_is_consumed_whole(self) -> None:
        # A parenthesised value that closes is one value; no dangling tail.
        assert redact_text("password: (rotated 2026-01-01)") == "password: [REDACTED]"

    def test_dangling_wrapper_keeps_the_scalar_rule(self) -> None:
        # No container to leak, so the text is not withheld.
        out = redact_text("password: (rotated")
        assert out == "password: [REDACTED]"

    def test_harmless_text_does_not_protect_a_sensitive_value(self) -> None:
        out = redact_text('credentials=<{"note": "nothing to see", "value": "hunter2024"}>')
        assert not leaks(out), out
        assert out == "credentials=[REDACTED]"

    def test_harmless_wrapped_scalar_under_a_sensitive_key_is_redacted(self) -> None:
        assert redact_text('credentials=<"see the vault">') == "credentials=[REDACTED]"

    def test_benign_wrapper_under_a_non_sensitive_key_is_untouched(self) -> None:
        text = 'note=<{"value": "hunter2024"}>'
        assert redact_text(text) == text

    def test_trailing_fields_survive(self) -> None:
        out = redact_text('credentials=<{"value": "hunter2024"}> user=alice')
        assert out == "credentials=[REDACTED] user=alice"

    def test_wrapper_redaction_is_idempotent(self) -> None:
        for text in (
            'credentials=<{"value": "hunter2024"}>',
            "credentials=('value', 'hunter2024')",
            "credentials: - hunter2024",
        ):
            once = redact_text(text)
            assert redact_text(once) == once

    def test_all_three_entry_points(self) -> None:
        text = 'credentials=<{"value": "hunter2024"}>'
        assert SECRET not in redact_text(text)
        assert SECRET not in json.dumps(redact_payload({"log": text}))
        summary = summarize_payload({"log": text}, max_bytes=512)
        assert SECRET not in json.dumps(summary)
        assert SECRET not in json.dumps(redact_mapping({"log": text}))


# --------------------------------------------------------------------------
# D-2  ``{name, value}`` credential records (revision 5).
# --------------------------------------------------------------------------
class TestCredentialRecords:
    @pytest.mark.parametrize(
        "text",
        [
            '{"name": "api_key", "value": "hunter2024"}',
            "{'name': 'api_key', 'value': 'hunter2024'}",
            '{"value": "hunter2024", "name": "api_key"}',
            'Credential(name="api_key", value="hunter2024")',
            "name=api_key, value=hunter2024",
            "value=hunter2024; name=api_key",
            '{"name": "token", "value": "ghp_abcd1234efgh"}',
        ],
    )
    def test_record_secret_is_redacted(self, text: str) -> None:
        out = redact_text(text)
        assert not leaks(out), out
        assert "ghp_abcd1234efgh" not in out
        assert "[REDACTED]" in out

    def test_json_record_shape_is_preserved(self) -> None:
        text = '{"name": "api_key", "value": "hunter2024"}'
        assert redact_text(text) == '{"name": "api_key", "value": "[REDACTED]"}'

    def test_reversed_record_shape_is_preserved(self) -> None:
        text = '{"value": "hunter2024", "name": "api_key"}'
        assert redact_text(text) == '{"value": "[REDACTED]", "name": "api_key"}'

    def test_nested_record(self) -> None:
        text = '{"error": {"name": "token", "value": "hunter2024"}}'
        out = redact_text(text)
        assert not leaks(out), out
        assert '"name": "token"' in out

    def test_record_with_container_value(self) -> None:
        out = redact_text('{"name": "token", "value": {"nested": "hunter2024"}}')
        assert not leaks(out), out

    def test_record_with_array_value(self) -> None:
        out = redact_text('{"name": "token", "value": ["a", "hunter2024"]}')
        assert not leaks(out), out

    def test_non_sensitive_name_is_ordinary_data(self) -> None:
        text = '{"name": "username", "value": "hunter2024"}'
        assert redact_text(text) == text

    def test_record_without_a_value_field_is_untouched(self) -> None:
        text = '{"name": "api_key", "count": 3}'
        assert redact_text(text) == text

    def test_malformed_record_still_loses_its_secret(self) -> None:
        out = redact_text('{"name": "api_key", "value": "hunter2024"')
        assert not leaks(out), out

    def test_harmless_value_text_is_still_redacted(self) -> None:
        out = redact_text('{"name": "api_key", "value": "see the vault"}')
        assert out == '{"name": "api_key", "value": "[REDACTED]"}'

    def test_encoded_record_fails_closed(self) -> None:
        inner = '{"name": "api_key", "value": "hunter2024"}'
        for text in (json.dumps(inner), json.dumps(json.dumps(inner))):
            out = redact_text(text)
            assert not leaks(out), out
            assert REDACTED in out

    def test_record_in_text_behind_a_comma_is_not_mistaken_for_a_key(self) -> None:
        # The comma guard: ``api_key`` is a value here, not a key, so the
        # following field must not be rewritten.
        text = '{"a": "token", "b": "x"}'
        assert redact_text(text) == text

    def test_record_redaction_is_idempotent(self) -> None:
        for text in (
            '{"name": "api_key", "value": "hunter2024"}',
            "name=api_key, value=hunter2024",
            "Credential(name=\"api_key\", value=\"hunter2024\")",
        ):
            once = redact_text(text)
            assert redact_text(once) == once

    def test_all_three_entry_points(self) -> None:
        text = '{"name": "api_key", "value": "hunter2024"}'
        assert SECRET not in redact_text(text)
        assert SECRET not in json.dumps(redact_mapping({"log": text}))
        assert SECRET not in json.dumps(redact_payload({"log": text}))
        assert SECRET not in json.dumps(summarize_payload({"log": text}, max_bytes=512))

    def test_structural_record_mapping(self) -> None:
        # The structural walk already handled this shape; the text path must
        # agree rather than regress it.
        out = redact_mapping({"event": '{"name": "api_key", "value": "hunter2024"}'})
        assert SECRET not in json.dumps(out)


# --------------------------------------------------------------------------
# D-3  decorated-key recognition limits (documented, pinned).
# --------------------------------------------------------------------------
class TestDecoratedKeyLimits:
    """The key token is ``[A-Za-z0-9_.-]{1,64}`` plus escaped runs.

    Recognition requires the separator to follow the token directly (optionally
    through one quote). These tests pin both sides: what is recognised, and the
    limits that are documented rather than silently accepted.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "api_key: hunter2024",
            '"api_key": "hunter2024"',
            "__api_key__: hunter2024",
            "x-api-key: hunter2024",
            "x.api_key: hunter2024",
        ],
    )
    def test_supported_decorations_are_recognized(self, text: str) -> None:
        assert not leaks(redact_text(text)), text

    @pytest.mark.parametrize(
        "text",
        [
            "[api_key]: hunter2024",
            "`api_key`: hunter2024",
            "*api_key*: hunter2024",
            "/api_key/: hunter2024",
        ],
    )
    def test_surrounding_punctuation_is_a_documented_limitation(self, text: str) -> None:
        # R29: decorative punctuation between the key token and the separator
        # is not recognised. Pinning the limit keeps it visible; it is not a
        # claim that the value is safe.
        assert leaks(redact_text(text)), text

    def test_overlong_key_is_a_documented_limitation(self) -> None:
        text = "k" * 65 + ": hunter2024"
        assert leaks(redact_text(text)), text

    def test_zero_width_character_after_the_key_is_a_documented_limitation(self) -> None:
        text = "api_key\u200b: hunter2024"
        assert leaks(redact_text(text)), text
