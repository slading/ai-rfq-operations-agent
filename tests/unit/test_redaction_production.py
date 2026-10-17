"""Production probes beyond the published gate; all secrets are synthetic."""

from __future__ import annotations

import itertools
import json
import random
from collections.abc import Mapping

import pytest
from pydantic import BaseModel, ValidationError

from rfq_agent.config import DatabaseSettings, Settings
from rfq_agent.domain.values import DomainModel
from rfq_agent.observability.redaction import (
    REDACTED,
    REDACTED_KEY,
    redact_mapping,
    redact_payload,
    redact_text,
    summarize_payload,
)

SECRET = "hunter2024"  # noqa: S105 - synthetic, no provider-prefix backstop


def assert_safe(text: str) -> None:
    """Check every public rendering path, fragments, and repeat application."""
    outputs = [
        redact_text(text),
        redact_mapping({"log": text}),
        redact_payload({"log": text}),
        summarize_payload({"log": text}, max_bytes=4096),
        summarize_payload({"log": text}, max_bytes=21),
    ]
    for output in outputs:
        for start in range(len(SECRET) - 7):
            assert SECRET[start : start + 8] not in str(output)
    once = redact_text(text)
    assert redact_text(once) == once
    structured = redact_payload({"log": text})
    assert redact_payload(structured) == structured


@pytest.mark.parametrize("count", [31, 32, 33, 129, 2000])
def test_record_redaction_limit_fails_closed(count):
    record = '{"name":"api_key","value":{"leaf":"hunter2024"}}'
    assert_safe(" ".join([record] * count))


@pytest.mark.parametrize("count", [127, 128, 129])
def test_candidate_limit_cannot_hide_a_later_record(count):
    assert_safe("{} " * count + '{"name":"api_key","value":{"leaf":"hunter2024"}}')


@pytest.mark.parametrize("padding", [262100, 262144, 300000])
@pytest.mark.parametrize(
    "record",
    [
        '{"name":"api_key","value":"hunter2024"',
        '{"name":"api_key","value":{"leaf":"hunter2024"}',
        '{"value":{"leaf":"hunter2024"},"name":"api_key"',
        "name=api_key, value=hunter2024",
        '"{\\"name\\":\\"api_key\\",\\"value\\":\\"hunter2024\\"}"',
    ],
)
def test_large_record_never_bypasses_pair_or_encoding_limits(padding, record):
    assert_safe("x" * padding + " " + record)


@pytest.mark.parametrize(
    "text",
    [
        '{"name":"api_key","other":{"value":"decoy"},"value":"hunter2024"}',
        '{"name":"api_key","value":"decoy","value":"hunter2024"}',
        '{"name":"api_key","value":"hunter2024","value":"decoy"}',
        '{"name":"username","name":"api_key","value":"hunter2024"}',
        '{"name":"api_key","name":"username","value":"hunter2024"}',
        '{"name":"api_key","value":{"leaf":"hunter2024"}',
        '{"value":{"leaf":"hunter2024"},"name":"api_key"',
        "name=api_key, value=<leaf: hunter2024}",
        'Credential(value={"leaf":"hunter2024"}, name="api_key")',
        '{"name":"api_key","value":"decoy","other":{"name":"api_key","metadata":3,"value":"hunter2024"}}',
        'credentials="[REDACTED] hunter2024"',
        'credentials={\\"v\\": \\"a}, hunter2024\\"}',
        'credentials=<{\\"v\\": \\"a}, hunter2024\\"}>',
    ],
)
def test_record_and_wrapper_disclosures_found_during_implementation(text):
    assert_safe(text)


def test_duplicate_values_are_both_removed_and_keys_are_preserved():
    text = '{"name":"api_key","value":"hunter2024","value":"hunter2024"}'
    assert redact_text(text) == '{"name":"api_key","value":"[REDACTED]","value":"[REDACTED]"}'


def test_container_quote_parity_keeps_following_business_fields():
    text = "credentials=" + json.dumps({"v": "trailing\\"}) + " user=alice"
    assert redact_text(text) == "credentials=[REDACTED] user=alice"
    text = "credentials=(" + json.dumps({"v": "trailing\\"}) + ",) user=alice"
    assert redact_text(text) == "credentials=[REDACTED] user=alice"


@pytest.mark.parametrize("depth", [31, 32, 33, 63, 64, 65, 100])
@pytest.mark.parametrize(("opener", "closer"), [("(", ")"), ("<", ">"), ("(<", ">)")])
def test_wrapper_depth_overflow_never_falls_back_to_partial_scalar(depth, opener, closer):
    assert_safe("credentials=" + opener * depth + "('value','hunter2024')" + closer * depth)


def test_oversized_wrapped_value_fails_closed():
    assert_safe("credentials=<" + " " * 65536 + '{"value":"hunter2024"}>')


@pytest.mark.parametrize("name_field", ["name", "key", "field", "type", "Name"])
@pytest.mark.parametrize("value_field", ["value", "Value"])
def test_structural_and_text_record_fields_agree(name_field, value_field):
    record = {name_field: "api_key", value_field: SECRET}
    assert SECRET not in str(redact_payload(record))
    assert_safe(json.dumps(record))


@pytest.mark.parametrize("name", ["api_key", "authorization", "proxy-authorization", "token"])
def test_credential_name_without_value_is_preserved(name):
    text = json.dumps({"name": name, "count": 3})
    assert redact_text(text) == text


def test_seeded_wrapper_fuzz_all_entry_points():
    rng = random.Random(30501)  # noqa: S311 - reproducible test data
    for _ in range(400):
        pairs = [rng.choice([("<", ">"), ("(", ")")]) for _ in range(rng.randint(1, 5))]
        inner = rng.choice(
            [json.dumps({"leaf": SECRET}), repr(("value", SECRET)), f"value: {SECRET}"]
        )
        text = "credentials=" + "".join(o for o, _ in pairs) + inner
        text += "".join(c for _, c in reversed(pairs))
        assert_safe(text)
        # Deleting a closer is malformed; a secret must still disappear.
        assert_safe(text[:-1])


def test_record_fuzz_values_orders_and_spacing():
    for name, value, order, separator in itertools.product(
        ["api_key", "password", "token", "secret"],
        [SECRET, {"leaf": SECRET}, ["decoy", SECRET]],
        [False, True],
        [",", ", ", ",\n"],
    ):
        fields = [("name", name), ("value", value)]
        if order:
            fields.reverse()
        text = "{" + separator.join(json.dumps(k) + ":" + json.dumps(v) for k, v in fields) + "}"
        assert_safe(text)
        assert_safe(text[:-1])


def test_hostile_mapping_and_key_collisions_fail_closed_without_loss():
    class Hostile(Mapping):
        def __iter__(self):
            raise ValueError(SECRET)

        def __len__(self):
            return 1

        def __getitem__(self, key):
            raise ValueError(SECRET)

    assert redact_mapping(Hostile()) == {REDACTED_KEY: REDACTED}
    data = {"api_key=hunter2024": 1, "[REDACTED_KEY]": 2, "[REDACTED_KEY]#2": 3}
    redacted = redact_mapping(data)
    assert sorted(redacted.values()) == [1, 2, 3]
    assert redact_mapping(redacted) == redacted


def test_pydantic_boundary_is_preserved():
    assert Settings.model_config.get("hide_input_in_errors") is True
    assert not DatabaseSettings.model_config.get("hide_input_in_errors", False)
    assert not DomainModel.model_config.get("hide_input_in_errors", False)
    with pytest.raises(ValidationError) as raised:
        DatabaseSettings(busy_timeout_ms=SECRET)
    error = raised.value
    assert SECRET in str(error)
    assert SECRET in str(error.errors())
    assert SECRET in error.json()
    assert SECRET not in redact_text(str(error))
    assert SECRET not in str(redact_mapping({"errors": error.errors()}))


def test_pydantic_model_and_summary_integration():
    class Event(BaseModel):
        log: str

    event = Event(log='credentials=<{"value":"hunter2024"}>')
    assert SECRET not in str(redact_payload(event))
    summary = summarize_payload({"event": event}, max_bytes=1024)
    assert summary["payload_status"] == "INLINE"
    assert SECRET not in str(summary)


def test_r29_and_r32_remain_explicit_text_disclosure_limits():
    # These are residual disclosures, never evidence of safety for arbitrary logs.
    for text in ("log [api_key]: hunter2024", '{"a":"token","b":"hunter2024"}'):
        assert SECRET in redact_text(text)
        assert SECRET in str(redact_mapping({"log": text}))


def test_large_benign_name_value_prose_is_preserved():
    text = "name=widget, value=Warsaw " * 15000
    assert redact_text(text) == text
