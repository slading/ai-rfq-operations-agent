# Project 04 — Session B: Independent Adversarial Redaction QA

**Repository:** `slading/ai-rfq-operations-agent`
**Baseline verified:** `d5611eefab3b326e9ad5caa9789d892e69ce3875` (merge of PR #7 `fix/f3b-sensitive-config-errors`), clean working tree, branch `arena/1190adbb-ai-rfq-operations-agent`.
**Scope:** `src/rfq_agent/observability/redaction.py`, its serialization dependencies (`domain/values.py: canonical_json`), config error rendering (`config.py`), exception rendering (`contracts/errors.py`), and `tests/unit/test_observability.py`.
**Method:** black-box + white-box probing with synthetic secrets only (`gsk_FAKEsupersecret0123456789`, `hunter2`, `jan.kowalski@example.com`). No real credentials were used or found. Session A was not consulted.
**Mode:** read-only. No tracked files modified. Artefacts live outside the repo in `/home/user/qa-session-b/`.

---

## 0. Executive summary

The redaction choke point is **regex-driven, line-oriented, and assumes `key=value` / `key: value` syntax**. It works for the exact shapes its own tests cover and fails for almost every serialised form a real log line or trace payload actually takes: JSON, Python `repr`, quoted dotenv values, header pair arrays, URL userinfo, and anything truncated. In addition, two of the four regexes have **super-linear worst-case complexity** that a hostile customer email can trigger.

Headline numbers from the regression module I wrote (`test_redaction_adversarial.py`, stdlib + pytest only):

| | |
|---|---|
| Tests encoding expected-safe behaviour | **89** |
| Failing on baseline | **84** |
| Deterministic fuzz: `<sensitive-key><sep><secret>` shapes that leak | **431 / 800 (54 %)** |
| Catastrophic-backtracking inputs found | 2 regexes (`_SECRET_ASSIGN_RE`, `_EMAIL_RE`) — 48 KB input ⇒ 14–18 s |

**Important reachability caveat:** at `d5611eef`, `redact_text`, `redact_mapping` and `summarize_payload` have **no production callers** — only tests import them. The trace writer does not yet route through them. Every finding below is therefore *latent* today and becomes *live* the moment Phase 5 wires the writer/logging handler through this module. That is exactly when a regression suite must already exist.

---

## 1. Repository state verification

```
$ git rev-parse HEAD            → d5611eefab3b326e9ad5caa9789d892e69ce3875
$ git status --short            → (clean)
$ grep -rn "summarize_payload\|redact_text\|redact_mapping" src/ | grep -v redaction.py
                                → (no results: zero production callers)
$ pytest tests/unit/test_observability.py  → 39 passed
$ python -c "import hypothesis" → ModuleNotFoundError  (property tests must be stdlib-only)
```

Environment: Python 3.11.2, pydantic 2.14.1, project installed into a throwaway venv at `/tmp/venv` (not in the repo).

---

## 2. Code under test (relevant excerpts and their consequences)

| Component | Behaviour | Consequence |
|---|---|---|
| `_AUTH_HEADER_RE` `\b(authorization\|api[-_]?key\|x-api-key\|token)(\s*[:=]\s*)([^\r\n]+)` | Eats to end of line | Over-redaction of business text; `\b` before `api` fails for `GROQ_API_KEY`, quoted keys `"api_key":` never match because `"` sits between key and `:` |
| `_SECRET_ASSIGN_RE` `(?<![A-Za-z0-9])([A-Za-z0-9_\-]*(?:api[-_]?key\|secret\|token\|password\|passwd\|credential\|key)[A-Za-z0-9_\-]*)(\s*[:=]\s*)([A-Za-z0-9._\-/+=]{12,})` | Value must start immediately with a value-class char and be ≥12 | Quoted values, short passwords, Basic auth, PEM, CSV all leak; `*`…keyword…`*` with lookbehind is O(n²) |
| `_BEARER_RE` `\bbearer\s+[A-Za-z0-9._\-/+=]{8,}` | Bearer only | `Basic`, `Digest`, `Token`, `ApiKey` schemes not handled; tokens < 8 chars leak |
| `_EMAIL_RE` `\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b` | ASCII only, trailing `\b` | IDN / Unicode local parts leak; `email_` or `emailż` leaks; O(n²) on dotted runs |
| `_SENSITIVE_KEYS` | Exact match after `strip().lower()` | `x-api-key` is in the regex **but not in the key set**; `cookie`, `set-cookie`, `proxy-authorization`, `secret_key`, `private_key`, `*_password` all leak wholesale |
| `redact_mapping` | `key.strip()` | `AttributeError` on any non-`str` key (int, None, tuple) — the choke point crashes |
| `_redact_value` | handles `str`, `Mapping`, `list`, `tuple` | `set`, `frozenset`, `bytes`, `bytearray`, pydantic models, exceptions pass through verbatim |
| `summarize_payload` non-mapping path | `redact_text(canonical_json(payload))` | Canonical JSON has quoted keys ⇒ regexes never fire ⇒ list payloads leak |
| `summarize_payload` TRUNCATED path | `redact_text(serialized[:max_bytes])` | Same quoted-key problem **plus** char slicing vs byte accounting, plus prefix leak when cut lands inside a value |
| `canonical_json(default=str)` | `str()` fallback | A non-JSON object is stringified; `SecretStr` renders `**********` (safe); arbitrary objects render their `__str__` |

---

## 3. Confirmed findings

Severity scale: **Critical** = secret/PII written to durable storage under normal operation once wired; **High** = leak under common but non-default input, or availability impact; **Medium** = leak requires unusual input or has mitigating controls; **Low** = correctness/robustness issue with security adjacency.
Reachability: *Latent* = no caller today; *Live* = reachable at baseline.

### B-01 — `summarize_payload` TRUNCATED and non-mapping paths leak sensitive values — **Critical / Latent / Reproduced**

The INLINE mapping path redacts structurally. The other two paths redact the **canonical JSON text**, where keys are quoted (`"api_key":"…"`). None of the regexes match a quoted key, so the value survives.

```python
>>> summarize_payload({"api_key": SECRET, "body": "x"*2000}, max_bytes=200)["payload"]
'{"api_key":"gsk_FAKEsupersecret0123456789","body":"xxxxxxxxxx…'
>>> summarize_payload([{"api_key": SECRET}], max_bytes=1024)["payload"]
'[{"api_key":"gsk_FAKEsupersecret0123456789"}]'
```

The existing test `test_summarized_payload_is_redacted` only exercises INLINE + mapping, so it cannot catch this. Note also that truncation can cut *inside* a secret and leave a usable prefix (`'{"a_token":"gsk_FAKEsupers'`).

**Expected safe behaviour:** redact structurally first, serialise second, truncate the *redacted* serialisation. Hash remains over the unredacted canonical JSON (the existing invariant is fine).

### B-02 — Quoted-key syntaxes (JSON, repr, dotenv-quoted) are never redacted — **Critical / Latent / Reproduced**

```python
>>> redact_text(json.dumps({"password": SECRET}))     → '{"password": "gsk_FAKE…"}'
>>> redact_text(repr({"token": SECRET}))              → "{'token': 'gsk_FAKE…'}"
>>> redact_text(f'GROQ_API_KEY="{SECRET}"')           → 'GROQ_API_KEY="gsk_FAKE…"'
>>> redact_mapping({"raw": json.dumps({"api_key": SECRET})})  → leaks
```

This is the most likely real-world shape: `ParseFailure.raw`, httpx request dumps, `str(exc)` of SDK errors, and `.env` echo all look like this. Deterministic fuzz over 7 keys × 8 separators × 6 wrappers × 10 prefixes: **431/800 leak**.

### B-03 — `_SENSITIVE_KEYS` drifts from the regex keyword set and misses header spellings — **High / Latent / Reproduced**

```python
>>> redact_mapping({"x-api-key": SECRET})          → leaks   # in regex, not in key set
>>> redact_mapping({"X-API-KEY": "short1"})        → leaks
>>> redact_mapping({"cookie": …}) / {"set-cookie"} / {"proxy-authorization"} / {"secret_key"} / {"private_key"} / {"aws_secret_access_key"} / {"db_password"} / {"api key"} / {"api_key:"}  → all leak
```

Because values under a non-sensitive key fall through to `redact_text(value)`, and a *bare* value has no `key=` prefix, **any short or non-value-class secret under an unlisted key leaks**. Recommendation: match key *substrings/tokens* (`api_key`, `token`, `secret`, `password`, `passwd`, `credential`, `authorization`, `cookie`, `private_key`, `email`) after normalising `[-_ :]`, not exact strings.

### B-04 — Pair arrays, name/value dicts, sets, bytes pass through — **High / Latent / Reproduced**

```python
>>> redact_mapping({"headers": [["api_key", "short1"], ["Authorization", "Basic dXNlcjpwYXNz"]]})  → both leak
>>> redact_mapping({"headers": [{"name": "api_key", "value": "abc"}]})                              → leaks
>>> redact_mapping({"s": {f"token={SECRET}"}})  / frozenset                                          → leaks (not recursed)
>>> redact_mapping({"b": f"token={SECRET}".encode()}) / bytearray                                    → leaks (passthrough)
```

`[["Authorization", "Bearer …"]]` only passes because of the Bearer regex; the pair-array shape itself is not understood. httpx/requests/ASGI header lists are exactly this shape.

### B-05 — Catastrophic backtracking in `_SECRET_ASSIGN_RE` and `_EMAIL_RE` — **High / Latent (will be Live on untrusted email bodies) / Reproduced**

Measured with `redact_text` on a single string (Python 3.11.2):

| Input | 3 KB | 12 KB | 48 KB |
|---|---|---|---|
| `"key"*n + "="` | 54 ms | 870 ms | **14.2 s** |
| `"a-"*n + "key"` | 71 ms | 1 090 ms | **17.9 s** |
| `"a.b"*n` (email rule) | 7 ms | 105 ms | 1.7 s |
| `"a1."*n + "@b."` | 7 ms | 105 ms | 1.7 s |
| plain prose 432 KB | — | — | 65 ms (linear) |

Scaling is ~16× per 4× input ⇒ O(n²). A 1 MB email body (well within normal attachment-less mail) extrapolates to hours. The module docstring explicitly says untrusted customer content passes through here; a single malicious RFQ email could stall the trace writer / request thread. The 300 s wall-clock timeout on my first probe batch was this.

Root cause: `[A-Za-z0-9_\-]*(?:…keyword…)[A-Za-z0-9_\-]*` with a negative lookbehind rather than an anchored token; and `[A-Za-z0-9._%+\-]+@` which rescans from every word boundary. Fix shape: tokenise on `[^A-Za-z0-9_\-]` first and test each token, or use a possessive-style construction (`(?>…)` / `*+` are available in Python 3.11 `re`), plus an input length cap at the call boundary.

### B-06 — URL userinfo credentials are not handled — **High / Latent / Reproduced**

```python
>>> redact_text(f"postgresql://rfq:{SECRET}@db:5432/rfq")   → leaks
>>> redact_text(f"redis://:{SECRET}@cache:6379/0")           → leaks
>>> redact_text(f"https://user:{SECRET}@api.example.com/")   → '[REDACTED]' — but ONLY because the
                                                                email regex matched 'secret@api.example.com'
```

The apparently-passing HTTPS case is an accident of the email regex and fails for any host without a TLD-shaped suffix. `DatabaseSettings` DSNs and future SMTP/broker URLs will go through logs on connection failure.

### B-07 — Short / non-value-class secrets under explicit secret keys leak — **Medium / Latent / Reproduced**

```python
>>> redact_text("password: hunter2")              → leaks   (<12 chars)
>>> redact_text("passwd=secret99")                → leaks
>>> redact_text("Bearer abc1234")                 → leaks   (<8 chars)
>>> redact_text("Authorization: Basic dXNlcjpwYXNz")  → ok only because 'authorization' is in the header rule
>>> redact_text("private_key=-----BEGIN PRIVATE KEY-----\nMIIEvQ…")  → key body leaks (spaces/newlines)
>>> redact_text(f"api_key,{SECRET}")              → leaks   (CSV separator)
```

The 12-char floor is a deliberate false-positive guard, but it is applied even when the key is unambiguous (`password=`). Expected: when the key is in the explicit sensitive set, redact regardless of value length/charset.

### B-08 — Unicode normalisation and email coverage gaps — **Medium / Latent / Reproduced**

| Input | Result |
|---|---|
| `api_key＝SECRET` (U+FF1D) / `api_key：SECRET` (U+FF1A) | leaks |
| `ａｐｉ_ｋｅｙ=SECRET` (fullwidth letters) | leaks |
| `kontakt@przykład.pl`, `żółw@example.com` | leaks (ASCII-only email class; relevant for a PL deployment) |
| `"john doe"@example.com` | leaks |
| `jan@example.com_`, `jan@example.comż`, `jan@example.com0` | leaks (trailing `\b`) |
| `żjan@example.com` | partial: `jan@…` redacted, leading char stays — acceptable |
| `api\u200b_key=…`, `api_key\u00a0= …` | redacted (OK) |

Expected: NFKC-fold before matching (collapses fullwidth), use `\w`-aware or `[^\s@]+@[^\s@]+\.\S+` email class with explicit punctuation trimming instead of `\b`.

### B-09 — `summarize_payload` truncation measured in bytes, sliced in characters; `max_bytes<=0` accepted — **Medium / Latent / Reproduced**

```python
>>> r = summarize_payload({"body": "😀"*100}, max_bytes=50)
>>> len(r["payload"].encode())  → 173   # > max_bytes; the column budget is violated
>>> summarize_payload({"a":1}, max_bytes=0)   → {'payload_status': 'TRUNCATED', 'payload': ''}
>>> summarize_payload({"a":1}, max_bytes=-1)  → payload '{"a":1' (Python negative slice)
```

Contrast: `UntrustedText.from_text` in `domain/trust.py` does this correctly (encode → slice bytes → decode `errors="ignore"`). The two truncation helpers should share one implementation.

### B-10 — Lone surrogates crash `summarize_payload`; circular refs crash `redact_mapping` — **Medium / Latent / Reproduced**

```python
>>> summarize_payload({"body": "ab\udcffcd"}, max_bytes=100)
UnicodeEncodeError: 'utf-8' codec can't encode character '\udcff'
>>> d = {}; d["self"] = d; redact_mapping(d)   → RecursionError
>>> redact_mapping({1: SECRET})                → AttributeError: 'int' object has no attribute 'strip'
```

Mail bodies decoded with `surrogateescape` produce exactly this string. A crash inside the choke point means the caller either drops the audit row (append-only ledger gap) or — the dangerous default in `except: logger.exception(...)` handlers — logs the *raw* record. Expected: never raise; `encode("utf-8", errors="replace")`, `str(key)` for non-string keys, recursion depth guard.

### B-11 — Pydantic `ValidationError` carries the raw input through every rendering path — **High / Live (config) / Reproduced**

PR #7 set `hide_input_in_errors=True` on `Settings`. Verified:

| Path | Operator token / pasted secret present? |
|---|---|
| `str(ValidationError)` from `Settings(...)` incl. nested section errors | **hidden** ✔ |
| `str(ValidationError)` from `GroqSettings(...)` constructed directly | **leaks** (section models lack the flag) |
| `exc.errors()` (default `include_input=True`) | **leaks** |
| `exc.json()` | **leaks** |
| `exc.errors()` after `redact_mapping({"errors": …})` | **leaks** (key is `input`, not sensitive; value is bare) |
| model-level validator failure, `exc.errors()[0]["input"]` | **whole settings mapping incl. `http.operator_token`** |

Any structured-logging handler (`extra={"errors": exc.errors()}`), Sentry-style integration, or FastAPI exception handler in Phase 6 will use `.errors()`/`.json()`, not `str()`. Expected: `_ConfiguredModel.model_config` also sets `hide_input_in_errors=True`; `input` / `input_value` / `ctx` added to the sensitive key set; a test asserting `"gsk_" not in exc.json()`.

### B-12 — False positives destroy audit evidence — **Medium / Latent / Reproduced**

The header rule redacts to end-of-line and the assignment rule matches `key` as a *substring*:

```
'token: 40 units of X-120 to Warsaw by Friday, PO-2026-00123' → 'token: [REDACTED]'
'idempotency_key=ORD-2026-000123'   → 'idempotency_key=[REDACTED]'
'primary_key=RFQ-2026-000042'       → '[REDACTED]'
'Keyboard=KB-2026-BLACK-US', 'Turkey: ANKARA-WAREHOUSE-01', 'Hockey-Stick=SKU-00012345678' → redacted
'token_count: 1234567890123'        → redacted
'Bearer Instrument-2026-A'          → 'Bearer [REDACTED]'
```

For an *audit* table the reverse failure matters too: redacting identifiers makes `tool_calls.args_json` useless for replay/dispute. `redact_mapping({"token": 5})` also silently changes an int to the string `"[REDACTED]"`, which will fail a strict schema downstream.

### B-13 — Secret-bearing keys and non-string containers are not inspected — **Low / Latent / Reproduced**

```python
>>> redact_mapping({f"api_key={SECRET}": 1})     → key leaks
>>> redact_mapping({EMAIL: "value"})             → key leaks
>>> redact_mapping({"my_key": SecretStr(S)})["my_key"].get_secret_value()  → S  (object passthrough)
>>> redact_mapping({"error": DomainError("x", detail=f"Bearer {S}")})     → exception object passthrough;
                                                                             leaks if the sink calls str()
```

Keys from `dict(zip(names, values))`-style construction are plausible. `tuple` is converted to `list` (shape change) while `set`/`bytes`/objects are left alone — inconsistent policy.

### B-14 — `DomainError.__str__` interpolates `detail` verbatim — **Low / Live / Observed (by design)**

`str(DomainError("provider rejected", detail=f"Authorization: Bearer {S}"))` contains the secret; `redact_text` of that string does remove it. Not a bug in itself, but it means **every** `str(exc)` must route through `redact_text` — a convention, not an invariant. Worth a lint/test that `DomainError.detail` is never constructed from provider response bodies.

---

## 4. Things that worked (negative findings)

- INLINE mapping path with a sensitive key: redacted wholesale ✔
- Key match is case-insensitive and whitespace-trimmed ✔
- Nested dict / list / tuple recursion ✔
- `Authorization: Bearer …` in JSON string values (Bearer regex is key-agnostic) ✔
- `Client(api_key='…')`, `export GROQ_API_KEY=…`, `-e GROQ_API_KEY=…`, YAML `- api_key: …` ✔
- `GroqSettings`/`HttpSettings` `model_dump`, `repr`, `str`, `model_dump_json`, `canonical_json` all mask `SecretStr` ✔
- `hide_input_in_errors` on root `Settings` for `str(exc)` — PR #7's stated goal holds ✔
- Zero-width / NBSP inside keys do not evade (they split the key, but the suffix still matches) ✔
- `redact_text` is idempotent over 400 fuzz cases ✔
- Plain prose scales linearly (432 KB in 65 ms) ✔

---

## 5. Severity / reachability / evidence matrix

| ID | Finding | Severity | Reachability at `d5611eef` | Evidence |
|---|---|---|---|---|
| B-01 | TRUNCATED / non-mapping `summarize_payload` leaks | Critical | Latent (no caller) | 8 failing tests |
| B-02 | JSON / repr / quoted-dotenv never redacted | Critical | Latent | 12 failing tests + 431/800 fuzz |
| B-11 | `ValidationError.errors()/json()` echo input incl. operator token | High | **Live** (config load) | 2 failing tests + probe4 |
| B-05 | O(n²) regex on hostile input | High | Latent; will be Live on untrusted mail | 5 failing tests, timing table |
| B-03 | key-set drift (`x-api-key`, cookie, `*_password`, …) | High | Latent | 11 failing tests |
| B-04 | pair arrays / sets / bytes passthrough | High | Latent | 6 failing tests |
| B-06 | URL userinfo | High | Latent | 6 failing tests |
| B-07 | short / non-charset values under explicit keys | Medium | Latent | 6 failing tests |
| B-08 | Unicode / IDN / `\b` email edge | Medium | Latent (PL deployment ⇒ IDN plausible) | 9 failing tests |
| B-09 | byte-vs-char truncation, `max_bytes<=0` | Medium | Latent | 3 failing tests |
| B-10 | crashes on surrogates / non-str keys / cycles | Medium | Latent | 6 failing tests |
| B-12 | false positives wipe identifiers / change types | Medium | Latent | 7 failing tests |
| B-13 | keys and opaque objects not inspected | Low | Latent | 1 failing test |
| B-14 | `DomainError.__str__` interpolates detail | Low | Live | observed |

---

## 6. Proposed security invariants and how to test them without new dependencies

All of the following are implemented in `/home/user/qa-session-b/test_redaction_adversarial.py` (89 tests, pytest + stdlib only; Hypothesis is **not** installed and not needed).

| Invariant | Test style | Where |
|---|---|---|
| I1. For every `(key, sep, wrap, prefix)` combination, `redact_text(f"{key}{sep}{wrap}{S}{wrap}")` does not contain `S` | Deterministic fuzz: `random.Random(seed)` over fixed alphabets, 200 cases × 4 seeds; failure message prints the first 5 offending shapes so the seed reproduces | `TestDeterministicFuzz` |
| I2. `redact_text` is idempotent | Same fuzz corpus, `redact(redact(x)) == redact(x)` | `TestDeterministicFuzz` |
| I3. `summarize_payload(p, max_bytes=m)` never contains a sensitive value for any `m`, any payload type | Parametrised over sensitive keys × INLINE/TRUNCATED × mapping/list | `TestSummarizePayloadSerializationPaths` |
| I4. `len(stored.encode()) <= max_bytes` for TRUNCATED; `max_bytes <= 0` raises `ValueError` | Deterministic | `TestTruncationBoundary` |
| I5. `redact_text` on 48 KB hostile input < 200 ms and cost(4n) < 8·cost(n) | Timing with generous bounds (baseline is 70–90× over) | `TestRegexPerformance` |
| I6. `redact_mapping` never raises for any key type, surrogate strings, bytes, sets | Parametrised | `TestSensitiveKeyNormalisation`, `TestPairArrays`, `TestUnicode` |
| I7. `ValidationError.errors()` / `.json()` wrapped by `redact_mapping` / `redact_text` never contain the input | Deterministic | `TestExceptionRendering` |
| I8. Business identifiers in an allow-list survive unchanged | Deterministic | `TestFalsePositives` |

Baseline run: `84 failed, 5 passed in 39.8 s` (the 40 s is itself B-05). All failures are stable across runs.

If Hypothesis is ever added, I1/I2 translate directly to `@given(st.sampled_from(KEYS), st.sampled_from(SEPS), st.text(min_size=12))`; nothing in the current tests depends on it.

---

## 7. Prioritised test matrix (exact inputs → expected safe output)

Priority 1 = must pass before the module gains a production caller.

| Pri | ID | Input | Expected |
|---|---|---|---|
| 1 | B-01 | `summarize_payload({"api_key": S, "zz_body": "x"*4000}, max_bytes=200)` | `S ∉ str(result)`; `payload_status == "TRUNCATED"` |
| 1 | B-01 | `summarize_payload([{"api_key": S}], max_bytes=4096)` | `S ∉ str(result)` |
| 1 | B-01 | `summarize_payload({"a_token": S, …}, max_bytes=len('{"a_token":"')+16)` | `S[:8] ∉ str(result)` |
| 1 | B-02 | `redact_text(json.dumps({"password": S}))` | `'{"password": "[REDACTED]"}'` (or any string without `S`) |
| 1 | B-02 | `redact_text(repr({"token": S}))` | no `S` |
| 1 | B-02 | `redact_text(f'GROQ_API_KEY="{S}"')` | no `S` |
| 1 | B-11 | `GroqSettings(requests_per_minute_budget=S)` → `redact_mapping({"errors": e.errors()})` | no `S` |
| 1 | B-11 | same → `redact_text(str(e))` and `e.json()` | no `S` |
| 1 | B-05 | `redact_text("a-"*16000 + "key")` | completes < 200 ms |
| 1 | B-05 | `redact_text("a.b"*16000)` | completes < 200 ms |
| 1 | B-03 | `redact_mapping({"x-api-key": "short1"})` | `{"x-api-key": "[REDACTED]"}` |
| 1 | B-03 | `redact_mapping({"cookie": …})`, `{"set-cookie": …}`, `{"proxy-authorization": …}`, `{"secret_key": …}`, `{"private_key": …}`, `{"db_password": …}` | value `"[REDACTED]"` |
| 1 | B-10 | `redact_mapping({1: f"api_key={S}"})`, `{None: …}` | no exception, no `S` |
| 1 | B-10 | `summarize_payload({"body": "ab\udcffcd", "token": S}, max_bytes=100)` | no exception, no `S` |
| 2 | B-04 | `redact_mapping({"headers": [["api_key","short1"],["Authorization","Basic dXNlcjpwYXNz"]]})` | neither value present |
| 2 | B-04 | `redact_mapping({"headers": [{"name":"api_key","value":"abc"}]})` | `"abc"` absent |
| 2 | B-04 | `redact_mapping({"s": {f"token={S}"}})`, `frozenset`, `bytes`, `bytearray` | no `S` |
| 2 | B-06 | `redact_text(f"postgresql://rfq:{S}@db:5432/rfq")` | `postgresql://rfq:[REDACTED]@db:5432/rfq` |
| 2 | B-06 | `redact_text("postgresql://rfq:hunter2@db/rfq")` | `hunter2` absent |
| 2 | B-07 | `redact_text("password: hunter2")`, `"passwd=secret99"`, `"Bearer abc1234"` | value absent |
| 2 | B-07 | PEM block under `private_key=` | base64 body absent |
| 2 | B-09 | `summarize_payload({"body": "😀"*500}, max_bytes=64)` | `len(payload.encode()) <= 64` |
| 2 | B-09 | `summarize_payload({"a":1}, max_bytes=0)` | `ValueError` |
| 3 | B-08 | `redact_text("api_key＝"+S)`, `"api_key："+S`, fullwidth `ａｐｉ_ｋｅｙ=`+S | no `S` |
| 3 | B-08 | `redact_text("kontakt@przykład.pl")`, `"żółw@example.com"`, `'"john doe"@example.com'` | address absent |
| 3 | B-08 | `redact_text("jan@example.com_")`, `…comż`, `…com0` | `jan@example.com` absent |
| 3 | B-12 | `redact_text("idempotency_key=ORD-2026-000123")` etc. | unchanged |
| 3 | B-12 | `redact_text("token: 40 units … PO-2026-00123")` | `PO-2026-00123` present |
| 3 | B-13 | `redact_mapping({f"api_key={S}": 1, EMAIL: 2})` | neither in keys |
| 3 | I2 | fuzz corpus | `redact(redact(x)) == redact(x)` |

Reproduce everything:

```
python -m venv /tmp/venv && /tmp/venv/bin/pip install -e ".[dev]"
PYTHONPATH=src /tmp/venv/bin/pytest /home/user/qa-session-b/test_redaction_adversarial.py -q
```

---

## 8. Residual risk analysis

1. **Architecture risk, not just regex risk.** A text-first redactor can never be complete against serialised structures; it should be the *last* line, after structural redaction of mappings, pair arrays and pydantic models. The current `summarize_payload` inverts this for two of its three paths (B-01). Any fix that only widens regexes will reopen under the next serialisation format (YAML, form-encoded, multipart, logfmt).
2. **Prose secrets remain out of scope by design** (`test_prose_secrets_are_out_of_scope_for_the_pattern_rules`). That is defensible *only* while `SecretStr` is the sole holder of credentials. B-11 shows pydantic itself breaks that assumption via `ValidationError.errors()`; provider SDK exceptions (`groq.AuthenticationError` echoing the request) will too. A provider-prefix rule (`gsk_[A-Za-z0-9]{20,}`, `sk-…`, `ghp_…`) is a cheap, low-false-positive backstop and should be added regardless.
3. **Availability.** B-05 is exploitable by anyone who can send an RFQ email once the writer calls `redact_text` on bodies. An input length cap at the call site (e.g. redact only the first N KB and record `truncated=True`) is needed in addition to regex repair, because a linear regex over a 50 MB body is still a problem.
4. **Over-redaction is not free** for an audit ledger (B-12). The project's stated posture "over-redaction is the safe failure" is correct for secrets but conflicts with `tool_calls.args_json` being replayable. Recommend a *separate*, narrower redactor for structured tool args (key-based only, no free-text regex) versus the broad one for free-text logs.
5. **Crash-on-redact is a leak vector** (B-10). Whatever wraps the choke point must guarantee that an exception inside redaction results in `"[REDACTION_FAILED]"` being stored, never the raw payload. This should be an explicit test on the *caller* once it exists, not only on the module.
6. **Email hashing** is sound (`email_hash` normalises case/whitespace), but `_EMAIL_RE` as a detector is weaker than the `sender_email` validator in `domain/intake.py` (`^[^@\s]+@[^@\s]+\.[^@\s]+$`). The detector should be at least as permissive as the validator, otherwise an address that *entered* the system can leave it unredacted.
7. **Coverage debt in the existing suite.** `tests/unit/test_observability.py` has 39 passing tests and would have caught none of B-01…B-13. The suite tests the regexes' happy paths rather than the invariant "no secret survives", so it gives false assurance. The 89 tests in this session should be adopted (as `xfail(strict=True)` initially if the fix lands later) so the gap is visible in CI.

---

## 9. Artefacts

| Path | Purpose |
|---|---|
| `/home/user/qa-session-b/REDACTION_QA_REPORT.md` | this report |
| `/home/user/qa-session-b/test_redaction_adversarial.py` | 89 regression tests, stdlib + pytest only |
| `/tmp/probe1.py … probe5.py`, `/tmp/perf.py`, `/tmp/idem.py` | raw probes (ephemeral) |

No tracked files in `/home/user/ai-rfq-operations-agent` were modified; `git status` is clean. No commits, pushes, PRs or merges were made.
