"""Unit and adversarial security tests for the F3-C safe observability boundary."""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from rfq_agent.config import Settings
from rfq_agent.contracts.llm import ModelPurpose, ParseFailure, ParseIssue
from rfq_agent.observability import boundary
from rfq_agent.observability.boundary import (
    DIAGNOSTIC_SCHEMA,
    MAX_DIAGNOSTIC_BYTES,
    DiagnosticErrorCode,
    DiagnosticEvent,
    DiagnosticPurpose,
    DiagnosticResultStatus,
    build_diagnostic_event,
    summarize_diagnostic_event,
)
from rfq_agent.observability.ids import TraceContext
from rfq_agent.observability.spans import ToolResultStatus
from rfq_agent.observability.validation_errors import render_validation_error

_API_KEY = "gsk_SYNTHETIC_A1b2C3d4E5f6G7h8"
_OPERATOR_TOKEN = "op_tok_SYNTHETIC_87654321"  # noqa: S105 - synthetic
_DATABASE_PASSWORD = "SYNTHETIC_DB_PASS_0123456789"  # noqa: S105 - synthetic
_DATABASE_URL = f"postgresql://rfquser:{_DATABASE_PASSWORD}@db.synthetic:5432/rfq"
_LONG_SECRET = "ghp_" + "A1b2C3d4E5f6" * 16
_CANARY_PROSE = "Bearer gsk_SYNTHETIC_PROSE_SECRET_9876543210"
_R29_DECORATED = "log [api_key]: hunter2024_r29_canary"
_R29_ZERO_WIDTH = "api_key\u200b: hunter2024_zw_canary"
_R32_CANARY = "SYNTHETIC_R32_SEMANTIC_SECRET_2026"

#: Synthetic secrets that match the WITHDRAWN pattern ``^[a-z][a-z0-9_.-]{0,63}$`` exactly.
_REGEX_MATCHING_SECRETS: tuple[str, ...] = (
    "hunter2024",
    "secret123",
    "token2024x",
    "a1b2c3d4",
    "pass.word-2024",
)

_EXPECTED_FALLBACK: dict[str, object] = {
    "schema": "rfq.diagnostic/v1",
    "event": "diagnostic_unavailable",
    "status": "UNAVAILABLE",
    "error_code": None,
    "trace_id": None,
    "rfq_id": None,
    "run_id": None,
    "span_id": None,
    "validation": None,
    "attributes": {},
    "attributes_withheld": 0,
}

_EXPECTED_PAYLOAD_KEYS: frozenset[str] = frozenset(_EXPECTED_FALLBACK.keys())

_EXPECTED_INT_KEYS: frozenset[str] = frozenset(
    {
        "duration_ms",
        "latency_ms",
        "count",
        "attempt",
        "retries",
        "repair_attempts",
        "step",
        "seq",
        "error_count",
        "validation_error_count",
        "tokens_in",
        "tokens_out",
        "tokens_cached",
        "request_bytes",
        "result_bytes",
        "args_bytes",
    }
)

_EXPECTED_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "output_valid",
        "truncated",
        "duplicate",
        "eligible_for_human_review",
        "requires_human_approval",
    }
)

_EXPECTED_ENUM_KEYS: frozenset[str] = frozenset({"purpose", "result_status"})

_OMITTED_V1_KEYS: tuple[str, ...] = (
    "model",
    "tool_name",
    "stage",
    "status_code",
    "outcome",
    "reason_code",
    "finish_reason",
    "messages_sha256",
    "args_sha256",
    "result_sha256",
    "payload_sha256",
    "body_sha256",
    "detail_sha256",
)


def _assert_secret_absent(rendered: str, secret: str, *, min_length: int = 8) -> None:
    """Assert neither the whole secret nor any >= min_length substring appears."""
    assert secret not in rendered
    for length in range(min_length, len(secret) + 1):
        for start in range(len(secret) - length + 1):
            assert secret[start : start + length] not in rendered


def _assert_valid_wire_payload(payload: Mapping[str, object]) -> str:
    """Assert that a boundary payload obeys key, JSON, and 1024-byte invariants."""
    assert type(payload) is dict
    assert set(payload.keys()) == _EXPECTED_PAYLOAD_KEYS
    serialized = json.dumps(payload, allow_nan=False, sort_keys=True)
    assert len(serialized.encode("utf-8")) <= MAX_DIAGNOSTIC_BYTES
    assert json.loads(serialized) == payload
    summary = summarize_diagnostic_event(payload)
    assert type(summary) is str
    assert len(serialized) + len(summary) <= MAX_DIAGNOSTIC_BYTES + 128
    return f"{serialized}\n{summary}"


def _settings_validation_error() -> ValidationError:
    """Build nested Settings validation errors carrying synthetic credentials."""
    settings_input = {
        "groq": {"api_key": [_API_KEY]},
        "http": {"operator_token": [_OPERATOR_TOKEN]},
        "database": {"url": [_DATABASE_URL]},
        "agent": {"max_steps": _LONG_SECRET},
        "workflow": {"auto_approve_enabled": True, "require_human_approval": False},
    }
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate(settings_input)
    return raised.value


class _HostileValue:
    """Object that records and fails if any representation or comparison hook runs."""

    def __init__(self, calls: list[str], canary: str = "SYNTHETIC_HOSTILE_OBJECT_SECRET") -> None:
        self._calls = calls
        self._canary = canary

    @property
    def __class__(self) -> type:  # type: ignore[override]
        self._calls.append("class")
        raise RuntimeError(self._canary)

    def __str__(self) -> str:
        self._calls.append("str")
        raise AssertionError(self._canary)

    def __repr__(self) -> str:
        self._calls.append("repr")
        raise AssertionError(self._canary)

    def __format__(self, _spec: str) -> str:
        self._calls.append("format")
        raise AssertionError(self._canary)

    def __eq__(self, _other: object) -> bool:
        self._calls.append("eq")
        raise RuntimeError(self._canary)

    def __hash__(self) -> int:
        self._calls.append("hash")
        raise RuntimeError(self._canary)


class _CollidingHostileKey:
    """Non-string dict key whose hash collides with 'schema' and raises on __eq__."""

    def __init__(self, calls: list[str], target_key: str = "schema") -> None:
        self._calls = calls
        self._hash = hash(target_key)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, _other: object) -> bool:
        self._calls.append("key_eq")
        raise RuntimeError("SYNTHETIC_HOSTILE_KEY_EQ_SECRET")

    def __str__(self) -> str:
        self._calls.append("key_str")
        raise AssertionError("SYNTHETIC_HOSTILE_KEY_STR_SECRET")

    def __repr__(self) -> str:
        self._calls.append("key_repr")
        raise AssertionError("SYNTHETIC_HOSTILE_KEY_REPR_SECRET")


# ---------------------------------------------------------------------------
# Happy-path and contract coverage
# ---------------------------------------------------------------------------


def test_happy_path_validation_failed_embeds_f3a2_verbatim() -> None:
    error = _settings_validation_error()
    trace = TraceContext(
        trace_id="0123456789abcdef0123456789abcdef",
        rfq_id="RFQ-2026-0001",
        run_id="RUN-2026-0001",
        span_id="abcdef0123456789",
    )

    payload = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        trace=trace,
        validation_error=error,
    )

    assert payload == {
        "schema": DIAGNOSTIC_SCHEMA,
        "event": "validation_failed",
        "status": "SAFE",
        "error_code": "VALIDATION_ERROR",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "rfq_id": "RFQ-2026-0001",
        "run_id": "RUN-2026-0001",
        "span_id": "abcdef0123456789",
        "validation": render_validation_error(error),
        "attributes": {},
        "attributes_withheld": 0,
    }
    assert summarize_diagnostic_event(payload) == (
        "Diagnostic event: validation_failed (VALIDATION_ERROR)."
    )
    _assert_valid_wire_payload(payload)


def test_happy_path_validation_failed_withholds_all_attributes() -> None:
    error = _settings_validation_error()

    payload = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=error,
        attributes={
            "purpose": DiagnosticPurpose.EXTRACT,
            "duration_ms": 42,
            "output_valid": False,
        },
    )

    assert payload["status"] == "SAFE"
    assert payload["attributes"] == {}
    assert payload["attributes_withheld"] == 3


@pytest.mark.parametrize("code", list(DiagnosticErrorCode))
def test_happy_path_operation_failed_all_error_codes(code: DiagnosticErrorCode) -> None:
    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=code,
    )

    assert payload["status"] == "SAFE"
    assert payload["event"] == "operation_failed"
    assert payload["error_code"] == code.value
    assert type(payload["error_code"]) is str
    assert payload["validation"] is None
    assert payload["attributes"] == {}
    assert payload["attributes_withheld"] == 0
    assert summarize_diagnostic_event(payload) == (
        f"Diagnostic event: operation_failed ({code.value})."
    )
    _assert_valid_wire_payload(payload)


def test_happy_path_all_allowlisted_attribute_keys() -> None:
    for purpose in DiagnosticPurpose:
        for result_status in DiagnosticResultStatus:
            payload = build_diagnostic_event(
                event=DiagnosticEvent.OPERATION_FAILED,
                error_code=DiagnosticErrorCode.TOOL_FAILURE,
                attributes={
                    "purpose": purpose,
                    "result_status": result_status,
                },
            )
            assert payload["attributes"] == {
                "purpose": purpose.value,
                "result_status": result_status.value,
            }
            assert type(payload["attributes"]["purpose"]) is str  # type: ignore[index]
            assert type(payload["attributes"]["result_status"]) is str  # type: ignore[index]
            _assert_valid_wire_payload(payload)

    for key in sorted(_EXPECTED_INT_KEYS):
        payload = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes={key: 2**31 - 1},
        )
        assert payload["attributes"] == {key: 2**31 - 1}
        _assert_valid_wire_payload(payload)

    for key in sorted(_EXPECTED_BOOL_KEYS):
        for flag in (True, False):
            payload = build_diagnostic_event(
                event=DiagnosticEvent.OPERATION_FAILED,
                error_code=DiagnosticErrorCode.WORKFLOW_ERROR,
                attributes={key: flag},
            )
            assert payload["attributes"] == {key: flag}
            _assert_valid_wire_payload(payload)


def test_happy_path_diagnostic_unavailable_discards_caller_inputs() -> None:
    error = _settings_validation_error()
    trace = TraceContext(trace_id="a" * 32, rfq_id="RFQ-001", run_id="RUN-001", span_id="b" * 16)

    payload = build_diagnostic_event(
        event=DiagnosticEvent.DIAGNOSTIC_UNAVAILABLE,
        trace=trace,
        validation_error=error,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes={"duration_ms": 10, "secret": _API_KEY},
    )

    assert payload == _EXPECTED_FALLBACK
    assert summarize_diagnostic_event(payload) == "Diagnostic unavailable."


def test_runtime_type_annotations_resolve() -> None:
    assert get_type_hints(build_diagnostic_event)
    assert get_type_hints(summarize_diagnostic_event)


# ---------------------------------------------------------------------------
# Adversarial Matrix A01 - A23
# ---------------------------------------------------------------------------


def test_a01_nested_settings_validation_error_with_secrets() -> None:
    """A01 (I-4): Settings ValidationError with synthetic secrets yields only F3-A2 dict."""
    error = _settings_validation_error()
    custom_error = ValidationError.from_exception_data(
        "SettingsSecretContext",
        [
            {
                "type": PydanticCustomError(
                    "secret_context_error",
                    "failed with {api_key} and {db_url}",
                    {"api_key": _API_KEY, "db_url": _DATABASE_URL},
                ),
                "loc": ("groq", "api_key", _OPERATOR_TOKEN),
                "input": _LONG_SECRET,
            }
        ],
    )

    for candidate in (error, custom_error):
        payload = build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            validation_error=candidate,
        )
        combined = _assert_valid_wire_payload(payload)
        assert payload["validation"] == render_validation_error(candidate)
        for secret in (
            _API_KEY,
            _OPERATOR_TOKEN,
            _DATABASE_PASSWORD,
            _DATABASE_URL,
            _LONG_SECRET,
        ):
            _assert_secret_absent(combined, secret)


def test_a02_hostile_ctx_object_in_validation_error_not_triggered() -> None:
    """A02 (I-2/I-4): Hostile ctx object in ValidationError is never formatted by boundary."""
    calls: list[str] = []

    class HostileContextError(ValueError):
        def __str__(self) -> str:
            calls.append("str")
            return "SYNTHETIC_A02_CTX_SECRET_VALUE"

        def __repr__(self) -> str:
            calls.append("repr")
            return "SYNTHETIC_A02_CTX_SECRET_VALUE"

    class ModelWithHostileValidator(BaseModel):
        field: str

        @field_validator("field")
        @classmethod
        def _fail(cls, _v: str) -> str:
            raise HostileContextError

    with pytest.raises(ValidationError) as raised:
        ModelWithHostileValidator(field="trigger")

    calls.clear()
    payload = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=raised.value,
    )
    combined = _assert_valid_wire_payload(payload)

    assert calls == []
    assert payload["status"] == "SAFE"
    _assert_secret_absent(combined, "SYNTHETIC_A02_CTX_SECRET_VALUE")


@pytest.mark.parametrize("behavior", ["raise", "spoof", "hostile_object", "none", "subclass"])
def test_a03_non_validation_error_and_hostile_class_hooks(behavior: str) -> None:
    """A03 (I-3/I-8): Non-ValidationError & hostile __class__ hooks fail closed safely."""
    calls: list[str] = []

    class HostileClassSpoof:
        @property
        def __class__(self) -> type:  # type: ignore[override]
            calls.append("class")
            if behavior == "raise":
                raise RuntimeError("SYNTHETIC_A03_CLASS_SECRET")
            return ValidationError

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("must not call __str__")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("must not call __repr__")

    class GuardedValidationError(ValidationError):
        def __getattribute__(self, name: str) -> object:
            calls.append(name)
            raise AssertionError("must not read instance attribute")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("must not represent")

        def errors(self, *_args: object, **_kwargs: object) -> list[object]:  # type: ignore[override]
            calls.append("errors")
            raise AssertionError("must not call errors()")

        def json(self, *_args: object, **_kwargs: object) -> str:  # type: ignore[override]
            calls.append("json")
            raise AssertionError("must not call json()")

        def error_count(self) -> int:
            calls.append("error_count_override")
            raise AssertionError("must use native descriptor")

    if behavior in {"raise", "spoof"}:
        candidate: object = HostileClassSpoof()
    elif behavior == "hostile_object":
        candidate = _HostileValue(calls, "SYNTHETIC_A03_OBJECT_SECRET")
    elif behavior == "none":
        candidate = None
    else:
        candidate = GuardedValidationError.from_exception_data(
            "SYNTHETIC_A03_SUBCLASS_SECRET",
            [{"type": "int_parsing", "loc": ("x",), "input": "SYNTHETIC_A03_SUBCLASS_SECRET"}],
        )
        calls.clear()

    payload = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=candidate,  # type: ignore[arg-type]
    )
    combined = _assert_valid_wire_payload(payload)

    assert calls == []
    if behavior == "subclass":
        assert payload["status"] == "SAFE"
        assert payload["validation"]["error_count"] == 1  # type: ignore[index]
    else:
        assert payload == _EXPECTED_FALLBACK
        assert summarize_diagnostic_event(payload) == "Diagnostic unavailable."
    _assert_secret_absent(combined, "SYNTHETIC_A03")


def test_a04_event_rejects_strings_ints_hostile_objects_and_lookalikes() -> None:
    """A04 (I-1/I-3): event requires exact DiagnosticEvent singleton identity."""
    calls: list[str] = []

    class LookalikeEvent(StrEnum):
        VALIDATION_FAILED = "validation_failed"
        OPERATION_FAILED = "operation_failed"

    forged_event = str.__new__(DiagnosticEvent, "operation_failed")

    invalid_events: tuple[object, ...] = (
        "validation_failed",
        "operation_failed",
        "diagnostic_unavailable",
        "hunter2024",
        0,
        1,
        None,
        True,
        LookalikeEvent.VALIDATION_FAILED,
        LookalikeEvent.OPERATION_FAILED,
        forged_event,
        _HostileValue(calls, "SYNTHETIC_A04_EVENT_SECRET"),
    )

    for bad_event in invalid_events:
        payload = build_diagnostic_event(
            event=bad_event,  # type: ignore[arg-type]
            error_code=DiagnosticErrorCode.TIMEOUT,
        )
        assert payload == _EXPECTED_FALLBACK
        assert summarize_diagnostic_event(payload) == "Diagnostic unavailable."

    assert calls == []


def test_a05_free_text_smuggling_and_omitted_v1_keys_withheld() -> None:
    """A05 (I-1/I-5): Unknown keys and omitted v1 keys are dropped and counted."""
    smuggled_attributes: dict[str, object] = {
        "note": f"api_key={_API_KEY}",
        "message": f"operator_token={_OPERATOR_TOKEN}",
        "exception": _DATABASE_URL,
        "error": "hunter2024",
        "model": "openai/gpt-oss-120b",
        "tool_name": "search_catalog",
        "stage": "resolve",
        "status_code": "500",
        "outcome": "failed",
        "reason_code": "WORKFLOW_ERROR",
        "finish_reason": "stop",
        "messages_sha256": "a" * 64,
        "args_sha256": "b" * 64,
        "result_sha256": "c" * 64,
        "payload_sha256": "d" * 64,
        "duration_ms": 125,
    }

    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.PROVIDER_ERROR,
        attributes=smuggled_attributes,
    )
    combined = _assert_valid_wire_payload(payload)

    assert payload["status"] == "SAFE"
    assert payload["attributes"] == {"duration_ms": 125}
    assert payload["attributes_withheld"] == 15
    for secret in (_API_KEY, _OPERATOR_TOKEN, _DATABASE_PASSWORD, _DATABASE_URL, "hunter2024"):
        _assert_secret_absent(combined, secret)
    for omitted_key in _OMITTED_V1_KEYS:
        assert omitted_key not in payload["attributes"]  # type: ignore[operator]


def test_a06_r29_shapes_as_attribute_keys_and_values_never_emitted() -> None:
    """A06 (I-5): R29 shapes (decorated/zero-width/10^4-char keys and values) are dropped."""
    overlong_key = "api_key_" + ("k" * 10_000)
    r29_attributes: dict[str, object] = {
        "[api_key]": "hunter2024",
        "api_key\u200b": "hunter2024",
        overlong_key: "hunter2024",
        "count": _R29_DECORATED,
        "step": _R29_ZERO_WIDTH,
        "retries": 2,
    }

    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.WORKFLOW_ERROR,
        attributes=r29_attributes,
    )
    combined = _assert_valid_wire_payload(payload)

    assert payload["attributes"] == {"retries": 2}
    assert payload["attributes_withheld"] == 5
    _assert_secret_absent(combined, "hunter2024")
    _assert_secret_absent(combined, _R29_DECORATED)
    _assert_secret_absent(combined, _R29_ZERO_WIDTH)


def test_a07_r32_semantic_shapes_and_nested_containers_withheld() -> None:
    """A07 (I-1/I-5): R32 shapes and nested containers are dropped and never traversed."""
    r32_attributes: dict[str, object] = {
        "a": "token",
        "b": _R32_CANARY,
        "nested_dict": {"a": "token", "b": _R32_CANARY},
        "nested_list": ["token", _R32_CANARY],
        "nested_set": {_R32_CANARY},
        "nested_tuple": ("token", _R32_CANARY),
        "purpose": {"nested": DiagnosticPurpose.TRIAGE},
        "attempt": 1,
    }

    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.ATTEMPTS_EXHAUSTED,
        attributes=r32_attributes,
    )
    combined = _assert_valid_wire_payload(payload)

    assert payload["attributes"] == {"attempt": 1}
    assert payload["attributes_withheld"] == 7
    _assert_secret_absent(combined, _R32_CANARY)


def test_a08_hostile_attribute_values_and_mappings_fail_closed_without_repr() -> None:
    """A08 (I-2/I-3/I-8): Hostile attribute values/keys/mappings never trigger str/repr/eq."""
    calls: list[str] = []
    hostile_val = _HostileValue(calls, "SYNTHETIC_A08_VAL_SECRET")
    hostile_key = _CollidingHostileKey(calls, "latency_ms")

    self_ref_list: list[object] = []
    self_ref_list.append(self_ref_list)
    self_ref_dict: dict[str, object] = {}
    self_ref_dict["self"] = self_ref_dict

    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TOOL_FAILURE,
        attributes={
            "duration_ms": hostile_val,
            "purpose": hostile_val,
            "output_valid": hostile_val,
            "unknown_hostile": hostile_val,
            "cycle_list": self_ref_list,
            "cycle_dict": self_ref_dict,
            hostile_key: 100,  # type: ignore[dict-item]
            "tokens_in": 50,
        },
    )
    combined = _assert_valid_wire_payload(payload)

    assert calls == []
    assert payload["status"] == "SAFE"
    assert payload["attributes"] == {"tokens_in": 50}
    assert payload["attributes_withheld"] == 7
    _assert_secret_absent(combined, "SYNTHETIC_A08")

    class RaisingItemsMapping(Mapping[str, object]):
        def __getitem__(self, _key: str) -> object:
            calls.append("getitem")
            raise RuntimeError("SYNTHETIC_A08_ITEMS_SECRET")

        def __iter__(self) -> Iterator[str]:
            calls.append("iter")
            raise RuntimeError("SYNTHETIC_A08_ITEMS_SECRET")

        def __len__(self) -> int:
            return 1

        def items(self):  # type: ignore[override]
            raise RuntimeError("SYNTHETIC_A08_ITEMS_SECRET")

    class RaisingLenMapping(Mapping[str, object]):
        def __getitem__(self, _key: str) -> object:
            raise RuntimeError("SYNTHETIC_A08_LEN_SECRET")

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("SYNTHETIC_A08_LEN_SECRET")

        def __len__(self) -> int:
            raise RuntimeError("SYNTHETIC_A08_LEN_SECRET")

    for bad_mapping in (RaisingItemsMapping(), RaisingLenMapping(), hostile_val):
        fallback = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TOOL_FAILURE,
            attributes=bad_mapping,  # type: ignore[arg-type]
        )
        assert fallback == _EXPECTED_FALLBACK
    assert calls == []


def test_a09_scalar_edges_rejected_by_exact_type_and_range() -> None:
    """A09 (I-1/I-6): Bool-as-int, int-as-bool, floats, Decimal, UUID, bytes, out-of-range ints."""
    edge_cases: tuple[dict[str, object], ...] = (
        {"duration_ms": True},
        {"duration_ms": False},
        {"output_valid": 1},
        {"output_valid": 0},
        {"latency_ms": 1.0},
        {"latency_ms": float("inf")},
        {"latency_ms": float("-inf")},
        {"latency_ms": float("nan")},
        {"count": Decimal("10")},
        {"count": UUID("12345678-1234-5678-1234-567812345678")},
        {"count": datetime(2026, 10, 18, tzinfo=UTC)},
        {"count": b"hunter2024"},
        {"count": 2**100},
        {"count": 2**31},
        {"count": -1},
        {"count": None},
    )

    for attrs in edge_cases:
        payload = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.WORKFLOW_ERROR,
            attributes=attrs,
        )
        _assert_valid_wire_payload(payload)
        assert payload["status"] == "SAFE"
        assert payload["attributes"] == {}
        assert payload["attributes_withheld"] == 1

    boundary_valid = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.WORKFLOW_ERROR,
        attributes={"count": 0, "step": 2**31 - 1},
    )
    assert boundary_valid["attributes"] == {"count": 0, "step": 2**31 - 1}
    assert boundary_valid["attributes_withheld"] == 0


def test_a10_string_shape_edges_and_injection_strings_rejected() -> None:
    """A10 (I-1/T-10): Formatting/injection/Unicode/hash strings rejected in all attributes."""
    injection_strings: tuple[str, ...] = (
        "UPPERCASE_SECRET_TOKEN",
        "spaces in string hunter2024",
        "line1\nERROR forged log line hunter2024",
        "\x1b[31mANSI_ESCAPE_SECRET\x1b[0m",
        "{__import__('os').environ}",
        "%s%s%s%s%n",
        "🔑_emoji_secret_hunter2024",
        "x" * 4096,
        "not_a_hex_hash_1234567890",
        "deadbeef",
        "a" * 64,
    )

    for candidate in injection_strings:
        payload = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.MALFORMED_MODEL_OUTPUT,
            attributes={
                "purpose": candidate,
                "result_status": candidate,
                "messages_sha256": candidate,
                "args_sha256": candidate,
                "result_sha256": candidate,
                "stage": candidate,
                "model": candidate,
                "tool_name": candidate,
            },
        )
        combined = _assert_valid_wire_payload(payload)
        assert payload["attributes"] == {}
        assert payload["attributes_withheld"] == 8
        assert candidate not in combined


def test_a11_volume_bounds_lying_mappings_17th_attribute_and_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A11 (I-6/I-8): Volume caps, lying len mappings, 16-attr cap, 17th attr, 1024-byte cap."""

    class HugeMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return 1

        def __iter__(self) -> Iterator[str]:
            return (f"k_{i}" for i in range(100_000))

        def __len__(self) -> int:
            return 100_000

    class LyingOverMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return 1

        def __iter__(self) -> Iterator[str]:
            return (f"k_{i}" for i in range(100_000))

        def __len__(self) -> int:
            return 2

        def items(self):  # type: ignore[override]
            return ((f"k_{i}", i) for i in range(100_000))

    class LyingUnderMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return 1

        def __iter__(self) -> Iterator[str]:
            return iter(("count",))

        def __len__(self) -> int:
            return 5

        def items(self):  # type: ignore[override]
            return iter((("count", 1),))

    class LyingZeroMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return 1

        def __iter__(self) -> Iterator[str]:
            return iter(("count",))

        def __len__(self) -> int:
            return 0

        def items(self):  # type: ignore[override]
            return iter((("count", 1),))

    for hostile_map in (
        HugeMapping(),
        LyingOverMapping(),
        LyingUnderMapping(),
        LyingZeroMapping(),
    ):
        assert (
            build_diagnostic_event(
                event=DiagnosticEvent.OPERATION_FAILED,
                error_code=DiagnosticErrorCode.TIMEOUT,
                attributes=hostile_map,
            )
            == _EXPECTED_FALLBACK
        )

    all_int_keys = sorted(_EXPECTED_INT_KEYS)
    assert len(all_int_keys) == 16
    attrs_at_cap = {key: idx for idx, key in enumerate(all_int_keys)}
    at_cap_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes=attrs_at_cap,
    )
    _assert_valid_wire_payload(at_cap_payload)
    assert at_cap_payload["status"] == "SAFE"
    assert len(at_cap_payload["attributes"]) == 16  # type: ignore[arg-type]
    assert at_cap_payload["attributes_withheld"] == 0

    attrs_17 = {**attrs_at_cap, "output_valid": True}
    assert (
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes=attrs_17,
        )
        == _EXPECTED_FALLBACK
    )

    withheld_cap_attrs = {f"unknown_{i}": i for i in range(30)}
    withheld_cap_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes=withheld_cap_attrs,
    )
    assert withheld_cap_payload["status"] == "SAFE"
    assert withheld_cap_payload["attributes"] == {}
    assert withheld_cap_payload["attributes_withheld"] == 16

    attrs_65 = {f"unknown_{i}": i for i in range(65)}
    assert (
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes=attrs_65,
        )
        == _EXPECTED_FALLBACK
    )

    monkeypatch.setattr(boundary, "MAX_DIAGNOSTIC_BYTES", 200)
    oversized = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes=attrs_at_cap,
    )
    assert oversized == _EXPECTED_FALLBACK


def test_a12_trace_stand_ins_hostile_reads_and_valid_trace_context() -> None:
    """A12 (I-3): Trace stand-ins, raising attribute reads, model_construct, and happy path."""
    calls: list[str] = []

    class RaisingTraceMock:
        @property
        def trace_id(self) -> str:
            calls.append("trace_id")
            raise RuntimeError("SYNTHETIC_A12_TRACE_SECRET")

        @property
        def rfq_id(self) -> str:
            calls.append("rfq_id")
            raise RuntimeError("SYNTHETIC_A12_TRACE_SECRET")

    class SubclassTrace(TraceContext):
        @property
        def trace_id(self) -> str:  # type: ignore[override]
            calls.append("subclass_trace_id")
            raise RuntimeError("SYNTHETIC_A12_SUBCLASS_SECRET")

    uninitialized_trace = object.__new__(TraceContext)
    forged_trace_id = TraceContext.model_construct(
        trace_id="hunter2024_invalid_trace",
        rfq_id="RFQ-001",
        run_id="RUN-001",
        span_id="a" * 16,
        parent_span_id=None,
    )
    forged_parent_span = TraceContext.model_construct(
        trace_id="a" * 32,
        rfq_id="RFQ-001",
        run_id="RUN-001",
        span_id="b" * 16,
        parent_span_id="hunter2024_bad_parent",
    )

    for bad_trace in (
        "a" * 32,
        {"trace_id": "a" * 32},
        RaisingTraceMock(),
        _HostileValue(calls, "SYNTHETIC_A12_HOSTILE_TRACE"),
        uninitialized_trace,
        forged_trace_id,
        forged_parent_span,
        SubclassTrace.model_construct(trace_id="a" * 32),
    ):
        payload = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            trace=bad_trace,  # type: ignore[arg-type]
        )
        combined = _assert_valid_wire_payload(payload)
        assert payload["status"] == "SAFE"
        assert payload["trace_id"] is None
        assert payload["rfq_id"] is None
        assert payload["run_id"] is None
        assert payload["span_id"] is None
        _assert_secret_absent(combined, "SYNTHETIC_A12")
        _assert_secret_absent(combined, "hunter2024")

    assert calls == []

    minimal_trace = TraceContext(trace_id="f" * 32)
    minimal_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        trace=minimal_trace,
    )
    assert minimal_payload["trace_id"] == "f" * 32
    assert minimal_payload["rfq_id"] is None
    assert minimal_payload["run_id"] is None
    assert minimal_payload["span_id"] is None

    parent_trace = TraceContext(
        trace_id="0" * 32,
        rfq_id="RFQ-001",
        run_id="RUN-001",
        span_id="1" * 16,
    )
    child_trace = parent_trace.child("2" * 16)
    child_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        trace=child_trace,
    )
    assert child_payload["trace_id"] == "0" * 32
    assert child_payload["rfq_id"] == "RFQ-001"
    assert child_payload["run_id"] == "RUN-001"
    assert child_payload["span_id"] == "2" * 16
    assert "parent_span_id" not in child_payload


def test_a13_summarize_diagnostic_event_rejects_foreign_and_tampered_payloads() -> None:
    """A13: summarize_diagnostic_event validates full structure & nested fields without repr."""
    calls: list[str] = []
    hostile = _HostileValue(calls, "SYNTHETIC_A13_HOSTILE_SECRET")
    hostile_key = _CollidingHostileKey(calls, "schema")

    error = _settings_validation_error()
    valid_op = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        trace=TraceContext(
            trace_id="a" * 32,
            rfq_id="RFQ-001",
            run_id="RUN-001",
            span_id="b" * 16,
        ),
        attributes={
            "purpose": DiagnosticPurpose.RESOLVE,
            "result_status": DiagnosticResultStatus.OK,
            "duration_ms": 10,
            "output_valid": True,
        },
    )
    valid_val = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=error,
    )

    assert summarize_diagnostic_event(_EXPECTED_FALLBACK) == "Diagnostic unavailable."

    class DictSubclass(dict):  # type: ignore[type-arg]
        pass

    # Non-dict / foreign containers
    for non_dict in (None, "string", 123, [valid_op], DictSubclass(valid_op), hostile):
        assert summarize_diagnostic_event(non_dict) == "Diagnostic unavailable."  # type: ignore[arg-type]

    # Unexpected top-level keys, missing keys, and colliding non-str keys
    extra_key_payload = {**valid_op, "extra": "hunter2024"}
    missing_key_payload = {k: v for k, v in valid_op.items() if k != "span_id"}
    colliding_key_payload = {(hostile_key if k == "schema" else k): v for k, v in valid_op.items()}
    for candidate in (extra_key_payload, missing_key_payload, colliding_key_payload):
        assert summarize_diagnostic_event(candidate) == "Diagnostic unavailable."  # type: ignore[arg-type]

    # Forged enum strings and StrEnum instances in event, error_code, purpose, result_status
    forged_code_enum = str.__new__(DiagnosticErrorCode, "TIMEOUT")
    forged_purpose_enum = str.__new__(DiagnosticPurpose, "resolve")
    tampered_cases: list[dict[str, object]] = [
        {**valid_op, "schema": "rfq.diagnostic/v2"},
        {**valid_op, "schema": hostile},
        {**valid_op, "event": "custom_event"},
        {**valid_op, "event": DiagnosticEvent.OPERATION_FAILED},
        {**valid_op, "event": hostile},
        {**valid_op, "status": "UNAVAILABLE"},
        {**valid_op, "status": "OK"},
        {**valid_op, "status": hostile},
        {**valid_op, "error_code": "VALIDATION_ERROR"},
        {**valid_op, "error_code": "hunter2024"},
        {**valid_op, "error_code": DiagnosticErrorCode.TIMEOUT},
        {**valid_op, "error_code": forged_code_enum},
        {**valid_op, "error_code": None},
        {**valid_op, "error_code": hostile},
        # Trace ID tampering
        {**valid_op, "trace_id": "hunter2024"},
        {**valid_op, "trace_id": "A" * 32},
        {**valid_op, "trace_id": None},  # rfq_id/run_id/span_id still non-None!
        {**valid_op, "rfq_id": "pass.word-2024"},
        {**valid_op, "rfq_id": "x"},
        {**valid_op, "rfq_id": hostile},
        {**valid_op, "run_id": "pass.word-2024"},
        {**valid_op, "run_id": hostile},
        {**valid_op, "span_id": "hunter2024"},
        {**valid_op, "span_id": "g" * 16},
        {**valid_op, "span_id": hostile},
        # Validation field on operation_failed must be None
        {**valid_op, "validation": valid_val["validation"]},
        {**valid_op, "validation": {}},
        {**valid_op, "validation": hostile},
        # attributes_withheld tampering
        {**valid_op, "attributes_withheld": -1},
        {**valid_op, "attributes_withheld": 17},
        {**valid_op, "attributes_withheld": True},
        {**valid_op, "attributes_withheld": "0"},
        {**valid_op, "attributes_withheld": hostile},
        # Nested attributes tampering
        {**valid_op, "attributes": None},
        {**valid_op, "attributes": hostile},
        {**valid_op, "attributes": {"unknown_key": 1}},
        {**valid_op, "attributes": {_CollidingHostileKey(calls, "count"): 1}},
        {**valid_op, "attributes": {"purpose": "forged_purpose"}},
        {**valid_op, "attributes": {"purpose": DiagnosticPurpose.RESOLVE}},
        {**valid_op, "attributes": {"purpose": forged_purpose_enum}},
        {**valid_op, "attributes": {"purpose": hostile}},
        {**valid_op, "attributes": {"result_status": "FORGED_STATUS"}},
        {**valid_op, "attributes": {"result_status": DiagnosticResultStatus.OK}},
        {**valid_op, "attributes": {"result_status": hostile}},
        {**valid_op, "attributes": {"duration_ms": True}},
        {**valid_op, "attributes": {"duration_ms": -1}},
        {**valid_op, "attributes": {"duration_ms": 2**31}},
        {**valid_op, "attributes": {"duration_ms": "10"}},
        {**valid_op, "attributes": {"duration_ms": [10]}},
        {**valid_op, "attributes": {"duration_ms": {"nested": 10}}},
        {**valid_op, "attributes": {"duration_ms": hostile}},
        {**valid_op, "attributes": {"output_valid": 1}},
        {**valid_op, "attributes": {"output_valid": "true"}},
        {**valid_op, "attributes": {"output_valid": hostile}},
        {
            **valid_op,
            "attributes": {
                **dict.fromkeys(sorted(_EXPECTED_INT_KEYS), 1),
                "output_valid": True,
            },
        },
        # Nested validation tampering on validation_failed
        {**valid_val, "error_code": "TIMEOUT"},
        {**valid_val, "attributes": {"duration_ms": 1}},
        {**valid_val, "validation": None},
        {**valid_val, "validation": hostile},
        {**valid_val, "validation": {**valid_val["validation"], "extra": 1}},  # type: ignore[dict-item]
        {
            **valid_val,
            "validation": {
                k: v
                for k, v in valid_val["validation"].items()  # type: ignore[union-attr]
                if k != "locations"
            },
        },
        {
            **valid_val,
            "validation": {
                (_CollidingHostileKey(calls, "code") if k == "code" else k): v
                for k, v in valid_val["validation"].items()  # type: ignore[union-attr]
            },
        },
        {**valid_val, "validation": {**valid_val["validation"], "schema": "bad"}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "status": "UNAVAILABLE"}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "code": "OTHER"}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "error_count": 0}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "error_count": 33}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "error_count": True}},  # type: ignore[dict-item]
        {**valid_val, "validation": {**valid_val["validation"], "error_count": hostile}},  # type: ignore[dict-item]
        {
            **valid_val,
            "validation": {
                **valid_val["validation"],  # type: ignore[dict-item]
                "error_count": 5,
                "error_count_truncated": True,
            },
        },
        {
            **valid_val,
            "validation": {**valid_val["validation"], "error_count_truncated": 1},  # type: ignore[dict-item]
        },
        {
            **valid_val,
            "validation": {**valid_val["validation"], "locations": ["secret_field"]},  # type: ignore[dict-item]
        },
        {
            **valid_val,
            "validation": {**valid_val["validation"], "locations_status": "INLINE"},  # type: ignore[dict-item]
        },
    ]

    for tampered in tampered_cases:
        assert summarize_diagnostic_event(tampered) == "Diagnostic unavailable."

    assert calls == []


def test_a14_mutation_isolation_and_determinism() -> None:
    """A14 (I-9): Mutating input mapping or returned dicts never affects subsequent calls."""
    error = _settings_validation_error()
    input_attrs: dict[str, object] = {
        "purpose": DiagnosticPurpose.TRIAGE,
        "duration_ms": 42,
        "output_valid": True,
        "dropped": "hunter2024",
    }

    op_results: list[dict[str, object]] = []
    val_results: list[dict[str, object]] = []
    fallback_results: list[dict[str, object]] = []

    for _ in range(3):
        op_out = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.RATE_LIMITED,
            attributes=input_attrs,
        )
        val_out = build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            validation_error=error,
        )
        fb_out = build_diagnostic_event(
            event=DiagnosticEvent.DIAGNOSTIC_UNAVAILABLE,
        )
        op_results.append(json.loads(json.dumps(op_out)))
        val_results.append(json.loads(json.dumps(val_out)))
        fallback_results.append(json.loads(json.dumps(fb_out)))

        input_attrs["duration_ms"] = 42
        op_out["error_code"] = "MUTATED"
        op_out["attributes"]["duration_ms"] = 999999  # type: ignore[index]
        val_out["validation"]["error_count"] = 999  # type: ignore[index]
        fb_out["status"] = "SAFE"
        fb_out["attributes"]["leaked"] = "hunter2024"  # type: ignore[index]

    assert op_results[0] == op_results[1] == op_results[2]
    assert val_results[0] == val_results[1] == val_results[2]
    assert fallback_results[0] == fallback_results[1] == fallback_results[2] == _EXPECTED_FALLBACK


def test_a15_strict_json_serialisation_and_size_bounds() -> None:
    """A15 (I-6/I-7): json.dumps without default= succeeds and stays <= 1024 bytes."""
    error = _settings_validation_error()
    max_trace = TraceContext(
        trace_id="a" * 32,
        rfq_id="R" + "a" * 62,
        run_id="U" + "b" * 62,
        span_id="c" * 16,
    )
    max_attrs = dict.fromkeys(sorted(_EXPECTED_INT_KEYS), 2**31 - 1)

    samples = (
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.MALFORMED_MODEL_OUTPUT,
            trace=max_trace,
            attributes=max_attrs,
        ),
        build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            trace=max_trace,
            validation_error=error,
            attributes=max_attrs,
        ),
        build_diagnostic_event(event=DiagnosticEvent.DIAGNOSTIC_UNAVAILABLE),
    )

    for out in samples:
        dumped = json.dumps(out, allow_nan=False)
        assert len(dumped.encode("utf-8")) <= MAX_DIAGNOSTIC_BYTES
        summary = summarize_diagnostic_event(out)
        assert len(dumped) + len(summary) <= MAX_DIAGNOSTIC_BYTES + 128


def test_a16_composition_regression_parse_failure_and_exc_errors_archetype() -> None:
    """A16 (I-1/I-2): ParseFailure.model_dump() and exc.errors() offered as attributes."""
    error = _settings_validation_error()
    raw_errors = error.errors()
    parse_failure = ParseFailure(
        issues=(
            ParseIssue(
                field=f"groq.api_key.{_API_KEY}",
                message=f"invalid secret {_OPERATOR_TOKEN}",
                error_type="value_error",
            ),
        ),
        raw=f'{{"api_key": "{_API_KEY}", "password": "{_DATABASE_PASSWORD}"}}',
        provider_error=f"auth failed for {_API_KEY}",
        retryable=False,
        latency_ms=85,
        model=f"model-{_OPERATOR_TOKEN}",
    )

    attrs: dict[str, object] = {
        **parse_failure.model_dump(),
        "pydantic_errors": raw_errors,
    }
    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.MALFORMED_MODEL_OUTPUT,
        attributes=attrs,
    )
    combined = _assert_valid_wire_payload(payload)

    assert payload["status"] == "SAFE"
    assert payload["attributes"] == {"latency_ms": 85}
    for secret in (_API_KEY, _OPERATOR_TOKEN, _DATABASE_PASSWORD, _DATABASE_URL, _LONG_SECRET):
        _assert_secret_absent(combined, secret)


def test_a17_settings_shaped_mapping_and_secret_str_objects_withheld() -> None:
    """A17 (I-1/I-5): Settings.model_dump() and SecretStr-like objects offered as attributes."""
    calls: list[str] = []

    class CountingSecretStr:
        def get_secret_value(self) -> str:
            calls.append("get_secret_value")
            return _API_KEY

        def __str__(self) -> str:
            calls.append("str")
            return _API_KEY

        def __repr__(self) -> str:
            calls.append("repr")
            return _API_KEY

    settings = Settings(
        groq={"api_key": SecretStr(_API_KEY)},  # type: ignore[arg-type]
        http={"operator_token": SecretStr(_OPERATOR_TOKEN)},  # type: ignore[arg-type]
    )

    attrs: dict[str, object] = {
        **settings.model_dump(),
        "api_key": CountingSecretStr(),
        "operator_token": SecretStr(_OPERATOR_TOKEN),
        "settings_obj": settings,
        "step": 3,
    }
    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.PROVIDER_NOT_CONFIGURED,
        attributes=attrs,
    )
    combined = _assert_valid_wire_payload(payload)

    assert calls == []
    assert payload["attributes"] == {"step": 3}
    for secret in (_API_KEY, _OPERATOR_TOKEN):
        _assert_secret_absent(combined, secret)


@pytest.mark.parametrize("count", [0, 1, 2, 31, 32, 33, 80, 1024])
def test_a18_idempotence_and_stability_across_f3a2_error_caps(count: int) -> None:
    """A18 (I-4): 0/1/2/31/32/33/80/1024-error ValidationErrors through boundary."""
    error = ValidationError.from_exception_data(
        "SYNTHETIC_A18_TITLE_SECRET",
        [{"type": "int_parsing", "loc": ("field",), "input": _API_KEY}] * count,
    )

    first = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=error,
    )
    second = build_diagnostic_event(
        event=DiagnosticEvent.VALIDATION_FAILED,
        validation_error=error,
    )
    assert first == second
    combined = _assert_valid_wire_payload(first)
    _assert_secret_absent(combined, "SYNTHETIC_A18_TITLE_SECRET")
    _assert_secret_absent(combined, _API_KEY)

    if count == 0:
        assert first == _EXPECTED_FALLBACK
        assert summarize_diagnostic_event(first) == "Diagnostic unavailable."
    else:
        assert first["status"] == "SAFE"
        assert first["validation"] == {
            "schema": "rfq.validation-diagnostic/v1",
            "status": "SAFE",
            "code": "VALIDATION_ERROR",
            "error_count": min(count, 32),
            "error_count_truncated": count > 32,
            "locations": None,
            "locations_status": "WITHHELD",
        }
        assert summarize_diagnostic_event(first) == (
            "Diagnostic event: validation_failed (VALIDATION_ERROR)."
        )


def test_a19_import_surface_and_ast_no_representation_proof() -> None:
    """A19 (I-2/I-10): boundary.py imports no logging/IO/config/persistence and no str/repr."""
    source_path = Path(inspect.getfile(boundary))
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    allowed_imports = {
        "__future__",
        "collections.abc",
        "enum",
        "json",
        "re",
        "pydantic",
        "rfq_agent.observability.ids",
        "rfq_agent.observability.validation_errors",
    }
    assert imported_modules == allowed_imports

    forbidden_prefixes = (
        "logging",
        "os",
        "sys",
        "pathlib",
        "socket",
        "urllib",
        "http",
        "subprocess",
        "sqlalchemy",
        "alembic",
        "rfq_agent.config",
        "rfq_agent.persistence",
        "rfq_agent.contracts",
        "rfq_agent.demo",
        "rfq_agent.seed",
        "rfq_agent.observability.redaction",
    )
    for mod in imported_modules:
        assert not mod.startswith(forbidden_prefixes), f"Forbidden import: {mod}"

    forbidden_calls = {"str", "repr", "format", "ascii", "getattr", "isinstance"}
    forbidden_attrs = {"errors", "json", "model_dump", "model_dump_json", "args", "__dict__"}

    for node in ast.walk(tree):
        assert not isinstance(node, ast.JoinedStr), "boundary.py must not use f-strings"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, f"Forbidden call: {node.func.id}()"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_attrs, (
                    f"Forbidden attribute call: .{node.func.attr}()"
                )
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attrs, f"Forbidden attribute access: .{node.attr}"


def test_a20_pathological_mapping_refused_without_iteration() -> None:
    """A20 (I-6): Mapping reporting len > 64 is refused with 0 items() calls."""
    items_calls = 0

    class PathologicalMapping(Mapping[str, object]):
        def __getitem__(self, _key: str) -> object:
            raise AssertionError("must not index pathological mapping")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("must not iterate pathological mapping")

        def __len__(self) -> int:
            return 65

        def items(self):  # type: ignore[override]
            nonlocal items_calls
            items_calls += 1
            raise AssertionError("must not call items() when len > 64")

    payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes=PathologicalMapping(),
    )

    assert items_calls == 0
    assert payload == _EXPECTED_FALLBACK


def test_a21_no_free_text_and_no_representation_across_all_inputs() -> None:
    """A21 (I-1/I-2/I-5/I-8): Full canary corpus incl. withdrawn-regex secrets across all inputs."""
    calls: list[str] = []

    class SecretModelLike:
        def model_dump(self) -> dict[str, str]:
            calls.append("model_dump")
            return {"secret": _API_KEY}

        def __str__(self) -> str:
            calls.append("str")
            return _API_KEY

        def __repr__(self) -> str:
            calls.append("repr")
            return _API_KEY

    string_canaries: tuple[str, ...] = (
        *_REGEX_MATCHING_SECRETS,
        _API_KEY,
        _OPERATOR_TOKEN,
        _DATABASE_PASSWORD,
        _DATABASE_URL,
        _LONG_SECRET,
        _CANARY_PROSE,
        _R29_DECORATED,
        _R29_ZERO_WIDTH,
        _R32_CANARY,
    )
    object_canaries: tuple[object, ...] = (
        ValueError(_API_KEY),
        RuntimeError(_OPERATOR_TOKEN),
        SecretStr(_API_KEY),
        SecretModelLike(),
        _HostileValue(calls, "SYNTHETIC_A21_HOSTILE_SECRET"),
    )
    full_corpus: tuple[object, ...] = (*string_canaries, *object_canaries)

    allowed_output_strings: frozenset[str] = frozenset(
        {
            DIAGNOSTIC_SCHEMA,
            "SAFE",
            "UNAVAILABLE",
            "VALIDATION_ERROR",
            *(e.value for e in DiagnosticEvent),
            *(c.value for c in DiagnosticErrorCode),
            *(p.value for p in DiagnosticPurpose),
            *(s.value for s in DiagnosticResultStatus),
        }
    )

    target_attr_keys: tuple[str, ...] = (
        "stage",
        "model",
        "tool_name",
        "purpose",
        "result_status",
        "duration_ms",
        "output_valid",
        "unknown_key",
        *_REGEX_MATCHING_SECRETS,
    )

    for item in full_corpus:
        # 1. Offered as event
        out_event = build_diagnostic_event(
            event=item,  # type: ignore[arg-type]
            error_code=DiagnosticErrorCode.TIMEOUT,
        )
        # 2. Offered as error_code
        out_code = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=item,  # type: ignore[arg-type]
        )
        # 3. Offered as validation_error
        out_val = build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            validation_error=item,  # type: ignore[arg-type]
        )
        # 4. Offered as trace
        out_trace = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            trace=item,  # type: ignore[arg-type]
        )
        # 5. Offered in every attribute key/value position
        attr_map: dict[object, object] = dict.fromkeys(target_attr_keys, item)
        if type(item) is str:
            attr_map[item] = item
        out_attrs = build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes=attr_map,  # type: ignore[arg-type]
        )
        # 6. Offered directly to summarize_diagnostic_event
        summary_direct = summarize_diagnostic_event(item)  # type: ignore[arg-type]
        assert summary_direct == "Diagnostic unavailable."

        assert out_event == _EXPECTED_FALLBACK
        assert out_code == _EXPECTED_FALLBACK
        assert out_val == _EXPECTED_FALLBACK
        assert out_trace["status"] == "SAFE"
        assert out_trace["trace_id"] is None
        assert out_attrs["status"] == "SAFE"
        assert out_attrs["attributes"] == {}
        assert out_attrs["attributes_withheld"] == len(attr_map)

        for out in (out_event, out_code, out_val, out_trace, out_attrs):
            combined = _assert_valid_wire_payload(out)
            for secret in string_canaries:
                _assert_secret_absent(combined, secret)
            for value in out.values():
                if isinstance(value, str):
                    assert value in allowed_output_strings
                elif isinstance(value, dict):
                    for nested_val in value.values():
                        if isinstance(nested_val, str):
                            assert nested_val in allowed_output_strings

    assert calls == []


def test_a22_vocabulary_and_schema_stability_snapshot() -> None:
    """A22 (§3.2/§3.4): Literal snapshot of all v1 vocabularies, keys, and fallback."""
    # (i) §3.1 key set
    assert {
        "schema",
        "event",
        "status",
        "error_code",
        "trace_id",
        "rfq_id",
        "run_id",
        "span_id",
        "validation",
        "attributes",
        "attributes_withheld",
    } == boundary._PAYLOAD_KEYS

    # (ii) Three DiagnosticEvent values
    assert [e.value for e in DiagnosticEvent] == [
        "validation_failed",
        "operation_failed",
        "diagnostic_unavailable",
    ]

    # (iii) 16 DiagnosticErrorCode values + fixed VALIDATION_ERROR
    assert [c.value for c in DiagnosticErrorCode] == [
        "WORKFLOW_ERROR",
        "ILLEGAL_TRANSITION",
        "WORKFLOW_TIMEOUT",
        "STEP_BUDGET_EXCEEDED",
        "RUN_BUDGET_EXCEEDED",
        "ATTEMPTS_EXHAUSTED",
        "PROVIDER_ERROR",
        "RATE_LIMITED",
        "TIMEOUT",
        "TOOL_FAILURE",
        "TOOL_DENIED",
        "TOOL_NOT_FOUND",
        "TOOL_TIMEOUT",
        "MALFORMED_MODEL_OUTPUT",
        "PROVIDER_NOT_CONFIGURED",
        "UNKNOWN",
    ]
    assert len(DiagnosticErrorCode) == 16
    assert boundary._VALIDATION_ERROR_CODE == "VALIDATION_ERROR"

    # (iv) 5 DiagnosticPurpose & 4 DiagnosticResultStatus values (mirrored from source enums)
    assert [p.value for p in DiagnosticPurpose] == [
        "triage",
        "extract",
        "resolve",
        "repair",
        "classify_trust",
    ]
    assert [p.value for p in DiagnosticPurpose] == [p.value for p in ModelPurpose]
    assert [s.value for s in DiagnosticResultStatus] == [
        "OK",
        "ERROR",
        "DENIED",
        "TRUNCATED",
    ]
    assert [s.value for s in DiagnosticResultStatus] == [s.value for s in ToolResultStatus]

    # (v) §3.2.3 attribute key list by type category (2 + 16 + 5 = 23)
    assert boundary._ENUM_ATTRIBUTE_KEYS == _EXPECTED_ENUM_KEYS
    assert boundary._INT_ATTRIBUTE_KEYS == _EXPECTED_INT_KEYS
    assert boundary._BOOL_ATTRIBUTE_KEYS == _EXPECTED_BOOL_KEYS
    assert len(boundary._ALL_ALLOWED_ATTRIBUTE_KEYS) == 23

    # (vi) Omitted v1 dynamic identifier keys are absent
    for omitted in _OMITTED_V1_KEYS:
        assert omitted not in boundary._ALL_ALLOWED_ATTRIBUTE_KEYS
    assert not any(k.endswith("_sha256") for k in boundary._ALL_ALLOWED_ATTRIBUTE_KEYS)

    # (vii) §5 fallback dict literally
    assert (
        build_diagnostic_event(event=DiagnosticEvent.DIAGNOSTIC_UNAVAILABLE) == _EXPECTED_FALLBACK
    )

    # (viii) Plain strings equal to vocabulary values and lookalike enums are rejected
    for vocab_str in ("TIMEOUT", "WORKFLOW_ERROR", "VALIDATION_ERROR"):
        assert (
            build_diagnostic_event(
                event=DiagnosticEvent.OPERATION_FAILED,
                error_code=vocab_str,  # type: ignore[arg-type]
            )
            == _EXPECTED_FALLBACK
        )

    plain_attr_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes={
            "purpose": "triage",
            "result_status": "OK",
            "ModelPurpose": ModelPurpose.TRIAGE,
            "ToolResultStatus": ToolResultStatus.OK,
        },
    )
    assert plain_attr_payload["attributes"] == {}
    assert plain_attr_payload["attributes_withheld"] == 4

    enum_impostor_payload = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
        attributes={
            "purpose": ModelPurpose.TRIAGE,
            "result_status": ToolResultStatus.OK,
        },
    )
    assert enum_impostor_payload["attributes"] == {}
    assert enum_impostor_payload["attributes_withheld"] == 2


def test_a23_fallback_and_summary_non_reflection_across_all_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A23 (I-5/I-8): Every fallback trigger produces byte-identical constant output."""
    calls: list[str] = []
    hostile = _HostileValue(calls, "SYNTHETIC_A23_HOSTILE_SECRET")
    error = _settings_validation_error()

    fallback_triggers: list[dict[str, object]] = [
        # 1. Pattern-valid secret error_code
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code="hunter2024",  # type: ignore[arg-type]
        ),
        # 2. Hostile objects in event / error_code / validation_error / attributes
        build_diagnostic_event(event=hostile),  # type: ignore[arg-type]
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=hostile,  # type: ignore[arg-type]
        ),
        build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            validation_error=hostile,  # type: ignore[arg-type]
        ),
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes=hostile,  # type: ignore[arg-type]
        ),
        # 3. Oversized attributes (> 64 entries) and 17th valid attribute
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes={f"hunter2024_{i}": "hunter2024" for i in range(65)},
        ),
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            attributes={
                **dict.fromkeys(sorted(_EXPECTED_INT_KEYS), 1),
                "output_valid": True,
            },
        ),
        # 4. Disallowed cross-event arguments
        build_diagnostic_event(
            event=DiagnosticEvent.VALIDATION_FAILED,
            validation_error=error,
            error_code=DiagnosticErrorCode.TIMEOUT,
        ),
        build_diagnostic_event(
            event=DiagnosticEvent.OPERATION_FAILED,
            error_code=DiagnosticErrorCode.TIMEOUT,
            validation_error=error,
        ),
    ]

    expected_json = json.dumps(_EXPECTED_FALLBACK, sort_keys=True)
    for result in fallback_triggers:
        assert result == _EXPECTED_FALLBACK
        assert json.dumps(result, sort_keys=True) == expected_json
        assert summarize_diagnostic_event(result) == "Diagnostic unavailable."

    # 5. Tampered payload to summarize_diagnostic_event with secret planted in EVERY field
    base_op = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
    )
    for secret in _REGEX_MATCHING_SECRETS:
        for field in _EXPECTED_PAYLOAD_KEYS:
            tampered = dict(base_op)
            if field == "attributes":
                tampered[field] = {"purpose": secret, "unknown": secret}
            elif field == "validation":
                tampered[field] = {"code": secret}
            else:
                tampered[field] = secret
            summary = summarize_diagnostic_event(tampered)
            assert summary == "Diagnostic unavailable."
            assert secret not in summary

    # 6. Byte ceiling fallback non-reflection
    monkeypatch.setattr(boundary, "MAX_DIAGNOSTIC_BYTES", 64)
    byte_fallback = build_diagnostic_event(
        event=DiagnosticEvent.OPERATION_FAILED,
        error_code=DiagnosticErrorCode.TIMEOUT,
    )
    assert byte_fallback == _EXPECTED_FALLBACK
    assert json.dumps(byte_fallback, sort_keys=True) == expected_json
    assert calls == []
