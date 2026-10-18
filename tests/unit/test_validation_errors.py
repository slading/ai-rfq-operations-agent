"""Security tests for the aggregate-only safe ValidationError renderer."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from rfq_agent.config import Settings
from rfq_agent.observability.validation_errors import (
    render_validation_error,
    summarize_validation_error,
)

_API_KEY = "gsk_SYNTHETIC_A1b2C3d4E5f6G7h8"
_OPERATOR_TOKEN = "op_tok_SYNTHETIC_87654321"  # noqa: S105 - synthetic
_DATABASE_PASSWORD = "SYNTHETIC_DB_PASS_0123456789"  # noqa: S105 - synthetic
_DATABASE_URL = f"postgresql://rfquser:{_DATABASE_PASSWORD}@db.synthetic:5432/rfq"
_SHORT_SECRET = "sk_X7aB9"  # noqa: S105 - synthetic
_LONG_SECRET = "ghp_" + "A1b2C3d4E5f6" * 16


def _all_fragments_absent(rendered: str, secret: str, *, min_length: int = 8) -> None:
    """Assert that no contiguous substring of a synthetic secret was emitted."""
    for length in range(min_length, len(secret) + 1):
        for start in range(len(secret) - length + 1):
            assert secret[start : start + length] not in rendered


def _settings_validation_error() -> ValidationError:
    """Build nested configuration errors whose inputs carry synthetic secrets."""
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


def test_nested_configuration_errors_render_as_bounded_json() -> None:
    error = _settings_validation_error()

    diagnostic = render_validation_error(error)

    assert diagnostic == {
        "schema": "rfq.validation-diagnostic/v1",
        "status": "SAFE",
        "code": "VALIDATION_ERROR",
        "error_count": 5,
        "error_count_truncated": False,
        "locations": None,
        "locations_status": "WITHHELD",
    }
    # Every value is one of the fixed scalar types accepted by JSON.
    assert json.loads(json.dumps(diagnostic, sort_keys=True)) == diagnostic


def test_api_operator_and_database_secrets_never_reach_either_renderer() -> None:
    error = _settings_validation_error()

    structured = json.dumps(render_validation_error(error), sort_keys=True)
    summary = summarize_validation_error(error)
    rendered = f"{structured}\n{summary}"

    for secret in (
        _API_KEY,
        _OPERATOR_TOKEN,
        _DATABASE_PASSWORD,
        _DATABASE_URL,
        _LONG_SECRET,
    ):
        _all_fragments_absent(rendered, secret)


def test_short_and_partially_truncated_secrets_do_not_survive() -> None:
    for secret in (_SHORT_SECRET, _LONG_SECRET):
        error = ValidationError.from_exception_data(
            "SyntheticInput",
            [
                {
                    "type": "int_parsing",
                    "loc": ("field",),
                    "input": secret,
                }
            ],
        )
        rendered = json.dumps(render_validation_error(error)) + summarize_validation_error(error)
        assert secret not in rendered
        _all_fragments_absent(rendered, secret)


def test_renderer_does_not_format_untrusted_exception_context() -> None:
    calls: list[str] = []

    class HostileValueError(ValueError):
        def __str__(self) -> str:
            calls.append("str")
            return "SYNTHETIC_HOSTILE_CONTEXT_SECRET"

        def __repr__(self) -> str:
            calls.append("repr")
            return "SYNTHETIC_HOSTILE_CONTEXT_SECRET"

    class ModelWithHostileError(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _reject_value(cls, _value: str) -> str:
            raise HostileValueError

    with pytest.raises(ValidationError) as raised:
        ModelWithHostileError(value="synthetic")

    # Pydantic may format a ValueError while constructing the ValidationError.
    # Clear that construction-time observation and verify the renderer itself
    # does not trigger a second conversion while obtaining diagnostics.
    calls.clear()
    diagnostic = render_validation_error(raised.value)
    summary = summarize_validation_error(raised.value)

    assert diagnostic["error_count"] == 1
    assert summary.startswith("Validation failed: 1 error;")
    assert "SYNTHETIC_HOSTILE_CONTEXT_SECRET" not in json.dumps(diagnostic) + summary
    assert calls == []


def test_unknown_custom_error_and_malicious_location_are_withheld() -> None:
    secret_location = "SYNTHETIC_API_KEY_LOCATION_SECRET"  # noqa: S105 - synthetic
    secret_message = "SYNTHETIC_CUSTOM_ERROR_MESSAGE_SECRET"  # noqa: S105 - synthetic
    error = ValidationError.from_exception_data(
        "SyntheticUnknownError",
        [
            {
                "type": PydanticCustomError(
                    "future_secret_bearing_error_type",
                    f"invalid value {secret_message}",
                    {"credential": secret_message},
                ),
                "loc": ("nested", secret_location, "field"),
                "input": secret_message,
            }
        ],
    )

    diagnostic = render_validation_error(error)
    summary = summarize_validation_error(error)
    rendered = json.dumps(diagnostic, sort_keys=True) + summary

    _all_fragments_absent(rendered, secret_location)
    _all_fragments_absent(rendered, secret_message)
    assert diagnostic["code"] == "VALIDATION_ERROR"
    assert diagnostic["locations"] is None
    assert diagnostic["locations_status"] == "WITHHELD"


def test_malformed_or_non_validation_objects_fail_closed_without_repr() -> None:
    calls: list[str] = []

    class HostileObject:
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("must not be stringified")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("must not be represented")

        def errors(self) -> None:
            calls.append("errors")
            raise AssertionError("must not extract raw errors")

        def json(self) -> None:
            calls.append("json")
            raise AssertionError("must not serialize raw errors")

    invalid = HostileObject()
    diagnostic = render_validation_error(invalid)  # type: ignore[arg-type]
    summary = summarize_validation_error(invalid)  # type: ignore[arg-type]

    assert diagnostic["status"] == "UNAVAILABLE"
    assert diagnostic["error_count"] is None
    assert summary == "Validation failed; safe diagnostic unavailable."
    assert calls == []


def test_oversized_input_and_deep_location_are_not_traversed_or_returned() -> None:
    secret = "SYNTHETIC_OVERSIZED_INPUT_SECRET_0123456789"  # noqa: S105 - synthetic
    oversized_input = secret * 25_000
    deep_location = tuple(f"untrusted_{index}" for index in range(128))
    deeply_nested_input: object = secret
    for _ in range(128):
        deeply_nested_input = {"nested": deeply_nested_input}
    error = ValidationError.from_exception_data(
        "SyntheticLargeInput",
        [
            {
                "type": "int_parsing",
                "loc": deep_location,
                "input": {"large": oversized_input, "nested": deeply_nested_input},
            }
        ],
    )

    diagnostic = render_validation_error(error)
    summary = summarize_validation_error(error)
    rendered = json.dumps(diagnostic, sort_keys=True) + summary

    _all_fragments_absent(rendered, secret)
    assert diagnostic["locations"] is None
    assert len(rendered) < 512


def test_error_count_and_summary_are_capped() -> None:
    class ManyInvalidItems(BaseModel):
        values: list[int]

    with pytest.raises(ValidationError) as raised:
        ManyInvalidItems(values=["SYNTHETIC_INVALID_ITEM"] * 80)

    diagnostic = render_validation_error(raised.value)
    summary = summarize_validation_error(raised.value)

    assert diagnostic["error_count"] == 32
    assert diagnostic["error_count_truncated"] is True
    assert len(summary) < 128
    assert "at least 32 errors" in summary
    assert "SYNTHETIC_INVALID_ITEM" not in json.dumps(diagnostic) + summary


def test_repeated_rendering_is_deterministic_and_human_summary_is_distinct() -> None:
    error = _settings_validation_error()

    first = render_validation_error(error)
    second = render_validation_error(error)
    first_summary = summarize_validation_error(error)
    second_summary = summarize_validation_error(error)

    assert first == second
    assert first_summary == second_summary
    assert isinstance(first, dict)
    assert isinstance(first_summary, str)
    assert first_summary != json.dumps(first, sort_keys=True)
