"""Safe observability boundary for structured diagnostics and summaries.

Every future sink, trace writer, and operator diagnostic must pass through this
module. It introduces no sink, performs no I/O, and never calls representation
methods (``str()``, ``repr()``, ``format()``, ``.errors()``, ``.json()``, or
``model_dump()``) on caller-supplied objects.

Caller-influenced strings are admitted exclusively from closed, boundary-owned
enum vocabularies checked by exact type and member identity. Validation failures
enter solely through the F3-A2 aggregate renderer. Any structural, type, depth,
count, size, or hostile-object violation fails closed to a constant payload.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import ValidationError

from rfq_agent.observability.ids import TraceContext
from rfq_agent.observability.validation_errors import render_validation_error

__all__ = [
    "DIAGNOSTIC_SCHEMA",
    "MAX_DIAGNOSTIC_BYTES",
    "DiagnosticErrorCode",
    "DiagnosticEvent",
    "DiagnosticPurpose",
    "DiagnosticResultStatus",
    "build_diagnostic_event",
    "summarize_diagnostic_event",
]

DIAGNOSTIC_SCHEMA = "rfq.diagnostic/v1"
MAX_DIAGNOSTIC_BYTES = 1024

_MAX_ATTRIBUTES = 16
_MAX_ATTRIBUTE_SCAN = 64
_MAX_INT_VALUE = 2**31 - 1
_MAX_VALIDATION_ERROR_COUNT = 32

_VALIDATION_DIAGNOSTIC_SCHEMA = "rfq.validation-diagnostic/v1"
_VALIDATION_ERROR_CODE = "VALIDATION_ERROR"
_UNAVAILABLE_SUMMARY = "Diagnostic unavailable."

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{2,63}$")


class DiagnosticEvent(StrEnum):
    """Closed event vocabulary — the ONLY events a sink may emit in v1."""

    VALIDATION_FAILED = "validation_failed"
    OPERATION_FAILED = "operation_failed"
    DIAGNOSTIC_UNAVAILABLE = "diagnostic_unavailable"


class DiagnosticErrorCode(StrEnum):
    """Closed failure-code vocabulary for OPERATION_FAILED (v1, exhaustive)."""

    WORKFLOW_ERROR = "WORKFLOW_ERROR"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    RUN_BUDGET_EXCEEDED = "RUN_BUDGET_EXCEEDED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TOOL_FAILURE = "TOOL_FAILURE"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    UNKNOWN = "UNKNOWN"


class DiagnosticPurpose(StrEnum):
    """Closed purpose vocabulary (values mirror contracts.llm.ModelPurpose)."""

    TRIAGE = "triage"
    EXTRACT = "extract"
    RESOLVE = "resolve"
    REPAIR = "repair"
    CLASSIFY_TRUST = "classify_trust"


class DiagnosticResultStatus(StrEnum):
    """Closed result vocabulary (values mirror observability.spans.ToolResultStatus)."""

    OK = "OK"
    ERROR = "ERROR"
    DENIED = "DENIED"
    TRUNCATED = "TRUNCATED"


_EVENT_TABLE: tuple[tuple[DiagnosticEvent, str], ...] = (
    (DiagnosticEvent.VALIDATION_FAILED, "validation_failed"),
    (DiagnosticEvent.OPERATION_FAILED, "operation_failed"),
    (DiagnosticEvent.DIAGNOSTIC_UNAVAILABLE, "diagnostic_unavailable"),
)

_ERROR_CODE_TABLE: tuple[tuple[DiagnosticErrorCode, str], ...] = (
    (DiagnosticErrorCode.WORKFLOW_ERROR, "WORKFLOW_ERROR"),
    (DiagnosticErrorCode.ILLEGAL_TRANSITION, "ILLEGAL_TRANSITION"),
    (DiagnosticErrorCode.WORKFLOW_TIMEOUT, "WORKFLOW_TIMEOUT"),
    (DiagnosticErrorCode.STEP_BUDGET_EXCEEDED, "STEP_BUDGET_EXCEEDED"),
    (DiagnosticErrorCode.RUN_BUDGET_EXCEEDED, "RUN_BUDGET_EXCEEDED"),
    (DiagnosticErrorCode.ATTEMPTS_EXHAUSTED, "ATTEMPTS_EXHAUSTED"),
    (DiagnosticErrorCode.PROVIDER_ERROR, "PROVIDER_ERROR"),
    (DiagnosticErrorCode.RATE_LIMITED, "RATE_LIMITED"),
    (DiagnosticErrorCode.TIMEOUT, "TIMEOUT"),
    (DiagnosticErrorCode.TOOL_FAILURE, "TOOL_FAILURE"),
    (DiagnosticErrorCode.TOOL_DENIED, "TOOL_DENIED"),
    (DiagnosticErrorCode.TOOL_NOT_FOUND, "TOOL_NOT_FOUND"),
    (DiagnosticErrorCode.TOOL_TIMEOUT, "TOOL_TIMEOUT"),
    (DiagnosticErrorCode.MALFORMED_MODEL_OUTPUT, "MALFORMED_MODEL_OUTPUT"),
    (DiagnosticErrorCode.PROVIDER_NOT_CONFIGURED, "PROVIDER_NOT_CONFIGURED"),
    (DiagnosticErrorCode.UNKNOWN, "UNKNOWN"),
)

_PURPOSE_TABLE: tuple[tuple[DiagnosticPurpose, str], ...] = (
    (DiagnosticPurpose.TRIAGE, "triage"),
    (DiagnosticPurpose.EXTRACT, "extract"),
    (DiagnosticPurpose.RESOLVE, "resolve"),
    (DiagnosticPurpose.REPAIR, "repair"),
    (DiagnosticPurpose.CLASSIFY_TRUST, "classify_trust"),
)

_RESULT_STATUS_TABLE: tuple[tuple[DiagnosticResultStatus, str], ...] = (
    (DiagnosticResultStatus.OK, "OK"),
    (DiagnosticResultStatus.ERROR, "ERROR"),
    (DiagnosticResultStatus.DENIED, "DENIED"),
    (DiagnosticResultStatus.TRUNCATED, "TRUNCATED"),
)

_VALID_ERROR_CODE_STRINGS: frozenset[str] = frozenset(code for _, code in _ERROR_CODE_TABLE)
_VALID_PURPOSE_STRINGS: frozenset[str] = frozenset(purpose for _, purpose in _PURPOSE_TABLE)
_VALID_RESULT_STATUS_STRINGS: frozenset[str] = frozenset(
    status for _, status in _RESULT_STATUS_TABLE
)

_INT_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
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

_BOOL_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "output_valid",
        "truncated",
        "duplicate",
        "eligible_for_human_review",
        "requires_human_approval",
    }
)

_ENUM_ATTRIBUTE_KEYS: frozenset[str] = frozenset({"purpose", "result_status"})

_ALL_ALLOWED_ATTRIBUTE_KEYS: frozenset[str] = (
    _ENUM_ATTRIBUTE_KEYS | _INT_ATTRIBUTE_KEYS | _BOOL_ATTRIBUTE_KEYS
)

_CANONICAL_ATTRIBUTE_KEYS: dict[str, str] = {key: key for key in _ALL_ALLOWED_ATTRIBUTE_KEYS}

_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

_VALIDATION_DIAGNOSTIC_KEYS: frozenset[str] = frozenset(
    {
        "schema",
        "status",
        "code",
        "error_count",
        "error_count_truncated",
        "locations",
        "locations_status",
    }
)

_SUMMARY_BY_EVENT_AND_CODE: dict[tuple[str, str], str] = {
    ("validation_failed", "VALIDATION_ERROR"): (
        "Diagnostic event: validation_failed (VALIDATION_ERROR)."
    ),
    ("operation_failed", "WORKFLOW_ERROR"): (
        "Diagnostic event: operation_failed (WORKFLOW_ERROR)."
    ),
    ("operation_failed", "ILLEGAL_TRANSITION"): (
        "Diagnostic event: operation_failed (ILLEGAL_TRANSITION)."
    ),
    ("operation_failed", "WORKFLOW_TIMEOUT"): (
        "Diagnostic event: operation_failed (WORKFLOW_TIMEOUT)."
    ),
    ("operation_failed", "STEP_BUDGET_EXCEEDED"): (
        "Diagnostic event: operation_failed (STEP_BUDGET_EXCEEDED)."
    ),
    ("operation_failed", "RUN_BUDGET_EXCEEDED"): (
        "Diagnostic event: operation_failed (RUN_BUDGET_EXCEEDED)."
    ),
    ("operation_failed", "ATTEMPTS_EXHAUSTED"): (
        "Diagnostic event: operation_failed (ATTEMPTS_EXHAUSTED)."
    ),
    ("operation_failed", "PROVIDER_ERROR"): (
        "Diagnostic event: operation_failed (PROVIDER_ERROR)."
    ),
    ("operation_failed", "RATE_LIMITED"): ("Diagnostic event: operation_failed (RATE_LIMITED)."),
    ("operation_failed", "TIMEOUT"): ("Diagnostic event: operation_failed (TIMEOUT)."),
    ("operation_failed", "TOOL_FAILURE"): ("Diagnostic event: operation_failed (TOOL_FAILURE)."),
    ("operation_failed", "TOOL_DENIED"): ("Diagnostic event: operation_failed (TOOL_DENIED)."),
    ("operation_failed", "TOOL_NOT_FOUND"): (
        "Diagnostic event: operation_failed (TOOL_NOT_FOUND)."
    ),
    ("operation_failed", "TOOL_TIMEOUT"): ("Diagnostic event: operation_failed (TOOL_TIMEOUT)."),
    ("operation_failed", "MALFORMED_MODEL_OUTPUT"): (
        "Diagnostic event: operation_failed (MALFORMED_MODEL_OUTPUT)."
    ),
    ("operation_failed", "PROVIDER_NOT_CONFIGURED"): (
        "Diagnostic event: operation_failed (PROVIDER_NOT_CONFIGURED)."
    ),
    ("operation_failed", "UNKNOWN"): ("Diagnostic event: operation_failed (UNKNOWN)."),
}


def _unavailable_event() -> dict[str, str | int | bool | None]:
    """Return a fresh copy of the deterministic fail-closed fallback payload."""
    return {
        "schema": DIAGNOSTIC_SCHEMA,
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


def _match_enum_identity(
    candidate: object,
    enum_type: type[StrEnum],
    table: tuple[tuple[StrEnum, str], ...],
) -> str | None:
    """Resolve an enum member to its canonical literal by exact type and identity."""
    if type(candidate) is not enum_type:
        return None
    for member, canonical in table:
        if candidate is member:
            return canonical
    return None


def _has_exact_str_keys(mapping: dict[object, object], expected_keys: frozenset[str]) -> bool:
    """Verify that a plain dict contains only plain str keys equal to expected_keys."""
    if len(mapping) != len(expected_keys):
        return False
    return all(type(key) is str and key in expected_keys for key in mapping)


def _is_valid_trace_values(
    trace_id: object,
    rfq_id: object,
    run_id: object,
    span_id: object,
    *,
    require_trace_id: bool,
) -> bool:
    """Validate correlation IDs against their syntactic domain patterns."""
    if trace_id is None:
        if require_trace_id:
            return False
        return rfq_id is None and run_id is None and span_id is None
    if type(trace_id) is not str or _TRACE_ID_RE.fullmatch(trace_id) is None:
        return False
    if rfq_id is not None and (type(rfq_id) is not str or _IDENTIFIER_RE.fullmatch(rfq_id) is None):
        return False
    if run_id is not None and (type(run_id) is not str or _IDENTIFIER_RE.fullmatch(run_id) is None):
        return False
    return span_id is None or (type(span_id) is str and _SPAN_ID_RE.fullmatch(span_id) is not None)


def _extract_trace(
    trace: TraceContext | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Read correlation IDs from a trusted TraceContext, degrading to None on any fault."""
    if trace is None or type(trace) is not TraceContext:
        return (None, None, None, None)
    try:
        trace_id = trace.trace_id
        rfq_id = trace.rfq_id
        run_id = trace.run_id
        span_id = trace.span_id
        parent_span_id = trace.parent_span_id
    except Exception:  # noqa: BLE001 - fail closed on hostile or uninitialized stand-in
        return (None, None, None, None)

    if parent_span_id is not None and (
        type(parent_span_id) is not str or _SPAN_ID_RE.fullmatch(parent_span_id) is None
    ):
        return (None, None, None, None)
    if not _is_valid_trace_values(trace_id, rfq_id, run_id, span_id, require_trace_id=True):
        return (None, None, None, None)
    return (trace_id, rfq_id, run_id, span_id)


def _is_valid_validation_diagnostic(validation: object) -> bool:
    """Verify that a validation diagnostic dict matches the exact F3-A2 SAFE contract."""
    if type(validation) is not dict:
        return False
    if not _has_exact_str_keys(validation, _VALIDATION_DIAGNOSTIC_KEYS):
        return False
    schema = validation["schema"]
    status = validation["status"]
    code = validation["code"]
    locations_status = validation["locations_status"]
    if (
        type(schema) is not str
        or schema != _VALIDATION_DIAGNOSTIC_SCHEMA
        or type(status) is not str
        or status != "SAFE"
        or type(code) is not str
        or code != _VALIDATION_ERROR_CODE
        or validation["locations"] is not None
        or type(locations_status) is not str
        or locations_status != "WITHHELD"
    ):
        return False
    error_count = validation["error_count"]
    truncated = validation["error_count_truncated"]
    if (
        type(error_count) is not int
        or error_count <= 0
        or error_count > _MAX_VALIDATION_ERROR_COUNT
        or type(truncated) is not bool
    ):
        return False
    return not (truncated and error_count != _MAX_VALIDATION_ERROR_COUNT)


def _admit_single_attribute(key: object, value: object) -> tuple[bool, str, str | int | bool]:
    """Check whether one attribute key/value pair is admissible on OPERATION_FAILED."""
    if type(key) is not str:
        return (False, "", False)
    canonical_key = _CANONICAL_ATTRIBUTE_KEYS.get(key)
    if canonical_key is None:
        return (False, "", False)
    if canonical_key == "purpose":
        purpose_val = _match_enum_identity(value, DiagnosticPurpose, _PURPOSE_TABLE)
        return (purpose_val is not None, canonical_key, purpose_val or "")
    if canonical_key == "result_status":
        status_val = _match_enum_identity(value, DiagnosticResultStatus, _RESULT_STATUS_TABLE)
        return (status_val is not None, canonical_key, status_val or "")
    if canonical_key in _INT_ATTRIBUTE_KEYS:
        if type(value) is not int or value < 0 or value > _MAX_INT_VALUE:
            return (False, "", False)
        return (True, canonical_key, int(value))
    if type(value) is not bool:
        return (False, "", False)
    return (True, canonical_key, bool(value))


def _scan_attribute_items(
    attributes: Mapping[object, object],
    raw_len: int,
    *,
    allow_admission: bool,
) -> tuple[bool, dict[str, str | int | bool], int]:
    """Iterate at most _MAX_ATTRIBUTE_SCAN entries and enforce length honesty."""
    admitted: dict[str, str | int | bool] = {}
    withheld = 0
    seen = 0
    for key, value in attributes.items():
        seen += 1
        if seen > raw_len or seen > _MAX_ATTRIBUTE_SCAN:
            return (False, {}, 0)
        if not allow_admission:
            withheld += 1
            continue
        ok, canonical_key, canonical_val = _admit_single_attribute(key, value)
        if not ok:
            withheld += 1
            continue
        if canonical_key in admitted or len(admitted) >= _MAX_ATTRIBUTES:
            return (False, {}, 0)
        admitted[canonical_key] = canonical_val

    if seen != raw_len:
        return (False, {}, 0)
    return (True, admitted, min(withheld, _MAX_ATTRIBUTES))


def _process_attributes(
    attributes: object,
    *,
    allow_admission: bool,
) -> tuple[bool, dict[str, str | int | bool], int]:
    """Validate and filter caller attributes within scan and count ceilings."""
    if attributes is None:
        return (True, {}, 0)
    try:
        if not issubclass(type(attributes), Mapping):
            return (False, {}, 0)
        raw_len = len(attributes)
        if type(raw_len) is not int or raw_len < 0 or raw_len > _MAX_ATTRIBUTE_SCAN:
            return (False, {}, 0)
        return _scan_attribute_items(attributes, raw_len, allow_admission=allow_admission)
    except Exception:  # noqa: BLE001 - hostile Mapping fails closed
        return (False, {}, 0)


def _verify_serialized_bounds(payload: Mapping[str, object]) -> bool:
    """Verify strict JSON serialisability without default= and the byte ceiling."""
    if type(MAX_DIAGNOSTIC_BYTES) is not int or MAX_DIAGNOSTIC_BYTES <= 0:
        return False
    try:
        serialized = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True)
        encoded = serialized.encode("utf-8")
    except Exception:  # noqa: BLE001 - unserialisable structure fails closed
        return False
    return len(encoded) <= MAX_DIAGNOSTIC_BYTES


def _build_validation_failed(
    validation_error: ValidationError | None,
    error_code: DiagnosticErrorCode | None,
    attributes: Mapping[str, object] | None,
) -> tuple[bool, str, dict[str, str | int | bool | None] | None, dict[str, str | int | bool], int]:
    """Construct the event-specific fields for VALIDATION_FAILED."""
    if error_code is not None or validation_error is None:
        return (False, "", None, {}, 0)
    attrs_ok, admitted_attrs, withheld_count = _process_attributes(
        attributes, allow_admission=False
    )
    if not attrs_ok:
        return (False, "", None, {}, 0)
    rendered = render_validation_error(validation_error)
    if not _is_valid_validation_diagnostic(rendered):
        return (False, "", None, {}, 0)
    clean_validation: dict[str, str | int | bool | None] = {
        "schema": _VALIDATION_DIAGNOSTIC_SCHEMA,
        "status": "SAFE",
        "code": _VALIDATION_ERROR_CODE,
        "error_count": int(rendered["error_count"]),  # type: ignore[arg-type]
        "error_count_truncated": bool(rendered["error_count_truncated"]),
        "locations": None,
        "locations_status": "WITHHELD",
    }
    return (True, _VALIDATION_ERROR_CODE, clean_validation, admitted_attrs, withheld_count)


def _build_operation_failed(
    validation_error: ValidationError | None,
    error_code: DiagnosticErrorCode | None,
    attributes: Mapping[str, object] | None,
) -> tuple[bool, str, dict[str, str | int | bool | None] | None, dict[str, str | int | bool], int]:
    """Construct the event-specific fields for OPERATION_FAILED."""
    if validation_error is not None:
        return (False, "", None, {}, 0)
    matched_code = _match_enum_identity(error_code, DiagnosticErrorCode, _ERROR_CODE_TABLE)
    if matched_code is None:
        return (False, "", None, {}, 0)
    attrs_ok, admitted_attrs, withheld_count = _process_attributes(attributes, allow_admission=True)
    if not attrs_ok:
        return (False, "", None, {}, 0)
    return (True, matched_code, None, admitted_attrs, withheld_count)


def build_diagnostic_event(
    *,
    event: DiagnosticEvent,
    trace: TraceContext | None = None,
    validation_error: ValidationError | None = None,
    error_code: DiagnosticErrorCode | None = None,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, str | int | bool | None]:
    """Build a bounded, closed-vocabulary diagnostic event dictionary.

    Args:
        event: Closed event member from :class:`DiagnosticEvent`.
        trace: Optional validated :class:`TraceContext` for correlation IDs.
        validation_error: Optional Pydantic :class:`ValidationError` (for
            ``VALIDATION_FAILED`` only, rendered via F3-A2).
        error_code: Optional :class:`DiagnosticErrorCode` member (required for
            ``OPERATION_FAILED``, forbidden otherwise).
        attributes: Optional mapping of telemetry attributes (admitted only on
            ``OPERATION_FAILED``; counted as withheld on ``VALIDATION_FAILED``).

    Returns:
        A fresh JSON-serialisable dictionary conforming to ``rfq.diagnostic/v1``.
    """
    try:
        canonical_event = _match_enum_identity(event, DiagnosticEvent, _EVENT_TABLE)
        if canonical_event == "validation_failed":
            ok, canonical_code, clean_validation, admitted_attrs, withheld_count = (
                _build_validation_failed(validation_error, error_code, attributes)
            )
        elif canonical_event == "operation_failed":
            ok, canonical_code, clean_validation, admitted_attrs, withheld_count = (
                _build_operation_failed(validation_error, error_code, attributes)
            )
        else:
            return _unavailable_event()

        if not ok:
            return _unavailable_event()

        trace_id, rfq_id, run_id, span_id = _extract_trace(trace)
        payload: dict[str, str | int | bool | None] = {
            "schema": DIAGNOSTIC_SCHEMA,
            "event": canonical_event,
            "status": "SAFE",
            "error_code": canonical_code,
            "trace_id": trace_id,
            "rfq_id": rfq_id,
            "run_id": run_id,
            "span_id": span_id,
            "validation": clean_validation,  # type: ignore[dict-item]
            "attributes": admitted_attrs,  # type: ignore[dict-item]
            "attributes_withheld": withheld_count,
        }
        if not _verify_serialized_bounds(payload):
            return _unavailable_event()
    except Exception:  # noqa: BLE001 - fail closed on any unexpected error
        return _unavailable_event()
    else:
        return payload


def _is_valid_admitted_entry(key: str, value: object) -> bool:
    """Verify one admitted attribute entry inside an event payload."""
    if key == "purpose":
        return type(value) is str and value in _VALID_PURPOSE_STRINGS
    if key == "result_status":
        return type(value) is str and value in _VALID_RESULT_STATUS_STRINGS
    if key in _INT_ATTRIBUTE_KEYS:
        return type(value) is int and 0 <= value <= _MAX_INT_VALUE
    if key in _BOOL_ATTRIBUTE_KEYS:
        return type(value) is bool
    return False


def _is_valid_admitted_attributes(attributes: object, *, allow_non_empty: bool) -> bool:
    """Validate the nested attributes dictionary inside a diagnostic event payload."""
    if type(attributes) is not dict:
        return False
    if not allow_non_empty:
        return len(attributes) == 0
    if len(attributes) > _MAX_ATTRIBUTES:
        return False
    if not all(type(key) is str and key in _ALL_ALLOWED_ATTRIBUTE_KEYS for key in attributes):
        return False
    return all(_is_valid_admitted_entry(key, value) for key, value in attributes.items())


def _is_valid_event_specific_fields(
    event_name: str,
    error_code: object,
    validation: object,
    attributes: object,
) -> bool:
    """Validate event-specific error_code, validation, and attributes fields."""
    if event_name == "validation_failed":
        return (
            type(error_code) is str
            and error_code == _VALIDATION_ERROR_CODE
            and _is_valid_validation_diagnostic(validation)
            and _is_valid_admitted_attributes(attributes, allow_non_empty=False)
        )
    if event_name == "operation_failed":
        return (
            type(error_code) is str
            and error_code in _VALID_ERROR_CODE_STRINGS
            and validation is None
            and _is_valid_admitted_attributes(attributes, allow_non_empty=True)
        )
    return False


def _validate_event_payload(event_payload: object) -> tuple[str, str] | None:
    """Validate the entire diagnostic payload structure before summarising."""
    if type(event_payload) is not dict or not _has_exact_str_keys(event_payload, _PAYLOAD_KEYS):
        return None

    schema = event_payload["schema"]
    event_name = event_payload["event"]
    status = event_payload["status"]
    withheld = event_payload["attributes_withheld"]
    if (
        type(schema) is not str
        or schema != DIAGNOSTIC_SCHEMA
        or type(event_name) is not str
        or type(status) is not str
        or status != "SAFE"
        or type(withheld) is not int
        or withheld < 0
        or withheld > _MAX_ATTRIBUTES
    ):
        return None

    if not _is_valid_trace_values(
        event_payload["trace_id"],
        event_payload["rfq_id"],
        event_payload["run_id"],
        event_payload["span_id"],
        require_trace_id=False,
    ):
        return None

    error_code = event_payload["error_code"]
    if not _is_valid_event_specific_fields(
        event_name,
        error_code,
        event_payload["validation"],
        event_payload["attributes"],
    ):
        return None

    if not _verify_serialized_bounds(event_payload):
        return None

    return (event_name, error_code)  # type: ignore[return-value]


def summarize_diagnostic_event(event_payload: Mapping[str, object]) -> str:
    """Return a fixed human-readable summary for a validated diagnostic payload.

    Re-validates the entire structure of ``event_payload`` — including top-level
    keys, schema, event, status, error code, trace IDs, nested validation
    diagnostics, nested attributes, withheld count, and serialised byte length.
    Any foreign, malformed, or tampered payload returns ``"Diagnostic unavailable."``.
    """
    try:
        validated = _validate_event_payload(event_payload)
        if validated is None:
            return _UNAVAILABLE_SUMMARY
        return _SUMMARY_BY_EVENT_AND_CODE.get(validated, _UNAVAILABLE_SUMMARY)
    except Exception:  # noqa: BLE001 - fail closed on any unexpected error
        return _UNAVAILABLE_SUMMARY
