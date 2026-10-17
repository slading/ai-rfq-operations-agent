"""Non-normative reference prototype for the revised F3-A design.

This file is an *evidence appendix*, not a patch and not authorized for merge:
it exists to prove the design in `/home/user/f3a-design-revision.md` is
implementable and to make the claims in that report reproducible. It lives
outside the repository, has no tests or docstrings to the project's standard,
and must not be copied into `src/` as-is.

Run it with the repository virtualenv and `PYTHONPATH=src:<directory>`.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel
from rfq_agent.domain.values import Json, canonical_json, sha256_bytes

REDACTED = "[REDACTED]"
REDACTED_KEY = "[REDACTED_KEY]"

_MAX_JSON_PARSE = 256 * 1024
_MAX_DEPTH = 32

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|api[-_]?key|x-api-key|token)(\s*[:=]\s*)([^\r\n]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-/+=]{8,}")
_URL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s]+)@(?=[A-Za-z0-9.\-]+(?::\d+)?(?:[/\s]|$))"
)
_KEY_SEP_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])"
    r"(?P<key>[A-Za-z0-9_.\-]{1,64})"
    r"(?P<sep>(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?)"
)
_VALUE_RE = re.compile(r"(?!\[REDACTED\])[^\s\"',;}\]\)\\<>&]{8,}")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

_SENSITIVE_KEY_EXACT = frozenset(
    """
    apikey apikeys apikeyid authorization authorizationcode auth authentication
    token tokens accesstoken refreshtoken sessiontoken secret secrets clientsecret
    password passwords passwd passphrase credential credentials operatortoken
    groqapikey senderemail customeremail email emailaddress useremail key
    privatekey secretkey signingkey encryptionkey accesskey accesskeyid
    secretaccesskey sessionid cookie setcookie jwt oauth bearer proxyauthorization
    """.split()
)
_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "privatekey",
)
_SAFE_SCALARS = (bool, int, float, Decimal, datetime, date, time, timedelta, UUID, Enum)


def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).casefold())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _strip_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub("\ufffd", text)


def _redact_url(match: re.Match[str]) -> str:
    scheme, userinfo = match.group(1), match.group(2)
    if ":" in userinfo:
        user, _, _password = userinfo.partition(":")
        return f"{scheme}{user}:{REDACTED}@"
    return f"{scheme}{REDACTED}@"


def _redact_key_values(text: str) -> str:
    out: list[str] = []
    pos = 0
    for match in _KEY_SEP_RE.finditer(text):
        if match.start() < pos or not _is_sensitive_key(match["key"]):
            continue
        value = _VALUE_RE.match(text, match.end())
        if value is None:
            continue
        out.append(text[pos : match.start()])
        out.append(match["key"] + match["sep"] + REDACTED)
        pos = value.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("redact_text() requires str")
    redacted = _strip_surrogates(text)
    redacted = _URL_RE.sub(_redact_url, redacted)
    redacted = _AUTH_HEADER_RE.sub(rf"\1\2{REDACTED}", redacted)
    redacted = _redact_key_values(redacted)
    redacted = _BEARER_RE.sub(f"Bearer {REDACTED}", redacted)
    return _EMAIL_RE.sub(REDACTED, redacted)


def _looks_structured(text: str) -> bool:
    """True when the text opens like JSON/Python container data."""
    stripped = text.strip()
    return len(stripped) >= 2 and stripped[0] in "{(["


def _decode_quoted(stripped: str, quote: str) -> object:
    try:
        if quote == '"':
            return json.loads(stripped)
        return ast.literal_eval(stripped)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _UNSET


def _parse_container(text: str) -> object:
    """Parse JSON first, then a Python literal. ``_UNSET`` when neither works."""
    if text[:1] in "{[":
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _UNSET


def _redact_string(text: str, depth: int) -> str:
    if depth > _MAX_DEPTH:
        return REDACTED
    if len(text) > _MAX_JSON_PARSE:
        # Over the structural parse cap: a quoted/escaped key or a nested value
        # can hide a secret from the linear text rules, so withhold the value.
        head = text.strip()[:1]
        if head in "{[(\"'":
            return REDACTED
        return redact_text(text)
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        decoded = _decode_quoted(stripped, stripped[0])
        if isinstance(decoded, str):
            nested = _redact_string(decoded, depth + 1)
            if nested != decoded:
                return canonical_json(nested)
        return redact_text(text)
    redacted = redact_text(text)
    if not _looks_structured(redacted):
        return redacted
    parsed = _parse_container(redacted.strip())
    if isinstance(parsed, (dict, list, tuple)):
        structural = _redact_value(parsed, depth + 1, set())
        if _safe_changed(structural, parsed):
            return repr(structural) if isinstance(parsed, tuple) else canonical_json(structural)
        return redacted
    # Structured-looking but unparseable: cannot prove a nested value is safe.
    return REDACTED


class _Unset:
    pass


_UNSET = _Unset()


def _safe_changed(left: object, right: object) -> bool:
    try:
        return bool(left != right)
    except Exception:  # noqa: BLE001 - comparison of arbitrary content must not raise
        return True


def _coerce_key(key: object) -> str:
    try:
        return key if isinstance(key, str) else str(key)
    except Exception:  # noqa: BLE001 - hostile __str__
        return REDACTED_KEY


def redact_mapping(data: Mapping[str, Json]) -> dict[str, Json]:
    return _redact_mapping(data, 0, set())


def _redact_mapping(data: Mapping[object, Json], depth: int, seen: set[int]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    counts: dict[str, int] = {}
    for key, value in data.items():
        raw = _coerce_key(key)
        if _is_sensitive_key(raw):
            redacted_value: Json = REDACTED
            renamed = redact_text(raw)
            final_key = raw if renamed == raw else REDACTED_KEY
        else:
            redacted_value = _redact_value(value, depth, seen)
            renamed = redact_text(raw)
            final_key = raw if renamed == raw else REDACTED_KEY
        count = counts.get(final_key, 0) + 1
        counts[final_key] = count
        if count > 1:
            final_key = f"{final_key}#{count}"
        result[final_key] = redacted_value
    return result


def _redact_value(value: Json, depth: int, seen: set[int]) -> Json:
    if depth > _MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, _SAFE_SCALARS):
        return value
    try:
        if isinstance(value, str):
            return _redact_string(value, depth)
        if isinstance(value, BaseModel):
            return _redact_value(value.model_dump(), depth + 1, seen)
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            ident = id(value)
            if ident in seen:
                return REDACTED
            seen.add(ident)
            try:
                if isinstance(value, Mapping):
                    return _redact_mapping(value, depth + 1, seen)
                if isinstance(value, (set, frozenset)):
                    return REDACTED
                if (
                    len(value) == 2
                    and isinstance(value[0], str)
                    and _is_sensitive_key(value[0])
                ):
                    return [_redact_string(value[0], depth + 1), REDACTED]
                return [_redact_value(item, depth + 1, seen) for item in value]
            finally:
                seen.discard(ident)
        return _redact_string(str(value), depth + 1)
    except Exception:  # noqa: BLE001 - fail closed on hostile content
        return REDACTED


def redact_payload(payload: Json) -> Json:
    return _redact_value(payload, 0, set())


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8", "surrogatepass")
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def summarize_payload(payload: Json, *, max_bytes: int, label: str = "payload") -> dict[str, Json]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        msg = "max_bytes must be a non-negative integer"
        raise ValueError(msg)
    try:
        serialized = canonical_json(payload)
        encoded = serialized.encode("utf-8", "surrogatepass")
        digest: str | None = sha256_bytes(encoded)
    except Exception as error:  # noqa: BLE001 - fail closed
        return _withheld(label, None, None, error)
    try:
        redacted = redact_payload(payload)
        redacted_serialized = canonical_json(redacted)
        redacted_bytes = redacted_serialized.encode("utf-8", "surrogatepass")
    except Exception as error:  # noqa: BLE001 - fail closed
        return _withheld(label, len(encoded), digest, error)
    if len(encoded) <= max_bytes and len(redacted_bytes) <= max_bytes:
        return {
            f"{label}_status": "INLINE",
            f"{label}_bytes": len(encoded),
            f"{label}_sha256": digest,
            label: redacted if isinstance(payload, Mapping) else redacted_serialized,
        }
    return {
        f"{label}_status": "TRUNCATED",
        f"{label}_bytes": len(encoded),
        f"{label}_sha256": digest,
        label: _truncate_utf8(redacted_serialized, max_bytes),
    }


def _withheld(label: str, size: int | None, digest: str | None, error: BaseException) -> dict[str, Json]:
    return {
        f"{label}_status": "WITHHELD",
        f"{label}_bytes": size,
        f"{label}_sha256": digest,
        label: None,
        f"{label}_error": type(error).__name__,
    }
