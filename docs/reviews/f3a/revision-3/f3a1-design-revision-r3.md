# F3-A1 Design Revision 3 — Targeted Blocker Closure

**Project 04 / Session A — read-only design correction.**
Repository `slading/ai-rfq-operations-agent`, baseline `d5611eefab3b326e9ad5caa9789d892e69ce3875`.
Design under revision: commit `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`.
Revision 2 (superseded by this document): commit `3469b00ce0cbfd18f1377600b4f35d2872bb7303`.
Adversarial gate: commit `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.

| Gate | Status |
| --- | --- |
| Application code modified | **NO** |
| Tracked files modified | **NO** |
| Tests added to the repository | **NO** |
| Commits created | **NO** |
| PR created / merged | **NO** |
| Implementation authorized | **NO** |

**Verdict: REVISION 3 SUBMITTED — the six Session D blockers (C-8 … C-12 plus
a reproducible regression suite) are closed in the reference prototype and
measured. This is not an ACCEPT and not a claim of one.**

> **I am not claiming independent ACCEPT.** Revision 2 was returned REVISE by
> Session D on six concrete points. Each is addressed below with a measurement
> and a regression test that fails on revision 2 and passes on revision 3.
> The design has not been implemented; the gate is an acceptance *gate*, not a
> substitute for independent review. §16 lists what must be true before
> implementation is authorized, and §15 lists what this design still does not
> protect.

| Measurement | Result |
| --- | --- |
| QA gate (89 tests, unmodified) | **89 passed, 0 failed, 0.10 s** |
| Reproducible regression suite, revision 3 (new) | **162 passed, 0 failed, 0.16 s** |
| The same suite against the revision-2 prototype | **38 failed, 124 passed** (falsifiability) |
| Extended matrix beyond the gate | **101/101** |
| Project suite (unmodified tests) with revision 3 | 1881 passed, 3 failed, 3 skipped, 95.15 s |
| Project suite with the two approved §12 test edits | **1887 passed, 3 skipped, 95.25 s** |
| `tests/unit/test_observability.py` with §12 applied | **42 passed** |
| 48 KB repeated `BEGIN` markers (`redact_text`) | 1 191.81 ms → **0.64 ms** |
| 1 MB repeated `BEGIN` markers (`redact_text`) | 65 137.98 ms → **13.21 ms** |
| Text-path leak over the 46 structural keys | 10/46 leaked → **0/46** |
| Double-encoded / nested JSON secret | leaked → **redacted, idempotent** |

The three remaining project failures are **not** security failures and are
**not** new: they are the two functions in §12 that pin the over-redaction
behaviour revision 2 deliberately removed (three test IDs after
parametrisation). With §12 applied the suite is exactly **1887 passed,
3 skipped** — the C-12 acceptance count.

---

## 0. Revision 3 delta — what Session D asked for

Session D returned **REVISE** on revision 2 despite 89/89 and 101/101. The
blockers were gaps *outside* the gate's coverage, so every one of them is
closed **and** covered by the new regression suite (§13.4) rather than by
arguing that the existing gate already proves it.

| ID | Revision 2 defect | Revision 3 correction | Proof |
| --- | --- | --- | --- |
| **C-8** | `_PEM_RE` = `-----BEGIN …-----([\s\S]{0,65536}?)-----END …-----` is quadratic: the lazy body retried at every position. 48 KB of `BEGIN` markers took 1 191.81 ms; 1 MB took 65 137.98 ms (denial of service through a log record). | `_redact_pem()` — one `finditer` over the marker list, `BEGIN` paired with the next `END`, single cursor walk, O(n) with a constant per marker. A block longer than `_MAX_PEM_BLOCK` (1 MiB) and any unterminated `BEGIN` are **withheld** to the end of the text (fail-closed). No lazy body quantifier anywhere. | 48 KB **0.64 ms**, 1 MB **13.21 ms**, 4 MB **53.96 ms** (linear). `summarize_payload` 48 KB **0.89 ms**, 1 MB **17.46 ms**. 9 regression tests. |
| **C-9** | The text path used a hand-maintained 36-name `_STRONG_TEXT_KEYS` subset of the 46-name structural set. Ten exact names leaked with a non-provider secret: `auth`, `authentication`, `customeremail`, `email`, `emailaddress`, `input`, `passphrase`, `senderemail`, `sessionid`, `useremail` (`session_id` too). | The text tier is **derived** from the structural policy: `_is_strong_text_key(k) = _is_sensitive_key(k) and _normalize_key(k) not in {"key"}`. The cheap substring gate `_KEY_HINTS` is widened to `pass`, `session`, `email` so no sensitive key can be skipped before the scanner runs. `_STRONG_TEXT_KEYS` is deleted — there is no second list to drift. | 0/46 leaks in text; 0/46 in structure; 46 parametrised text tests + 46 structural + 14 suffix/header spellings + 5 named C-9 keys + 13 benign identifiers + 3 tier/agreement tests = 127 in the class. |
| **C-10** | A secret inside JSON that was encoded twice (or nested inside an innocuous field) survived: `json.dumps(json.dumps({"api_key": "hunter2"}))`, nested-in-`note`, nested escaped key. | The key/separator scanner tolerates **1–8 backslashes** before a quote or before `u`, so it reads 1–8 encoding levels directly (`\\u005f` decodes to `_`). Anything still escaped after the pipeline goes through `_nested_escape_probe()`: up to `_MAX_ESCAPE_DEPTH = 8` unescape levels are re-scanned with the key/header rules; a payload larger than `_MAX_NESTED_PROBE = 256 KiB`, or still escaped after 9 levels, is **withheld** wholesale. The probe itself is linear (no regex, no re-parse). | Double, triple, 4-level and 9-level payloads redacted; 9 levels → `[REDACTED]`; benign `say "hello"` unchanged; benign 300 KB escaped JSON preserved byte-for-byte; 17 regression tests. |
| **C-11** | §10.2 told A2 to set `hide_input_in_errors` on **`DomainModel`**. The leaking config classes are `DatabaseSettings`, `GroqSettings` and `Settings`, which derive from **`_ConfiguredModel`**; `DomainModel` is untouched by them. The design also implied `hide_input_in_errors` was sufficient — it is not: `.errors()` and `.json()` still carry the raw input. | §10.2 retargeted to `_ConfiguredModel` (inherited by every config section, including nested `Settings`). A **safe rendering policy** is specified: `str(e)`/`repr(e)` are made input-free by the flag; `.errors()` and `.json()` are documented as raw-by-design and must be rendered through the A1 redactor (`redact_mapping({"errors": e.errors()})`) or the new `safe_validation_report()` wrapper. A2 remains **design only** — no A2 code is written here. | In-memory probe, this session: raw `str(e)`/`.errors()`/`.json()` all leak for `DatabaseSettings`, `GroqSettings`, `Settings` and a `DomainModel` subclass; `hide_input_in_errors=True` on `_ConfiguredModel` removes the leak from `str(e)` only; `redact_mapping` removes it from `.errors()` and from `json.loads(e.json())`. |
| **C-12** | Revision 2 projected "1884 passed" and did not state the `email_hash` contract. | Acceptance count is **1887 passed, 3 skipped**, measured (§13.3). `email_hash` is explicitly retained: `sha256(lowercase(strip(address)))`, exported in `__all__`, stable across runs, `redact_*` never changes its input normalisation. The rest of the public surface (`REDACTED`, `redact_text`, `redact_mapping`, `summarize_payload`, plus the new `redact_payload`, `REDACTED_KEY`) and the `INLINE`/`TRUNCATED`/`WITHHELD` status vocabulary are pinned by tests. | 9 public-API regression tests; full-suite measurement. |
| **7** | The 89-test gate does not cover PEM, key-policy parity or nested JSON — that is why C-8/C-9/C-10 escaped revision 2. | A **new reproducible regression suite**, `tests/unit/test_redaction_regression.py` in the implementation PR, 162 tests, outside the 89-gate, run in the default job. Each C-blocker test is written so it **fails against the revision-2 prototype**. | 162 passed; 38 of them fail on revision 2; 251 tests total across gate + regressions. |

**Scope discipline.** No architecture changed. The pipeline order, public
signatures, structural walk, `summarize_payload` semantics, vocabulary (except
deriving the text tier from it) and implementation boundary are revision 2's.
The three C-8/C-9/C-10 corrections are local to three functions plus the
vocabulary gate.

---

## 1. What changed in revisions 1 and 2

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
* The new regression suite (`f3a1-regression-r3.py`, 162 tests) was executed
  against the same shim. It lives outside the repository like the rest of the
  evidence; the implementation PR adds it unchanged as
  `tests/unit/test_redaction_regression.py`.
* All probes use synthetic credentials (`gsk_FAKEsupersecret0123456789`,
  `hunter2`, `hunter2024`, `jan.kowalski@example.com`). No real secret was read
  or printed.
* The prototype lives **outside** the repository and is installed at test time
  through a `sitecustomize.py` shim that rebinds names on
  `rfq_agent.observability.redaction`. The repository was never modified: the
  shim also extends `__all__` so the new public names can be asserted before
  they exist in the tracked module. The implementation PR ships the real names;
  the shim exists only because the prototype is injected, not merged.

  ```python
  import importlib, os
  _proto = importlib.import_module(os.environ.get("F3A_PROTO_MODULE", "f3a1_prototype"))
  import rfq_agent.observability.redaction as _r
  _r.redact_text = _proto.redact_text
  _r.redact_mapping = _proto.redact_mapping
  _r.summarize_payload = _proto.summarize_payload
  _r.redact_payload = _proto.redact_payload
  _r.REDACTED_KEY = _proto.REDACTED_KEY
  for _name in ("redact_payload", "REDACTED_KEY"):
      if _name not in _r.__all__:
          _r.__all__ = [*_r.__all__, _name]
  ```

* **Leak standard:** an intact run of **≥ 8 consecutive characters** of the
  synthetic secret. This is the same detector the F3-B config tests use.
* Reproduce everything (artefacts: `f3a1-prototype-r3.py`,
  `f3a1-regression-r3.py`, `f3a1-test-shim.py`, `f3a1-extended-matrix.py`):

  ```bash
  python -m venv .venv && .venv/bin/pip install -e ".[dev]"
  git show 28af34cb5e3cdb936d087a0df5b536dc23ee40c4:docs/reviews/f3a/test_redaction_adversarial.py \
      > /tmp/test_redaction_adversarial.py
  mkdir -p /tmp/f3a && cp f3a1-test-shim.py /tmp/f3a/sitecustomize.py
  cp f3a1-prototype-r3.py /tmp/f3a/f3a1_prototype.py

  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest /tmp/test_redaction_adversarial.py -q   # 89
  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest f3a1-regression-r3.py -q                 # 162
  PYTHONPATH=src:/tmp/f3a .venv/bin/python f3a1-extended-matrix.py                            # 101
  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest -m "not live" -q                         # 1881/3/3
  ```

  The last line needs the two §12 test edits to reach 1887; without them it
  reports the three expected failures and nothing else.

* **Revision-2 baseline for every new test.** The whole regression suite was
  run against the revision-2 prototype (`f3a1_prototype_v2_backup.py`):
  **38 failed, 124 passed** — 20 key-policy failures, 12 nested-encoding
  failures, 6 PEM failures (including the two timing budgets and the
  unterminated-block leak). A regression that cannot fail on the previous
  revision is not evidence, so this run is part of the deliverable.

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
| Raw exception | **A2** | `_ConfiguredModel.model_config["hide_input_in_errors"] = True` ⟦R3⟧ (revision 2 said `DomainModel`; C-11) | `str(ValidationError)` from any `_ConfiguredModel` subclass — `DatabaseSettings`, `GroqSettings`, nested `Settings` — leaks no input; diagnostics preserved; `.errors()` still structured |
| Safe rendering of `.errors()`/`.json()` | **A2** | `safe_validation_report()` / `redact_mapping` ⟦R3⟧ | no intact ≥ 8-char fragment in the rendered dict, including `json.loads(e.json())` |

Measured for A1, three secret shapes:

| Secret | `redact_text(str(e))` leak | `redact_mapping({"errors": e.errors()})` leak |
| --- | --- | --- |
| `gsk_FAKEsupersecret0123456789` | no | no |
| `hunter2` | no | no |
| `correct-horse-battery` | no | no |

Measured for A2 (an in-memory `model_config` override, no file changed) —
**retargeted this session**:

| Model | `str(e)` leak today | `.errors()` leak today | `.json()` leak today | `str(e)` after `hide_input_in_errors` on its base |
| --- | --- | --- | --- | --- |
| `DatabaseSettings` (`_ConfiguredModel`) | **yes** | **yes** | **yes** | **no** |
| `GroqSettings` (`_ConfiguredModel`) | **yes** | **yes** | **yes** | **no** |
| `Settings` (`_ConfiguredModel`) | **yes** (nested) | **yes** | **yes** | **no** |
| `DomainModel` subclass (control) | **yes** | **yes** | **yes** | **no, only when its own config is changed** |

The flag does nothing for `.errors()`/`.json()` in every row: those surfaces are
raw by design and must go through §10.2's safe rendering.

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
| 6 | ValidationError hardening to A2 with acceptance criteria; structured error fields in A1 | Done (§3 C-6, §10). ⟦R3⟧ A2 targets `_ConfiguredModel` and adds a safe-rendering policy for `.errors()`/`.json()` (C-11) |
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
2. PEM blocks                     marker scan, BEGIN paired with next END -> [REDACTED]
                                  unterminated or > 1 MiB block -> withheld to end
3. URL userinfo                   scheme://user:password@host           -> scheme://user:[REDACTED]@host
                                  scheme://token@host                   -> scheme://[REDACTED]@host
4. Header keys (EOL)              authorization / proxy-authorization  -> rest of line
5. Key + value scan               key, separator (= : ,), optional quotes/wrappers,
                                  bounded value; strong tier any length, weak tier shape-tested
6. Bearer                         Bearer <credential> -> Bearer [REDACTED]
                                  (business-ID-shaped tokens exempt)
7. Provider tokens                gsk_ / sk- / ghp_ / AKIA / xox… / hf_ / npm_ -> [REDACTED]
8. E-mail                         token pass + quoted-local pass        -> [REDACTED]
9. Nested-escape probe            escaped quote still present -> 9 bounded unescape
                                  levels re-scanned; clean -> unchanged, still
                                  escaped or > 256 KiB -> [REDACTED]
```

Order is load-bearing and each step is gated by a cheap `in` test so lines that
cannot contain the construct skip the scan entirely. Step 9 is the only step
that runs on the *output* of the others; it exists because a payload encoded
more than once can hide a key/value pair from steps 4–5 (C-10).

### 5.2 Key/value scanner  ⟦R3⟧

```python
_KEY_SEP_RE = re.compile(
    # A key never starts immediately after a backslash: that position belongs to
    # an escape sequence, and rejecting it keeps a run of ``\\u005f`` linear.
    r"(?i)(?<![A-Za-z0-9_.\-\\])"
    # Possessive quantifiers: a failed separator check must not re-scan the key
    # at every shorter length (measured: 113 ms -> 0.80 ms at 48 KB).
    r"(?P<key>(?:[A-Za-z0-9_.\-]|\\{1,8}+u[0-9a-fA-F]{4}){1,64}+)"
    # A quoted key or value is escaped once per encoding level; 0-8 backslashes
    # covers every real serialisation. Deeper nesting is caught by the probe.
    r"(?P<sep>(?:\\{0,8}[\"'])?\s*[:=,]\s*)"
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

### 5.3 Complexity argument  ⟦R3⟧

* `_KEY_SEP_RE` has a negative lookbehind that fails immediately inside a run of
  key characters **or of backslashes**. Only the first position of a run can
  attempt a match, so the scan is O(n) with a 64-character constant, not O(n²).
  Measured on the three adversarial shapes from `B-09`.
* The key's escape alternative is **possessive** (`{1,64}+`) and so is the
  escape run (`\\{1,8}+`). Without both, a 48 KB run of `\\u005f` costs
  113 ms (the engine backtracks the separator check at every key length);
  with both it costs 0.80 ms. This is the one performance regression Session D
  found *after* the C-8 fix, and it is pinned by an extended-matrix assertion.
* **PEM is no longer a regex.** `_redact_pem()` is a single `finditer` over
  `_PEM_MARKER_RE` (`-----BEGIN|END <label>-----`), one pass with a cursor,
  O(n) in the number of markers and bytes. The lazy `[\s\S]{0,65536}?` body is
  deleted; that shape is quadratic because it retries the bounded body scan at
  every character. Growth is linear and was measured at 48 KB, 200 KB, 1 MB and
  4 MB (0.64 / 2.61 / 13.21 / 53.96 ms).
* The value loop is a single cursor walk; replacements are collected in a list
  and joined once.
* The e-mail detector is one `sub` over tokens plus one anchored match per
  token; the local part is capped at 64 characters, so no token can trigger
  backtracking across itself.
* Every regex quantifier in the module is bounded (`{0,31}`, `{1,64}`,
  `{0,65536}`, `{1,512}`, `{1,8}`, `{1,8}+`). The nested-escape probe is a
  bounded loop of `str.replace` calls (`_MAX_ESCAPE_DEPTH + 1` iterations, each
  linear), guarded by `_MAX_NESTED_PROBE` so a large payload is withheld rather
  than unescaped.

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
| `_WEAK_TEXT_KEYS` | `key` only | the single name that keeps the credential-shape rule in free text |
| `_STRONG_TEXT_KEYS` | **deleted** | ⟦R3⟧ the text tier is now derived, not a second list |

**Text tier (C-9 correction).** Revision 2 kept a hand-maintained 36-name
`_STRONG_TEXT_KEYS`. It had drifted from the 46-name structural set, so ten
exact names were strong in structure and weak in prose. Revision 3 deletes the
list and derives the tier:

```python
def _is_strong_text_key(key: str) -> bool:
    """Text tier mirrors the structural policy; only bare ``key`` stays weak."""
    return _is_sensitive_key(key) and _normalize_key(key) not in _WEAK_TEXT_KEYS
```

There is now exactly one authority, `_SENSITIVE_KEY_EXACT` +
`_SENSITIVE_KEY_SUFFIXES`, and the two paths cannot disagree. Because a second
list was not the only failure mode, the **cheap gate** matters too: it decides
whether the scanner runs at all, so a hint missing there silently disables a
whole key family. `_KEY_HINTS` is therefore widened to
`key token secret pass auth credential cookie jwt bearer session email input`,
covering `passphrase`/`password`/`passwd` through `pass`, every `*email*`
spelling through `email`, and `sessionid`/`sessiontoken` through `session`.

| C-9 leak in revision 2 | Revision 3 mechanism | Result |
| --- | --- | --- |
| `passphrase`, `auth`, `authentication`, `sessionid` | derived text tier + `pass`/`session` gate | redacted |
| `email`, `emailaddress`, `senderemail`, `customeremail`, `useremail` | derived text tier + `email` gate | redacted |
| `input`, `inputvalue` | derived text tier | redacted |
| `session_id`, `x-api-key`, `proxy-authorization`, `set-cookie` | suffix rule unchanged, now reached through the same gate | redacted |

**Deliberately absent:** bare `key` as a suffix, and anything that would make
`monkey`, `turkey`, `hockey`, `keyboard`, `primary_key`, `idempotency_key`,
`tokenizer`, `secretary`, `public_key`, `max_tokens` or `token_count`
sensitive. All eleven are pinned by tests in both suites. Deriving the text tier
does **not** widen the suffix rule, so these stay benign; the one behavioural
consequence is that a value under bare `key` still requires a credential shape.

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
* ⟦R3⟧ A PEM block that is unterminated or larger than `_MAX_PEM_BLOCK` is
  replaced with `[REDACTED]` *before* serialisation, so truncation can never
  expose a key body either. Measured at 48 KB / 1 MB: 0.89 / 17.46 ms.

---

## 9. Escaped JSON and nested encodings (decision 8, C-10)  ⟦R3⟧

A JSON-encoded key can hide a credential name from a naive scanner:
`{"api\u005fkey": "…"}`. Revision 2 decoded escapes **inside the key** for
normalisation only. That is enough for one encoding level and for a payload
that is valid JSON, and it failed for everything else Session D tried: a secret
inside `json.dumps(json.dumps({...}))`, a JSON document nested in an innocuous
`note` field, and a key escaped inside a string that is not itself parsed.

### 9.1 Two levels of defence

**Level 1 — the scanner reads the escapes directly.** The key alternative and
the separator both tolerate a run of backslashes, so a key that is escaped once,
twice or eight times is still recognised as a key, and the value that follows
is redacted in place:

```python
_JSON_ESCAPE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})")   # \u, \\u, ... -> one char

def _decode_key_escapes(text):
    """Decode ``\\uXXXX`` so JSON-escaped keys normalise identically.

    A key can be escaped more than once (``api\\u005fkey`` inside a JSON string
    that is itself JSON-encoded), so the pattern tolerates any run of
    backslashes before ``u``.
    """
    if "\\u" not in text:
        return text
    return _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
```

The cheap hint gate is escape-aware: text containing `\u` bypasses the gate.

**Level 2 — a bounded, fail-closed probe.** Level 1 tops out at eight encoding
levels. Beyond that, the value cannot be located precisely without unescaping
the whole string, so `redact_text` checks the *shape* of what is left:

```python
_MAX_ESCAPE_DEPTH = 8          # encoding levels the pattern scanner tolerates
_MAX_NESTED_PROBE = 256 * 1024 # above this an escaped payload is withheld

def _nested_escape_probe(text: str) -> str | None:
    if '\\"' not in text:                      # nothing is still escaped
        return None
    lowered = text.casefold()
    if "\\u" not in text and not any(h in lowered for h in _KEY_HINTS):
        return None                            # benign escaped text, no key word
    if len(text) > _MAX_NESTED_PROBE:
        return REDACTED                        # fail closed, no unescape copy
    view = text
    for _ in range(_MAX_ESCAPE_DEPTH + 1):
        view = view.replace('\\"', '"').replace("\\\\", "\\")
        if _redact_headers(view) != view or _redact_key_values(view) != view:
            return REDACTED                    # a rule fires at this depth
        if '\\"' not in view:
            return None                        # fully unescaped and clean
    return REDACTED                            # still escaped -> withhold
```

Design points that matter:

* The probe runs **after** the pipeline on the already-redacted text, so a
  value the Level-1 scanner handled is not re-examined. It only answers
  "could something still be hidden one level down?".
* The order inside the probe is deliberate: the credential-word gate is tested
  **before** the size bound. A 300 KB JSON document containing `say "hello"`
  and no key word is preserved byte-for-byte (`5.08 ms`); the same document
  with a key word is withheld. An earlier draft checked the size first and
  over-redacted benign escaped prose — the new regression suite caught it
  before delivery. This is exactly the kind of ordering bug a gate alone
  would not find.
* `'\\"'` is the only trigger. A string containing a plain `"` is not escaped
  and never reaches the probe, so `say "hello"` in ordinary prose is untouched.
* The probe never parses JSON: it does not allocate a second document, it does
  not recurse, and it cannot be driven into exponential work by a crafted
  string. It is at most 9 linear `replace` passes plus 9 scanner passes over
  text that already passed the pipeline.
* Withholding is the safe direction. `REDACTED`, not the input, is returned; a
  digest is still available through `summarize_payload` for correlation.

### 9.2 Coverage

| Input | Result |
| --- | --- |
| `{"api\u005fkey": "<secret>"}` via `redact_text` | key redacted in place |
| `{"pass\u0077ord": "<secret>"}` via `redact_text` | key redacted in place |
| `{"api\u005fkey": "<secret>"}` inside a mapping value | redacted (structural parse) |
| `json.dumps(json.dumps({"api_key": "<secret>"}))` | redacted, idempotent |
| three and four encoding levels | redacted, idempotent |
| nine encoding levels | `[REDACTED]` (probe, fail-closed) |
| `{"note": "… {\"password\": \"<secret>\"} …"}` | redacted |
| `{"model": "x"}` | unchanged |
| `<secret>`-free escaped JSON > 256 KiB | unchanged |
| escaped JSON > 256 KiB containing a key word | `[REDACTED]` |

Residual: an escape that hides a **value** (rather than a key) inside a string
that is not valid JSON is covered by the provider-token backstop, by
`_redact_string`'s JSON re-parse, and now by the probe — but only up to the
depth and size bounds above. Beyond them the whole string is withheld, which is
a fidelity cost, not a leak.

---

## 10. ValidationError hardening — A1 vs A2  ⟦R3 corrected⟧

Session D's **C-11**: revision 2 pointed A2 at the wrong base class and implied
that `hide_input_in_errors` was a complete fix. Both are corrected here.

### 10.1 A1 (this gate)

`input` and `input_value` are sensitive keys; `input_value` is also a strong
text key. That covers the pydantic error envelope when it is *rendered through
the redactor*:

```python
[{'type': 'int_parsing',
  'loc': ('requests_per_minute_budget',),
  'msg': 'Input should be a valid integer …',
  'input': '<secret>',          # <- redacted structurally by redact_mapping
  'url': 'https://errors.pydantic.dev/…'}]
```

and the rendered message `… [input_value='<secret>', input_type=str]`.

A1 does **not** change pydantic's behaviour. It provides the primitives; A2 is
the unit that applies them to the rendering surfaces.

### 10.2 A2 — corrected target and rendering policy

**Target (C-11, primary correction).** A2 sets
`hide_input_in_errors=True` on **`_ConfiguredModel`**, not on `DomainModel`.

```python
# src/rfq_agent/config.py
class _ConfiguredModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,   # A2: added here
    )
```

`_ConfiguredModel` is the base of every configuration section in
`src/rfq_agent/config.py` — including `DatabaseSettings`, `GroqSettings` and the
nested `Settings` — because `Settings(BaseSettings)` and the section models
share that base. `DomainModel` (in `src/rfq_agent/domain/values.py`) is *not*
in their MRO and is never reached by configuration parsing:

```text
DatabaseSettings -> _ConfiguredModel -> pydantic.BaseModel
GroqSettings     -> _ConfiguredModel -> pydantic.BaseModel
Settings         -> _ConfiguredModel -> BaseSettings -> pydantic.BaseModel
DomainModel      -> pydantic.BaseModel          (unrelated)
```

Verified in this session: with a secret string where an `int` is expected,
`str(e)`, `e.errors()` and `e.json()` all leak for `DatabaseSettings`,
`GroqSettings` and `Settings`; `hide_input_in_errors=True` on
`_ConfiguredModel` removes the leak from `str(e)` for all three, and does
**nothing** for a `DomainModel` subclass (its own config governs it). Adding the
flag to `DomainModel` alone would have left every configuration leak in place.

**Rendering policy (C-11, second correction).** The flag removes the input
from `__str__`/`__repr__` only. It does **not** sanitise the machine-readable
surfaces — measured this session:

| Surface | `hide_input_in_errors=True` | Must be |
| --- | --- | --- |
| `str(e)` / `repr(e)` | input-free | safe by default |
| `e.errors()["input"]` | **still raw** | raw-by-design; never logged directly |
| `e.errors()["input_value"]` (older pydantic paths) | **still raw** | same |
| `e.json()` | **still raw** (`"input": "hunter2024"`) | must go through safe rendering |

So A2 specifies a single safe rendering path, and documents the raw one as
internal:

```python
# src/rfq_agent/observability/redaction.py (public, part of A2's PR)
def safe_validation_report(exc: BaseException) -> dict[str, object]:
    """Render a ValidationError envelope with every input value redacted.

    ``ValidationError.errors()`` deliberately keeps the raw input for
    programmatic consumers (constraint values, retries). It must never be
    logged, printed or serialised directly; this function is the boundary.
    """
    if not isinstance(exc, ValidationError):
        return {"error": redact_text(str(exc))}
    return {
        "error_type": type(exc).__name__,
        "errors": redact_mapping({"errors": exc.errors()})["errors"],
    }
```

Policy:

1. `str(e)` / `repr(e)` may be logged directly **only after** A2 sets the flag;
   until then, and for any exception not covered by `_ConfiguredModel`, it goes
   through `redact_text`.
2. `e.errors()` / `e.json()` are **raw by design** and are never handed to a
   logger, an HTTP error body, a task result or a trace attribute. They are for
   programmatic branches (which field failed, retryable or not).
3. The only approved renderings are `safe_validation_report(e)` or
   `redact_mapping({"errors": e.errors()})`. Both were verified clean this
   session for all three config models, including the parsed `json.loads(e.json())`
   form.
4. A2 adds a lint/test rule: `grep`-based assertion in the test suite that
   `logger.*` calls in `config.py`, `persistence/engine.py` and `demo/` never
   receive `e.errors()` or `e.json()`.

**A2 acceptance criteria (corrected).**

| ID | Criterion | How it is checked |
| --- | --- | --- |
| AC-A2-1 | `str(ValidationError)` from any `_ConfiguredModel` subclass — `DatabaseSettings`, `GroqSettings`, nested `Settings` — contains no intact ≥ 8-char fragment of any input value | parametrised over 3 secret shapes × 3 models, plus a `DomainModel` control that must **not** depend on this flag |
| AC-A2-2 | Diagnostics survive: field name, error type and message are still present | assert `"requests_per_minute_budget"` and `"int_parsing"` in `str(e)` |
| AC-A2-3 | `e.errors()` remains structured and still carries `input` (raw) | assert `errors()[0]["input"] == secret`; this is the documented contract, not a leak |
| AC-A2-4 | `safe_validation_report(e)` and `redact_mapping({"errors": e.errors()})` contain no ≥ 8-char fragment | parametrised over the same 3 × 3 matrix |
| AC-A2-5 | `json.loads(e.json())` is only ever produced through the safe path; a direct `e.json()` is never logged | source assertion + rendering test on the parsed dict |
| AC-A2-6 | `Settings` production-invariant failures still print a usable message | existing `TestDisclosureFixKeepsDiagnostics` |
| AC-A2-7 | Demo CLI and alembic subprocess contracts unchanged (exit code 2, message shape) | existing F3-B subprocess tests |
| AC-A2-8 | Full suite green with A1 + A2 applied | `pytest -m "not live"` |

All of AC-A2-1 … AC-A2-5 were probed in-memory this session; A2 is **not**
implemented and not part of the 89-test gate or the 162 regressions. The
`hide_input_in_errors` flag, the `_ConfiguredModel` target and the wrapper are
design only, pending separate authorization.

## 11. Implementation boundary

### 11.1 F3-A1 — the QA-gated unit

| File | Change |
| --- | --- |
| `src/rfq_agent/observability/redaction.py` | full rewrite of the rules, structural walk, `redact_payload`, `summarize_payload`; new public names `REDACTED_KEY`, `redact_payload`; `__all__` gains both; `safe_validation_report` is **not** in A1 (it belongs to A2's file list, §11.2) |
| `src/rfq_agent/observability/ids.py` | `LogFields` gains a wrap serializer and `__repr__`/`__str__` (uses `redact_mapping`) |
| `tests/unit/test_observability.py` | two test functions updated (§12); new unit tests for the rules |
| `tests/unit/test_redaction_adversarial.py` | **new** — the 89-test gate, adopted verbatim |
| `tests/unit/test_redaction_regression.py` | **new** — the 162-test revision-3 regression suite (§13.4), adopted verbatim from `f3a1-regression-r3.py` |

`redaction.py` grows from 145 lines to about 645 (prototype revision 3). No new dependency (stdlib +
pydantic, already imported through `domain.values`).

### 11.2 F3-A2 — rendering surfaces (depends on A1)

| File | Change |
| --- | --- |
| `src/rfq_agent/config.py` | `_ConfiguredModel.model_config["hide_input_in_errors"] = True` (**corrected target**, C-11 — not `DomainModel`); `DatabaseSettings.url` and `GroqSettings.base_url`: `field_serializer` **and** `__repr__`/`__str__` (on the declared classes, never a subclass — see revision 1 §6.3) |
| `src/rfq_agent/observability/redaction.py` | adds `safe_validation_report()` and exports it (A2's addition to the A1 module) |
| `src/rfq_agent/persistence/engine.py` | `create_engine(..., hide_parameters=True)` |
| `tests/unit/test_redaction_regression.py` | extends the safe-rendering assertions (AC-A2-4/5) |
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

| Configuration | Result (measured, revision 3) |
| --- | --- |
| Unmodified suite, revision 3 primitives | 1881 passed, **3 failed**, 3 skipped, 95.15 s |
| The 3 failures | exactly the two functions above (three test IDs after parametrisation) |
| `tests/unit/test_observability.py` with §12.1–12.2 applied | **42 passed** (39 original tests, 3 new) |
| Whole suite with §12.1–12.2 applied | **1887 passed, 3 skipped**, 95.25 s |
| Whole suite, same edits, revision 2 primitives | 1887 passed, 3 skipped, 90.66 s (delta ≈ 4.6 s is machine noise, not a code path) |

No other existing test changed behaviour: the F3-B config suite, the demo CLI
subprocess tests, the alembic tests and the persistence tests all pass against
revision 2 unchanged.

---

## 13. Acceptance gate and results  ⟦R3⟧

### 13.1 The gate

`docs/reviews/f3a/test_redaction_adversarial.py` (89 tests) is adopted **as
shipped** — no edits, no skips, no `xfail`. It becomes
`tests/unit/test_redaction_adversarial.py` in the implementation PR and runs in
the default `pytest -m "not live"` job.

### 13.2 Results

| Suite | Result |
| --- | --- |
| QA gate (89) | **89 passed, 0 failed, 0.10 s** |
| Revision-3 regression suite (162) | **162 passed, 0 failed, 0.16 s** |
| `tests/unit/test_observability.py` (updated, §12) | **42 passed** |
| Whole project suite (unmodified tests) | 1881 passed, 3 failed, 3 skipped, 95.15 s |
| Whole project suite with §12 applied | **1887 passed, 3 skipped, 95.25 s** |
| Extended matrix | **101/101** |

Per-test coverage with the mechanism that makes each one pass:
**`f3a1-qa-coverage-matrix-r3.md`** (89 gate rows + 162 regression rows).

### 13.3 The C-12 acceptance count

**1887 passed, 3 skipped** is the acceptance count, and it is now *measured*
rather than projected:

```text
1887 passed = 1881 passed (unmodified suite, 3 of which fail in the 39-test module)
            -   36 (the module's currently-passing tests)
            +   42 (the same module after §12: 39 original + 3 new)
            = 1887 passed, 3 skipped  (1890 collected)
```

The 3 skips are pre-existing and unrelated (`test_package_surface.py` reports
that four packages declare no `__all__`). The 3 failures without the §12 edits
are the same two functions Session D already approved in revision 2; no new
failure appeared.

`email_hash` and the public surface are pinned by 9 regression tests (§13.5);
the implementation PR must keep them, and the count above only holds if it does.

### 13.4 The reproducible regression suite (new) — why it exists

The 89-test gate is a point-in-time adversarial review. It did not contain a
PEM case, did not iterate the structural key set against free text, and did not
encode a payload twice — which is exactly why C-8, C-9 and C-10 reached
revision 2. The fix is not "test more manually"; it is a second, *permanent*
suite that the implementation PR adopts verbatim:

`tests/unit/test_redaction_regression.py` — 162 tests, stdlib + pytest only,
0.16 s, no live services, no fixtures beyond constants.

| Class | Tests | Covers |
| --- | --- | --- |
| `TestPemLinearScanner` | 9 | 48 KB and 1 MB repeated `BEGIN`; repeated `BEGIN`/`END` pairs; a real PEM block; unterminated block fail-closed; markers with no body; the `summarize_payload` path at 48 KB and 1 MB; hash/status preserved while the text is withheld. **6 of 9 fail on revision 2** |
| `TestTextKeyPolicy` | 127 | **20 of 127 fail on revision 2**; **every** exact structural key (46) with a non-prefixed secret `hunter2` in free text; the same 46 structurally; 14 suffix/header spellings; the 5 keys named in C-9; short values under strong keys; bare `key` stays shape-gated; 13 benign identifiers must survive unchanged; text/structure agreement |
| `TestNestedEncodedJson` | 17 | **12 of 17 fail on revision 2**; double encoding × 5 keys; 1–4 encoding levels; nested-in-innocuous-field; nested escaped key; flat escaped key; 9 levels fail closed; benign nested text untouched; idempotence; `summarize_payload`; oversized escaped payload fails closed |
| `TestPublicApiRetained` | 9 | `email_hash` exported, case/whitespace normalisation, sha256 equality, distinctness; e-mails still redacted; `max_bytes` 0 and negative rejected; `redact_payload` exported; status vocabulary closed |

Each of the 162 was written against the failure first. Run against the
revision-2 prototype the suite reports **38 failed, 124 passed** (20
`TestTextKeyPolicy`, 12 `TestNestedEncodedJson`, 6 `TestPemLinearScanner`) —
the failures are the six blockers, not incidental assertions. The C-8 tests
assert budgets rather than exact durations, so they are stable on slow CI:

| Path | 48 KB | 1 MB |
| --- | --- | --- |
| `redact_text` | < 0.2 s | < 0.5 s |
| `summarize_payload` | < 0.5 s | < 1.0 s |

Measured revision 3: 0.64 ms / 13.21 ms and 0.89 ms / 17.46 ms — two to three
orders of magnitude under budget. Against revision 2 the same inputs take
1.19 s and 65.1 s and fail on both budget and wall clock.

### 13.5 Public surface retained (C-12)

| Name | Contract | Pinned by |
| --- | --- | --- |
| `email_hash(address)` | `sha256(address.strip().lower().encode()).hexdigest()`; stable across processes and runs; the same normalisation the e-mail redactor uses | 4 tests |
| `REDACTED` | the literal `"[REDACTED]"` | existing + gate |
| `redact_text(text)` | `str` in, `str` out; `TypeError` otherwise | existing |
| `redact_mapping(mapping)` | mapping in, mapping out; new keys never collide | existing |
| `redact_payload(value)` | any JSON-shaped root; new export | 1 test |
| `summarize_payload(value, max_bytes)` | `INLINE` / `TRUNCATED` / `WITHHELD`; `max_bytes <= 0` raises `ValueError` | 3 tests |
| `REDACTED_KEY` | the literal `"[REDACTED_KEY]"`; new export | coverage matrix |

`email_hash` is **not** removed, renamed, re-scoped or re-salted. It stays a
pure function of the normalised address, so existing stored hashes remain valid
and correlation across logs still works. The redactor still calls it with the
same normalisation, so a redacted e-mail and its hash agree.

## 14. Performance evidence  ⟦R3⟧

Best of five runs, Python 3.11, project virtualenv, `time.perf_counter()`.
Budgets are the assertions shipped in the regression suite / extended matrix.

### 14.1 The C-8 repair (the reason for revision 3)

| Input | Revision 2 | Revision 3 | Budget |
| --- | --- | --- | --- |
| 48 KB `"-----BEGIN A-----" × n` | **1 191.81 ms** | **0.64 ms** | 200 ms |
| 200 KB same | ≈ 20 s (extrapolated quadratic) | **2.61 ms** | — |
| 1 MB same | **65 137.98 ms** | **13.21 ms** | 500 ms |
| 4 MB same | ≈ 260 s (extrapolated) | **53.96 ms** | — |
| 76 KB `BEGIN`/`END` pairs (2 000) | not measured (body bounded per pair) | **0.83 ms** | 500 ms |
| 1 MB `BEGIN`/`END` pairs | not measured | **18.44 ms** | — |
| `summarize_payload`, 48 KB markers | — | **0.89 ms** | 500 ms |
| `summarize_payload`, 1 MB markers | — | **17.46 ms** | 1 000 ms |

The budget column is the assertion shipped in `f3a1-regression-r3.py`
(`TestPemLinearScanner`), not an aspiration.

Growth from 1 MB to 4 MB is 13.21 → 53.96 ms — 4.08× for 4× the input, i.e.
linear with a small constant. Revision 2 grew 54× from 48 KB to 1 MB (54.6×
input, 54.6× time from an O(n²) shape whose constant is the 64 KB window).

### 14.2 Baseline shapes (unchanged from revision 2)

| Input | Revision 2 | Revision 3 | Budget |
| --- | --- | --- | --- |
| 48 KB `"key" * 16 000 + "="` | 1.59 ms | **1.91 ms** | 200 ms |
| 32 KB `"a-" * 16 000 + "key"` | 1.11 ms | **1.29 ms** | 200 ms |
| 48 KB `"a.b" * 16 000` | 0.47 ms | **0.53 ms** | 200 ms |
| 48 KB `"a1." * 16 000 + "@b."` | 0.80 ms | **0.94 ms** | 200 ms |
| 48 KB `"\\u005f" * 8 000` | 1.8 ms (leaked) | **1.84 ms** | 200 ms |
| 1 MB prose | 10.9 ms | **11.85 ms** | — |
| 1 MB `"a=" * 500 000` | 10.8 ms | **12.14 ms** | — |
| 110 KB `password=…` × 5 000 | 41.8 ms | **39.41 ms** | — |

The `\\u005f` run is the shape that broke the *first* C-8 patch: tolerating
escape runs without possessive quantifiers and the backslash-excluding
lookbehind made it 113 ms, which the extended matrix caught. The shipped
combination is 1.84 ms — statistically the same as revision 2, because the
lookbehind means the pattern never starts inside the run.

### 14.3 Structural, summary and nested-encoding paths

| Input | Revision 3 | Budget |
| --- | --- | --- |
| `summarize_payload`, 256 KB JSON string | **15.10 ms** | — |
| `summarize_payload`, 5 000-key mapping | **30.01 ms** | — |
| `redact_payload`, 1 MB structured | **0.12 ms** | — |
| double-encoded payload (8 KB padding) | **0.67 ms** | — |
| 9-level encoded payload | **0.31 ms** | — |
| benign 256 KB JSON with escaped quotes | **5.08 ms** | — |

The probe is not on the hot path: it exits on the first `if '\\"' not in text`
for every input that contains no escaped quote, which is almost all log text.
Where it does run, it is bounded by `_MAX_NESTED_PROBE` and nine replace/scan
passes.

## 15. Residual risks  ⟦R3 updated⟧

These are **not fixed**. They are documented so the implementation and the A2
review can treat them as accepted, deferred or blocking. "Direction" says what
happens when the risk materialises: *leak* (a secret survives), *fidelity* (a
benign value is lost), *usability*.

| # | Risk | Direction | Status / decision |
| --- | --- | --- | --- |
| R1 | Prose secret with no known prefix (`"the password is hunter2"` — note: no separator) | leak | Out of scope by design; `SecretStr` prevents construction-level leakage; provider prefixes are the backstop. **Unchanged.** |
| R2 | Bearer credential shaped like an artefact ID (`ABC-2026-XYZ`) | leak | Allowlist required by QA `B-11`; still caught under `Authorization:` or a sensitive key. **Unchanged.** |
| R3 | Over-redaction of the bare key `key` in structured args (`{"key": "X-120"}`) | usability | Accepted; separate replayable-args redactor deferred. **Unchanged.** |
| R4 | `input` / `input_value` redacted in unrelated structured payloads | usability | Accepted for the error-envelope guarantee. **Unchanged.** |
| R5 | NFKC rewrites benign compatibility characters | fidelity | Only when NFKC changes the text. **Unchanged.** |
| R6 | Structured string > 256 KiB is withheld entirely | fidelity | Fail-closed; digest retained. **Unchanged.** |
| R7 | Localised key names (`hasło`, `klucz`, `pwd`) are not in the vocabulary | leak | Deferred. C-9 widens the English key set, not the localisation. **Unchanged.** |
| R8 | Secret split across lines or containing spaces | leak | Text rules are line/token scoped; structural keys still redact. **Unchanged.** |
| R9 | Keys longer than 64 characters | leak | Not matched by the key pattern. C-9 does not change the cap. **Unchanged.** |
| R10 | Binary/undecodable bytes | fidelity | Decoded with `errors="replace"`. **Unchanged.** |
| R11 | `[REDACTED]` used as a literal value | ambiguity | Indistinguishable from a redacted value. **Unchanged.** |
| R12 | A2 `hide_input_in_errors` hides input from `str(e)` but `.errors()`/`.json()` still carry it | leak if rendered raw | **Corrected scope in §10.2**: raw-by-design, safe rendering mandatory (`safe_validation_report` / `redact_mapping`), AC-A2-4/5 assert it. Still a policy risk, not a code guarantee. |
| R13 | The gate measures a prototype, not the repository | process | §16 requires gate + regression suite against the real module before merge. **Unchanged.** |
| **R14** | **9+ encoding levels withhold the whole string** | fidelity | New in revision 3 (C-10 bound). `_MAX_ESCAPE_DEPTH = 8`; beyond it the probe returns `[REDACTED]`, losing the payload. Fail-closed by choice; a secret at depth 9 is removed, but so is everything else on the line. |
| **R15** | **Escaped JSON > 256 KiB containing a credential word is withheld** | fidelity | New in revision 3 (`_MAX_NESTED_PROBE`). A large benign payload that happens to contain `key`, `token`, `secret`, `pass`, `auth`, `session`, `email` … and an escaped quote is withheld. Measured: the benign 256 KB case is preserved only because it has no credential word. |
| **R16** | **PEM blocks > 1 MiB are withheld to end of input** | fidelity | New in revision 3 (`_MAX_PEM_BLOCK`). A real 2 MiB PEM would take the rest of the log line with it. Fail-closed; no leak. |
| **R17** | **Text tier is now exactly the structural tier minus `key`** | usability | New in revision 3 (C-9). Any future structural key automatically becomes prose-strong. That is the intent, but it removes the old "carefully curated prose" safety net; new keys must be reviewed for prose false positives. Pinned by 13 benign-identifier tests. |
| **R18** | **`key: Warsaw2024` / value with digits under bare `key` is redacted** | usability | Carried from revision 2, re-confirmed. `Warsaw2024` looks credential-shaped (letters + digits, 11 chars). Business identifiers with digits under bare `key` are lost. |
| **R19** | **`email_hash` is unsalted sha256** | privacy | Retained deliberately for C-12/back-compat. Anyone with a candidate address can confirm it from a hash. Changing it would break stored hashes; a keyed hash is an A2+ decision, not A1. |

R14–R17 exist **because** C-8/C-9/C-10 were fixed: every bounded scanner and
every fail-closed guard trades fidelity for a hard limit. They are listed here
so that trade is visible rather than implied.

## 16. Acceptance criteria for implementation  ⟦R3⟧

The design is ready to implement when the owner accepts the following. The
implementation PR is mergeable only when **all** of them hold.

**Design acceptance (this document)**

1. `redact_payload` is the single entry point for arbitrary payloads;
   `redact_text` and `redact_mapping` keep their signatures.
2. The vocabulary (§6) is the intended policy: the text tier is **derived**
   from `_SENSITIVE_KEY_EXACT` + suffixes minus bare `key` (C-9), and bare `key`
   keeps the credential-shape rule.
3. Bounded header values, with end-of-line redaction limited to
   `Authorization`/`Proxy-Authorization`, and the business-ID allowlist for bare
   `Bearer` are the intended behaviour.
4. Deterministic fail-closed behaviour is acceptable for: an unterminated PEM
   block, a PEM block > 1 MiB, structured text > 256 KiB, escaped text > 256 KiB
   containing a credential word, and 9+ encoding levels (R6, R14–R16).
5. `max_bytes <= 0` raises `ValueError`.
6. NFKC is applied only when it changes the text, and only to non-ASCII input.
7. The two existing tests in §12 are updated exactly as shown, and no other
   existing test is changed.
8. **C-11:** A2 targets `_ConfiguredModel`, never `DomainModel`, and treats
   `.errors()`/`.json()` as raw-by-design behind `safe_validation_report`.
   A2 is a separate PR and is **not** implemented by A1.

**Implementation acceptance (F3-A1)**

9. **C-8:** `_PEM_RE` does not exist in the merged module. A 1 MiB repeated
   `BEGIN` input completes within the shipped budgets (< 0.5 s
   `redact_text`, < 1 s `summarize_payload`) and the result contains
   `[REDACTED]`; an unterminated block is withheld.
10. **C-9:** for **every** name in `_SENSITIVE_KEY_EXACT`, a non-provider
    secret (`hunter2`) is redacted in free text *and* structurally, and
    `_key_might_be_sensitive(name)` is true for every one. No second
    hand-maintained strong-key list exists.
11. **C-10:** a double-encoded JSON secret, a secret nested in an innocuous
    field and an escaped key are all redacted by `redact_text`; 9 levels yields
    `[REDACTED]`; benign escaped text without a credential word is unchanged.
12. `tests/unit/test_redaction_adversarial.py` exists at the QA commit's content
    (byte-identical) and **89/89 pass** against the real `redaction.py`.
13. `tests/unit/test_redaction_regression.py` exists at `f3a1-regression-r3.py`'s
    content and **162/162 pass**; together with the gate that is **251 tests**.
14. `pytest -m "not live"` is green: **1887 passed, 3 skipped** (C-12). No new
    failure beyond the two §12 functions, and those are updated, not skipped.
15. `email_hash`, `REDACTED`, `redact_text`, `redact_mapping`, `redact_payload`,
    `summarize_payload`, `REDACTED_KEY` and the `INLINE`/`TRUNCATED`/`WITHHELD`
    vocabulary are all importable from `rfq_agent.observability.redaction` and
    behave as §13.5 states. `email_hash` is unchanged: sha256 of the lowercased,
    stripped address.
16. `ruff check .` and `ruff format --check .` pass (line length 100, existing
    rule set; `except Exception` carries `# noqa: BLE001`).
17. No new dependency; `redaction.py` imports only stdlib and
    `rfq_agent.domain.values`.
18. Idempotence holds for every test corpus: `redact(redact(x)) == redact(x)`.
19. The extended matrix (`f3a1-extended-matrix.py`) passes unchanged: 101/101.
20. No file outside the §11.1 list is touched.
21. The three C-8/C-9/C-10 regressions are **falsifiable**: each fails against
    the revision-2 prototype. Recorded in §2.

**Implementation acceptance (F3-A2, separate PR)**

22. AC-A2-1 … AC-A2-8 (§10.2) all pass.
23. §11.2 file list only; A2 is not merged before A1.
24. Config rendering keeps host/port/path visible and the real values
    reachable.
25. No logger, HTTP body, task result or trace attribute receives
    `e.errors()`/`e.json()` directly (AC-A2-5).

**Reporting rule.** A green `pytest -m "not live"` on the *existing* suite is
**not** evidence of safety: at baseline those tests pass while 84 of the 89
adversarial tests fail, and revision 2 passed 89/89 while still failing C-8,
C-9 and C-10. The gate is gate **plus** regression suite, not the project
suite.

## 17. Provenance  ⟦R3⟧

* Repository baseline `d5611eef` — verified clean before and after this
  session. No commit, no branch change, no tracked file modified.
* Design under revision: `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`
  (`docs/reviews/f3a/design-revision.md`, `design-prototype.py`).
* **Revision 2, returned REVISE:** `3469b00ce0cbfd18f1377600b4f35d2872bb7303`
  (`docs/reviews/f3a/revision-2/`: design, prototype, coverage matrix, extended
  matrix, shim). Artefacts were re-extracted from that commit and their SHA-256
  matched the delivered files before revision 3 began.
* Adversarial gate: `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`
  (`docs/reviews/f3a/adversarial-qa-report.md`,
  `docs/reviews/f3a/test_redaction_adversarial.py`), executed unmodified.
* Artefacts delivered with this document (all outside the repository):
  * `f3a1-prototype-r3.py` — reference prototype revision 3 (non-normative)
  * `f3a1-regression-r3.py` — the 162-test regression suite (§13.4)
  * `f3a1-qa-coverage-matrix-r3.md` — 89 gate rows **plus** 162 regression rows
  * `f3a1-extended-matrix.py` — 101-assertion matrix beyond the gate
  * `f3a1-test-shim.py` — the `sitecustomize` used to inject the prototype
* Commands used: `git show`, `git fetch` (for the revision-2 and QA commits,
  whose objects are not in the default clone), `pytest -m "not live" -q`,
  `pytest test_redaction_adversarial.py -v`, `pytest
  test_redaction_regression.py -q`, `time.perf_counter()` benchmarks,
  `ValidationError.errors()/.json()` probes on `_ConfiguredModel` subclasses.
* **Session D blockers:** C-8, C-9, C-10, C-11, C-12 and the regression-suite
  requirement are each answered in §0 with a measurement; nothing in this
  revision claims they were fixed *in the repository*.

**Status: application code modified NO · tracked files modified NO · tests added
to the repository NO · commits created NO · PR created NO · implementation
authorized NO · independent ACCEPT claimed NO.**
