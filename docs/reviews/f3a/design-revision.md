# F3-A Design Revision — Redaction Choke Point and Rendering Surfaces

**Project 04 / Session A — senior security design review (read-only).**
Baseline commit `d5611eefab3b326e9ad5caa9789d892e69ce3875`, branch
`arena/c577861f-ai-rfq-operations-agent`. Repository tree clean at the time of
writing. This document is a **design revision**: it specifies what would be
changed, not a change.

| Gate | Status |
| --- | --- |
| Code modified | **NO** |
| Tests added | **NO** |
| Commits created | **NO** |
| PR created | **NO** |
| Implementation authorized | **NO** |

**Readiness verdict: ACCEPT (design level).** Every review finding has a
reproduced failure mode, a named remediation, and a validating test. The revised
design was exercised end-to-end as a throwaway prototype: the full offline suite
(1884 passed, 3 skipped) stays green with the new redaction primitives swapped
in, and a 107-case security matrix passes. Implementation remains unauthorized
pending the owner's explicit approval.

> Provenance note. The audit report (`f3a-audit-report.md`) and the independent
> review (`f3a-independent-review.md`) produced in earlier sessions are **not
> present in the current workspace snapshot**; only their recorded summaries
> survived. The finding register below is therefore reconstructed from that
> record, with every failure mode **re-reproduced in this session against
> `d5611eef`**. If the owner supplies the original review, the finding IDs can be
> re-mapped to its section numbers without changing any design decision.

---

## 1. Scope, method, and evidence standard

### 1.1 In scope

* `src/rfq_agent/observability/redaction.py` — the designated single choke point.
* `src/rfq_agent/observability/ids.py` — `LogFields` rendering.
* `src/rfq_agent/config.py` — URL-bearing settings fields (rendering only).
* `src/rfq_agent/persistence/engine.py` — `create_engine(..., hide_parameters=True)`.
* Tests in `tests/unit/test_observability.py`, `tests/unit/test_config.py`,
  `tests/persistence/test_sqlite_config.py`.

### 1.2 Explicitly out of scope

No LLM/provider integration, no autonomous workflow or tool calling, no
human-approval automation, no READY transitions, no outbound email, no web UI,
no database schema or migration change, and no change to deterministic
quotation/pricing/stock/delivery/discount rules. The invariant *"the LLM
produces claims; deterministic code produces business facts"* is untouched, as
are all F3-B controls (`SecretStr`, `hide_input_in_errors` on `Settings`,
`_as_bool` not echoing values, `redact_logs` forced in production).

### 1.3 Method

* Baseline verified with `git status -b`, `git rev-parse HEAD`, and `gh pr view 7`
  (PR #7 is merged; `d5611eef` is the reviewed tree).
* All reproductions use **synthetic credentials only** (`gsk_SESSION_A_…`,
  `pw_DB_…`, `pw_PROXY_…`). No real secret, environment value, or production
  data was read or printed.
* **Leak standard:** a rendering is a leak if any intact fragment of **≥ 8
  consecutive characters** of the synthetic secret survives. This matches the
  `_leaked_fragments(..., min_length=8)` detector already used by the F3-B
  config tests and is stricter than a whole-string containment check.
* Probes ran against the repository virtualenv (`PYTHONPATH=src`). The revised
  design was implemented **only in `/tmp`** and injected at test time through a
  `sitecustomize.py` shim, so the repository was never modified. The full
  offline suite ran three times: with the new redaction primitives, with
  `hide_parameters=True`, and with `hide_input_in_errors=True` on
  `DomainModel` — **1884 passed, 3 skipped** in every configuration.
* The reference prototype is preserved as a non-normative appendix:
  `/home/user/f3a_design_appendix_prototype.py` (307 lines). It is **not** a
  patch, has no project docstrings/tests, and must not be merged as-is.
* Two earlier design attempts were rejected on measurement; see §9.4.

### 1.4 Consequence of the missing inputs

Every finding below is stated as *observed behaviour* (with the exact command
output in this session) rather than *"the review says X"*. Where the reviewed
proposal — not the baseline — was the defective artifact, the failure mode is
reconstructed and reproduced against the prototype variant that exhibited it,
and the remediation is stated against that behaviour. This keeps the design
defensible even if the original review wording differs.

---

## 2. Findings register

Status legend: **FIXED** = remediation specified and verified in the prototype;
**FIXED-BY-DESIGN** = structural choice removes the class of failure;
**DEFERRED** = acknowledged, deliberately outside this change.

| ID | Finding | Observed on baseline / reviewed proposal | Revised design response | Status |
| --- | --- | --- | --- | --- |
| F1 | Nested assignment: a value redaction must not swallow the surrounding assignment, and truncation must not expose a prefix | An earlier prototype variant matched `key + separator + value` in one regex and consumed the preceding assignment on `note: x=password=<secret>`; on baseline, truncation slices text *before* redacting, so the **whole** secret survives in the payload | Key and value are matched **separately**: `_KEY_SEP_RE` matches only `key + separator` (≤ 64 chars, atomic prefix match); an anchored `_VALUE_RE` extracts the value in the same `finditer` pass. Truncation order reversed: redact → serialise → cut bytes | **FIXED** |
| F2 | Non-mapping and encoded roots bypass redaction | `summarize_payload([{"api_key": S}], max_bytes=1024)` returns the secret; JSON-string, Python-literal and double-encoded roots likewise | `redact_payload()` accepts **any** JSON root: mapping, list, tuple, set, BaseModel, scalar and string; string roots are parsed (JSON then Python literal, quoted-string unwrap) and walked structurally | **FIXED** |
| F3 | Key-spelling coverage (compound, camelCase, escaped) | Nine spellings leak structurally on baseline; `{"api\u005fkey": S}` survives because the JSON key escape is never decoded | One key policy for text and structure: `_normalize_key` (casefold, strip non-alphanumerics) + exact set + suffix tuple; structural walks see decoded keys | **FIXED** |
| F4 | Pair handling for lists, tuples and JSON pair arrays | `[["api_key", S]]` and `("api_key", S)` leak on baseline | A two-element sequence whose first element is a sensitive key is treated as a pair: `[key, "[REDACTED]"]`; this applies after JSON/literal parsing too | **FIXED** |
| F5 | Over-cap structured content bypass | Design-level defect: once a parse cap exists, structured text above it must not fall back to text-only redaction (a nested value, or an escaped key, survives that fallback). Reproduced with prototype v1's over-cap branch and with baseline truncation | Structured-looking text (`{`, `[`, `(`, or a quoted/escaped form) above the cap is **wholly withheld** (`[REDACTED]`); unparseable structured text is withheld too. No heuristic gate (see §9.4, D3) | **FIXED-BY-DESIGN** |
| F6 | URL handling: rule ordering and host preservation | Baseline `_EMAIL_RE` eats `password@host` inside a URL, so the host disappears (`postgresql://rfquser:[REDACTED]:5432/rfq`); passwords containing `@` are mishandled | URL userinfo rule runs **first**, redacts only the password (or the whole userinfo when no `:`), and leaves scheme, user, host, port and path intact | **FIXED** |
| F7 | Byte-aware truncation | `max_bytes=128` yields 366 bytes (€) and 485 bytes (emoji): the cap is applied to characters | `_truncate_utf8` encodes with `surrogatepass`, cuts at the byte offset, decodes with `errors="ignore"`; the cap is bytes on the wire | **FIXED** |
| F8 | `max_bytes` validation | `-1` (and `True`, `1.5`, `"128"`, `None`) is accepted and silently misbehaves (`-1` leaks the whole payload) | `summarize_payload` rejects non-`int`, `bool` and negative values with `ValueError` | **FIXED** |
| F9 | Surrogate handling | A lone surrogate raises `UnicodeEncodeError` out of the summary path | Surrogates are normalised to U+FFFD in text; summaries encode with `surrogatepass`; no exception escapes | **FIXED** |
| F10 | Secret in the key name | `{"api_key=<secret>": "1"}` keeps the secret in the key | A key that changes under redaction is replaced by `[REDACTED_KEY]`; collisions get a deterministic `#n` suffix | **FIXED** |
| F11 | Missing `__str__`/`__str__`-equivalent rendering on payload types | `LogFields`, `Settings.database` and `Settings.groq.base_url` leak through `repr`, `str`, f-strings, `model_dump`, `model_dump(mode="json")` and `model_dump_json` | `LogFields` gets a wrap serializer plus `__repr__`/`__str__`; URL-bearing config sections get serializers **and** `__repr__`/`__str__`. Serializer-only and repr-only fixes were each measured insufficient (§6.3) | **FIXED** |
| F12 | Possibly excessive scope in config / pydantic / database echo | Review question, not a defect | The change is split into two independently reviewable units (A1 primitives + `LogFields`; A2 rendering surfaces). A2 is 3 small edits and depends on A1. `DomainModel.hide_input_in_errors` is **deferred** | **SPLIT** |
| F13 | *(new, this session)* Super-linear backtracking in `_SECRET_ASSIGN_RE` | Measured: 1.4 KB `api_key`×200 → **0.73 s**; 2.8 KB → **6.45 s**; growth ~8× per doubling | The revised linear scan does 64 KB of the same input in **2.9 ms**. Regression test included | **FIXED** |
| F14 | *(new)* Silent pass-through of unsupported kinds; non-`str` keys crash | `redact_mapping({1: "a"})` raises `AttributeError`; a `bytes` value is returned **unredacted**; a cyclic payload makes `summarize_payload` raise `ValueError` | Non-`str` keys are coerced; `bytes`/arbitrary objects are stringified and text-redacted; sets are withheld; cycles map to `[REDACTED]`; summary failures return `WITHHELD` | **FIXED** |

### 2.1 Reproduction evidence (baseline, this session)

```
# F1 truncation prefix (baseline)
summarize_payload({"api_key": S, "body": "x"*5000}, max_bytes=64)
  -> payload = '{"api_key":"gsk_SESSION_A_0123456789abcdefABCDEF","body":"xxxxxx'
     (the complete secret is inside the retained prefix)

# F2 roots (baseline)
summarize_payload([{"api_key": S}], max_bytes=1024)     -> secret present
summarize_payload(json.dumps({"api_key": S}), 1024)     -> secret present
redact_mapping({"pairs": [["api_key", S]]})             -> secret present
redact_mapping({"pairs": ("api_key", S)})               -> secret present

# F3 key spellings (baseline, structural) — 9 of 10 survive
x-api-key, db_password, operator-token, accessToken, clientSecret,
senderEmail, customerEmail, key, authentication            -> 9 leaks
{"api\u005fkey": S}                                        -> survives

# F6 URL ordering (baseline) — the host is destroyed
redact_text("postgresql://rfquser:pw_DB_9f8e7d6c5b@db.internal:5432/rfq")
  -> "postgresql://rfquser:[REDACTED]:5432/rfq"

# F7/F8/F9 (baseline)
max_bytes=128, payload "€"*500     -> 366 bytes
max_bytes=128, payload "😀"*500     -> 485 bytes
max_bytes=-1                        -> full payload inlined
payload {"body": "\ud800"}          -> UnicodeEncodeError

# F11 (baseline)
LogFields(message="api_key="+S): dump / dump_json / repr / str / f-string -> all leak
Settings(database.url=…, groq.base_url=…): repr / str / dump / json -> all leak
Settings.groq.api_key: clean in every rendering (F3-B holds)

# F13 (baseline) — pathological backtracking
_SECRET_ASSIGN_RE.sub on "api_key"*50   (350 chars) -> 0.013 s
                          "api_key"*100  (700 chars) -> 0.091 s
                          "api_key"*200 (1400 chars) -> 0.727 s
                          "api_key"*300 (2100 chars) -> 2.402 s
                          "api_key"*400 (2800 chars) -> 6.450 s

# F14 (baseline)
redact_mapping({1: "a"})              -> AttributeError: 'int' object has no attribute 'strip'
redact_mapping({"n": b"api_key="+S})  -> b'api_key=gsk_SESSION_…'  (unredacted)
summarize_payload(cycle, max_bytes=64)-> ValueError: Circular reference detected
```

---

## 3. Revised design — `redact_text`

### 3.1 Rule order (normative)

```text
1. _strip_surrogates      replace lone surrogates with U+FFFD
2. _URL_RE                scheme://userinfo@host  -> redact the password only
3. _AUTH_HEADER_RE        Authorization/api_key/x-api-key/token -> end of line
4. _redact_key_values     sensitive key + separator, value extracted separately
5. _BEARER_RE             Bearer <token> -> "Bearer [REDACTED]"
6. _EMAIL_RE              address -> [REDACTED]
7. idempotence            every rule is written so it cannot re-match its own output
```

Order is load-bearing: the URL rule must run **before** the email rule, because
`password@host` is e-mail-shaped. Baseline fails exactly here (F6).

### 3.2 Key policy (shared by text and structure)

```python
def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).casefold())

def _is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
```

* **Exact set** (normalised): `apikey apikeys apikeyid authorization
  authorizationcode auth authentication token tokens accesstoken refreshtoken
  sessiontoken secret secrets clientsecret password passwords passwd passphrase
  credential credentials operatortoken groqapikey senderemail customeremail
  email emailaddress useremail key privatekey secretkey signingkey encryptionkey
  accesskey accesskeyid secretaccesskey sessionid cookie setcookie jwt oauth
  bearer proxyauthorization`.
* **Suffix tuple** (normalised): `apikey`, `token`, `secret`, `password`,
  `passwd`, `credential`, `credentials`, `privatekey`.
* Benign words are **not** sensitive: `monkey`, `tokenizer`, `max_tokens`,
  `secretary`, `keyboard`, `public_key`, `hockey` (regression-tested).
* `key` alone is sensitive; `public_key` is not (normalised `publickey` matches
  neither list). This is a deliberate, documented asymmetry: `key` is the field
  name used by ad-hoc logs, while `public_key` is a known-benign identifier.

### 3.3 Key/value scanning (linear, no nesting swallow)

```python
_KEY_SEP_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])"
    r"(?P<key>[A-Za-z0-9_.\-]{1,64})"
    r"(?P<sep>(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?)"
)
_VALUE_RE = re.compile(r"(?!\[REDACTED\])[^\s\"',;}\]\)\\<>&]{8,}")
```

A single `finditer` pass over `_KEY_SEP_RE`; for each sensitive key, `_VALUE_RE`
is anchored at `match.end()`. Consequences:

* the key/separator match is **atomic** — it can never consume a preceding
  assignment (`note: x=password=…` redacts only `password=…`);
* quoted JSON keys (`"api_key": "…"`) and escaped quotes are matched, so flat
  JSON is redacted by the text rule as well as structurally;
* a `[REDACTED]` value is skipped by the negative lookahead, so the function is
  idempotent (`redact_text(redact_text(x)) == redact_text(x)`, verified over the
  whole matrix);
* cost is linear, one pass, bounded key length (≤ 64) — no backtracking on a
  repeated-token input (F13);
* value threshold is **8 characters**, not 12. Eight is the leak standard the
  review and the F3-B tests use; anything shorter cannot be a realistic
  credential for this system's providers. Trade-off accepted: `the key:Warsaw2024`
  is now redacted (over-redaction, safe direction); `the key: Warsaw` is not
  (value shorter than the threshold, and prose is preserved).

### 3.4 URL rule

```python
_URL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s]+)@(?=[A-Za-z0-9.\-]+(?::\d+)?(?:[/\s]|$))"
)
```

* `user:password@host` → `user:[REDACTED]@host` (host preserved);
* `token@host` (no colon) → `[REDACTED]@host` (the token is the credential);
* `user:p@ss@host` → `user:[REDACTED]@host` (greedy userinfo, last `@` wins);
* `sqlite:///var/rfq_agent.db` and `https://api.groq.com/openai/v1` are
  untouched (no userinfo);
* driver-qualified schemes (`postgresql+psycopg://`) are preserved verbatim.

Verified cases (expected == actual): eight URL shapes, including the
driver-qualified form and a password containing `@`.

### 3.5 What `redact_text` deliberately does not do

* It does not protect **short** secrets (≤ 7 chars) outside structural context.
* It does not redact a secret that is *split* across lines.
* It does not parse structure; structure is the job of §4. This separation is
  what keeps the text rule linear and side-effect free.

---

## 4. Revised design — structural walk

### 4.1 Public entry points

| Function | Input | Output | Notes |
| --- | --- | --- | --- |
| `redact_text(str)` | `str` | `str` | unchanged signature; `TypeError` on non-`str` |
| `redact_mapping(Mapping)` | mapping | `dict[str, Json]` | unchanged signature and return type |
| `redact_payload(Json)` | **any** JSON-ish root | redacted JSON-shaped value (`tuple`→`list`, `set`/`bytes`→`str`) | **new public function** |
| `summarize_payload(payload, *, max_bytes, label="payload")` | any root | summary dict | extended semantics (§5) |

`redact_payload` is the single correct entry point for arbitrary payloads; the
old pair (`redact_mapping` / `redact_text`) forced callers to guess the root
kind, which is how F2 happened.

### 4.2 Algorithm

```text
redact_payload(v)          = _redact_value(v, depth=0, seen=set())
redact_mapping(m)          = _redact_mapping(m, depth=0, seen=set())

_redact_value(v, depth, seen):
    if depth > 32:                      -> REDACTED
    if v is None or benign scalar:      -> v          # bool,int,float,Decimal,
                                                      # datetime,date,time,timedelta,
                                                      # UUID, Enum
    if str:                             -> _redact_string(v, depth)
    if BaseModel:                       -> _redact_value(v.model_dump(), depth+1, seen)
    if container (Mapping/list/tuple/set):
        if id(v) in seen:               -> REDACTED   # cycle
        seen.add(id(v)); try: … finally: seen.discard(id(v))
        Mapping                     -> _redact_mapping(v, depth+1, seen)
        set/frozenset               -> REDACTED       # no key context: fail closed
        pair (len == 2, v[0] is a sensitive key)
                                    -> [_redact_string(v[0]), REDACTED]
        otherwise                   -> [_redact_value(x, depth+1, seen) for x in v]
    bytes / other object:               -> _redact_string(str(v), depth+1)
    any exception                       -> REDACTED   # hostile __str__, exotic types
```

`_redact_mapping` coerces keys with `_coerce_key` (never raises), applies
`_is_sensitive_key` to the **raw** key, renames keys that change under
redaction to `[REDACTED_KEY]`, and appends `#2`, `#3`, … for collisions.

### 4.3 `_redact_string`

```text
1. depth > 32                                      -> REDACTED
2. len > 256 KiB and first char in { [ ( \ " '     -> REDACTED   (F5)
3. fully quoted (same first/last quote char)
       decode (JSON for ", literal otherwise)
       decoded str -> recurse one level; re-serialise only if changed
       -> redact_text(text) otherwise
4. redacted = redact_text(text)
5. not structured-looking (first char not in { [ ( ) -> returned as is
6. parse JSON (for { or [), else Python literal
       container -> _redact_value(..., set())
                    changed?  -> canonical_json / repr
                    unchanged?-> return the original text (formatting preserved)
7. structured-looking but unparseable             -> REDACTED   (fail closed)
```

Step 3 is what fixes double-encoded payloads (`'"{\\"api_key\\":\\"…\\"}"'`).
Step 6 is what fixes JSON-string, Python-literal and pair-array roots while
preserving benign formatting (`{ "model": "x" }` unchanged). Step 2 and step 7
are the over-cap / unparseable fail-closed policy.

### 4.4 Constants (single source of truth)

| Constant | Value | Rationale |
| --- | --- | --- |
| `_MAX_JSON_PARSE` | 256 KiB (characters) | above this the walk cannot prove safety; withholding is cheaper than parsing. Measured: 1 MB structured value → withheld in 0.009 ms |
| `_MAX_DEPTH` | 32 | deeper nesting carries no diagnostic value; bounds recursion |
| key length cap | 64 chars | bounds the key regex without excluding real names |
| value threshold | 8 chars | matches the leak standard |
| `REDACTED` | `[REDACTED]` | unchanged, tests depend on it |
| `REDACTED_KEY` | `[REDACTED_KEY]` | new, exported |

The reviewed proposal's separate `_MAX_AST_PARSE = 64 KiB` is **removed**: one
cap for all structured parsing is simpler and has no measured downside (a 689 KB
literal is rejected by the cap before `ast` sees it).

### 4.5 Fail-closed policy (explicit)

| Input | Behaviour | Why |
| --- | --- | --- |
| Structured text > 256 KiB | whole string → `[REDACTED]` | a quoted/escaped key or nested value can hide from the text rules |
| Structured text ≤ 256 KiB that does not parse | whole string → `[REDACTED]` | cannot prove the absence of a nested secret |
| Prose > 256 KiB | `redact_text` applied, text preserved | linear rule, no structure to hide behind |
| `set` / `frozenset` | `[REDACTED]` | unordered, no key context |
| Cyclic container | cycle edge → `[REDACTED]` | never loop; verified on dict and list cycles |
| Object whose `__str__` raises | `[REDACTED]` | hostile content cannot break logging |
| `summarize_payload` processing failure | `WITHHELD` record | see §5.3 |

The false-positive cost is bounded and visible: a *payload string value* that
opens with a bracket is withheld when it is malformed or over cap. Plain prose —
including text that merely contains `key:` or parentheses — is unaffected
unless it is the first character. `redact_text` (log-line filtering) is never
subject to this policy.

---

## 5. Revised design — `summarize_payload`

### 5.1 Order of operations

```text
validate max_bytes
serialize  = canonical_json(payload)          # digest domain, unredacted
encoded    = serialize.encode("utf-8", "surrogatepass")
digest     = sha256_bytes(encoded)            # identical to sha256_text for valid text
redacted   = redact_payload(payload)
redacted_s = canonical_json(redacted)
redacted_b = redacted_s.encode("utf-8", "surrogatepass")
INLINE      if len(encoded) <= max_bytes and len(redacted_b) <= max_bytes
TRUNCATED   otherwise, payload = truncate_utf8(redacted_s, max_bytes)
```

* **Redaction happens before truncation** (F1). A secret can never straddle the
  cut, because there is no secret left to straddle it.
* **Both** the original and the redacted serialisation must fit; if redaction
  *expands* the payload (`secret` → `[REDACTED]`), the result is `TRUNCATED`
  rather than an over-cap `INLINE` record.
* `TRUNCATED` cuts the **redacted** text by bytes. `INLINE` keeps the historic
  shapes: a `dict` for a mapping root, a JSON string for every other root.

### 5.2 Status vocabulary

`INLINE`, `TRUNCATED`, `WITHHELD` (new). No other value is ever emitted.

### 5.3 `WITHHELD` shape

```python
{
  "payload_status": "WITHHELD",
  "payload_bytes":  int | None,
  "payload_sha256": str | None,
  "payload":        None,
  "payload_error":  str,      # exception class name only, never its message
}
```

Two stages: a canonicalisation failure (cycle, hostile `__str__`) yields
`None/None`; a redaction/serialisation failure keeps the real `bytes`/`sha256`
so the event remains auditable. Verified with a cyclic payload. `Exception` is
caught, `BaseException` is not (no swallowing of interrupts).

### 5.4 Validation

`max_bytes` must be a non-boolean `int` ≥ 0; otherwise `ValueError`. Rejecting
`bool` matters because `True == 1` would otherwise silently produce a 1-byte
cap. There are **no production call sites** today (`grep` over `src/` is empty),
so the stricter contract cannot break a caller.

### 5.5 Digest compatibility

`sha256_bytes(serialized.encode("utf-8", "surrogatepass"))` equals the historic
`sha256_text(serialized)` for every payload whose serialisation contains no lone
surrogates — verified byte-identical for mapping, list, string, `Decimal` and
timezone-aware `datetime` roots (5/5). For surrogates the new code defines a
digest where the old code raised. Stored hashes therefore remain comparable.

---

## 6. Rendering surfaces

### 6.1 `LogFields`

```python
@model_serializer(mode="wrap")
def _redacted_serialization(self, handler, info) -> dict[str, Any]:
    return redact_mapping(handler(self))

def __repr__(self) -> str:
    return f"{type(self).__name__}({self.model_dump()!r})"

__str__ = __repr__
```

Evidence (prototype):

| Rendering | Before | After |
| --- | --- | --- |
| `model_dump()` (python + json) | leaks | clean |
| `model_dump(exclude_none=True)` | leaks | clean |
| `model_dump_json()` | leaks | clean |
| `repr` / `str` / f-string | leaks | clean |
| `TraceContext` ids and typed attributes | — | intact |
| double pass (`[REDACTED]]`) | — | none |

Two deliberate choices: redaction lives at **serialisation** time, not in a
validator, so `model_construct` (validation bypass) is still covered — verified;
and `__repr__` delegates to `model_dump()` so future fields are covered
automatically. `LogFields.attributes` is declared flat
(`dict[str, str | int | float | bool | None]`), so nested attribute dicts are
rejected by validation today; the serializer protects that boundary if the type
is ever widened.

### 6.2 Configuration sections

| Field | Today | Change |
| --- | --- | --- |
| `DatabaseSettings.url` | plain `str`; leaks in 6 renderings | `@field_serializer("url")` → `redact_text(value)` + `__repr__`/`__str__` |
| `GroqSettings.base_url` | plain `str`; leaks in 6 renderings | same |
| `GroqSettings.api_key` | `SecretStr` + serializer | **unchanged** (F3-B) |
| `HttpSettings.operator_token` | `SecretStr` + serializer | **unchanged** (F3-B) |
| `Settings` itself | pydantic repr delegates to sections | **unchanged**; no override needed |

Measured on the real classes with a patched copy of `config.py` in `/tmp`:

| Rendering | Baseline | Serializer only | `__repr__` only | Both |
| --- | --- | --- | --- | --- |
| `repr(settings)`, `str(settings)`, f-string | leak | leak | clean | clean |
| `model_dump()`, `mode="json"`, `model_dump_json()` | leak | clean | leak | clean |

So the serializer fixes the machine-readable path and `__repr__`/`__str__` fixes
the human path; **neither alone is sufficient**. With both, all seven renderings
are clean, the host/port/path stay visible
(`postgresql://rfquser:[REDACTED]@db.internal:5432/rfq`), the real values remain
reachable on the model, and the sqlite default renders unchanged.

### 6.3 The declared-class rule (pydantic)

Pydantic serialises a nested model using the **declared field schema**, not the
runtime class. Verified in this session:

```
serializer on a subclass, parent declares the base type   -> dump leak: True
serializer on the declared class                          -> dump leak: False
```

A `field_serializer` therefore only protects values if it sits on the exact class
annotated in `Settings`. This is a documentation trap: a well-meaning
`class ProdDatabaseSettings(DatabaseSettings)` with its own serializer leaves
`Settings.database` unprotected. Two rules follow:

1. Put the serializer on `DatabaseSettings` / `GroqSettings` themselves, never on
   a subclass and never on `Settings.database` as a field-level substitute.
2. Pydantic 2.14 raises `PydanticUserError: Multiple field serializer functions
   were defined for field 'url'` if a subclass redefines one, so an accidental
   second serializer fails loudly at import time. The remaining trap is the
   *serializer-less* base plus a patched subclass, which rule 1 forbids.

Tests must assert on `Settings(database=DatabaseSettings(...))`, not on the
section in isolation.

### 6.4 Database echo

`build_engine` passes `hide_parameters=True` to `create_engine`. Evidence:

* `engine.hide_parameters` is `True` (inspectable attribute);
* a failing statement whose bound parameter is a secret renders
  `[SQL parameters hidden due to hide_parameters=True]`;
* default `DatabaseSettings.echo` is `False`, but `RFQ_DATABASE__ECHO=true`
  exists, so the exposure is one environment variable away;
* SQLAlchemy ≥ 1.3.8 supports the flag; the project floor is `>= 2.0`;
* full suite green with the flag enabled (1884 passed).

### 6.5 What stays exactly as it is

`SecretStr` for `api_key`/`operator_token`, their serializers, `hide_input_in_errors`
on `Settings`, `_as_bool` not echoing the offending value, `redact_logs` default
and production enforcement, the demo CLI's
`render_as_string(hide_password=True)` header, and every F3-B test.

---

## 7. Exact implementation boundary

### 7.1 Files, functions, anchors

**A1 — primitives and `LogFields` (self-contained):**

| File | Anchor | Change |
| --- | --- | --- |
| `src/rfq_agent/observability/redaction.py` | module docstring | document the fail-closed policy and the capability table |
| same | imports | add `ast`, `json`, `datetime`, `decimal`, `enum`, `uuid`, `BaseModel`, `sha256_bytes` |
| same | `__all__` (l.25–31) | add `REDACTED_KEY`, `redact_payload` |
| same | constants (l.33–75) | replace `_SECRET_ASSIGN_RE`/`_SENSITIVE_KEYS` with `_URL_RE`, `_KEY_SEP_RE`, `_VALUE_RE`, `_SURROGATE_RE`, `_SENSITIVE_KEY_EXACT`, `_SENSITIVE_KEY_SUFFIXES`, `_SAFE_SCALARS`, caps |
| same | `redact_text` (l.83–88) | new pipeline (§3.1) |
| same | `redact_mapping` (l.91–103) | delegate to `_redact_mapping(data, 0, set())` |
| same | `_redact_value` (l.106–114) | replace with the 3-argument walker (§4.2) |
| same | new private helpers | `_normalize_key`, `_is_sensitive_key`, `_redact_key_values`, `_redact_url`, `_strip_surrogates`, `_redact_string`, `_redact_mapping`, `_coerce_key`, `_looks_structured`, `_parse_container`, `_decode_quoted`, `_safe_changed`, `_truncate_utf8`, `_withheld` |
| same | `summarize_payload` (l.117–145) | rewrite per §5; keep the signature |
| `src/rfq_agent/observability/ids.py` | `LogFields` (l.116–130) | add `model_serializer`, `__repr__`, `__str__` |

Rough size: `redaction.py` 145 → ~300 lines; `ids.py` +12 lines. No public
signature breaks; no new dependency (stdlib only).

**A2 — rendering surfaces (depends on A1):**

| File | Anchor | Change |
| --- | --- | --- |
| `src/rfq_agent/config.py` | imports | `from rfq_agent.observability.redaction import redact_text` (no cycle: `observability` imports only `domain`) |
| same | `GroqSettings` (l.121–154) | `@field_serializer("base_url")`, `__repr__`, `__str__` |
| same | `DatabaseSettings` (l.224–245) | `@field_serializer("url")`, `__repr__`, `__str__` |
| `src/rfq_agent/persistence/engine.py` | `create_engine` (l.173–179) | `hide_parameters=True` |

`__repr__` implementation shape (both classes): iterate `type(self).model_fields`,
render `url`/`base_url` through `redact_text`, everything else with `repr`.
This keeps `Decimal`, `RetryPolicy` and other diagnostics exactly as pydantic
renders them today (verified: `retry=RetryPolicy(max_attempts=4, …)`).

### 7.2 Public API

* `redact_text`, `redact_mapping`, `summarize_payload`, `email_hash` — unchanged.
* `redact_payload`, `REDACTED_KEY` — new, exported in `__all__`.
  `test_package_surface::test_all_is_accurate` requires every exported name to
  exist; it currently **skips** for `rfq_agent.observability` (no `__all__`), so
  do not add an `__all__` there unless the exports are complete.

### 7.3 Out of bounds

No change to `contracts/`, `domain/`, `persistence/models/`, `persistence/writers.py`,
`demo/`, `seed/`, `migrations/`, or `pyproject.toml`. No schema, no migration, no
new dependency, no logging-sink wiring, no `TraceRecorder` implementation.

### 7.4 Ordering constraint

A2 **requires** A1. Using baseline `redact_text` for the config fields destroys
the host:

```
postgresql://rfquser:[REDACTED]:5432/rfq
```

because the email rule consumes `password@host`. The A2 tests must therefore
assert host preservation, and A2 must not be cherry-picked alone.

---

## 8. Test matrix

Placement follows the repository's existing layout; no new test module is
needed except a new class inside `test_observability.py`. Total ≈ 60 test
functions (more test cases after parametrisation), all offline.

### 8.1 `tests/unit/test_observability.py`

**`TestRedactText` (extend)**

| ID | Test | Pins |
| --- | --- | --- |
| T01 | `test_nested_assignment_keeps_outer_context` (`note: password = S` → `note: password = [REDACTED]`) | F1 |
| T02 | `test_assignment_after_assignment` (`note: x=password=S`) | F1 |
| T03 | `test_compound_key_spellings_are_redacted` (param: 9 spellings) | F3 |
| T04 | `test_benign_key_substrings_are_preserved` (param: 7 words) | F3 (FP guard) |
| T05 | `test_threshold_is_eight_characters` (7-char value preserved, 8-char redacted) | §3.3 |
| T06 | `test_redaction_is_idempotent` (param: all fixtures) | §3.3 |
| T07 | `test_url_userinfo_is_redacted_and_host_preserved` (param: 8 URLs) | F6 |
| T08 | `test_url_without_userinfo_is_untouched` (sqlite, Groq base URL) | F6 |
| T09 | `test_authorization_header_redacts_to_end_of_line` | compat |
| T10 | `test_lone_surrogates_are_replaced` | F9 |
| T11 | `test_non_str_input_is_a_type_error` | API |
| T12 | `test_adversarial_repetition_is_bounded` (`"api_key"*400`, generous 2 s bound, comment explaining the old 6.45 s) | F13 |

**`TestRedactMapping` (extend)**

| ID | Test | Pins |
| --- | --- | --- |
| T13 | `test_compound_keys_are_redacted_structurally` (param) | F3 |
| T14 | `test_escaped_json_key_is_redacted` (`api\u005fkey` via parsed JSON) | F3 |
| T15 | `test_non_string_keys_are_coerced` | F14 |
| T16 | `test_colliding_keys_get_a_stable_suffix` (`{1:…, "1":…}`) | F10 |
| T17 | `test_secret_in_key_becomes_redacted_key` | F10 |
| T18 | `test_bytes_values_are_redacted` | F14 |
| T19 | `test_sets_are_withheld` | §4.5 |
| T20 | `test_cycles_do_not_recurse_forever` | F14 |

**`TestRedactPayload` (new class)**

| ID | Test | Pins |
| --- | --- | --- |
| T21 | `test_list_root_is_redacted` | F2 |
| T22 | `test_tuple_root_is_redacted` | F2 |
| T23 | `test_json_string_root_is_redacted` | F2 |
| T24 | `test_python_literal_root_is_redacted` | F2 |
| T25 | `test_double_encoded_root_is_redacted` (JSON + repr forms) | F2 |
| T26 | `test_pair_arrays_are_redacted` (list/tuple/JSON) | F4 |
| T27 | `test_benign_pair_is_preserved` (`["X-120", 40]`) | F4 (FP guard) |
| T28 | `test_nested_object_under_sensitive_key_is_withheld` | F5 |
| T29 | `test_over_cap_structured_text_is_withheld` | F5 |
| T30 | `test_over_cap_prose_is_preserved_and_redacted` | F5 |
| T31 | `test_unparseable_structured_text_is_withheld` | F5 |
| T32 | `test_benign_json_formatting_is_preserved` | §4.3 |
| T33 | `test_pydantic_payload_is_redacted` (`ParseRequest` with a secret in a message) | §4.2 |
| T34 | `test_depth_cap_is_applied` | §4.4 |
| T35 | `test_hostile_str_fails_closed` | §4.5 |
| T36 | `test_redaction_is_idempotent_over_structural_payloads` | §4.3 |

**`TestSummarizePayload` (extend)**

| ID | Test | Pins |
| --- | --- | --- |
| T37 | `test_truncation_never_leaks_a_prefix` (existing F3A-01 shape) | F1 |
| T38 | `test_byte_cap_is_enforced_for_multibyte_text` (€, emoji) | F7 |
| T39 | `test_negative_max_bytes_is_rejected` (param: `-1`, `True`, `1.5`, `"128"`, `None`) | F8 |
| T40 | `test_zero_is_hash_only` | §5 |
| T41 | `test_surrogate_payload_is_summarized` | F9 |
| T42 | `test_digest_matches_legacy_hash` (5 root kinds) | §5.5 |
| T43 | `test_expansion_is_truncated_not_inlined` | §5.1 |
| T44 | `test_processing_failure_yields_withheld` (cycle; both stage shapes) | F14 |
| T45 | `test_status_vocabulary_is_closed` | §5.2 |
| T46 | `test_non_mapping_roots_keep_string_payload_shape` | §5.1 |
| T47 | `test_list_root_secret_does_not_survive` | F2 |

**`TestLogFieldsRendering` (new class)**

| ID | Test | Pins |
| --- | --- | --- |
| T48 | `test_dump_is_redacted` (python + json modes) | F11 |
| T49 | `test_json_is_redacted` | F11 |
| T50 | `test_repr_str_and_fstring_are_redacted` | F11 |
| T51 | `test_trace_ids_and_typed_values_survive` | compat |
| T52 | `test_model_construct_is_still_redacted` | §6.1 |
| T53 | `test_dump_is_idempotent` | §6.1 |

### 8.2 `tests/unit/test_config.py`

| ID | Test | Pins |
| --- | --- | --- |
| T54 | `test_database_url_is_not_rendered` (repr, str, f-string, dump, dump(json), dump_json) | F11 |
| T55 | `test_groq_base_url_is_not_rendered` (same six) | F11 |
| T56 | `test_rendering_preserves_host_and_path` | F6/A2 |
| T57 | `test_real_values_remain_reachable` (`url` attribute, `SecretStr.get_secret_value`) | compat |
| T58 | `test_defaults_render_unchanged` (sqlite path, `api.groq.com`) | FP guard |

Existing F3-B tests in this module (`TestValidationErrorDisclosure`,
`TestDisclosureFixKeepsDiagnostics`, subprocess demo/alembic tests) must pass
unchanged.

### 8.3 `tests/persistence/test_sqlite_config.py`

| ID | Test | Pins |
| --- | --- | --- |
| T59 | `test_engine_hides_parameters` (`build_engine(...).hide_parameters is True`) | F12 |
| T60 | `test_statement_error_does_not_echo_parameters` (failing statement, secret in the bound parameter) | F12 |

### 8.4 Suite-level evidence already obtained

The prototype (not a patch) was injected via `sitecustomize` and the full
offline suite run:

```
pytest -m "not live" -q
1884 passed, 3 skipped   (96 s)   # new redaction primitives
1884 passed, 3 skipped   (93 s)   # + hide_parameters=True
1884 passed, 3 skipped   (95 s)   # + hide_input_in_errors=True on DomainModel
```

The 107-case security matrix passed **107/107** in its final form, covering all
findings above plus idempotence, false-positive controls, URL shapes, pydantic
payloads and all `summarize_payload` statuses.

---

## 9. Compatibility, performance, and rejected alternatives

### 9.1 Existing tests that pin behaviour

| Test | Constraint | Verdict |
| --- | --- | --- |
| `TestSummarizePayload::test_large_payload_is_truncated_but_still_verifiable` | `len(str(payload)) <= 128`, `payload_bytes > 128` | holds: byte cap ⇒ ≤ 128 bytes ⇒ ≤ 128 chars |
| `test_small_payload_is_inlined` | mapping root returns a `dict` containing `X-120`; digest equals `sha256_text(canonical_json(…))` | holds |
| `test_summarized_payload_is_redacted` / `test_hash_is_stable_…` | secret absent; digest differs per value | holds |
| `TestRedactMapping` innocuous-field / `the key: Warsaw` / non-string passthrough | prose and short values preserved | holds (`the key: Warsaw` unchanged) |
| `test_config` F3-B subprocess tests (demo CLI, alembic) | no secret on stderr, diagnostics preserved, exit code 2 | holds |
| `test_package_surface::test_all_is_accurate` | every `__all__` name exists | holds (`redaction` gains two real names) |

### 9.2 Other compatibility constraints

* Python ≥ 3.11, line length 100, ruff ruleset unchanged; `except Exception`
  needs `# noqa: BLE001` (already the project convention).
* No new dependency: `ast`, `json`, `re`, `hashlib` (via `domain.values`).
* `redact_text` remains `str -> str`; `redact_mapping` remains
  `Mapping[str, Json] -> dict[str, Json]`.
* `summarize_payload` keeps its keyword-only `max_bytes` and `label`.
* `email_hash` unchanged (email hashing policy untouched).
* The `redact_logs` flag, `Settings` validators and production invariants are
  untouched.

### 9.3 Performance (measured, final prototype)

| Input | Operation | Time |
| --- | --- | --- |
| 1 MB prose | `redact_text` | 170 ms (0.16 µs/char) |
| 200 KB prose | `redact_text` | 33 ms |
| 100 KB `"a="` × 50 000 | `redact_text` | 72 ms (linear: 17.8 / 35.7 / 71.4 / 143.5 ms at 25 k / 50 k / 100 k / 200 k chars) |
| 64 KB `"api_key"` × 8 000 | `redact_text` | 2.9 ms (old rule: **6.45 s at 2.8 KB**) |
| 1 MB double-quoted JSON | `redact_payload` | 0.009 ms (withheld before parsing) |
| 256 KB JSON | `summarize_payload` | 33 ms |
| 5 000-key mapping | `summarize_payload` | 57 ms |
| 689 KB Python literal | `summarize_payload` | 49 ms |
| 1 000 shared references (DAG) | `summarize_payload` | 12.6 ms |
| 100 000-deep `[` … `]` | `redact_payload` | 12.6 ms → `[REDACTED]` |
| cycle | `summarize_payload` | 0.1 ms → `WITHHELD` |

### 9.4 Rejected alternatives (do not retry)

| # | Alternative | Why rejected |
| --- | --- | --- |
| D1 | Non-consuming `search` for any key-shaped token | quadratic: 18.3 s on 50 KB of `"a="` |
| D2 | One regex matching key + separator + *value* | swallows nesting (`note: x=password=…`), and its value class cannot express quoted or nested values |
| D3 | Heuristic gate (`_has_sensitive_assignment`) before failing closed over cap | **confirmed false negative**: `{"api\u005fkey": …}` above the cap matches no text hint, so the secret survived. Deterministic withholding replaced it |
| D4 | Config serializer only | `repr`/`str`/f-string still leak (measured) |
| D5 | Config `__repr__` only | `model_dump`/JSON still leak (measured) |
| D6 | Truncate, then redact (baseline) | exposes the secret prefix (F1) |
| D7 | Character-based cap | 366/485 bytes for 128 "characters" (F7) |
| D8 | Redact in `LogFields.__init__` / a validator | `model_construct` bypasses validation; a wrap serializer covers it (verified) |
| D9 | `DomainModel.hide_input_in_errors` in this change | one line, closes `str(ValidationError)` echo (suite green), but it is a different surface and `errors()` still carries input; deferred with a note (§11) |

---

## 10. Findings addressed outside the code path

### 10.1 `hide_input_in_errors` on `DomainModel` (deferred)

Measured: `str(ValidationError)` for `TraceContext(trace_id=<secret>)` embeds
the input (`"String should match pattern … input_value='gsk_SESSION_A_…'"`).
Setting `hide_input_in_errors=True` on `DomainModel` removes it and keeps the
suite green (1884 passed), and `e.errors()[0]` still exposes the input — so it
is a **log/traceback-path** mitigation, not a data-model guarantee. It is
deferred because (a) F3-B already covers `Settings`, (b) the leak requires
someone to stringify a `ValidationError`, and (c) it belongs with the eventual
error-rendering policy (`safe_validation_errors`). If the owner wants it in
A1, it is one line in `domain/values.py` plus one test; the design does not
depend on it.

### 10.2 Trace-row redaction remains unimplemented (unchanged)

`persistence/models/trace.py` documents `payload_json`/`args_json` as redacted
snapshots, but `writers.py` has no recorder and `TraceRecorder` has no
implementation. This revision does **not** claim to fix that; it supplies a
correct `summarize_payload` so that the eventual recorder cannot get it wrong by
construction. Recorded as a residual risk, not a finding closed here.

---

## 11. Essential vs deferred

### 11.1 Essential (A1 + A2)

1. `redaction.py`: the rules, the structural walk, byte-aware cap, fail-closed
   policy, `redact_payload`, `WITHHELD`.
2. `ids.py`: `LogFields` wrap serializer + `__repr__`/`__str__`.
3. `config.py`: `DatabaseSettings.url` and `GroqSettings.base_url` serializers +
   `__repr__`/`__str__` (A2, depends on A1).
4. `engine.py`: `hide_parameters=True`.

Rationale: these are the four places where a secret is *currently* rendered or
would be rendered by the next caller. Everything else is policy or wiring.

### 11.2 Deferred

* Log-sink wiring and the real `TraceRecorder` (`observability` has no call
  sites today).
* Trace-row redaction enforcement at the persistence boundary.
* PII inventory beyond email (`sender_name`, `subject`, `body_text`).
* `passphrase`/`key` wording policy beyond the normalised lists.
* `DomainModel.hide_input_in_errors` and `safe_validation_errors`.
* Streaming/redaction of > 256 KiB structured payloads (currently withheld).
* Localisation of the key vocabulary (Polish field names such as `hasło`).

---

## 12. Residual risks and unsupported formats

| Risk | Status |
| --- | --- |
| Structured payload > 256 KiB | withheld entirely; no preview, digest retained |
| Malformed bracket-leading payload string | withheld entirely (safe direction) |
| Truncated Python tuple repr inside a string, > cap | withheld; under cap it parses, or is withheld when unparseable |
| Secrets shorter than 8 characters in free text | not redacted (threshold) |
| Secret split across lines or with whitespace inside | not redacted as text; structural keys still are |
| Keys longer than 64 characters | not matched by the text rule; still caught structurally |
| Non-UTF-8 bytes / arbitrary binary objects | stringified via `str()`, then redacted; exotic types withhold |
| Localised or abbreviated key names (`hasło`, `pwd`, `klucz`) | not in the vocabulary (deferred) |
| PII that is not an email address | out of scope (deferred inventory) |
| `[REDACTED]` used as a literal value | indistinguishable from a redacted value; documented |
| A value that changes under redaction and collides with another key | deterministic `#n` suffix, no data loss |
| Secrets embedded in numbers (e.g. a numeric token) | not redacted (by design: numbers are business data) |

---

## 13. Acceptance criteria and verdict

The design is **ready for ACCEPT** when the owner agrees that:

1. `redact_payload` is the single entry point for arbitrary payloads, and
   `redact_text`/`redact_mapping` keep their signatures.
2. Deterministic fail-closed withholding (over cap or unparseable structured
   text) is acceptable, with the documented preview loss.
3. The value threshold is 8 characters and the normalised key vocabulary above is
   the intended policy.
4. Rendering surfaces are patched at the **declared class**, with both a
   serializer and `__repr__`/`__str__`.
5. A1 and A2 are separable, and A2 is not merged without A1.
6. `DomainModel.hide_input_in_errors` stays deferred.

The eventual implementation PR is acceptable when: all T01–T60 exist and pass;
the existing 1884 tests remain green; `ruff check` / `ruff format --check` pass;
no production call site bypasses `redact_payload`; and the two-unit split is
respected.

**Verdict: ACCEPT (design). Implementation NOT authorized.**
No code was modified, no tests were added, no commit or PR was created.

---

## 14. Provenance

* Baseline: `d5611eefab3b326e9ad5caa9789d892e69ce3875`; `git status` clean;
  PR #7 merged. Repository history is grafted (no parent commits), so `git show
  HEAD` lists the whole tree.
* Reference prototype: `/home/user/f3a_design_appendix_prototype.py`
  (non-normative, outside the repository).
* Test-only injection: `/tmp/f3a_swap/sitecustomize.py`,
  `/tmp/f3a_engine/sitecustomize.py`, `/tmp/f3a_hide/sitecustomize.py`.
* Patched config copy used for the rendering measurements (outside the
  repository): `/tmp/f3a_config_patched.py`.
* Commands: `pytest -m "not live" -q`; `time.perf_counter()` benchmarks on the
  prototype; `rg`/`grep` for call sites and import cycles.
* Note: `f3a-audit-report.md` and `f3a-independent-review.md` were not present
  in this workspace snapshot; findings were reconstructed from the session
  record and re-reproduced, as stated in §1.4.

---

**Status: Code modified NO · Tests added NO · Commits created NO · PR created NO ·
Implementation authorized NO.**
