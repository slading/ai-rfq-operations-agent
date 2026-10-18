"""Safe aggregate rendering for Pydantic validation failures.

Pydantic's public ``ValidationError.errors()`` API formats each message even
when input, context and URLs are excluded. Formatting can invoke an arbitrary
exception object's ``__str__`` through custom error context. This module
therefore reads only the native, count-only ``error_count()`` API. Per-error
codes and locations are deliberately withheld until Pydantic offers a safe
message-free diagnostic interface.

Raw ``str(error)``, ``error.errors()`` and ``error.json()`` remain potentially
unsafe. This module does not make those APIs safe and has no logging side
 effects.
"""

from __future__ import annotations

from pydantic import ValidationError

__all__ = ["render_validation_error", "summarize_validation_error"]

_SCHEMA = "rfq.validation-diagnostic/v1"
_CODE = "VALIDATION_ERROR"
_MAX_REPORTED_ERRORS = 32


def _unavailable_diagnostic() -> dict[str, str | int | bool | None]:
    """Return the constant fail-closed shape for invalid/unreadable errors."""
    return {
        "schema": _SCHEMA,
        "status": "UNAVAILABLE",
        "code": _CODE,
        "error_count": None,
        "error_count_truncated": False,
        "locations": None,
        "locations_status": "WITHHELD",
    }


def render_validation_error(
    error: ValidationError,
) -> dict[str, str | int | bool | None]:
    """Return a bounded JSON-compatible aggregate diagnostic.

    Only Pydantic's native ``error_count()`` is read. The renderer never calls
    ``str(error)``, ``repr(error)``, ``errors()``, ``json()``, or any API that
    formats a Pydantic message. It does not inspect error locations, inputs,
    contexts, URLs, exception objects, or custom error types.

    The count is capped at 32. Locations are always marked withheld because the
    public Pydantic API does not expose locations without also formatting
    untrusted messages. Non-ValidationError objects, malformed counts, and
    unexpected failures produce the same generic unavailable result.
    """
    try:
        # Invoke the native descriptor directly: do not dispatch through an
        # instance attribute that could be overridden by an unusual subclass.
        # The descriptor also rejects non-ValidationError objects without
        # reading their potentially hostile __class__ attribute.
        count = ValidationError.error_count(error)
    except Exception:  # noqa: BLE001 - fail closed without exposing exception text
        return _unavailable_diagnostic()

    if type(count) is not int or count <= 0:
        return _unavailable_diagnostic()

    return {
        "schema": _SCHEMA,
        "status": "SAFE",
        "code": _CODE,
        "error_count": min(count, _MAX_REPORTED_ERRORS),
        "error_count_truncated": count > _MAX_REPORTED_ERRORS,
        "locations": None,
        "locations_status": "WITHHELD",
    }


def summarize_validation_error(error: ValidationError) -> str:
    """Return a safe human-readable summary distinct from the structured form.

    The summary is composed only from this module's fixed text and the bounded
    aggregate count. No Pydantic message or arbitrary exception content is
    copied into it.
    """
    diagnostic = render_validation_error(error)
    count = diagnostic["error_count"]
    if diagnostic["status"] != "SAFE" or type(count) is not int:
        return "Validation failed; safe diagnostic unavailable."
    if diagnostic["error_count_truncated"]:
        return f"Validation failed: at least {count} errors; per-error details withheld."
    noun = "error" if count == 1 else "errors"
    return f"Validation failed: {count} {noun}; per-error details withheld."
