# F3-A1 Design Revision 2 — Adversarial QA Remediation

**Project 04 / Session A — read-only design correction.**
Repository `slading/ai-rfq-operations-agent`, baseline `d5611eefab3b326e9ad5caa9789d892e69ce3875`.
Design under revision: commit `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`.
Adversarial gate: commit `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.

| Gate | Status |
| --- | --- |
| Application code modified | **NO** |
| Tracked files modified | **NO** |
| Tests added to the repository | **NO** |
| Commits created | **NO** |
| PR created / merged | **NO** |
| Implementation authorized | **NO** |

**Verdict: REVISED — 89/89 on the independent adversarial gate. Independent
confirmation requested.**

| Measurement | Result |
| --- | --- |
| QA suite on baseline `d5611eef` | 84 failed, 5 passed |
| QA suite on prototype revision 1 (committed design) | 31 failed, **58 passed** — reproduces the integration review exactly |
| QA suite on prototype revision 2 (this document) | **89 passed, 0 failed** |
| Project suite (unmodified tests) with revision 2 | 1881 passed, 3 failed, 3 skipped |
| Project module with the two documented test edits | **42 passed** (39 original + 3 new) |
| Extended matrix beyond the gate | **101/101** |

The three remaining project failures are **not** security failures; they are two
tests that pin the over-redaction behaviour this revision deliberately removes
(§12). No other project test changed behaviour.

> **I am not claiming ACCEPT.** The design passes the gate in a prototype that
> was injected at test time; it has not been implemented, and the gate is an
> acceptance *gate*, not a substitute for independent review. §16 lists what
> must be true before implementation is authorized, and §15 lists what this
> design still does not protect.

---

## 1. What changed since revision 1

| Area | Revision 1 (committed) | Revision 2 | Why |
| --- | --- | --- | --- |
| E-mail detection | `_EMAIL_RE`, a single backtracking regex | token-based detector: one pass over whitespace/delimiter tokens, anchored bounded match per token, plus a quoted-local pre-pass | C-1: the regex was O(n²) (1.7 s at 48 KB) |
| Key/value values | ≥ 8 credential characters for every key | two tiers: **strong** keys redact any non-empty value; **weak** (`key` only) keeps the credential-shape rule | C-2: `password=hunter2` leaked |
| Unicode | none | NFKC only when it changes the text, plus `\w`-based e-mail detection | C-3: full-width `＝`/`：` and IDN addresses leaked |
| Key vocabulary | missed `aws_secret_access_key`, `x-api-key` in some paths | explicit normalised set + suffix rule, includes `aws_secret_access_key` | C-4 |
| `{name, value}` records | not handled | `_credential_pair()` | C-4 |
| Header values | `_AUTH_HEADER_RE` ate to end of line for every key | bounded values; end-of-line only for `Authorization` / `Proxy-Authorization`; Bearer has a business-ID allowlist | C-5 |
| Bearer | `Bearer <8+ chars>` always redacted | allowlist for business-identifier shapes (`Instrument-2026-A`) | C-5 |
| ValidationError | out of scope | A1: `input`/`input_value` structured fields + provider-token backstop. A2: `hide_input_in_errors` | C-6 |
| `max_bytes` | `0` allowed (hash-only) | `max_bytes <= 0` raises `ValueError` | C-8 / QA `B-08` |
| Escaped JSON keys | covered structurally only | `\uXXXX` decoded for key normalisation in the text path too | C-8 |
| Sets / bytes | set withheld, bytes stringified via `repr` | sets recursed, `bytes`/`bytearray` decoded and redacted | QA `B-04` |
| Performance | 1 MB prose 170 ms; hint-free scans over every key match | cheap substring gate before the key scan; bounded patterns | C-1 |

Two behaviours were **added**, not just repaired, and both are backed by the QA
report's own recommendations:

* a **provider-token backstop** (`gsk_…`, `sk-…`, `ghp_…`, `AKIA…`, `xox…`,
  `hf_…`, `npm_…`) that catches a credential pasted into prose or into a
  provider SDK error message (QA report §8.2: *"should be added regardless"*);
* **structured error-field handling** for `input` / `input_value`, so pydantic
  `ValidationError.errors()` is safe for any secret shape, not only
  provider-shaped ones (C-6).

---

## 2. Method and evidence standard

* Baseline verified clean at `d5611eef`; the QA file and report were read from
  commit `28af34cb…` (`git show`), not from a summary.
* The QA suite was executed **unmodified**: 362 lines, 89 tests, stdlib +
  pytest only.
* All probes use synthetic credentials (`gsk_FAKEsupersecret0123456789`,
  `hunter2`, `jan.kowalski@example.com`). No real secret was read or printed.
* The prototype lives **outside** the repository and is installed at test time
  through a `sitecustomize.py` shim that rebinds four names on
  `rfq_agent.observability.redaction`. The repository was never modified:

  ```python
  import importlib, os
  _proto = importlib.import_module(os.environ.get("F3A_PROTO_MODULE", "f3a1_prototype"))
  import rfq_agent.observability.redaction as _r
  _r.redact_text = _proto.redact_text
  _r.redact_mapping = _proto.redact_mapping
  _r.summarize_payload = _proto.summarize_payload
  _r.redact_payload = _proto.redact_payload
  ```

* **Leak standard:** an intact run of **≥ 8 consecutive characters** of the
  synthetic secret. This is the same detector the F3-B config tests use.
* Reproduce everything:

  ```bash
  python -m venv .venv && .venv/bin/pip install -e ".[dev]"
  git show 28af34cb5e3cdb936d087a0df5b536dc23ee40c4:docs/reviews/f3a/test_redaction_adversarial.py \
      > /tmp/test_redaction_adversarial.py
  PYTHONPATH=src:<dir with f3a1-prototype.py and the shim> \
      .venv/bin/python -m pytest /tmp/test_redaction_adversarial.py -q
  ```

---

## 3. Blocking findings C-1 … C-6

### C-1 — the quadratic e-mail regex is replaced by a bounded detector

**Replacement.** `_EMAIL_RE` is deleted. E-mail detection becomes a single
`re.sub` over whitespace/delimiter tokens:

```python
_EMAIL_CANDIDATE_RE = re.compile(r"[^\s<>()\[\]{}\"',;]+")
_EMAIL_TOKEN_RE = re.compile(
    r"^[^\s<>()\[\]{}\"',;:@]{1,64}@(?:[^\s<>()\[\]{}\"',;:@.]+\.)+[^\s<>()\[\]{}\"',;:@.]{2,}"
)

def _redact_emails(text: str) -> str:
    def _one(m):
        token = m.group(0)
        return REDACTED if "@" in token and _EMAIL_TOKEN_RE.match(token) else token
    if '"' in text:
        text = _QUOTED_EMAIL_RE.sub(REDACTED, text)
    return _EMAIL_CANDIDATE_RE.sub(_one, text)
```

Every quantifier is bounded (`{1,64}` local part, `{2,}` TLD). The domain part is
a repeated `label.` group over a character class that cannot match `@`, so the
match cannot cross a token boundary.

**Measured (QA `TestRegexPerformance`, budget 200 ms):**

| Input | Baseline | Revision 1 | Revision 2 |
| --- | --- | --- | --- |
| `"a.b" * 16 000` (48 KB) | 1.7 s | fail | **0.47 ms** |
| `"a1." * 16 000 + "@b."` (48 KB) | 1.7 s | fail | **0.80 ms** |
| `"key" * 16 000 + "="` (48 KB) | 14.2 s | 1.6 ms | **1.59 ms** |
| `"a-" * 16 000 + "key"` (32 KB) | 17.9 s | fail (scale) | **1.11 ms** |
| Scaling `cost(8000) < 8 × cost(2000)` | fail | fail | **pass** |

The key/value scanner keeps the atomic `key`+separator match of revision 1, but
adds a cheap substring gate (`_key_might_be_sensitive`) so text without a
credential-ish word never enters the per-match loop:

| Input | Revision 1 | Revision 2 |
| --- | --- | --- |
| 1 MB prose | 170 ms | **10.9 ms** |
| 1 MB `"a=" * 500 000` | 72 ms / 100 KB | **10.8 ms** |

### C-2 — strong keys redact any non-empty value

Three tiers replace the single ≥ 8-character rule:

| Tier | Keys | Value rule |
| --- | --- | --- |
| Strong | `api_key`, `token`, `password`, `passwd`, `secret`, `client_secret`, `authorization`, `cookie`, `private_key`, `aws_secret_access_key`, `operator_token`, `jwt`, `oauth`, `bearer`, `input_value`, … (33 normalised names) | any **non-empty** value is redacted, regardless of length |
| Weak | `key` (bare) | value must look credential-ish: ≥ 8 credential-class chars, or ≥ 4 chars containing a digit |
| Everything else | — | untouched |

This is what makes `password=hunter2`, `passwd=secret99`, `api_key=abc12345`,
`token=x`, `secret=1` and `aws_secret_access_key=Zq7Xk2` all redact, while
`the key: Warsaw` survives (no digit, 6 characters) and `token_count: 1234567890123`
survives untouched (the normalised key is `tokencount`, which is neither strong
nor weak).

The weak tier is the deliberate compromise for the one key that is both an
API-credential label and an ordinary English noun. It is required by two QA
tests that pull in opposite directions (`key=abc123` must redact;
`the key: Warsaw` must not), and it is the only heuristic left in the text path.

### C-3 — Unicode, IDN and NFKC

* **Normalisation.** NFKC is applied **only when it actually changes the
  string**, and only to non-ASCII text:

  ```python
  if not work.isascii():
      work = _normalize_compat(_strip_surrogates(work))
  ```

  Consequences: plain ASCII is byte-identical to the input; precomposed Polish
  (ż, ó, ł, ę, ą, ś, ć, ń, ź) is NFKC-invariant, so benign Polish text is
  unchanged (verified: four benign strings returned exactly). Full-width
  `＝`, `：` and `ａｐｉ_ｋｅｙ` normalise to ASCII and are then caught by the
  ordinary rules.
* **Trade-off.** Text containing genuine compatibility characters (ligatures
  `ﬁ`, circled digits `①`, full-width forms) is stored in its NFKC form. The
  redacted copy is a log artefact, never a source of truth, so this is
  documented rather than avoided.
* **IDN e-mail.** The detector uses `\w`-style classes, which are Unicode-aware
  in Python 3, so `kontakt@przykład.pl`, `żółw@example.com` and
  `"john doe"@example.com` (quoted-local pre-pass) are all removed.
* **Trailing word characters.** `jan@example.com_`, `…comż`, `…com0` are removed
  because the detector is token-based and has no trailing `\b`.

### C-4 — `aws_secret_access_key` and `{name, value}` credentials

* `aws_secret_access_key` normalises to `awssecretaccesskey`, which is in the
  exact set **and** ends with the `accesskey` suffix — two independent reasons
  it is caught. Verified structurally and in text.
* Header/credential records of the form `{"name": <sensitive key>, "value": …}`
  are handled by `_credential_pair()`:

  ```python
  def _credential_pair(data):
      name = data.get("name")
      if isinstance(name, str) and _is_sensitive_key(name) and "value" in data:
          return "value"
  ```

  `{"name": "model", "value": "gpt"}` is untouched (verified).

### C-5 — bearer/header false positives

Two changes:

1. **Header values are bounded.** End-of-line redaction now applies only to
   `Authorization` and `Proxy-Authorization`, which are line-scoped by protocol.
   Every other key redacts up to the end of its value token, so
   `token: 40 units of X-120 to Warsaw by Friday, PO-2026-00123` keeps
   `PO-2026-00123`.
2. **Bearer gets a business-identifier allowlist.** A bare `Bearer` value that
   matches `^[A-Za-z][A-Za-z0-9]*(?:-\d{2,4})(?:-[A-Za-z0-9]+)+$` is left
   alone, because it is an artefact ID (`Instrument-2026-A`, `PO-2026-00123`),
   not a credential. The allowlist does **not** apply to `Authorization:`
   headers — those are always redacted to end of line.

  | Input | Result |
  | --- | --- |
  | `Bearer hunter2` | `Bearer [REDACTED]` |
  | `used bearer gsk_…` | `used bearer [REDACTED]` |
  | `Bearer Instrument-2026-A` | unchanged |
  | `Authorization: Basic dXNlcjpwYXNz` | `Authorization: [REDACTED]` |

Residual risk: a real bearer token whose text happens to match the artefact-ID
pattern (`ABC-2026-XYZ`) would not be redacted by the Bearer rule. It is still
caught if it appears under a header key or a sensitive key. §15 records this.

### C-6 — ValidationError hardening split across A1 and A2

| Layer | Owner | Mechanism | Acceptance criterion |
| --- | --- | --- | --- |
| Structured error fields | **A1** | `input` and `input_value` are in the sensitive-key set; `input_value` is a strong text key; `_redact_string` walks list/dict error records | `redact_mapping({"errors": e.errors()})` contains no intact ≥ 8-char fragment for **any** secret shape |
| Exception message | **A1** | provider-token backstop (`gsk_…` etc.) **and** the `input_value='…'` key/value form | `redact_text(str(e))` is clean for provider-shaped and for `input_value`-shaped secrets |
| Raw exception | **A2** | `DomainModel.model_config["hide_input_in_errors"] = True` | `str(ValidationError)` from any `DomainModel`/`Settings` construction leaks no input; diagnostics preserved; `.errors()` still structured |

Measured for A1, three secret shapes:

| Secret | `redact_text(str(e))` leak | `redact_mapping({"errors": e.errors()})` leak |
| --- | --- | --- |
| `gsk_FAKEsupersecret0123456789` | no | no |
| `hunter2` | no | no |
| `correct-horse-battery` | no | no |

Measured for A2 (an in-memory `model_config` override, no file changed):

| | `str(e)` leak | diagnostics preserved | `errors()[0]["input"]` present |
| --- | --- | --- | --- |
| `DomainModel` today | **yes** | yes | yes |
| `DomainModel` + `hide_input_in_errors` | **no** | yes | yes |

`.errors()` continues to carry the raw input by design: the exception object is
not a rendering boundary. A2's guarantee is that *rendered* forms are safe, and
A1's `redact_*` remains the second layer for `.errors()`.

---

## 4. Required decisions 1 … 9

| # | Decision | Resolution |
| --- | --- | --- |
| 1 | Replace the quadratic e-mail regex | Done. Token-based detector; 48 KB hostile inputs 0.47–1.59 ms against a 200 ms budget (§3 C-1) |
| 2 | Redact non-empty values under strong keys regardless of length | Done. Three-tier vocabulary; `password=hunter2`, `token=x`, `secret=1` redact (§3 C-2) |
| 3 | Unicode/IDN + NFKC without disturbing benign text | Done. NFKC only when it changes the text; ASCII and precomposed Polish byte-identical; IDN and quoted-local addresses removed (§3 C-3) |
| 4 | `aws_secret_access_key`; `{name, value}` credential dicts | Done (§3 C-4) |
| 5 | Bearer/header false positives, with test changes documented | Done. Bounded values, EOL limited to true headers, business-ID allowlist. Two existing test functions updated (§12) |
| 6 | ValidationError hardening to A2 with acceptance criteria; structured error fields in A1 | Done (§3 C-6, §10) |
| 7 | Adopt the QA suite as the F3-A1 acceptance gate | Done. 89/89; adopted verbatim, no edits, no `xfail` (§13) |
| 8 | `max_bytes=0`; escaped JSON keys | `max_bytes <= 0` and non-`int`/`bool` raise `ValueError`; `\uXXXX` escapes are decoded for key normalisation on both text and structural paths (§9) |
| 9 | Separate F3-A1 / F3-A2 boundaries | Done (§11) |

---

## 5. Normative design — `redact_text`

### 5.1 Pipeline

```text
0. type check                     str only, else TypeError
1. Unicode pre-pass               non-ASCII only: replace lone surrogates, then NFKC
                                  if (and only if) NFKC changes the string
2. PEM blocks                     -----BEGIN …----- … -----END …-----   -> [REDACTED]
3. URL userinfo                   scheme://user:password@host           -> scheme://user:[REDACTED]@host
                                  scheme://token@host                   -> scheme://[REDACTED]@host
4. Header keys (EOL)              authorization / proxy-authorization  -> rest of line
5. Key + value scan               key, separator (= : ,), optional quotes/wrappers,
                                  bounded value; strong tier any length, weak tier shape-tested
6. Bearer                         Bearer <credential> -> Bearer [REDACTED]
                                  (business-ID-shaped tokens exempt)
7. Provider tokens                gsk_ / sk- / ghp_ / AKIA / xox… / hf_ / npm_ -> [REDACTED]
8. E-mail                         token pass + quoted-local pass        -> [REDACTED]
```

Order is load-bearing and each step is gated by a cheap `in` test so lines that
cannot contain the construct skip the regex entirely.

### 5.2 Key/value scanner

```python
_KEY_SEP_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])"
    r"(?P<key>(?:[A-Za-z0-9_.\-]|\\u[0-9a-fA-F]{4}){1,64})"
    r"(?P<sep>[\"']?\s*[:=,]\s*)"
)
_VALUE_SKIP   = " \t\"'<({[\\"          # wrappers and quotes after the separator
_VALUE_END    = " \t\r\n\"',;)]}>&"     # value terminators
_VALUE_CLOSERS= "\"')]}>"               # paired with the openers actually skipped
```

* The key+separator match is atomic: it can never swallow a preceding
  assignment, so `note: x=password=…` redacts only `password=…`.
* Separators are `=`, `:`, `,` (comma covers CSV and `-e key,value`).
* Quotes and wrappers on both sides of the separator are tolerated, so JSON
  (`"api_key": "…"`), Python `repr` (`'token': '…'`), `<…>`, `{…}`, `[…]` and
  fuzz shapes all work.
* A trailing closer is consumed only for each opener actually skipped, so JSON
  structure is preserved: `{"api_key": "…", "n": 3}` keeps its `}`.
* Values that already begin with `[REDACTED]` are skipped, which is what makes
  the function idempotent.

### 5.3 Complexity argument

* `_KEY_SEP_RE` has a negative lookbehind that fails immediately inside a run of
  key characters. Only the first position of a run can attempt a match, so the
  scan is O(n) with a 64-character constant, not O(n²). Measured on the three
  adversarial shapes from `B-09`.
* The value loop is a single cursor walk; replacements are collected in a list
  and joined once.
* The e-mail detector is one `sub` over tokens plus one anchored match per
  token; the local part is capped at 64 characters, so no token can trigger
  backtracking across itself.
* Every regex quantifier in the module is bounded (`{0,31}`, `{1,64}`,
  `{0,65536}`, `{1,512}`).

---

## 6. Key vocabulary

One normaliser serves text and structure:

```python
def _normalize_key(key):
    return re.sub(r"[^a-z0-9]+", "", _decode_key_escapes(str(key)).casefold())
```

| Set | Contents (normalised) | Purpose |
| --- | --- | --- |
| `_SENSITIVE_KEY_EXACT` | 46 names: `apikey apikeys apikeyid authorization authorizationcode auth authentication token tokens accesstoken refreshtoken sessiontoken secret secrets clientsecret password passwords passwd passphrase credential credentials operatortoken groqapikey senderemail customeremail email emailaddress useremail key privatekey secretkey signingkey encryptionkey accesskey accesskeyid secretaccesskey awssecretaccesskey sessionid cookie setcookie jwt oauth bearer proxyauthorization input inputvalue` | structural redaction, wholesale |
| `_SENSITIVE_KEY_SUFFIXES` | `apikey token secret password passwd credential credentials privatekey accesskey` | compound spellings: `x-api-key`, `db_password`, `aws_secret_access_key`, `clientSecret` |
| `_STRONG_TEXT_KEYS` | 36 names, the subset that is unambiguous in prose | any non-empty value redacted in free text |
| `_WEAK_TEXT_KEYS` | `key` | value redacted only when credential-shaped |

**Deliberately absent:** bare `key` as a suffix, and anything that would make
`monkey`, `turkey`, `hockey`, `keyboard`, `primary_key`, `idempotency_key`,
`tokenizer`, `secretary`, `public_key`, `max_tokens` or `token_count`
sensitive. All eleven are pinned by tests in both suites.

---

## 7. Structural walk

Unchanged from revision 1 except for the listed repairs:

```text
redact_payload(v) -> _redact_value(v, 0, set())

depth > 32                         -> [REDACTED]
None / bool / int / float / Decimal / datetime / UUID / Enum -> unchanged
str                                -> _redact_string
BaseModel                          -> model_dump then recurse
bytes / bytearray                  -> UTF-8 decode with replacement, then _redact_string   (new)
Mapping                            -> _redact_mapping (key policy, [REDACTED_KEY], #n)
set / frozenset                    -> recurse into items, return a list                    (changed)
2-element seq with sensitive [0]   -> [REDACTED, REDACTED]                                 (pair)
cycle (id already on the path)     -> [REDACTED]
other object                       -> _redact_string(str(v))
any exception                      -> [REDACTED]
```

`_redact_mapping` additions: non-`str` keys are coerced (never raises),
`{name, value}` credential records are detected, and a key that changes under
redaction becomes `[REDACTED_KEY]` with a deterministic `#2`, `#3` suffix for
collisions.

---

## 8. `summarize_payload`

```text
validate max_bytes          int, not bool, > 0      else ValueError
serialize (unredacted)      canonical_json -> sha256_bytes(utf-8, surrogatepass)
redact                      redact_payload
serialize (redacted)
INLINE     if original <= max_bytes and redacted <= max_bytes      (mapping root -> dict,
                                                                    otherwise -> JSON string)
TRUNCATED  otherwise: cut the *redacted* bytes with surrogatepass + ignore
WITHHELD   on any processing failure: payload None, error class name only
```

Changes from revision 1:

* **`max_bytes=0` now raises `ValueError`** (QA `B-08`). Revision 1 allowed
  `0` as hash-only; the QA gate treats that as ambiguous, so the stricter
  contract wins. There are **no production callers** of `summarize_payload` at
  `d5611eef` (`grep` over `src/` is empty), so no caller can break.
* Redaction still happens **before** truncation, so a cut can never expose a
  secret prefix. The QA test
  `test_truncated_never_leaks_a_secret_prefix` cuts at
  `len('{"a_token":"') + 16` and passes.
* Truncation is byte-accurate (QA `B-08`); `😀` and `ż` payloads at
  `max_bytes=64` both fit.

---

## 9. Escaped JSON key coverage (decision 8)

A JSON-encoded key can hide a credential name from a naive scanner:
`{"api\u005fkey": "…"}`. Revision 2 decodes escapes **inside the key** for
normalisation only; the original text is preserved.

```python
_JSON_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

def _decode_key_escapes(text):
    return _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text) if "\\u" in text else text
```

* The key pattern accepts `\uXXXX` as a unit:
  `(?:[A-Za-z0-9_.\-]|\\u[0-9a-fA-F]{4}){1,64}`.
* The cheap hint gate is escape-aware: text containing `\u` bypasses the gate,
  and the gate itself decodes before testing substrings.

| Input | Result |
| --- | --- |
| `{"api\u005fkey": "<secret>"}` via `redact_text` | key redacted in place |
| `{"pass\u0077ord": "<secret>"}` via `redact_text` | key redacted in place |
| `{"api\u005fkey": "<secret>"}` inside a mapping value | redacted (structural parse) |
| `{"model": "x"}` | unchanged |

Residual: an escape that hides a **value** (rather than a key) inside a string
that is not valid JSON is only covered by the provider-token backstop and by
`_redact_string`'s JSON re-parse.

---

## 10. ValidationError hardening — A1 vs A2

### 10.1 A1 (this gate)

`input` and `input_value` are sensitive keys; `input_value` is also a strong
text key. That covers the pydantic error envelope:

```python
[{'type': 'int_parsing',
  'loc': ('requests_per_minute_budget',),
  'msg': 'Input should be a valid integer …',
  'input': '<secret>',          # <- redacted structurally
  'url': 'https://errors.pydantic.dev/…'}]
```

and the rendered message `… [input_value='<secret>', input_type=str]`.

### 10.2 A2 — explicit acceptance criteria

A2 changes **one line** in `src/rfq_agent/domain/values.py`:
`DomainModel.model_config` gains `"hide_input_in_errors": True`.

| ID | Criterion | How it is checked |
| --- | --- | --- |
| AC-A2-1 | `str(ValidationError)` from any `DomainModel` construction contains no intact ≥ 8-char fragment of any input value | parametrised over 3 secret shapes × 2 model families |
| AC-A2-2 | Diagnostics survive: field name, error type and message are still present | assert `"requests_per_minute_budget"` and `"int_parsing"` in `str(e)` |
| AC-A2-3 | `e.errors()` remains structured and still carries `input` | assert `errors()[0]["input"] == secret` |
| AC-A2-4 | Rendering `e.errors()` through `redact_mapping` is clean | A1 guarantee, re-asserted in A2 |
| AC-A2-5 | `Settings` production-invariant failures still print a usable message | existing `TestDisclosureFixKeepsDiagnostics` |
| AC-A2-6 | Demo CLI and alembic subprocess contracts unchanged (exit code 2, message shape) | existing F3-B subprocess tests |
| AC-A2-7 | Full suite green with A1 + A2 applied | `pytest -m "not live"` |

Verified in-memory for A2-A1..A2-A4 and A2-A5/A2-A6 by the earlier session and
re-checked this session; A2 is **not** part of the 89-test gate.

---

## 11. Implementation boundary

### 11.1 F3-A1 — the QA-gated unit

| File | Change |
| --- | --- |
| `src/rfq_agent/observability/redaction.py` | full rewrite of the rules, structural walk, `redact_payload`, `summarize_payload`; new public name `REDACTED_KEY`; `__all__` gains `redact_payload`, `REDACTED_KEY` |
| `src/rfq_agent/observability/ids.py` | `LogFields` gains a wrap serializer and `__repr__`/`__str__` (uses `redact_mapping`) |
| `tests/unit/test_observability.py` | two test functions updated (§12); new unit tests for the rules |
| `tests/unit/test_redaction_adversarial.py` | **new** — the 89-test gate, adopted verbatim |

`redaction.py` grows from 145 to roughly 450 lines. No new dependency (stdlib +
pydantic, already imported through `domain.values`).

### 11.2 F3-A2 — rendering surfaces (depends on A1)

| File | Change |
| --- | --- |
| `src/rfq_agent/config.py` | `DatabaseSettings.url` and `GroqSettings.base_url`: `field_serializer` **and** `__repr__`/`__str__` (on the declared classes, never a subclass — see revision 1 §6.3) |
| `src/rfq_agent/persistence/engine.py` | `create_engine(..., hide_parameters=True)` |
| `src/rfq_agent/domain/values.py` | `DomainModel.model_config["hide_input_in_errors"] = True` |
| `tests/unit/test_config.py` | rendering tests for both settings sections |
| `tests/persistence/test_sqlite_config.py` | `hide_parameters` and statement-error echo tests |

A2 must not be merged without A1: the config serializers call `redact_text`, and
revision 1 showed that the old text rule destroys the host
(`postgresql://user:[REDACTED]:5432/db`) by letting the e-mail rule eat
`password@host`. Revision 2 preserves host, port and path (re-verified).

### 11.3 Out of bounds (both units)

No change to `contracts/`, `domain/` (except the A2 one-liner), `persistence/models/`,
`persistence/writers.py`, `demo/`, `seed/`, `migrations/`, `pyproject.toml`, or the
logging/trace wiring. No schema, no migration, no dependency, no LLM or workflow
change. `SecretStr` usage, `redact_logs` enforcement and the demo CLI contract are
untouched.

---

## 12. Required changes to existing tests

Exactly **two** existing test functions conflict with the QA gate. Both are
**test-only** edits; neither weakens a security guarantee.

### 12.1 `test_a_header_rule_redacts_to_the_end_of_the_line`

Revision 2 redacts bounded values, so this test's exact-equality assertion is
obsolete. The security guarantee (`SECRET not in output`) is preserved; only the
requirement that the rest of the line disappear is dropped.

```diff
-    def test_a_header_rule_redacts_to_the_end_of_the_line(self) -> None:
-        # Deliberate: a header value runs to the newline, so anything trailing it
-        # on the same line is redacted too. Over-redaction is the safe failure.
-        redacted = redact_text(f"api_key={SECRET} and also token={SECRET}")
-        assert SECRET not in redacted
-        assert redacted == f"api_key={REDACTED}"
+    def test_assignment_values_are_bounded_and_each_is_redacted(self) -> None:
+        # Values are bounded to the token, not to the end of the line: a
+        # trailing business identifier must survive (Session B, B-12). The
+        # security guarantee is unchanged - every secret on the line is
+        # still removed - only the over-redaction is gone.
+        redacted = redact_text(f"api_key={SECRET} and also token={SECRET}")
+        assert SECRET not in redacted
+        assert redacted == f"api_key={REDACTED} and also token={REDACTED}"
+
+    def test_a_true_header_still_redacts_to_the_end_of_the_line(self) -> None:
+        # ``Authorization`` is line-scoped by protocol, so the whole value goes.
+        redacted = redact_text(f"Authorization: Basic {SECRET}")
+        assert SECRET not in redacted
+        assert redacted == f"Authorization: {REDACTED}"
```

### 12.2 `test_prose_secrets_are_out_of_scope_for_the_pattern_rules`

The provider-token backstop deliberately catches a `gsk_…` credential in prose.
The test keeps pinning the *remaining* limitation with a prefix-less secret and
gains a positive case for the new behaviour.

```diff
     @pytest.mark.parametrize(
         "text",
         [
-            f"the api key is {SECRET}",
-            f"someone pasted {SECRET} into a sentence",
+            "the api key is correcthorsebatterystaple",
+            "someone pasted correcthorsebatterystaple into a sentence",
         ],
     )
-    def test_prose_secrets_are_out_of_scope_for_the_pattern_rules(self, text: str) -> None:
+    def test_prose_secrets_without_a_known_prefix_are_out_of_scope(self, text: str) -> None:
         """Documented limitation, not a bug.
-
-        The rules target assignment-style secrets. A secret embedded in prose is
-        prevented by construction instead: keys and tokens are held in
-        ``SecretStr`` config fields and are never placed into a log record, so
-        there is no prose for them to leak through. Redaction is the second line
-        of defence, not the only one.
+
+        The rules target assignment-style secrets plus a small set of known
+        credential prefixes (``gsk_``, ``sk-``, ``ghp_`` ...). A prose secret
+        with no recognisable prefix is prevented by construction instead: keys
+        and tokens are held in ``SecretStr`` config fields and are never placed
+        into a log record. Redaction is the second line of defence, not the
+        only one.
         """
-        assert SECRET in redact_text(text)
+        assert "correcthorsebatterystaple" in redact_text(text)
+
+    @pytest.mark.parametrize(
+        "text",
+        [
+            f"the api key is {SECRET}",
+            f"someone pasted {SECRET} into a sentence",
+        ],
+    )
+    def test_prose_secrets_with_a_known_prefix_are_caught(self, text: str) -> None:
+        """The provider-prefix backstop is deliberately broader than key=value."""
+        assert SECRET not in redact_text(text)
```

### 12.3 Impact

| Configuration | Result |
| --- | --- |
| Unmodified suite, revision 2 primitives | 1881 passed, **3 failed**, 3 skipped |
| The 3 failures | exactly the two functions above (three test IDs after parametrisation) |
| `tests/unit/test_observability.py` with §12.1–12.2 applied | **42 passed** (39 original tests, 3 new) |
| Whole suite with §12.1–12.2 applied | projected 1884 passed, 3 skipped (no other test touches redaction — `grep` over `tests/` outside this module returns nothing) |

No other existing test changed behaviour: the F3-B config suite, the demo CLI
subprocess tests, the alembic tests and the persistence tests all pass against
revision 2 unchanged.

---

## 13. Acceptance gate and results

### 13.1 The gate

`docs/reviews/f3a/test_redaction_adversarial.py` (89 tests) is adopted **as
shipped** — no edits, no skips, no `xfail`. It becomes
`tests/unit/test_redaction_adversarial.py` in the implementation PR and runs in
the default `pytest -m "not live"` job.

### 13.2 Results

| Suite | Result |
| --- | --- |
| QA gate (89) | **89 passed, 0 failed, 0.10 s** |
| `tests/unit/test_observability.py` (updated, §12) | 42 passed |
| Whole project suite (unmodified tests) | 1881 passed, 3 failed, 3 skipped |
| Extended matrix | **101/101** |

Per-test coverage with the mechanism that makes each one pass:
**`f3a1-qa-coverage-matrix.md`** (all 89 rows).

### 13.3 Extended matrix (beyond the gate)

101 additional assertions in `f3a1-extended-matrix.py`, all passing:

* 10 adversarial performance shapes under 200 ms (including `@`-runs, dot-runs,
  quote-runs, brace-runs, `\u005f`-runs);
* short-value redaction for 9 key spellings;
* NFKC/IDN/email-suffix coverage and 4 benign-Unicode exact-equality checks;
* `aws_secret_access_key`, `{name, value}` records, benign name/value pairs;
* bearer/header false positives and true-header EOL;
* ValidationError for 3 secret shapes × 2 paths;
* escaped JSON keys (text, structural, benign);
* `max_bytes` rejection for 6 values and the `max_bytes=1` boundary;
* structural: pairs, sets, bytes, bytearray, int/tuple keys, secret-as-key,
  email-as-key, cycle, depth cap, hostile `__str__`, `Decimal` round-trip;
* summaries: prefix, list, byte cap, digest compatibility, surrogate, shape;
* 18 false-positive strings (exact equality);
* idempotence over a 327-case corpus.

---

## 14. Performance evidence

Best of five runs, Python 3.11.2, project virtualenv.

| Input | Revision 1 | Revision 2 | Budget |
| --- | --- | --- | --- |
| 48 KB `"key" * 16 000 + "="` | 1.6 ms | 1.59 ms | 200 ms |
| 32 KB `"a-" * 16 000 + "key"` | scaling failure | 1.11 ms | 200 ms |
| 48 KB `"a.b" * 16 000` | quadratic | 0.47 ms | 200 ms |
| 48 KB `"a1." * 16 000 + "@b."` | quadratic | 0.80 ms | 200 ms |
| 48 KB `"@" * 48 000` | — | 0.76 ms | — |
| 1 MB prose | 170 ms | 10.9 ms | — |
| 1 MB `"a=" * 500 000` | — | 10.8 ms | — |
| 110 KB `password=…` × 5 000 | 22 ms | 41.8 ms | — |
| `summarize_payload`, 256 KB JSON string | 33 ms | 12.2 ms | — |
| `summarize_payload`, 1 MB structured (over cap) | — | 0.94 ms | — |
| `summarize_payload`, 5 000-key mapping | 57 ms | 29.7 ms | — |

The one regression, `password=… × 5 000` (22 → 42 ms), is the price of
per-key strength checks on a line that genuinely contains 5 000 credentials.
It is linear and 5 000 credentials in one log line is already pathological.

---

## 15. Residual risks

| # | Risk | Direction | Mitigation / decision |
| --- | --- | --- | --- |
| R1 | Prose secret with no known prefix (`"the password is hunter2"`) | leak | Out of scope by design; `SecretStr` prevents construction-level leakage; provider prefixes are the backstop |
| R2 | Bearer credential shaped like an artefact ID (`ABC-2026-XYZ`) | leak | Allowlist is required by QA `B-11`; still caught under `Authorization:` or a sensitive key; documented |
| R3 | Over-redaction of the bare key `key` in structured args (`{"key": "X-120"}`) | usability | Accepted; QA §8.4 recommends a separate narrower redactor for replayable tool args — deferred |
| R4 | `input` / `input_value` redacted in unrelated structured payloads | usability | Accepted for the error-envelope guarantee; documented |
| R5 | NFKC rewrites benign compatibility characters (ligatures, circled digits) | fidelity | Only when NFKC changes the text; log copy is not a source of truth |
| R6 | Structured string > 256 KiB is withheld entirely | fidelity | Fail-closed; digest retained |
| R7 | Localised key names (`hasło`, `pwd`, `klucz`) are not in the vocabulary | leak | Deferred; Polish deployment noted in the QA report |
| R8 | Secret split across lines or containing spaces | leak | Text rules are line/token scoped; structural keys still redact |
| R9 | Keys longer than 64 characters | leak | Not matched by the key pattern; values are still scanned |
| R10 | Binary/undecodable bytes | fidelity | Decoded with `errors="replace"`; replacement is lossy but safe |
| R11 | `[REDACTED]` used as a literal value in input | ambiguity | Indistinguishable from a redacted value; documented since revision 1 |
| R12 | A2 `hide_input_in_errors` hides the raw input from `str(e)` but `.errors()` still carries it | leak if rendered raw | A1's `redact_*` is the rendering boundary; A2 AC-A2-4 asserts it |
| R13 | The gate measures a prototype, not the repository | process | §16 requires the gate to run against the real module before merge |

---

## 16. Acceptance criteria for implementation

The design is ready to implement when the owner accepts the following. The
implementation PR is mergeable only when **all** of them hold.

**Design acceptance (this document)**

1. `redact_payload` is the single entry point for arbitrary payloads;
   `redact_text` and `redact_mapping` keep their signatures.
2. The three-tier vocabulary (§6) is the intended policy, including the weak
   `key` tier and the deliberate absence of a bare `key` suffix.
3. Bounded header values with end-of-line redaction limited to
   `Authorization`/`Proxy-Authorization` is the intended behaviour, and the
   business-ID allowlist for bare `Bearer` is acceptable.
4. Deterministic fail-closed behaviour for structured text that is over 256 KiB
   or unparseable is acceptable.
5. `max_bytes <= 0` raises `ValueError`.
6. NFKC is applied only when it changes the text, and only to non-ASCII input.
7. The two existing tests in §12 are updated exactly as shown, and no other
   existing test is changed.

**Implementation acceptance (F3-A1)**

8. `tests/unit/test_redaction_adversarial.py` exists at the QA commit's content
   (byte-identical) and **89/89 pass** against the real `redaction.py`.
9. `pytest -m "not live"` is green: **1884 passed, 3 skipped**.
10. `ruff check .` and `ruff format --check .` pass (line length 100, existing
    rule set; `except Exception` carries `# noqa: BLE001`).
11. No new dependency; `redaction.py` imports only stdlib and
    `rfq_agent.domain.values`.
12. Idempotence holds for every test corpus: `redact(redact(x)) == redact(x)`.
13. The extended matrix (`f3a1-extended-matrix.py`) passes unchanged.
14. No file outside the §11.1 list is touched.

**Implementation acceptance (F3-A2, separate PR)**

15. AC-A2-1 … AC-A2-7 (§10.2) all pass.
16. §11.2 file list only; A2 is not merged before A1.
17. Config rendering keeps host/port/path visible and the real values
    reachable.

**Reporting rule.** A green `pytest -m "not live"` on the *existing* suite is
**not** evidence of safety: at baseline those tests pass while 84 of the 89
adversarial tests fail. The gate is the adversarial suite, not the project
suite.

---

## 17. Provenance

* Repository baseline `d5611eef` — verified clean before and after this
  session. No commit, no branch change, no tracked file modified.
* Design under revision: `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`
  (`docs/reviews/f3a/design-revision.md`, `design-prototype.py`).
* Adversarial gate: `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`
  (`docs/reviews/f3a/adversarial-qa-report.md`,
  `docs/reviews/f3a/test_redaction_adversarial.py`).
* Artefacts delivered with this document:
  * `f3a1-prototype.py` — reference prototype revision 2 (non-normative)
  * `f3a1-qa-coverage-matrix.md` — all 89 QA tests with their mechanism
  * `f3a1-extended-matrix.py` — 101-assertion matrix beyond the gate
  * `f3a1-test-shim.py` — the `sitecustomize` used to inject the prototype
* Commands used: `git show`, `pytest -m "not live" -q`,
  `pytest test_redaction_adversarial.py -v`, `time.perf_counter()` benchmarks.

**Status: application code modified NO · tracked files modified NO · tests added
to the repository NO · commits created NO · PR created NO · implementation
authorized NO.**
