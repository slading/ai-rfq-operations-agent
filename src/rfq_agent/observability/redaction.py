"""The single redaction choke point (architecture §10.4).

Every log line and every trace payload passes through :func:`redact_text` or
:func:`redact_mapping`. Concentrating the rules here is deliberate: scattering
``logger.info`` calls around and hoping each one is careful is how secrets end
up in a log aggregator. Phase 5 adds a test that plants a secret and an email
address in a real record and asserts neither survives.

What must never appear in a log or trace row:

* API keys or any ``Authorization`` header value;
* the operator token;
* unredacted customer email addresses (store the hash instead);
* full prompt bodies in production (store ``messages_sha256``);
* untrusted customer content beyond a short, explicitly requested snippet.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from rfq_agent.domain.values import Json, canonical_json, sha256_text

__all__ = [
    "REDACTED",
    "email_hash",
    "redact_mapping",
    "redact_text",
    "summarize_payload",
]

#: Replacement marker. Stable so tests can assert on it.
REDACTED = "[REDACTED]"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
#: ``Authorization: <value>``, ``api_key=<value>``, ``x-api-key: <value>``.
#: There is deliberately no ``\b`` after the keyword group: the value is what
#: follows, and requiring a word boundary there would stop the rule matching
#: ``Authorization: Bearer <token>`` at all.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|api[-_]?key|x-api-key|token)(\s*[:=]\s*)([^\r\n]+)"
)
#: ``Bearer <token>`` anywhere, including as the value of a redacted header.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-/+=]{8,}")
#: Assignment-style secrets: ``GROQ_API_KEY=...``, ``token: ...``, ``key = ...``.
#: Values are matched from 12 characters up so ordinary prose is not eaten.
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_\-]*(?:api[-_]?key|secret|token|password|passwd"
    r"|credential|key)[A-Za-z0-9_\-]*)(\s*[:=]\s*)([A-Za-z0-9._\-/+=]{12,})"
)
#: Fields whose values are always replaced wholesale.
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "operator_token",
        "groq_api_key",
        "sender_email",
        "customer_email",
        "email",
    }
)


def email_hash(address: str) -> str:
    """Return a stable hash for an email address, for correlation without PII."""
    return sha256_text(address.strip().lower())


def redact_text(text: str) -> str:
    """Remove secrets and email addresses from a free-form string."""
    redacted = _AUTH_HEADER_RE.sub(rf"\1\2{REDACTED}", text)
    redacted = _SECRET_ASSIGN_RE.sub(rf"\1\2{REDACTED}", redacted)
    redacted = _BEARER_RE.sub(f"Bearer {REDACTED}", redacted)
    return _EMAIL_RE.sub(REDACTED, redacted)


def redact_mapping(data: Mapping[str, Json]) -> dict[str, Json]:
    """Return a redacted copy of ``data``.

    Sensitive keys are replaced wholesale; everything else is scanned as text,
    so a secret pasted into an innocuous field is still caught.
    """
    result: dict[str, Json] = {}
    for key, value in data.items():
        if key.strip().lower() in _SENSITIVE_KEYS:
            result[key] = REDACTED
            continue
        result[key] = _redact_value(value)
    return result


def _redact_value(value: Json) -> Json:
    """Redact a single value, recursing into containers."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def summarize_payload(payload: Json, *, max_bytes: int, label: str = "payload") -> dict[str, Json]:
    """Summarise a payload for storage: inline when small, hashed when large.

    This is what keeps ``tool_calls`` and ``llm_calls`` auditable without
    turning the audit table into a copy of every prompt (§10.2). The hash is
    computed over the *unredacted* canonical JSON, so it stays a stable identity
    for the payload even though the stored copy is redacted.
    """
    serialized = canonical_json(payload)
    encoded = serialized.encode("utf-8")
    digest = sha256_text(serialized)
    # A mapping is redacted key-by-key so sensitive *values* are replaced
    # wholesale; anything else is redacted as free text.
    redacted: Json = (
        redact_mapping(payload) if isinstance(payload, Mapping) else redact_text(serialized)
    )
    if len(encoded) <= max_bytes:
        return {
            f"{label}_status": "INLINE",
            f"{label}_bytes": len(encoded),
            f"{label}_sha256": digest,
            label: redacted,
        }
    return {
        f"{label}_status": "TRUNCATED",
        f"{label}_bytes": len(encoded),
        f"{label}_sha256": digest,
        label: redact_text(serialized[:max_bytes]),
    }
