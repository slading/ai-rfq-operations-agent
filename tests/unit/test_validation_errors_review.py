"""Independent adversarial regressions for safe validation diagnostics."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rfq_agent.observability.validation_errors import (
    render_validation_error,
    summarize_validation_error,
)


@pytest.mark.parametrize("behavior", ["raise", "spoof"])
def test_non_validation_class_hook_is_never_read(behavior: str) -> None:
    calls: list[str] = []

    class Hostile:
        @property
        def __class__(self):
            calls.append("class")
            if behavior == "raise":
                raise RuntimeError("SYNTHETIC_CLASS_SECRET")
            return ValidationError

    invalid = Hostile()
    diagnostic = render_validation_error(invalid)  # type: ignore[arg-type]
    summary = summarize_validation_error(invalid)  # type: ignore[arg-type]

    assert calls == []
    assert diagnostic == {
        "schema": "rfq.validation-diagnostic/v1",
        "status": "UNAVAILABLE",
        "code": "VALIDATION_ERROR",
        "error_count": None,
        "error_count_truncated": False,
        "locations": None,
        "locations_status": "WITHHELD",
    }
    assert summary == "Validation failed; safe diagnostic unavailable."


def test_native_descriptor_bypasses_validation_subclass_hooks() -> None:
    calls: list[str] = []

    class GuardedValidationError(ValidationError):
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError("must not access instance attributes")

        def __str__(self):
            calls.append("str")
            raise AssertionError("must not stringify")

        def __repr__(self):
            calls.append("repr")
            raise AssertionError("must not represent")

        def errors(self, *_args, **_kwargs):
            calls.append("errors")
            raise AssertionError("must not extract messages")

        def json(self, *_args, **_kwargs):
            calls.append("json")
            raise AssertionError("must not serialize")

        def error_count(self):
            calls.append("count override")
            raise AssertionError("must use native descriptor")

    error = GuardedValidationError.from_exception_data(
        "SYNTHETIC_TITLE_SECRET",
        [{"type": "int_parsing", "loc": ("value",), "input": "SYNTHETIC_INPUT_SECRET"}],
    )
    calls.clear()

    for _ in range(3):
        assert render_validation_error(error)["error_count"] == 1
        assert summarize_validation_error(error) == (
            "Validation failed: 1 error; per-error details withheld."
        )
    assert calls == []


@pytest.mark.parametrize("count", [0, 1, 2, 31, 32, 33, 80, 1024])
def test_native_count_boundaries_have_fixed_bounded_outputs(count: int) -> None:
    error = ValidationError.from_exception_data(
        "SYNTHETIC_TITLE_SECRET",
        [{"type": "int_parsing", "loc": ("value",), "input": "synthetic"}] * count,
    )

    diagnostic = render_validation_error(error)
    summary = summarize_validation_error(error)

    assert diagnostic["error_count"] == (min(count, 32) if count else None)
    assert diagnostic["status"] == ("SAFE" if count else "UNAVAILABLE")
    assert diagnostic["error_count_truncated"] is (count > 32)
    assert diagnostic["code"] == "VALIDATION_ERROR"
    assert diagnostic["locations"] is None
    assert diagnostic["locations_status"] == "WITHHELD"
    assert json.loads(json.dumps(diagnostic)) == diagnostic
    assert len(json.dumps(diagnostic) + summary) < 512
    assert "SYNTHETIC_TITLE_SECRET" not in json.dumps(diagnostic) + summary
    if count > 32:
        assert summary == "Validation failed: at least 32 errors; per-error details withheld."
    elif count:
        noun = "error" if count == 1 else "errors"
        assert summary == f"Validation failed: {count} {noun}; per-error details withheld."
    else:
        assert summary == "Validation failed; safe diagnostic unavailable."


def test_unexpected_native_failure_never_formats_exception(monkeypatch) -> None:
    calls: list[str] = []

    class HostileFailureError(RuntimeError):
        def __str__(self):
            calls.append("str")
            raise AssertionError("must not stringify failure")

        def __repr__(self):
            calls.append("repr")
            raise AssertionError("must not represent failure")

    def fail(_error):
        raise HostileFailureError("SYNTHETIC_NATIVE_FAILURE_SECRET")

    error = ValidationError.from_exception_data("synthetic", [])
    monkeypatch.setattr(ValidationError, "error_count", fail)

    assert render_validation_error(error)["status"] == "UNAVAILABLE"
    assert summarize_validation_error(error) == "Validation failed; safe diagnostic unavailable."
    assert calls == []


@pytest.mark.parametrize("count", [None, True, False, -1, 0, 1.0, "SYNTHETIC_COUNT_SECRET"])
def test_malformed_native_count_fails_closed(monkeypatch, count: object) -> None:
    error = ValidationError.from_exception_data("synthetic", [])
    monkeypatch.setattr(ValidationError, "error_count", lambda _error: count)

    assert render_validation_error(error)["status"] == "UNAVAILABLE"
    assert summarize_validation_error(error) == "Validation failed; safe diagnostic unavailable."


@pytest.mark.parametrize("valid", [False, True])
def test_diagnostic_results_do_not_share_mutable_state(valid: bool) -> None:
    errors = [{"type": "int_parsing", "loc": ("value",), "input": "synthetic"}] if valid else []
    error = ValidationError.from_exception_data("synthetic", errors)
    baseline = render_validation_error(error)
    mutated = render_validation_error(error)
    mutated["code"] = "SYNTHETIC_CALLER_MUTATION"

    assert render_validation_error(error) == baseline
