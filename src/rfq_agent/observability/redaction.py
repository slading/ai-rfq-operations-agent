"""Bounded redaction for log and trace payloads.

Text rules share the structural key policy. Sensitive containers are replaced
whole, malformed values fail closed, and summaries redact before truncating.
Raw Pydantic errors must be passed through these functions by their caller.
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

from rfq_agent.domain.values import Json, canonical_json, sha256_bytes, sha256_text

__all__ = [
    "REDACTED",
    "REDACTED_KEY",
    "email_hash",
    "redact_mapping",
    "redact_payload",
    "redact_text",
    "summarize_payload",
]

REDACTED = "[REDACTED]"
REDACTED_KEY = "[REDACTED_KEY]"

_MAX_STRUCTURED_PARSE = 256 * 1024
_MAX_DEPTH = 32
_MIN_CREDENTIAL_LENGTH = 8
_MIN_MIXED_CREDENTIAL_LENGTH = 4
_PAIR_LENGTH = 2

# ---------------------------------------------------------------------------
# Key vocabulary
#
# One policy for text and structure. ``_SENSITIVE_KEY_EXACT`` is the normalised
# (casefold, non-alphanumerics stripped) form of every key whose value must be
# redacted wherever it appears. ``_SENSITIVE_KEY_SUFFIXES`` catches compound
# spellings (``db_password``, ``x-api-key``, ``aws_secret_access_key``).
# ---------------------------------------------------------------------------
_SENSITIVE_KEY_EXACT = frozenset(
    [
        "apikey",
        "apikeys",
        "apikeyid",
        "authorization",
        "authorizationcode",
        "auth",
        "authentication",
        "token",
        "tokens",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "secret",
        "secrets",
        "clientsecret",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "operatortoken",
        "groqapikey",
        "senderemail",
        "customeremail",
        "email",
        "emailaddress",
        "useremail",
        "key",
        "privatekey",
        "secretkey",
        "signingkey",
        "encryptionkey",
        "accesskey",
        "accesskeyid",
        "secretaccesskey",
        "awssecretaccesskey",
        "sessionid",
        "cookie",
        "setcookie",
        "jwt",
        "oauth",
        "bearer",
        "proxyauthorization",
        "input",
        "inputvalue",
    ]
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
    r"""Decode ``\\uXXXX`` escapes so JSON-escaped keys normalise identically.

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
    r"(?P<key>(?:[A-Za-z0-9_.\-]|\\{1,16}+u[0-9a-fA-F]{4}){1,64}+)"
    # A quoted key or value is escaped once per encoding level. Sixteen levels
    # is far beyond any real serialisation; deeper nesting is caught by the
    # bounded fail-closed probe in ``redact_text``.
    r"(?P<sep>(?:\\{0,16}[\"'])?\s*[:=,]\s*)"
)
#: A bare identifier: used to tell a record field from a leaked token (D-2).
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
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

#: Containers larger than this are withheld rather than searched (F-1 bound).
_MAX_CONTAINER_CHARS = 64 * 1024
#: Nesting deeper than this is treated as malformed and withheld (F-1 bound).
_MAX_CONTAINER_DEPTH = 32
_CONTAINER_PAIRS = {"{": "}", "[": "]"}
#: A YAML block item: ``name:``, ``- item``, or a block scalar marker line.
_YAML_ITEM_RE = re.compile(r"(?:-[ \t]+|\S{1,64}:[ \t]|\S{1,64}:$)")
_YAML_BLOCK_SCALARS = frozenset({"|", ">", "|-", ">-", "|+", ">+"})
#: Two or more backslashes before ``uXXXX`` mean another encoding level.
_ESCAPE_REMAINS_RE = re.compile(r"\\\\u[0-9a-fA-F]{4}")
#: Runs the scanner cannot read directly (more than sixteen levels).
_DEEP_ESCAPE_RE = re.compile(r"\\{17,}u[0-9a-fA-F]{4}")
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
    if len(value) >= _MIN_CREDENTIAL_LENGTH and all(ch in _CREDENTIAL_CHARS for ch in value):
        return True
    return (
        len(value) >= _MIN_MIXED_CREDENTIAL_LENGTH
        and any(ch.isdigit() for ch in value)
        and value.isprintable()
    )


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
        if not _separator_is_real(text, match):
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


def _line_indent(text: str, pos: int) -> int:
    """Return the indentation (spaces/tabs) of the line containing ``pos``."""
    start = text.rfind("\n", 0, pos) + 1
    i = start
    while i < len(text) and text[i] in " \t":
        i += 1
    return i - start


def _yaml_block_bounds(  # noqa: PLR0912 - bounded YAML state machine
    text: str, match: re.Match[str]
) -> tuple[int, int | None, str] | None:
    """Locate a YAML block that follows ``key:``.

    Returns ``(block_start, block_end, insert)`` when the value is an indented
    mapping, list or block scalar; ``block_end`` is ``None`` when such a block
    exists but is larger than ``_MAX_CONTAINER_CHARS`` (the caller withholds
    the rest). ``insert`` is the text that separates the key from the marker.
    ``None`` when the shape is not a YAML block.
    """
    n = len(text)
    key_indent = _line_indent(text, match.start())
    sep = match["sep"]
    nl = sep.rfind("\n")
    insert = " "
    if nl >= 0:
        if sep[nl + 1 :].strip():
            return None
        block_start = match.start("sep") + nl
        first = match.end()
        if first >= n:
            return None
        indent = _line_indent(text, first)
        # D-1: a sequence may start at the key's *own* indentation
        # (``token:`` followed by ``- x: ...``), which the mapping rule below
        # would otherwise reject.
        same_indent_item = (
            indent == key_indent
            and text[first] == "-"
            and (first + 1 >= n or text[first + 1] in " \t")
        )
        if indent < key_indent or (indent == key_indent and not same_indent_item):
            return None
        line_end = text.find("\n", first)
        line = (text[first:] if line_end == -1 else text[first:line_end]).lstrip(" \t")
        if not _YAML_ITEM_RE.match(line):
            return None
    else:
        line_end = text.find("\n", match.end())
        tail = (text[match.end() :] if line_end == -1 else text[match.end() : line_end]).strip()
        if tail not in _YAML_BLOCK_SCALARS:
            return None
        block_start = match.end()
        first = min(n, line_end + 1) if line_end != -1 else n
        if first >= n:
            return None
        same_indent_item = False
        insert = ""
    limit = min(n, block_start + _MAX_CONTAINER_CHARS)
    pos = first
    while pos < limit:
        line_end = text.find("\n", pos)
        if line_end == -1:
            line_end = n
        if text[pos:line_end].strip():
            indent = _line_indent(text, pos)
            if indent < key_indent:
                break
            if indent == key_indent:
                if not same_indent_item:
                    break
                # A same-indent sequence continues only for further ``-`` items.
                if text[pos] != "-" or (pos + 1 < n and text[pos + 1] not in " \t"):
                    break
        pos = line_end + 1 if line_end < n else n
    if pos >= limit and pos < n:
        return block_start, None, insert
    return block_start, pos, insert


def _consume_container(text: str, pos: int) -> int | None:
    """Measure a container within the size and depth bounds.

    Unterminated, mismatched, oversized, and ambiguous nested-encoded values
    return ``None``; the caller withholds the remaining text. Native quoted
    strings use backslash parity, so a trailing escaped backslash is safe.
    """
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)
    stack: list[str] = []
    quote = ""
    i = pos
    while i < limit:
        ch = text[i]
        if quote:
            if ch == quote and not _quote_is_escaped(text, i):
                quote = ""
        elif ch in "\"'":
            if i > pos and text[i - 1] == "\\":
                return None  # cannot trust a bracket inside an encoded string
            quote = ch
        elif ch in "{[":
            if len(stack) >= _MAX_CONTAINER_DEPTH:
                return None
            stack.append(ch)
        elif ch in "}]":
            if not stack or _CONTAINER_PAIRS[stack[-1]] != ch:
                return None
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return None


def _quote_is_escaped(text: str, pos: int) -> bool:
    """A quote is escaped only after an odd run of backslashes."""
    start = pos
    while start > 0 and text[start - 1] == "\\":
        start -= 1
    return (pos - start) % 2 == 1


def _quoted_end(text: str, pos: int) -> int | None:
    """Return the index just past the closing quote of the string at ``pos``.

    Backslash parity distinguishes literal quotes from a terminator.
    ``None`` means unterminated or oversized.
    """
    quote = text[pos]
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)
    i = pos + 1
    while i < limit:
        if text[i] == quote and not _quote_is_escaped(text, i):
            return i + 1
        i += 1
    return None


#: Wrappers that decorate a value without being part of it (D-1).
_WRAPPER_PAIRS = {"<": ">", "(": ")", "{": "}", "[": "]"}


def _skip_wrappers(text: str, pos: int, limit: int) -> tuple[int, list[str]]:
    """Skip a bounded run of ``<``/``(`` wrappers, outermost first."""
    openers: list[str] = []
    i = pos
    while i < limit and text[i] in "<(" and len(openers) < _MAX_CONTAINER_DEPTH:
        openers.append(text[i])
        i += 1
        while i < limit and text[i] in " \t":
            i += 1
    return i, openers


def _decorated_like(text: str, pos: int) -> bool:
    """True when a wrapper run leads to a container or a quoted value."""
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)
    i, openers = _skip_wrappers(text, pos, limit)
    return bool(openers) and (
        i >= limit or len(openers) >= _MAX_CONTAINER_DEPTH or text[i] in "{[\"'"
    )


def _malformed_wrapper_like(text: str, pos: int) -> bool:
    """True when an unclosed ``<``/``(`` group starts with ``key:``/``key=``.

    ``credentials=<value: hunter2024}`` is a mis-nested container: the wrapper
    never closes with the matching bracket, so consuming it as a scalar leaves
    the secret behind. Such a group is withheld to the end of the line.
    Ordinary prose -- ``password: (rotated 2026-01-01)`` -- does not start with
    a key and keeps the scalar rule. Bounded by ``_MAX_CONTAINER_CHARS``.
    """
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)
    i, openers = _skip_wrappers(text, pos, limit)
    if not openers:
        return False
    word = _IDENT_RE.match(text, i)
    if word is None:
        return False
    j = word.end()
    while j < limit and text[j] in " \t":
        j += 1
    return j < limit and text[j] in ":="


def _consume_decorated(text: str, pos: int) -> int | None:
    """Return the index just past a complete wrapper-decorated value.

    ``credentials=<{"value": "x"}>``, ``credentials=({'a': 1},)`` and
    ``credentials=("x",)`` are consumed as one value so that no tail of the
    container survives as a partial redaction (D-1). ``None`` means the
    wrapper never closes, is mismatched, or exceeds the size bound; the caller
    then withholds the rest of the text.
    """
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)
    i, openers = _skip_wrappers(text, pos, limit)
    if not openers:
        return None
    stack = list(openers)
    quote = ""
    while i < limit:
        ch = text[i]
        if quote:
            if ch == quote and not _quote_is_escaped(text, i):
                quote = ""
        elif ch in "\"'":
            if i > pos and text[i - 1] == "\\":
                return None
            quote = ch
        elif ch in "{[<(":
            if len(stack) >= _MAX_CONTAINER_DEPTH * 2:
                return None
            stack.append(ch)
        elif ch in "}])>":
            if not stack or _WRAPPER_PAIRS[stack[-1]] != ch:
                return None
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return None


def _separator_is_real(text: str, match: re.Match[str]) -> bool:
    """False when a comma separates data items, not a key from its value (D-2).

    Two shapes must not be read as key/value pairs: a quoted token followed by
    ``,`` (a value inside a record: ``{"name": "api_key", "value": ...}``) and
    a bare token whose next comma is followed by another ``ident:``/``ident=``
    field (``name=api_key, value=...``). Revision 4 mangled both.
    """
    if "," not in match["sep"]:
        return True
    end = match.end("key")
    if end < len(text) and text[end] in "\"'":
        return False
    # One escape level per encoding: ``\"api_key\", \"value\"`` is the same
    # shape inside a ``json.dumps`` string.
    j = end
    while j < len(text) and text[j] == "\\":
        j += 1
    if j > end and j < len(text) and text[j] in "\"'":
        return False
    i = match.end()
    while i < len(text) and text[i] in " \t":
        i += 1
    word = _IDENT_RE.match(text, i)
    if word is None:
        return True
    after = word.end()
    while after < len(text) and text[after] in " \t":
        after += 1
    return not (after < len(text) and text[after] in ":=")


def _redact_key_values(text: str) -> str:  # noqa: PLR0912, PLR0915 - value-shape dispatch
    """Redact assignment-style values, containers and YAML blocks.

    Order per match: YAML block, container, quoted string, scalar token. A
    strong key replaces the whole value, so a nested secret can never survive
    a partial redaction; a weak key keeps the credential-shape rule.
    """
    out: list[str] = []
    cursor = 0
    n = len(text)
    for match in _KEY_SEP_RE.finditer(text):
        if match.start() < cursor:
            continue
        key = match["key"]
        if not _key_might_be_sensitive(key):
            continue
        strong = _is_strong_text_key(key)
        if not strong and _normalize_key(key) not in _WEAK_TEXT_KEYS:
            continue
        # D-2: a comma between two record fields is not a key separator.
        if not _separator_is_real(text, match):
            continue

        if strong:
            cpos = match.end()
            while cpos < n and text[cpos] in " \t":
                cpos += 1
            # Cheap gate: only a separator that crossed a newline, or a block
            # scalar marker on the key line, can start a YAML block. Calling
            # the block scanner for every strong key would scan each remaining
            # line once per match (quadratic on single-line text).
            if "\n" in match["sep"] or (cpos < n and text[cpos] in "|>"):
                bounds = _yaml_block_bounds(text, match)
            else:
                bounds = None
            if bounds is not None:
                block_start, block_end, insert = bounds
                out.append(text[cursor:block_start])
                out.append(f"{insert}{REDACTED}")
                if block_end is None:
                    out.append("\n")
                    cursor = n
                    break
                if block_end < n:
                    out.append("\n")
                cursor = block_end
                continue

            if cpos < n and text[cpos] in "{[":
                end = _consume_container(text, cpos)
                out.append(text[cursor:cpos])
                out.append(REDACTED)
                if end is None:
                    cursor = n
                    break
                cursor = end
                continue

            if cpos < n and text[cpos] in "\"'":
                end = _quoted_end(text, cpos)
                if end is None:
                    out.append(text[cursor : cpos + 1])
                    out.append(REDACTED)
                    cursor = n
                    break
                content = text[cpos + 1 : end - 1]
                if content and content != REDACTED:
                    out.append(text[cursor : cpos + 1])
                    out.append(REDACTED)
                    cursor = end - 1
                continue

            if cpos < n and text[cpos] in "<(":
                # D-1: ``<{...}>``, ``({'a': 1},)``, ``("x",)`` and a wrapped
                # scalar are one value. A group that closes is replaced whole.
                # A group that never closes is withheld **only** when it wraps
                # a container or a quote; a wrapped scalar keeps the scalar
                # rule so ordinary prose is not truncated.
                end = _consume_decorated(text, cpos)
                if end is not None:
                    out.append(text[cursor:cpos])
                    out.append(REDACTED)
                    cursor = end
                    continue
                if _decorated_like(text, cpos):
                    out.append(text[cursor:cpos])
                    out.append(REDACTED)
                    cursor = n
                    break
                if _malformed_wrapper_like(text, cpos):
                    # A mis-nested group whose content is ``key: value`` cannot
                    # be consumed safely; withhold its line (D-1 fails closed).
                    line_end = text.find("\n", cpos)
                    if line_end == -1:
                        line_end = n
                    out.append(text[cursor:cpos])
                    out.append(REDACTED)
                    if line_end - cpos > _MAX_CONTAINER_CHARS:
                        cursor = n
                        break
                    cursor = line_end
                    continue

            if cpos < n and text[cpos] == "-" and (cpos + 1 >= n or text[cpos + 1] in " \t"):
                # D-1: an inline YAML sequence marker after a strong key,
                # ``credentials: - hunter2024``. Item scope is the line.
                line_end = text.find("\n", cpos)
                if line_end == -1:
                    line_end = n
                out.append(text[cursor:cpos])
                out.append(REDACTED)
                if line_end - cpos > _MAX_CONTAINER_CHARS:
                    cursor = n
                    break
                cursor = line_end
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


#: Field vocabulary for credential records, shared by text and mappings.
_RECORD_NAME_KEYS = ("name", "key", "field", "type")
_RECORD_VALUE_KEYS = frozenset(
    {"value", "secret", "token", "password", "credential", "credentials"}
)
_MAX_RECORDS = 32
_MAX_RECORD_SCAN = 128
_MAX_RECORD_PAIR_CHARS = 256 * 1024
_RECORD_NAME_FIELD_RE = re.compile(
    r"(?i)(?<![\w.\-])(?:[\"']?)(?:name|key|field|type)(?:[\"']?)\s*[:=]\s*"
    r"(?:[\"'](?P<quoted>[^\"'\n]{1,64})[\"']|(?P<bare>[A-Za-z0-9_.\-]{1,64}))"
)
_RECORD_VALUE_FIELD_RE = re.compile(
    r"(?i)(?<![\w.\-])(?:[\"']?)(?:value|secret|token|password|credential|credentials)"
    r"(?:[\"']?)\s*[:=]\s*"
)


def _sensitive_record_name(match: re.Match[str]) -> bool:
    """Use the key policy on a record's name, including escaped spellings."""
    return _is_sensitive_key(match["quoted"] or match["bare"])


def _has_sensitive_record_name(text: str) -> bool:
    """Linear overflow probe; never silently skip a potentially secret tail."""
    return any(_sensitive_record_name(m) for m in _RECORD_NAME_FIELD_RE.finditer(text))


def _field_value_end(text: str, pos: int) -> int | None:
    """Measure a record field's whole value, or fail closed on malformed data."""
    if pos >= len(text):
        return None
    if text[pos] in "\"'":
        return _quoted_end(text, pos)
    if text[pos] in "{[":
        return _consume_container(text, pos)
    if text[pos] in "<(":
        return _consume_decorated(text, pos)
    end = pos
    while end < len(text) and text[end] not in " \t\r\n,;})]":
        end += 1
    return end if end > pos else None


def _record_fields(text: str) -> list[tuple[str, int, int]] | None:
    """Locate top-level fields without confusing nested or duplicate keys.

    Values are skipped as whole groups. Unlike a parsed dict, this retains
    duplicate fields, so every secret value can be removed. Only a bounded
    container slice is passed here.
    """
    fields: list[tuple[str, int, int]] = []
    pos = 1
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "}":
            return fields
        if text[pos] in "\"'":
            end = _quoted_end(text, pos)
            if end is None:
                return None
            key = _decode_quoted(text[pos:end], text[pos])
        else:
            match = _IDENT_RE.match(text, pos)
            if match is None:
                return None
            key, end = match.group(), match.end()
        if not isinstance(key, str):
            return None
        pos = end
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        if pos >= len(text) or text[pos] not in ":=":
            return None
        pos += 1
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        end = _field_value_end(text, pos)
        if end is None:
            return None
        fields.append((key.casefold(), pos, end))
        pos = end
    return None


def _redact_record_literal(text: str, depth: int) -> str:
    """Preserve a record's spelling while replacing every top-level value."""
    fields = _record_fields(text)
    if fields is None:
        return REDACTED if _has_sensitive_record_name(text) else text
    sensitive = False
    for key, start, end in fields:
        if key in _RECORD_NAME_KEYS:
            raw = text[start:end]
            name = _decode_quoted(raw, raw[0]) if raw[0] in "\"'" else raw
            if isinstance(name, str) and _is_sensitive_key(name):
                sensitive = True
    if not sensitive:
        return text
    out: list[str] = []
    cursor = 0
    for key, value_start, value_end in fields:
        start, end = value_start, value_end
        if key in _RECORD_VALUE_KEYS:
            if text[start] in "\"'":
                start, end = start + 1, end - 1
            replacement = REDACTED
        elif text[start] in "{[":
            replacement = _redact_credential_records(text[start:end], depth + 1)
        else:
            continue
        out.extend((text[cursor:start], replacement))
        cursor = end
    return "".join(out) + text[cursor:] if out else text


def _redact_record_pairs(text: str) -> str:  # noqa: PLR0912 - forward/reversed field scanners
    """Handle adjacent name/value fields in kwargs and malformed literals.

    Field prefixes are matched, then the shared bounded value scanner consumes
    containers and quoted strings. A fixed-size scalar regex would leave tails
    behind. Overflow with a sensitive record hint withholds the entire text.
    """
    if not _has_sensitive_record_name(text):
        return text
    if len(text) > _MAX_RECORD_PAIR_CHARS:
        return REDACTED if _RECORD_VALUE_FIELD_RE.search(text) else text
    spans: list[tuple[int, int]] = []
    for scanned, match in enumerate(_RECORD_NAME_FIELD_RE.finditer(text)):
        if scanned >= _MAX_RECORD_SCAN:
            return REDACTED
        if not _sensitive_record_name(match):
            continue
        pos = match.end()
        while pos < len(text) and text[pos] in " \t\r\n,;":
            pos += 1
        value = _RECORD_VALUE_FIELD_RE.match(text, pos)
        if value is not None:
            start = value.end()
            end = _field_value_end(text, start)
            if end is None:
                return text[:start] + REDACTED
            spans.append((start, end))
    # Reversed pairs: measure the value before checking its following name.
    for scanned, value in enumerate(_RECORD_VALUE_FIELD_RE.finditer(text)):
        if scanned >= _MAX_RECORD_SCAN:
            return REDACTED
        start = value.end()
        end = _field_value_end(text, start)
        if end is None:
            continue
        pos = end
        while pos < len(text) and text[pos] in " \t\r\n,;":
            pos += 1
        name = _RECORD_NAME_FIELD_RE.match(text, pos)
        if name is not None and _sensitive_record_name(name):
            spans.append((start, end))
    out: list[str] = []
    cursor = 0
    for value_start, value_end in sorted(spans):
        start, end = value_start, value_end
        if start < cursor:
            continue
        if text[start] in "\"'":
            start, end = start + 1, end - 1
        out.extend((text[cursor:start], REDACTED))
        cursor = end
    return "".join(out) + text[cursor:] if out else text


def _redact_credential_records(text: str, depth: int = 0) -> str:
    """Redact credential records with a bounded walk and fail-closed overflow."""
    if not _has_sensitive_record_name(text):
        return text
    if depth > _MAX_DEPTH:
        return REDACTED
    out: list[str] = []
    cursor = pos = found = scanned = 0
    while found < _MAX_RECORDS and scanned < _MAX_RECORD_SCAN:
        pos = text.find("{", pos)
        if pos == -1:
            break
        scanned += 1
        end = _consume_container(text, pos)
        if end is None:
            candidate = text[pos : pos + _MAX_CONTAINER_CHARS]
            if _has_sensitive_record_name(candidate):
                return "".join(out) + text[cursor:pos] + REDACTED
            pos += 1
            continue
        literal = text[pos:end]
        redacted = _redact_record_literal(literal, depth)
        if redacted == literal:
            pos += 1  # inspect nested records as well
            continue
        out.extend((text[cursor:pos], redacted))
        cursor = end
        found += 1
        pos = end
    if found >= _MAX_RECORDS or scanned >= _MAX_RECORD_SCAN:
        # Collapse the whole input at either budget. Keeping a partly scanned
        # prefix can move the cutoff on a second pass and break idempotence.
        return REDACTED
    body = "".join(out) + text[cursor:] if out else text
    return _redact_record_pairs(body)


def _record_probe(text: str) -> str | None:
    r"""Withhold text whose escaped view still hides a credential record.

    ``json.dumps`` of a record leaves ``\"`` quotes, which the flat key rules
    cannot pair up. The probe unescapes a bounded number of levels and applies
    the record pass to each view; a hit withholds the whole string instead of
    returning a partially redacted one.
    """
    if '\\"' not in text:
        return None
    lowered = text.casefold()
    if "value" not in lowered or not any(part in lowered for part in _RECORD_NAME_KEYS):
        return None
    if len(text) > _MAX_NESTED_PROBE:
        return REDACTED
    view = text
    for _ in range(_MAX_ESCAPE_DEPTH + 1):
        view = view.replace('\\"', '"').replace("\\\\", "\\")
        if _redact_credential_records(view) != view:
            return REDACTED
        if '\\"' not in view:
            return None
    return REDACTED


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
    r"""Redact PEM blocks in one linear pass over the marker list.

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
    each level adds backslashes before quotes. The scanner reads runs of up to
    sixteen backslashes; the probe unescapes at most nine times, so the whole
    string is withheld instead of being returned possibly-unredacted.
    """
    # F-2: a deep ``\\u`` run is a hidden key even when no quote is escaped,
    # so it must trigger the probe as well. The two-backslash substring test is
    # a fast C scan; the regex only runs on text that could contain a run.
    if '\\"' not in text and ("\\\\" not in text or not _DEEP_ESCAPE_RE.search(text)):
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
        # Stop only when nothing is escaped any more. A remaining ``\\\\uXXXX``
        # run is another encoding level, not clean text (F-2).
        if '\\"' not in view and not _ESCAPE_REMAINS_RE.search(view):
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
        lowered = work.casefold()
    # D-2: records are recognised after the key rules, never before them, so a
    # record's own field names are still intact when the structure is parsed.
    if (
        "value" in lowered
        and any(part in lowered for part in _RECORD_NAME_KEYS)
        and ("{" in work or ":" in work or "=" in work)
    ):
        work = _redact_credential_records(work)
    if "bearer" in lowered:
        work = _BEARER_RE.sub(_bearer_replace, work)
    if any(prefix in work for prefix in _PROVIDER_PREFIXES):
        work = _PROVIDER_TOKEN_RE.sub(REDACTED, work)
    if "@" in work:
        work = _redact_emails(work)
    return work


def email_hash(address: str) -> str:
    """Return a stable hash for an email address, for correlation without PII."""
    return sha256_text(address.strip().lower())


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
    if probe is not None:
        return probe
    probe = _record_probe(work)
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
    redacted = _redact_value(data, 0, set())
    return redacted if isinstance(redacted, dict) else {REDACTED_KEY: REDACTED}


def _credential_pair(data: Mapping[object, Json]) -> bool:
    """Recognise name/value records using the same field policy as text."""
    return any(
        isinstance(key, str)
        and key.casefold() in _RECORD_NAME_KEYS
        and isinstance(value, str)
        and _is_sensitive_key(value)
        for key, value in data.items()
    )


def _redact_mapping(data: Mapping[object, Json], depth: int, seen: set[int]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    counts: dict[str, int] = {}
    secret_field = _credential_pair(data)
    for key, value in data.items():
        raw = _coerce_key(key)
        renamed = redact_text(raw)
        final_key = raw if renamed == raw else REDACTED_KEY
        if _is_sensitive_key(raw) or (secret_field and raw.casefold() in _RECORD_VALUE_KEYS):
            redacted_value: Json = REDACTED
        else:
            redacted_value = _redact_value(value, depth, seen)
        count = counts.get(final_key, 0) + 1
        counts[final_key] = count
        base_key = final_key
        while final_key in result:
            final_key = f"{base_key}#{count}"
            count += 1
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
                if (
                    len(value) == _PAIR_LENGTH
                    and isinstance(value[0], str)
                    and _is_sensitive_key(value[0])
                ):
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
    return len(stripped) >= _PAIR_LENGTH and stripped[0] in "{(["


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
    if len(stripped) >= _PAIR_LENGTH and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        decoded = _decode_quoted(stripped, stripped[0])
        if isinstance(decoded, str):
            nested = _redact_string(decoded, depth + 1)
            if nested != decoded:
                return canonical_json(nested)
        return redact_text(text)
    if _looks_structured(stripped):
        # D-2: try the raw shape first. Revision 4 ran ``redact_text`` before
        # parsing, which rewrote a record's ``value`` key to ``[REDACTED]`` and
        # hid the credential pair from the structural walk.
        parsed = _parse_container(stripped)
        if isinstance(parsed, (dict, list, tuple)):
            structural = _redact_value(parsed, depth + 1, set())
            if _safe_changed(structural, parsed):
                return repr(structural) if isinstance(parsed, tuple) else canonical_json(structural)
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
