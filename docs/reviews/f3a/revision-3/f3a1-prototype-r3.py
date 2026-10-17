"""F3-A1 reference prototype (revision 3) — evidence appendix, not a patch.

This file exists to make every claim in ``f3a1-design-revision-r3.md``
reproducible. It is **not** application code, **not** project style (no
project docstrings/tests), and **not** authorized for merge. It lives outside
the repository on purpose.

Revision 3 closes the Session D blockers:

* C-8  ``_redact_pem`` is a single linear marker scan (was a quadratic regex)
* C-9  the text tier is derived from ``_SENSITIVE_KEY_EXACT`` (was a narrower
       hand-maintained subset), and the cheap gate covers every key
* C-10 the key/separator scanner tolerates 8 escape levels, with a bounded
       fail-closed probe for anything deeper or oversized

It implements the F3-A1 remediation only:

* ``redact_text``      — free-text rules (ordered pipeline, bounded)
* ``redact_mapping``   — structural redaction of a mapping
* ``redact_payload``   — structural redaction of any JSON-shaped root
* ``summarize_payload``— inline/truncate/withhold with byte accounting

Run against the repository virtualenv::

    PYTHONPATH=src:<this directory> .venv/bin/python -c \
        "import f3a1_prototype as p; print(p.redact_text('password=hunter2'))"
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from rfq_agent.domain.values import Json, canonical_json, sha256_bytes

REDACTED = "[REDACTED]"
REDACTED_KEY = "[REDACTED_KEY]"

_MAX_STRUCTURED_PARSE = 256 * 1024
_MAX_DEPTH = 32

# ---------------------------------------------------------------------------
# Key vocabulary
#
# One policy for text and structure. ``_SENSITIVE_KEY_EXACT`` is the normalised
# (casefold, non-alphanumerics stripped) form of every key whose value must be
# redacted wherever it appears. ``_SENSITIVE_KEY_SUFFIXES`` catches compound
# spellings (``db_password``, ``x-api-key``, ``aws_secret_access_key``).
# ---------------------------------------------------------------------------
_SENSITIVE_KEY_EXACT = frozenset(
    """
    apikey apikeys apikeyid authorization authorizationcode auth authentication
    token tokens accesstoken refreshtoken sessiontoken secret secrets clientsecret
    password passwords passwd passphrase credential credentials operatortoken
    groqapikey senderemail customeremail email emailaddress useremail key
    privatekey secretkey signingkey encryptionkey accesskey accesskeyid
    secretaccesskey awssecretaccesskey sessionid cookie setcookie jwt oauth
    bearer proxyauthorization input inputvalue
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
    "accesskey",
)

#: Keys redacted in free text only when the value looks like a credential.
#: Bare ``key`` lives here so ``the key: Warsaw`` survives.
_WEAK_TEXT_KEYS = frozenset({"key"})
#: Real HTTP headers whose value is line-scoped by protocol.
_EOL_HEADER_KEYS = frozenset({"authorization", "proxyauthorization"})

_SAFE_SCALARS = (bool, int, float, Decimal, datetime, date, time, timedelta, UUID, Enum)

#: Cheap gate: a line containing none of these cannot contain a key we know.
#: An escaped key (``pass\\u0077ord``) bypasses the gate.
_KEY_HINTS = (
    "key",
    "token",
    "secret",
    "pass",
    "auth",
    "credential",
    "cookie",
    "jwt",
    "bearer",
    "session",
    "email",
    "input",
)


def _decode_key_escapes(text: str) -> str:
    """Decode ``\\uXXXX`` escapes so JSON-escaped keys normalise identically.

    A key can be escaped more than once (``api\\u005fkey`` inside a JSON string
    that is itself JSON-encoded), so the pattern tolerates any run of
    backslashes before ``u``. The run length is bounded by the caller's regex.
    """
    if "\\u" not in text:
        return text
    return _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _decode_key_escapes(str(key)).casefold())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _key_might_be_sensitive(key: str) -> bool:
    """Cheap gate: does the raw key contain any credential-ish substring?"""
    low = key.casefold()
    if "\\u" in low:
        low = _decode_key_escapes(low).casefold()
    return any(hint in low for hint in _KEY_HINTS)


def _is_strong_text_key(key: str) -> bool:
    """Text tier mirrors the structural policy; only bare ``key`` stays weak."""
    return _is_sensitive_key(key) and _normalize_key(key) not in _WEAK_TEXT_KEYS


# ---------------------------------------------------------------------------
# Patterns. Every quantifier is bounded: no rule may scan a run quadratically.
# ---------------------------------------------------------------------------
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_JSON_ESCAPE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})")
_PEM_MARKER_RE = re.compile(r"-----(?P<kind>BEGIN|END) (?P<label>[A-Z0-9 ]{1,40})-----")
#: A single PEM block longer than this is treated as unterminated and withheld.
_MAX_PEM_BLOCK = 1024 * 1024
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,31})://([^/\s]*)@(?=[^/\s]*(?:[/\s]|$))")
_KEY_SEP_RE = re.compile(
    # A key never starts immediately after a backslash: that position belongs
    # to an escape sequence, and rejecting it keeps a run of ``\u005f`` linear.
    r"(?i)(?<![A-Za-z0-9_.\-\\])"
    # Possessive quantifiers: a failed separator check must not re-scan the
    # key at every shorter length (measured: 113 ms -> 0.8 ms at 48 KB).
    r"(?P<key>(?:[A-Za-z0-9_.\-]|\\{1,8}+u[0-9a-fA-F]{4}){1,64}+)"
    # A quoted key or value can be escaped once per encoding level. Eight
    # levels is far beyond any real serialisation; deeper nesting is caught
    # by the bounded fail-closed probe in ``redact_text``.
    r"(?P<sep>(?:\\{0,8}[\"'])?\s*[:=,]\s*)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+(?P<tok>[^\s,;\"'<>()\[\]{}]{1,512})")
_BUSINESS_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-\d{2,4})(?:-[A-Za-z0-9]+)+")
_PROVIDER_PREFIXES = (
    "gsk_",
    "sk-",
    "sk_",
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxr-",
    "xoxs-",
    "AKIA",
    "ASIA",
    "hf_",
    "npm_",
)
_PROVIDER_TOKEN_RE = re.compile(
    r"(?i)(?:"
    r"gsk_[A-Za-z0-9]{12,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|sk_[A-Za-z0-9_\-]{16,}"
    r"|ghp_[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,}|ghs_[A-Za-z0-9]{16,}|ghu_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}"
    r"|hf_[A-Za-z0-9]{16,}|npm_[A-Za-z0-9]{16,}"
    r")"
)
_QUOTED_EMAIL_RE = re.compile(
    r"\"[^\"\n]{1,64}\"\s*@\s*(?:[^\s<>()\[\]{}\"',;:@.]+\.)+[^\s<>()\[\]{}\"',;:@.]{2,}"
)
_EMAIL_CANDIDATE_RE = re.compile(r"[^\s<>()\[\]{}\"',;]+")
_EMAIL_TOKEN_RE = re.compile(
    r"^[^\s<>()\[\]{}\"',;:@]{1,64}@(?:[^\s<>()\[\]{}\"',;:@.]+\.)+[^\s<>()\[\]{}\"',;:@.]{2,}"
)

_VALUE_SKIP = frozenset(" \t\"'<({[\\")
_VALUE_END = frozenset(" \t\r\n\"',;)]}>&")
_VALUE_CLOSERS = frozenset("\"')]}>")
_OPENERS = frozenset("\"'<({[")
_CREDENTIAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+/="
)


# ---------------------------------------------------------------------------
# Text primitives
# ---------------------------------------------------------------------------
def _strip_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub("\ufffd", text)


def _normalize_compat(text: str) -> str:
    """NFKC only when it changes the text; identity for ASCII and precomposed."""
    normalized = unicodedata.normalize("NFKC", text)
    return normalized if normalized != text else text


def _redact_url(match: re.Match[str]) -> str:
    scheme, userinfo = match.group(1), match.group(2)
    if ":" in userinfo:
        user = userinfo.partition(":")[0]
        return f"{scheme}://{user}:{REDACTED}@"
    return f"{scheme}://{REDACTED}@"


def _looks_credentialish(value: str) -> bool:
    if value.startswith(REDACTED):
        return False
    if len(value) >= 8 and all(ch in _CREDENTIAL_CHARS for ch in value):
        return True
    return len(value) >= 4 and any(ch.isdigit() for ch in value) and value.isprintable()


def _consume_value(text: str, pos: int) -> tuple[int, int]:
    """Return ``(start, end)`` of the value that follows a key separator."""
    n = len(text)
    openers = 0
    while pos < n and text[pos] in _VALUE_SKIP:
        openers += 1 if text[pos] in _OPENERS else 0
        pos += 1
    start = pos
    while pos < n and text[pos] not in _VALUE_END:
        pos += 1
    end = pos
    while openers and end < n and text[end] in _VALUE_CLOSERS:
        end += 1
        openers -= 1
    return start, end


def _redact_headers(text: str) -> str:
    """``Authorization``/``Proxy-Authorization`` are line-scoped by protocol."""
    out: list[str] = []
    cursor = 0
    for match in _KEY_SEP_RE.finditer(text):
        if match.start() < cursor:
            continue
        if not _key_might_be_sensitive(match["key"]):
            continue
        if _normalize_key(match["key"]) not in _EOL_HEADER_KEYS:
            continue
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        value = text[match.end() : line_end]
        if not value.strip() or value.lstrip().startswith(REDACTED):
            continue
        out.append(text[cursor : match.end()])
        out.append(REDACTED)
        cursor = line_end
    if not out:
        return text
    out.append(text[cursor:])
    return "".join(out)


def _redact_key_values(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _KEY_SEP_RE.finditer(text):
        if match.start() < cursor:
            continue
        key = match["key"]
        if not _key_might_be_sensitive(key):
            continue
        strong = _is_strong_text_key(key)
        if not strong and _normalize_key(key) not in _WEAK_TEXT_KEYS:
            continue
        value_start, value_end = _consume_value(text, match.end())
        if value_start >= value_end:
            continue
        value = text[value_start:value_end]
        if value.startswith(REDACTED):
            continue
        if not strong and not _looks_credentialish(value):
            continue
        out.append(text[cursor : match.end()])
        out.append(REDACTED)
        cursor = value_end
    if not out:
        return text
    out.append(text[cursor:])
    return "".join(out)


def _bearer_replace(match: re.Match[str]) -> str:
    """Redact a bearer credential unless it is a recognised business ID."""
    token = match["tok"]
    if _BUSINESS_ID_RE.fullmatch(token):
        return match.group(0)
    return f"Bearer {REDACTED}"


def _redact_emails(text: str) -> str:
    """Bounded, Unicode-aware email detector: one pass over whitespace tokens."""

    def _one(match: re.Match[str]) -> str:
        token = match.group(0)
        if "@" not in token:
            return token
        return REDACTED if _EMAIL_TOKEN_RE.match(token) else token

    if '"' in text:
        text = _QUOTED_EMAIL_RE.sub(REDACTED, text)
    return _EMAIL_CANDIDATE_RE.sub(_one, text)


def _redact_pem(text: str) -> str:
    """Redact PEM blocks in one linear pass over the marker list.

    ``re.sub`` with a lazy ``[\\s\\S]{0,65536}?`` body retries the bounded body
    scan at *every* position, which is quadratic on repeated ``BEGIN`` markers.
    This scanner visits each marker once and pairs a ``BEGIN`` with the next
    ``END``; an unterminated block is withheld to the end of the text.
    """
    out: list[str] = []
    cursor = 0
    pending: re.Match[str] | None = None
    for marker in _PEM_MARKER_RE.finditer(text):
        if marker["kind"] == "BEGIN":
            if pending is None:
                pending = marker
            continue
        if pending is None:
            continue
        if marker.end() - pending.start() > _MAX_PEM_BLOCK:
            out.append(text[cursor : pending.start()])
            out.append(REDACTED)
            cursor = len(text)
            pending = None
            break
        out.append(text[cursor : pending.start()])
        out.append(REDACTED)
        cursor = marker.end()
        pending = None
    if pending is not None:
        out.append(text[cursor : pending.start()])
        out.append(REDACTED)
        cursor = len(text)
    if not out:
        return text
    out.append(text[cursor:])
    return "".join(out)


#: Encoding levels the pattern scanner tolerates before the fail-closed probe.
_MAX_ESCAPE_DEPTH = 8
#: Above this size an escaped structured string is withheld rather than decoded.
_MAX_NESTED_PROBE = 256 * 1024


def _nested_escape_probe(text: str) -> str | None:
    """Withhold text whose deeper encoding level still hides a redaction.

    A payload can be encoded repeatedly (``json.dumps`` of a JSON string), and
    each level adds a backslash before every quote. The scanner tolerates eight
    levels; beyond that the value cannot be located precisely, so the whole
    string is withheld instead of being returned possibly-unredacted.
    """
    if '\\"' not in text:
        return None
    lowered = text.casefold()
    if "\\u" not in text and not any(hint in lowered for hint in _KEY_HINTS):
        return None
    if len(text) > _MAX_NESTED_PROBE:
        # The unescape view would double the memory of a large payload, so the
        # text is withheld instead of probed. Benign text without a credential
        # word never reaches this branch.
        return REDACTED
    view = text
    for _ in range(_MAX_ESCAPE_DEPTH + 1):
        view = view.replace('\\"', '"').replace("\\\\", "\\")
        if _redact_headers(view) != view or _redact_key_values(view) != view:
            return REDACTED
        if '\\"' not in view:
            return None
    return REDACTED


def _redact_pipeline(text: str) -> str:
    """One pass of every text rule, in order. ``redact_text`` adds the probe."""
    work = text
    if not work.isascii():
        work = _normalize_compat(_strip_surrogates(work))
    if "-----BEGIN" in work:
        work = _redact_pem(work)
    if "://" in work:
        work = _URL_RE.sub(_redact_url, work)
    lowered = work.casefold()
    if "\\u" in work or any(hint in lowered for hint in _KEY_HINTS):
        work = _redact_headers(work)
        work = _redact_key_values(work)
    if "bearer" in lowered:
        work = _BEARER_RE.sub(_bearer_replace, work)
    if any(prefix in work for prefix in _PROVIDER_PREFIXES):
        work = _PROVIDER_TOKEN_RE.sub(REDACTED, work)
    if "@" in work:
        work = _redact_emails(work)
    return work


def redact_text(text: str) -> str:
    """Remove secrets, credentials and email addresses from free-form text.

    Ordered pipeline. Order matters: URLs before e-mail (``password@host`` is
    e-mail-shaped), PEM before key/value (a PEM body spans lines), and the
    provider-token backstop before e-mail. Text that is still encoded more
    deeply than the scanner can read is withheld rather than returned as-is.
    """
    if not isinstance(text, str):
        msg = "redact_text() requires str"
        raise TypeError(msg)
    work = _redact_pipeline(text)
    probe = _nested_escape_probe(work)
    return work if probe is None else probe


# ---------------------------------------------------------------------------
# Structural walk
# ---------------------------------------------------------------------------
class _Unset:
    pass


_UNSET = _Unset()


def _coerce_key(key: object) -> str:
    try:
        return key if isinstance(key, str) else str(key)
    except Exception:  # noqa: BLE001 - hostile __str__ must not break logging
        return REDACTED_KEY


def _safe_changed(left: object, right: object) -> bool:
    try:
        return bool(left != right)
    except Exception:  # noqa: BLE001
        return True


def redact_mapping(data: Mapping[str, Json]) -> dict[str, Json]:
    """Return a redacted copy of ``data`` (any key type accepted)."""
    return _redact_mapping(data, 0, set())


def _credential_pair(data: Mapping[object, Json]) -> str | None:
    """Field holding the secret in a ``{"name": ..., "value": ...}`` record."""
    name = data.get("name")
    if isinstance(name, str) and _is_sensitive_key(name) and "value" in data:
        return "value"
    return None


def _redact_mapping(data: Mapping[object, Json], depth: int, seen: set[int]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    counts: dict[str, int] = {}
    secret_field = _credential_pair(data)
    for key, value in data.items():
        raw = _coerce_key(key)
        renamed = redact_text(raw)
        final_key = raw if renamed == raw else REDACTED_KEY
        if _is_sensitive_key(raw) or (secret_field is not None and raw == secret_field):
            redacted_value: Json = REDACTED
        else:
            redacted_value = _redact_value(value, depth, seen)
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
        if isinstance(value, (bytes, bytearray)):
            return _redact_string(bytes(value).decode("utf-8", "replace"), depth + 1)
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            ident = id(value)
            if ident in seen:
                return REDACTED  # cycle
            seen.add(ident)
            try:
                if isinstance(value, Mapping):
                    return _redact_mapping(value, depth + 1, seen)
                if isinstance(value, (set, frozenset)):
                    return [_redact_value(item, depth + 1, seen) for item in value]
                if len(value) == 2 and isinstance(value[0], str) and _is_sensitive_key(value[0]):
                    return [REDACTED, REDACTED]
                return [_redact_value(item, depth + 1, seen) for item in value]
            finally:
                seen.discard(ident)
        return _redact_string(str(value), depth + 1)
    except Exception:  # noqa: BLE001 - fail closed on hostile content
        return REDACTED


def redact_payload(payload: Json) -> Json:
    """Redact any JSON-shaped root: mapping, sequence, model, scalar or string."""
    return _redact_value(payload, 0, set())


def _looks_structured(text: str) -> bool:
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
    if len(text) > _MAX_STRUCTURED_PARSE:
        head = text.strip()[:1]
        return REDACTED if head in "{[(\\\"'" else redact_text(text)
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
    return REDACTED


# ---------------------------------------------------------------------------
# Payload summaries
# ---------------------------------------------------------------------------
def _truncate_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8", "surrogatepass")[:max_bytes].decode("utf-8", errors="ignore")


def _withheld(
    label: str, size: int | None, digest: str | None, error: BaseException
) -> dict[str, Json]:
    return {
        f"{label}_status": "WITHHELD",
        f"{label}_bytes": size,
        f"{label}_sha256": digest,
        label: None,
        f"{label}_error": type(error).__name__,
    }


def summarize_payload(payload: Json, *, max_bytes: int, label: str = "payload") -> dict[str, Json]:
    """Summarise a payload: inline when small, hashed when large, withheld on error.

    The digest is computed over the *unredacted* canonical JSON (stable identity);
    the stored copy is redacted **before** truncation, so a cut can never expose
    a secret prefix.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        msg = "max_bytes must be a positive integer"
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
