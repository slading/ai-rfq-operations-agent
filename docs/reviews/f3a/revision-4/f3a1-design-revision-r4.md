# F3-A1 Design Revision 4 — Container, Escape-Boundary and Pydantic Closure

**Project 04 / Session A — read-only design correction.**
Repository `slading/ai-rfq-operations-agent`, baseline `d5611eefab3b326e9ad5caa9789d892e69ce3875`.
Design under revision: commit `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`.
Revision 3 (superseded by this document): commit `bee310553fb88701d63d0c7a6571e4bb2e9f36c0`.
Independent adversarial gate: `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.

| Gate | Status |
| --- | --- |
| Application code modified | **NO** |
| Tracked files modified | **NO** |
| Tests added to the repository | **NO** |
| Commits created | **NO** |
| PR created / merged | **NO** |
| Implementation authorized | **NO** |

**Verdict: REVISION 4 SUBMITTED — F-1 (container-valued keys), F-2 (escape-run
boundary) and F-3 (Pydantic contract) are closed in the reference prototype and
the design. Not an ACCEPT and not a claim of one.**

> **I am not claiming independent ACCEPT.** Revision 3 passed 89/89 and 101/101
> and still returned REVISE: the gate had no container case and stopped at eight
> escape levels, and the A2 section named the wrong base class. Revision 4 fixes
> the behaviour and adds 57 regression tests that fail on the revision-3
> prototype. The design is not implemented; §16 lists what must be true before
> implementation is authorized and §15 lists what is still not protected.

| Measurement | Result |
| --- | --- |
| QA gate (89 tests, unmodified) | **89 passed, 0 failed, 0.10 s** |
| Regression suite, revision 4 (219 tests) | **219 passed, 0 failed, 0.30–0.35 s** |
| The same suite against the revision-3 prototype | **37 failed, 182 passed** (falsifiability) |
| Extended matrix beyond the gate | **101/101** |
| Project suite (unmodified tests) | 1881 passed, 3 failed, 3 skipped, 86.66 s |
| Project suite with the two approved §12 test edits | **1887 passed, 3 skipped** |
| `tests/unit/test_observability.py` with §12 applied | **42 passed** |
| F-1 container leaks (`credentials`/`api_key`/`token`/`secret`) | **4/4 leaked → 0/4** |
| F-1 nested/repr/YAML/`input_value` shapes | **leaked → redacted or withheld** |
| F-2 escape runs | 8 readable, 9–16 **leaked** → **16 readable, 17+ withheld** |
| 48 KB / 1 MB repeated `BEGIN` (revision-3 guarantee) | 0.57–0.61 / 12.0–12.1 ms — **unchanged** (§14) |

The three remaining project failures are the same two functions as revision 3
(three test IDs), caused by the approved §12 test updates, not by this change.
With §12 applied the suite is exactly **1887 passed, 3 skipped**.

---

## 0. Revision 4 delta — the independent review's findings

Revision 3 passed every gate it had. The review found three things the gate did
not measure. Each is now closed **and** pinned by a test that fails on the
revision-3 prototype.

| ID | Finding | Revision 4 correction | Proof |
| --- | --- | --- | --- |
| **F-1** | A strong key followed by an object/array leaked the leaf value. Revision 3 did not merely miss it: it produced a **partial redaction** — `{"credentials": {"value": "hunter2024"}}` became `{"credentials": [REDACTED]: "hunter2024"}}`. Same for repr dicts, nested JSON strings, YAML blocks and pydantic `input_value` dicts/lists. | `_redact_key_values` now dispatches on the value shape before the scalar token walk: **container** (`_consume_container`), **quoted string** (`_quoted_end`), **YAML block** (`_yaml_block_bounds`). A container is measured exactly with `json.raw_decode`, or by a bounded string-aware bracket scan for repr/YAML/nested-escaped forms. Unterminated, mismatched, deeper than 32 or larger than 64 KiB ⇒ **withhold to end of text**. | All four review shapes plus repr/YAML/double-encoded/pydantic are redacted; malformed and unterminated containers end in `[REDACTED]`; 31 regression tests, **22 fail on revision 3**. |
| **F-2** | Nine backslashes before `u005f` defeated the scanner (pattern allowed 1–8) and the probe never ran, because it only triggered on an escaped quote. Runs 9–16 leaked. | The scanner tolerates **16** runs; the probe gate is now `escaped quote OR a 17+ backslash run`; the probe stops only when no `\\uXXXX` remains. Deeper runs are unescaped level by level (bounded at 9 passes / 256 KiB) and **withheld** if still escaped. | Runs 1–16 redacted in place; 17, 24, 32, 64 and 1 000 withheld; benign escaped keys survive; 26 tests, **15 fail on revision 3**. |
| **F-3** | Revision 3's A2 section was factually wrong: it said `Settings` derives from `_ConfiguredModel`, implied `_ConfiguredModel` needed no class-definition change, and used a runtime `model_config` mutation as its verification. | §10.2 is rewritten from the source: `Settings(BaseSettings)` **already** has `hide_input_in_errors=True`; the **eight** section models inherit `_ConfiguredModel`, which does **not**; A2 edits the `_ConfiguredModel` class body; `DomainModel` is untouched; `.errors()`/`.json()` stay raw; `safe_validation_report` is **A2 scope, not A1**. Verification uses a class-definition subclass, never runtime mutation. | `f3a2-follow-up-contract.md` + §10.2, measured against the real classes this session. |

**What did not change.** The pipeline order (except the new per-key dispatch),
the vocabulary, `summarize_payload`, the structural walk, `email_hash`, every
public signature, the C-8 linear PEM scanner, the C-9 derived text tier and the
C-10 probe are revision 3's. Revision 4 adds three value-shape branches inside
`_redact_key_values` and widens one bound.

---

## 0.1 Revision 3 (C-8 … C-12) — carried forward unchanged

Session D returned **REVISE** on revision 2 despite 89/89 and 101/101. The
blockers were gaps *outside* the gate's coverage, so every one of them is
closed **and** covered by the new regression suite (§13.4) rather than by
arguing that the existing gate already proves it.

| ID | Revision 2 defect | Revision 3 correction | Proof |
| --- | --- | --- | --- |
| **C-8** | `_PEM_RE` = `-----BEGIN …-----([\s\S]{0,65536}?)-----END …-----` is quadratic: the lazy body retried at every position. 48 KB of `BEGIN` markers took 1 191.81 ms; 1 MB took 65 137.98 ms (denial of service through a log record). | `_redact_pem()` — one `finditer` over the marker list, `BEGIN` paired with the next `END`, single cursor walk, O(n) with a constant per marker. A block longer than `_MAX_PEM_BLOCK` (1 MiB) and any unterminated `BEGIN` are **withheld** to the end of the text (fail-closed). No lazy body quantifier anywhere. | 48 KB **0.64 ms**, 1 MB **13.21 ms**, 4 MB **53.96 ms** (linear). `summarize_payload` 48 KB **0.89 ms**, 1 MB **17.46 ms**. 9 regression tests. |
| **C-9** | The text path used a hand-maintained 36-name `_STRONG_TEXT_KEYS` subset of the 46-name structural set. Ten exact names leaked with a non-provider secret: `auth`, `authentication`, `customeremail`, `email`, `emailaddress`, `input`, `passphrase`, `senderemail`, `sessionid`, `useremail` (`session_id` too). | The text tier is **derived** from the structural policy: `_is_strong_text_key(k) = _is_sensitive_key(k) and _normalize_key(k) not in {"key"}`. The cheap substring gate `_KEY_HINTS` is widened to `pass`, `session`, `email` so no sensitive key can be skipped before the scanner runs. `_STRONG_TEXT_KEYS` is deleted — there is no second list to drift. | 0/46 leaks in text; 0/46 in structure; 46 parametrised text tests + 46 structural + 14 suffix/header spellings + 5 named C-9 keys + 13 benign identifiers + 3 tier/agreement tests = 127 in the class. |
| **C-10** | A secret inside JSON that was encoded twice (or nested inside an innocuous field) survived: `json.dumps(json.dumps({"api_key": "hunter2"}))`, nested-in-`note`, nested escaped key. | The key/separator scanner tolerates **1–16 backslashes** before a quote or before `u` (revision 4, F-2; revision 3 read 8), so it reads 1–16 encoding levels directly (`\\u005f` decodes to `_`). Anything still escaped after the pipeline goes through `_nested_escape_probe()`: up to `_MAX_ESCAPE_DEPTH = 8` unescape levels are re-scanned with the key/header rules; a payload larger than `_MAX_NESTED_PROBE = 256 KiB`, or still escaped after nine unescape passes, is **withheld** wholesale. The probe itself is linear (no regex, no re-parse). | Double, triple, 4-level and 17-level payloads redacted; 17+ levels → `[REDACTED]`; benign `say "hello"` unchanged; benign 300 KB escaped JSON preserved byte-for-byte; 17 nested tests plus 26 escape-boundary tests. |
| **C-11** | §10.2 told A2 to set `hide_input_in_errors` on **`DomainModel`**. The leaking config classes are `DatabaseSettings`, `GroqSettings` and `Settings`, which derive from **`_ConfiguredModel`**; `DomainModel` is untouched by them. The design also implied `hide_input_in_errors` was sufficient — it is not: `.errors()` and `.json()` still carry the raw input. | §10.2 retargeted to `_ConfiguredModel` (inherited by every config section, including nested `Settings`). A **safe rendering policy** is specified: `str(e)`/`repr(e)` are made input-free by the flag; `.errors()` and `.json()` are documented as raw-by-design and must be rendered through the A1 redactor (`redact_mapping({"errors": e.errors()})`) or the new `safe_validation_report()` wrapper. A2 remains **design only** — no A2 code is written here. | In-memory probe, this session: raw `str(e)`/`.errors()`/`.json()` all leak for `DatabaseSettings`, `GroqSettings`, `Settings` and a `DomainModel` subclass; `hide_input_in_errors=True` on `_ConfiguredModel` removes the leak from `str(e)` only; `redact_mapping` removes it from `.errors()` and from `json.loads(e.json())`. |
| **C-12** | Revision 2 projected "1884 passed" and did not state the `email_hash` contract. | Acceptance count is **1887 passed, 3 skipped**, measured (§13.3). `email_hash` is explicitly retained: `sha256(lowercase(strip(address)))`, exported in `__all__`, stable across runs, `redact_*` never changes its input normalisation. The rest of the public surface (`REDACTED`, `redact_text`, `redact_mapping`, `summarize_payload`, plus the new `redact_payload`, `REDACTED_KEY`) and the `INLINE`/`TRUNCATED`/`WITHHELD` status vocabulary are pinned by tests. | 9 public-API regression tests; full-suite measurement. |
| **7** | The 89-test gate does not cover PEM, key-policy parity or nested JSON — that is why C-8/C-9/C-10 escaped revision 2. | A **new reproducible regression suite**, `tests/unit/test_redaction_regression.py` in the implementation PR, outside the 89-gate, run in the default job. Each C-blocker test is written so it **fails against the previous revision**. | 162 at revision 3 (38 fail on revision 2); **219 at revision 4** (37 fail on revision 3); 308 tests across gate + regressions. |

**Scope discipline.** No architecture changed. The pipeline order, public
signatures, structural walk, `summarize_payload` semantics, vocabulary (except
deriving the text tier from it) and implementation boundary are revision 2's.
The three C-8/C-9/C-10 corrections are local to three functions plus the
vocabulary gate.

---

## 1. What changed in revisions 1–3

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
* The regression suite (`f3a1-regression-r4.py`, **219 tests**) was executed
  against the same shim. It is revision 3's 162 tests plus **57 new F-1/F-2
  tests**. It lives outside the repository like the rest of the evidence; the
  implementation PR adds it unchanged as
  `tests/unit/test_redaction_regression.py`.
* **F-3 claims were checked against the source, not against a summary:**
  `src/rfq_agent/config.py` was read directly (`Settings(BaseSettings)` with
  `hide_input_in_errors=True` already present; `_ConfiguredModel` without it;
  eight subclasses), and the behavioural probe used **class-definition
  subclasses**. Runtime mutation of `model_config` is not used anywhere in this
  document: pydantic builds its core schema at class creation, so mutating the
  dict afterwards proves nothing about the shipped class.
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
* Reproduce everything (artefacts: `f3a1-prototype-r4.py`,
  `f3a1-regression-r4.py`, `f3a1-test-shim.py`, `f3a1-extended-matrix.py`,
  `f3a2-follow-up-contract.md`):

  ```bash
  python -m venv .venv && .venv/bin/pip install -e ".[dev]"
  git show 28af34cb5e3cdb936d087a0df5b536dc23ee40c4:docs/reviews/f3a/test_redaction_adversarial.py \
      > /tmp/test_redaction_adversarial.py
  mkdir -p /tmp/f3a && cp f3a1-test-shim.py /tmp/f3a/sitecustomize.py
  cp f3a1-prototype-r4.py /tmp/f3a/f3a1_prototype.py

  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest /tmp/test_redaction_adversarial.py -q   # 89
  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest f3a1-regression-r4.py -q                 # 219
  PYTHONPATH=src:/tmp/f3a .venv/bin/python f3a1-extended-matrix.py                            # 101
  PYTHONPATH=src:/tmp/f3a .venv/bin/python -m pytest -m "not live" -q                         # 1881/3/3
  ```

  The last line needs the two §12 test edits to reach 1887; without them it
  reports the three expected failures and nothing else.

* **Previous-revision baseline for every new test.** A regression that cannot
  fail on the previous revision is not evidence, so both runs are part of the
  deliverable:

  | Suite run against | Result | What failed |
  | --- | --- | --- |
  | revision-2 prototype (162 tests) | 38 failed, 124 passed | 20 key policy, 12 nested encoding, 6 PEM |
  | revision-3 prototype (219 tests) | **37 failed, 182 passed** | 22 container (F-1), 15 escape-boundary (F-2) |

  The 20 new tests that pass on revision 3 are the preservation half of the
  suite: benign containers, idempotence, structural containers and the
  `email_hash`/summary contracts. They exist so a future fix cannot "close"
  F-1 by destroying benign text.

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

### C-6 — ValidationError hardening split across A1 and A2  ⟦R4⟧

| Layer | Owner | Mechanism | Acceptance criterion |
| --- | --- | --- | --- |
| Structured error fields | **A1** | `input` and `input_value` are sensitive keys; `_redact_string` walks list/dict error records | `redact_mapping({"errors": e.errors()})` contains no intact ≥ 8-char fragment for **any** secret shape |
| Exception message | **A1** | provider-token backstop **and** the `input_value=…` key/value form, including dict/list values (F-1) | `redact_text(str(e))` is clean for provider-shaped and for container-shaped `input_value`s |
| Raw exception | **A2** | `_ConfiguredModel` class body gains `hide_input_in_errors=True` (class definition, not runtime) | `str(ValidationError)` from any of the **eight** section models leaks no input; `Settings` already had the flag and is unchanged |
| Safe rendering of `.errors()`/`.json()` | **A2** | `safe_validation_report()` / `redact_mapping` | no intact ≥ 8-char fragment in the rendered dict, including `json.loads(e.json())` |

Measured for A1, three secret shapes (revision 3, unchanged):

| Secret | `redact_text(str(e))` leak | `redact_mapping({"errors": e.errors()})` leak |
| --- | --- | --- |
| `gsk_FAKEsupersecret0123456789` | no | no |
| `hunter2` | no | no |
| `correct-horse-battery` | no | no |

Measured for A2 against the real classes this session. The "after" column uses
a **class-definition** subclass (`class Hardened(DatabaseSettings):
model_config = ConfigDict(**{**DatabaseSettings.model_config,
"hide_input_in_errors": True})`) — the same mechanism the A2 patch uses on
`_ConfiguredModel`; no runtime mutation:

| Model | Base | `str(e)` today | `.errors()` today | `.json()` today | `str(e)` after the class-level flag |
| --- | --- | --- | --- | --- | --- |
| `RetryPolicy`, `AgentSettings`, `TrustSettings`, `WorkflowSettings`, `BusinessSettings`, `HttpSettings` | `_ConfiguredModel` | **yes** | **yes** | **yes** | **no** |
| `DatabaseSettings` | `_ConfiguredModel` | **yes** | **yes** | **yes** | **no** |
| `GroqSettings` | `_ConfiguredModel` | **yes** | **yes** | **yes** | **no** |
| `Settings` | `BaseSettings` | **no (already hidden)** | **yes** | **yes** | no change needed |
| `DomainModel` subclass (control) | `BaseModel` | **yes** | **yes** | **yes** | **no — A2 must not touch it** |

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
5. Key + value scan               key, separator (= : ,); strong tier dispatches on the
                                  value shape — container / quoted string / YAML block /
                                  scalar — and replaces the whole value; weak tier keeps
                                  the credential-shape test on the scalar token
6. Bearer                         Bearer <credential> -> Bearer [REDACTED]
                                  (business-ID-shaped tokens exempt)
7. Provider tokens                gsk_ / sk- / ghp_ / AKIA / xox… / hf_ / npm_ -> [REDACTED]
8. E-mail                         token pass + quoted-local pass        -> [REDACTED]
9. Nested-escape probe            escaped quote, or a 17+ backslash run before uXXXX
                                  -> 9 bounded unescape levels re-scanned; clean ->
                                  unchanged, still escaped or > 256 KiB -> [REDACTED]
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
    r"(?P<key>(?:[A-Za-z0-9_.\-]|\\{1,16}+u[0-9a-fA-F]{4}){1,64}+)"
    # A quoted key or value is escaped once per encoding level; 0-8 backslashes
    # covers every real serialisation. Deeper nesting is caught by the probe.
    r"(?P<sep>(?:\\{0,16}[\"'])?\s*[:=,]\s*)"
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
  escape run (`\\{1,16}+`). Without both, a 48 KB run of `\\u005f` costs
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
  `{0,65536}`, `{1,512}`, `{1,16}`, `{1,16}+`). The nested-escape probe is a
  bounded loop of `str.replace` calls (`_MAX_ESCAPE_DEPTH + 1` iterations, each
  linear), guarded by `_MAX_NESTED_PROBE` so a large payload is withheld rather
  than unescaped.
* Container scanning adds work only for a **strong key followed by `{`/`[`**
  (F-1). Each region is consumed once — the cursor jumps past it, so the total
  is linear in the text, with a 64 KiB and depth-32 bound per container. A
  failed container withholds the remainder and returns, so a hostile line of
  unterminated openers costs one bounded scan, not one per opener.

### 5.4 Container, quoted and YAML values (F-1)  ⟦R4⟧

Revision 3's scanner reduced every value to a whitespace-delimited token. For a
container value it skipped the opening brace as a "wrapper", found the next
quote, and redacted the word between them — producing a **partial** redaction
that left the leaf visible:

```text
in : {"credentials": {"value": "hunter2024"}}
r3 : {"credentials": [REDACTED]: "hunter2024"}}      # leaked
r4 : {"credentials": [REDACTED]}                     # whole value replaced
```

Revision 4 dispatches on the shape immediately after the separator, in this
order, and only for a strong key:

| Value shape | Detection | Replacement |
| --- | --- | --- |
| container | next non-space byte is `{` or `[` | whole balanced container → `[REDACTED]` |
| quoted string | next non-space byte is `"` or `'` | string **content** → `[REDACTED]`, quotes kept |
| YAML block | separator crossed a newline and the next line is more indented and looks like `name:` / `- item`; or the key line ends in a block-scalar marker | the whole indented block → `[REDACTED]` |
| scalar | anything else | revision-3 token rule (unchanged) |

**Container scanner.** `_consume_container(text, pos)` returns the index just
past the matching closer:

```python
def _consume_container(text: str, pos: int) -> int | None:
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)          # 64 KiB
    try:
        _, end = _JSON_DECODER.raw_decode(text, pos)            # exact for JSON
    except (ValueError, RecursionError):
        pass
    else:
        return end if end <= limit else None
    stack: list[str] = []
    quote = ""
    escaped_quotes = False
    for i in range(pos, limit):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            if i > pos and text[i - 1] == "\\":
                escaped_quotes = True          # a deeper encoding level
            else:
                quote = ch
        elif ch in "{[":
            if len(stack) >= _MAX_CONTAINER_DEPTH:   # 32
                return None
            stack.append(ch)
        elif ch in "}]":
            if not stack or _CONTAINER_PAIRS[stack[-1]] != ch:
                return None
            stack.pop()
            if not stack:
                ...  # end-of-container sanity check, see below
                return i + 1
    return None
```

Rules that matter:

* **Valid JSON takes the exact path.** `raw_decode` handles strings, escapes,
  braces inside quoted values and Unicode without heuristics. It is bounded by
  the size check; a larger container returns `None` (withhold).
* **Non-JSON takes the bounded fallback.** Python repr (`{'a': ...}`), YAML flow
  maps (`{value: ...}`) and nested-encoded strings are not valid JSON, so the
  bracket scan runs. A quote preceded by a backslash belongs to a deeper
  encoding level and does not toggle string state — that is what makes
  `"{\"credentials\": {\"value\": \"x\"}}"` readable.
* **Early close is detected.** In a nested-encoded string a brace *inside* a
  quoted value can close the region too early. When the fallback saw escaped
  quotes and the character after the closer is a letter/digit/underscore, the
  end is not trustworthy and the function returns `None` — withhold, never a
  partial redaction.
* **Failure is always fail-closed.** `None` makes the caller append `[REDACTED]`
  and stop: the remainder of the text is dropped. That is the only outcome that
  cannot leak.
* **YAML.** A block is recognised only when the separator crossed a newline
  (and the first line is more indented and matches `name:` / `- item` /
  `name: |`), or when the key line ends in a block-scalar marker. The block is
  consumed while lines are blank or deeper than the key. A bare indented scalar
  needs no special case: the separator's `\s*` already consumed the newline, so
  the revision-3 scalar rule redacts it.
* **Quoted strings** are content-redacted, quotes preserved, so existing gate
  expectations (`"api_key": "[REDACTED]"`) do not change. A quoted value that
  itself contains a container is therefore replaced wholesale, which is the
  correct answer for `{"credentials": "{\"value\": \"x\"}"}`.

| Input | Revision 4 output |
| --- | --- |
| `{"credentials": {"value": "hunter2024"}}` | `{"credentials": [REDACTED]}` |
| `{"token": ["x", "hunter2024"]}` | `{"token": [REDACTED]}` |
| `{"secret": {"a": {"b": "hunter2024"}}}` | `{"secret": [REDACTED]}` |
| `{'credentials': {'value': 'hunter2024'}}` | `{'credentials': [REDACTED]}` |
| `json.dumps(json.dumps({"credentials": {...}}))` | `"{\"credentials\": [REDACTED]}"` |
| `credentials:\n  value: hunter2024` | `credentials: [REDACTED]` |
| `{"api_key": {"user": "hunter2024"` | `{"api_key": [REDACTED]` (rest withheld) |
| `input_value={'value': 'hunter2024'}` | `input_value=[REDACTED]` |

### 5.5 Escape-run boundary (F-2)  ⟦R4⟧

```python
r"(?P<key>(?:[A-Za-z0-9_.\-]|\\{1,16}+u[0-9a-fA-F]{4}){1,64}+)"
r"(?P<sep>(?:\\{0,16}[\"'])?\s*[:=,]\s*)"
_ESCAPE_REMAINS_RE = re.compile(r"\\\\u[0-9a-fA-F]{4}")   # two+ backslashes
_DEEP_ESCAPE_RE = re.compile(r"\\{17,}u[0-9a-fA-F]{4}")     # more than 16
```

* One encoding level adds one backslash before `u` and before each quote.
  Revision 3's eight-level window left runs 9–16 unreadable **and** the probe
  never ran, because its only trigger was `\"`. The result was a clean leak.
* The scanner now reads sixteen levels. The probe is triggered by an escaped
  quote **or** a 17+ run; it unescapes one level per pass (bounded at nine
  passes) and re-scans. It stops only when no `\\uXXXX` remains, so a
  partially-unescaped view is never declared clean.
* Runs 17+ are therefore redacted when they can be resolved within the pass
  budget and **withheld** otherwise. Nothing is ever returned partially
  redacted.
* The performance profile is unchanged because the scanner never starts inside
  a backslash run: the negative lookbehind excludes `\`, and the run itself is
  possessive. 48 KB of `\\u005f` is 1.8 ms, the same as revision 3.

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
  expose a key body either. Measured at 48 KB / 1 MB: 0.83–0.85 / 16.5–16.9 ms (§14).

---

## 9. Escaped JSON and nested encodings (decision 8, C-10, F-2)  ⟦R4⟧

A JSON-encoded key can hide a credential name from a naive scanner:
`{"api\u005fkey": "…"}`. Revision 2 decoded escapes **inside the key** for
normalisation only. That is enough for one encoding level and for a payload
that is valid JSON, and it failed for everything else Session D tried: a secret
inside `json.dumps(json.dumps({...}))`, a JSON document nested in an innocuous
`note` field, and a key escaped inside a string that is not itself parsed.

### 9.1 Two levels of defence

**Level 1 — the scanner reads the escapes directly.** The key alternative and
the separator both tolerate a run of backslashes, so a key that is escaped once,
twice or **sixteen** times is still recognised as a key, and the value that
follows is redacted in place:

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

**Level 2 — a bounded, fail-closed probe.** Level 1 tops out at sixteen
encoding levels. Beyond that, the value cannot be located precisely without unescaping
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
* Revision 3 triggered on `'\\"'` only. That was F-2: a key escaped nine times
  with unescaped quotes never reached the probe. Revision 4 adds the deep-run
  trigger; the two-backslash substring test in front of it is a fast C scan, so
  ordinary prose still costs nothing (1 MB prose: 11.9 ms).
* A string containing a plain `"` is not escaped and does not trigger on that
  ground alone, so `say "hello"` in ordinary prose is untouched.
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
| nine encoding levels | redacted in place (scanner reads 1–16) |
| seventeen encoding levels | `[REDACTED]` (probe, fail-closed) |
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

## 10. ValidationError hardening — A1 vs A2  ⟦R4 corrected⟧

Revision 3's §10.2 contained three factual errors, all found by the independent
review (**F-3**): it said `Settings` derives from `_ConfiguredModel`; it implied
the flag was missing everywhere; and it verified the fix by mutating
`model_config` at runtime. This section is rebuilt from
`src/rfq_agent/config.py` at `d5611eef`. Every statement below is checkable with
`grep` or one `python -c` line.

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

and the rendered message. F-1 matters here: a dict/list `input_value` used to
survive because the text scanner only redacted the first token. Revision 4
redacts the whole container:

| Rendered surface | A1 result |
| --- | --- |
| `str(e)` with `input_value='<secret>'` | clean (scalar rule) |
| `str(e)` with `input_value={'value': '<secret>'}` | clean (`input_value=[REDACTED]`) |
| `str(e)` with `input_value=['x', '<secret>']` | clean |
| `e.errors()` raw | **leaks by design** — must be rendered through A1 |
| `redact_mapping({"errors": e.errors()})` | clean |

A1 does **not** change pydantic's behaviour. It provides the primitives; A2 is
the unit that applies them to the rendering surfaces.

### 10.2 Verified class facts (F-3)

| # | Fact | Source |
| --- | --- | --- |
| 1 | `class Settings(BaseSettings)` — **not** `_ConfiguredModel` | `config.py:248` |
| 2 | `Settings.model_config` **already** sets `hide_input_in_errors=True` | `config.py:263` |
| 3 | `class _ConfiguredModel(BaseModel)` with `ConfigDict(extra="forbid", frozen=True)` — **no** `hide_input_in_errors` | `config.py:94` |
| 4 | **Eight** models inherit `_ConfiguredModel`: `RetryPolicy`, `GroqSettings`, `AgentSettings`, `TrustSettings`, `WorkflowSettings`, `BusinessSettings`, `HttpSettings`, `DatabaseSettings` | `grep -c "^class .*(_ConfiguredModel)"` |
| 5 | `DomainModel` inherits `BaseModel` directly and is unrelated to configuration | `domain/values.py:28` |
| 6 | `Settings` has section fields of the `_ConfiguredModel` types, so a nested construction is validated inside `Settings` | `config.py:265+` |

Measured MROs:

```text
Settings         -> BaseSettings      -> BaseModel -> object
DatabaseSettings -> _ConfiguredModel  -> BaseModel -> object
GroqSettings     -> _ConfiguredModel  -> BaseModel -> object
DomainModel      -> BaseModel         -> object
```

Consequence, measured behaviour today (`hunter2024` where an `int` is required):

| Model | `str(e)` | `e.errors()` | `e.json()` |
| --- | --- | --- | --- |
| `DatabaseSettings` | **leaks** | **leaks** | **leaks** |
| `GroqSettings` | **leaks** | **leaks** | **leaks** |
| `Settings` (nested) | already hidden | **leaks** | **leaks** |
| `DomainModel` subclass | **leaks** | **leaks** | **leaks** |

So: the leak is the **eight section models**, `Settings` is already protected
for `str()`, and `DomainModel` is not part of this problem.

### 10.3 A2 — the exact change

One class body in `src/rfq_agent/config.py`:

```python
class _ConfiguredModel(BaseModel):
    """Base for configuration sections: immutable, no unknown keys."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,   # A2: added at class definition
    )
```

Rules:

1. **Class definition only.** Pydantic builds its core schema when the class is
   created; assigning into `model_config` afterwards does not rebuild it. No A2
   code or test may mutate `model_config` at runtime.
2. **`Settings` is not edited.** It already sets the flag on
   `SettingsConfigDict`; duplicating or moving it would change `env_prefix`
   behaviour for no benefit.
3. **`DomainModel` is not touched.** This is a configuration concern; domain
   schemas keep their current behaviour. Changing both bases would widen A2's
   blast radius for no security gain.
4. **`.errors()` and `.json()` stay raw.** The flag only affects
   `str()`/`repr()`. They remain the machine-readable contract and are never
   logged, returned in an HTTP body, stored in a task result or set as a trace
   attribute.
5. **`safe_validation_report` is A2 scope, not A1.** A1 ships the redactor; A2
   ships the wrapper and its tests.

```python
# src/rfq_agent/observability/redaction.py — added by A2, not A1
def safe_validation_report(exc: BaseException) -> dict[str, object]:
    """Render a ValidationError envelope with every input value redacted.

    ``ValidationError.errors()`` deliberately keeps the raw input for
    programmatic consumers. It must never be logged, printed or serialised
    directly; this function is the boundary.
    """
    if not isinstance(exc, ValidationError):
        return {"error": redact_text(str(exc))}
    return {
        "error_type": type(exc).__name__,
        "errors": redact_mapping({"errors": exc.errors()})["errors"],
    }
```

### 10.4 Valid verification (F-3)

| Check | Method |
| --- | --- |
| The flag is present | `_ConfiguredModel.model_config["hide_input_in_errors"] is True` at import time |
| It is inherited | `Model.model_config["hide_input_in_errors"] is True` for all eight subclasses |
| It survives class creation | construct each model with a secret in an `int` field and assert no ≥ 8-char fragment in `str(e)` |
| `Settings` unchanged | its `SettingsConfigDict` still has the flag; `env_prefix`/`frozen` unchanged |
| `DomainModel` unchanged | its `model_config` has no such key; a `DomainModel` subclass still echoes input (documented) |
| No runtime mutation | source assertion: no `model_config[` assignment in `src/` or `tests/` |
| Raw surfaces documented | `e.errors()`/`e.json()` leak in a test that asserts the leak, then asserts the safe rendering is clean |

The probe used in this document defines a **class-definition** subclass of each
real section model (`class Hardened(DatabaseSettings): model_config =
ConfigDict(**{**DatabaseSettings.model_config, "hide_input_in_errors": True})`).
That reproduces the post-patch class exactly. It is not a runtime mutation and
is not offered as an acceptance test — the acceptance test reads the shipped
`_ConfiguredModel`.

### 10.5 A2 acceptance criteria

| ID | Criterion | How it is checked |
| --- | --- | --- |
| AC-A2-1 | `_ConfiguredModel.model_config["hide_input_in_errors"] is True` in the shipped source | import-time assertion |
| AC-A2-2 | All eight section models inherit it | parametrised assertion over the eight classes |
| AC-A2-3 | `str(ValidationError)` from each section model contains no intact ≥ 8-char fragment of the input | 3 secret shapes × 8 models |
| AC-A2-4 | Diagnostics survive: field name, error type and message still present | assert `"busy_timeout_ms"` / `"int_parsing"` in `str(e)` |
| AC-A2-5 | `e.errors()` still carries `input` (raw contract) | `errors()[0]["input"] == secret` |
| AC-A2-6 | `safe_validation_report(e)` and `json.loads(e.json())` through it are clean | 3 × 8 matrix |
| AC-A2-7 | `Settings` behaviour unchanged: `str(e)` already clean, `env_prefix`/`frozen` intact | existing config tests + one new assertion |
| AC-A2-8 | `DomainModel` untouched: no `hide_input_in_errors` in its config | source assertion |
| AC-A2-9 | No runtime mutation of `model_config` anywhere | `grep` assertion in the test suite |
| AC-A2-10 | `Settings` production-invariant failures still print a usable message | existing `TestDisclosureFixKeepsDiagnostics` |
| AC-A2-11 | Demo CLI and alembic subprocess contracts unchanged | existing F3-B subprocess tests |
| AC-A2-12 | Full suite green with A1 + A2 | `pytest -m "not live"` |

A2 remains **design only**. Nothing in this section is implemented by A1 or by
revision 4.

## 11. Implementation boundary

### 11.1 F3-A1 — the QA-gated unit

| File | Change |
| --- | --- |
| `src/rfq_agent/observability/redaction.py` | full rewrite of the rules, structural walk, `redact_payload`, `summarize_payload`; new public names `REDACTED_KEY`, `redact_payload`; `__all__` gains both; `safe_validation_report` is **not** in A1 (it belongs to A2's file list, §11.2) |
| `src/rfq_agent/observability/ids.py` | `LogFields` gains a wrap serializer and `__repr__`/`__str__` (uses `redact_mapping`) |
| `tests/unit/test_observability.py` | two test functions updated (§12); new unit tests for the rules |
| `tests/unit/test_redaction_adversarial.py` | **new** — the 89-test gate, adopted verbatim |
| `tests/unit/test_redaction_regression.py` | **new** — the 219-test revision-4 regression suite (§13.4), adopted verbatim from `f3a1-regression-r4.py` |

`redaction.py` grows from 145 lines to about 855 (prototype revision 4). No new dependency (stdlib +
pydantic, already imported through `domain.values`).

### 11.2 F3-A2 — rendering surfaces (depends on A1)

| File | Change |
| --- | --- |
| `src/rfq_agent/config.py` | `_ConfiguredModel` gains `model_config = ConfigDict(..., hide_input_in_errors=True)` **in the class body** (C-11/F-3 corrected target — not `DomainModel`, and **never** a runtime `model_config[...]` assignment); `DatabaseSettings.url` and `GroqSettings.base_url`: `field_serializer` **and** `__repr__`/`__str__` (on the declared classes, never a subclass — see revision 1 §6.3) |
| `src/rfq_agent/observability/redaction.py` | adds `safe_validation_report()` and exports it (A2's addition to the A1 module) |
| `src/rfq_agent/persistence/engine.py` | **not A2** — `create_engine(..., hide_parameters=True)` is optional hardening and needs separate authorization; it is listed here only so the boundary is explicit |
| `tests/unit/test_redaction_regression.py` | extends the safe-rendering assertions (AC-A2-6) |
| `tests/unit/test_config.py` | rendering tests for both settings sections |
| `tests/persistence/test_sqlite_config.py` | **not A2** — only if that hardening is separately authorized |

A2 must not be merged without A1: the config serializers call `redact_text`, and
revision 1 showed that the old text rule destroys the host
(`postgresql://user:[REDACTED]:5432/db`) by letting the e-mail rule eat
`password@host`. Revision 2 preserves host, port and path (re-verified).

### 11.3 Out of bounds (both units)

No change to `contracts/`, `domain/` at all (`DomainModel` is untouched), `persistence/models/`,
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
| Unmodified suite, **revision 4** primitives | 1881 passed, **3 failed**, 3 skipped, 86.66 s (109.42 s on a loaded run) — the same three IDs |
| Whole suite with §12.1–12.2 applied, **revision 4** primitives | **1887 passed, 3 skipped**, 90.83 s (86.4 s on the preceding run, 115.91 s on a loaded run) |

No other existing test changed behaviour: the F3-B config suite, the demo CLI
subprocess tests, the alembic tests and the persistence tests all pass against
revision 2 unchanged.

---

## 13. Acceptance gate and results  ⟦R4⟧

### 13.1 The gate

`docs/reviews/f3a/test_redaction_adversarial.py` (89 tests) is adopted **as
shipped** — no edits, no skips, no `xfail`. It becomes
`tests/unit/test_redaction_adversarial.py` in the implementation PR and runs in
the default `pytest -m "not live"` job.

### 13.2 Results

| Suite | Result |
| --- | --- |
| QA gate (89) | **89 passed, 0 failed, 0.10 s** |
| Regression suite revision 4 (219) | **219 passed, 0 failed, 0.30–0.35 s** |
| `tests/unit/test_observability.py` (updated, §12) | **42 passed** |
| Whole project suite (unmodified tests) | 1881 passed, 3 failed, 3 skipped, 86–110 s |
| Whole project suite with §12 applied | **1887 passed, 3 skipped**, 86–116 s across runs |
| Extended matrix | **101/101** |
| Same 219 tests against the revision-3 prototype | **37 failed, 182 passed** |

Per-test coverage with the mechanism that makes each one pass:
**`f3a1-qa-coverage-matrix-r4.md`** (89 gate rows + 219 regression rows).

### 13.3 The C-12 acceptance count

**1887 passed, 3 skipped** is unchanged from revisions 2 and 3:

```text
1887 passed = 1881 passed (unmodified suite, 3 of which fail in the 39-test module)
            -   36 (the module's currently-passing tests)
            +   42 (the same module after §12: 39 original + 3 new)
            = 1887 passed, 3 skipped  (1890 collected)
```

The 3 failures without §12 are the same two functions. No test outside
`test_observability.py` changed behaviour, and the full-suite time is within
noise of revision 3 (86–116 s vs 90.7 s; shared-runner load varies by more than 20 %, so the
counts, not the wall times, are the acceptance invariant).

### 13.4 The reproducible regression suite — 219 tests

The 89-test gate is a point-in-time review; it had no container case and no
run of nine backslashes, which is why F-1 and F-2 reached revision 3.

`tests/unit/test_redaction_regression.py` — 219 tests, stdlib + pytest only,
0.30–0.35 s, no live services, no fixtures beyond constants.

| Class | Tests | Covers | Fails on rev 3 |
| --- | --- | --- | --- |
| `TestPemLinearScanner` | 9 | 48 KB / 1 MB repeated `BEGIN`; pairs; real block; unterminated fail-closed; no-body markers; summary path at 48 KB and 1 MB; hash/status preserved | 6 |
| `TestTextKeyPolicy` | 127 | every exact structural key (46) in text and structure with a non-prefixed secret; 14 suffix spellings; the 5 C-9 keys; short values; bare `key`; 13 benign identifiers; text/structure agreement | 20 |
| `TestNestedEncodedJson` | 17 | double/triple/4-level encoding; nested-in-note; escaped keys; 17 levels fail closed; benign nested text; idempotence; summary; oversized payload | 12 |
| `TestPublicApiRetained` | 9 | `email_hash` export/normalisation/sha256/distinctness; e-mail redaction; `max_bytes` rejection; `redact_payload`; status vocabulary | 0 |
| **`TestContainerValuedKeys`** (new) | **31** | **F-1**: object/array/deep dict, repr, nested JSON string, quoted container, YAML block/list/block-scalar/deep block, multiple containers, 3 malformed shapes, oversized 70 KB, 5 000-deep, idempotence, benign containers, inner sensitive key, pydantic dict/nested/list `input_value`, structural containers, summary path | **22** |
| **`TestEscapedKeyBoundaries`** (new) | **26** | **F-2**: runs 1–16 redacted; 17/24/32/64/1 000 contained; benign escaped keys survive; deep run without quotes; boundary behaviour | **15** |

The 20 new tests that pass on revision 3 are the preservation half: benign
containers, idempotence, structural containers and the public-surface contract.
They exist so F-1 cannot be "closed" by redacting everything.

### 13.5 Public surface retained (C-12)

| Name | Contract | Pinned by |
| --- | --- | --- |
| `email_hash(address)` | `sha256(address.strip().lower().encode()).hexdigest()`; stable; same normalisation as the e-mail redactor | 4 tests |
| `REDACTED` | the literal `"[REDACTED]"` | gate + suite |
| `redact_text(text)` | `str` in, `str` out; `TypeError` otherwise | existing |
| `redact_mapping(mapping)` | mapping in, mapping out; new keys never collide | existing |
| `redact_payload(value)` | any JSON-shaped root; new export | 1 test |
| `summarize_payload(value, max_bytes)` | `INLINE` / `TRUNCATED` / `WITHHELD`; `max_bytes <= 0` raises `ValueError` | 3 tests |
| `REDACTED_KEY` | the literal `"[REDACTED_KEY]"`; new export | coverage matrix |

Revision 4 adds no public name and removes none. `email_hash` is untouched.

## 14. Performance evidence  ⟦R4⟧

Best of five runs, Python 3.11, project virtualenv, `time.perf_counter()`.
Budgets are the assertions shipped in the regression suite / extended matrix.
The revision-3 column is measured in the same environment for comparison.

| Input | Revision 3 | Revision 4 | Budget |
| --- | --- | --- | --- |
| 48 KB `"key" * 16 000 + "="` | 1.77 ms | **1.80 ms** | 200 ms |
| 32 KB `"a-" * 16 000 + "key"` | 1.25 ms | **1.30 ms** | 200 ms |
| 48 KB `"a.b" * 16 000` | 0.53 ms | **0.55 ms** | 200 ms |
| 48 KB `"\\u005f" * 8 000` | 1.78 ms | **1.85 ms** | 200 ms |
| 1 MB prose | 12.21 ms | **12.59 ms** | — |
| 1 MB `"a=" * 500 000` | 12.45 ms | **12.80 ms** | — |
| 110 KB `password=…` × 5 000 | 37.07 ms | **38.38 ms** | — |
| 48 KB repeated `BEGIN` markers | 0.61 ms | **0.57 ms** | 200 ms |
| 1 MB repeated `BEGIN` markers | 12.89 ms | **12.03 ms** | 500 ms |
| 4 MB repeated `BEGIN` markers | 51.46 ms | **48.59 ms** | — |
| `summarize_payload`, 48 KB markers | 0.89 ms | **0.83 ms** | 500 ms |
| `summarize_payload`, 1 MB markers | 16.95 ms | **16.53 ms** | 1 000 ms |

**C-8 remains closed:** growth from 1 MB to 4 MB is 12.0 → 48.6 ms (4.0× input,
4.0× time). No lazy body quantifier exists in the module.

### 14.1 F-1 paths

| Input | Revision 4 | Note |
| --- | --- | --- |
| 5 000 small containers, 105 KB (`{"api_key": {"a": 1}}` × 5 000) | **41.15 ms** | one `raw_decode` per container; linear |
| 5 000 unterminated containers, 65 KB | **10.49 ms** | first failure withholds and returns — not one scan per opener |
| 5 000 YAML blocks, 105 KB | **50.18 ms** | block scan runs only when the separator crossed a newline |
| one 70 KB container | **1.35 ms** | over the 64 KiB bound ⇒ withheld |
| double-encoded JSON, 8 KB pad | **0.70 ms** | probe + container scan |

**Load sensitivity.** The tables are best-of-five on an otherwise idle shared
runner. A later run on the same runner under load measured **up to 1.9×** every
row (48 KB key-run 3.32 ms, 1 MB prose 20.3 ms, 110 KB `password=…` 47.8 ms,
1 MB `BEGIN` 15.0 ms, `summarize` 1 MB 18.2 ms, 5 000 YAML blocks 55.8 ms) and
the 219-test suite still passed with every shipped budget. Margins are ≥ 60× on
the 200 ms budgets and ≥ 24× on the 1 000 ms summary budget, so no acceptance
decision depends on the wall time; the budgets exist to catch a complexity
regression, not to certify throughput.

The 110 KB `password=…` × 5 000 case is the important one: an earlier revision-4
draft called the YAML block scanner for every strong key, which scanned the
remaining line per match and produced a 4× regression (146 ms). The cheap gate
(`"\n" in sep or next byte is |/>`) restored it to 38 ms. The regression suite
does not measure time on that shape; the extended matrix's 200 ms budgets are
what caught it.

## 15. Residual risks  ⟦R4 updated⟧

These are **not fixed**. "Direction" says what happens when the risk
materialises: *leak* (a secret survives), *fidelity* (a benign value is lost),
*usability*, *cost*.

| # | Risk | Direction | Status / decision |
| --- | --- | --- | --- |
| R1 | Prose secret with no known prefix and no separator | leak | Out of scope by design; `SecretStr` prevents construction-level leakage. **Unchanged.** |
| R2 | Bearer credential shaped like an artefact ID (`ABC-2026-XYZ`) | leak | Required by QA `B-11`; caught under a header or a sensitive key. **Unchanged.** |
| R3 | Over-redaction of bare `key` in structured args (`{"key": "X-120"}`) | usability | Accepted; separate replayable-args redactor deferred. **Unchanged.** |
| R4 | `input` / `input_value` redacted in unrelated payloads | usability | Accepted for the error-envelope guarantee. **Unchanged.** |
| R5 | NFKC rewrites benign compatibility characters | fidelity | Only when NFKC changes the text. **Unchanged.** |
| R6 | Structured string > 256 KiB is withheld entirely | fidelity | Fail-closed; digest retained. **Unchanged.** |
| R7 | Localised key names (`hasło`, `klucz`, `pwd`) are not in the vocabulary | leak | Deferred. **Unchanged.** |
| R8 | Secret split across lines or containing spaces | leak | Text rules are line/token scoped; a quoted value under a strong key is now replaced whole, which narrows this. Structural keys still redact. **Partially narrowed.** |
| R9 | Keys longer than 64 characters | leak | Not matched. **Unchanged.** |
| R10 | Binary/undecodable bytes | fidelity | Decoded with `errors="replace"`. **Unchanged.** |
| R11 | `[REDACTED]` used as a literal value | ambiguity | Indistinguishable from a redacted value. **Unchanged.** |
| R12 | `.errors()`/`.json()` carry raw input | leak if rendered raw | Corrected in §10: raw-by-design, safe rendering mandatory (AC-A2-6). **Policy risk, not a code guarantee.** |
| R13 | The gate measures a prototype, not the repository | process | §16 requires gate + regression suite against the real module. **Unchanged.** |
| R14 | **17+ encoding levels withhold the whole string** | fidelity | Updated in revision 4 (F-2): the scanner reads 1–16 levels; at 17 the probe has 9 unescape passes, and anything still escaped is withheld. Fail-closed. |
| R15 | Escaped JSON > 256 KiB containing a credential word is withheld | fidelity | Unchanged from revision 3. |
| R16 | PEM blocks > 1 MiB are withheld to end of input | fidelity | Unchanged. |
| R17 | The text tier is exactly the structural tier minus `key` | usability | Unchanged; 13 benign-identifier tests pin the false-positive surface. |
| R18 | `key: Warsaw2024` is redacted (bare `key`, credential-shaped value) | usability | Unchanged. |
| R19 | `email_hash` is unsalted sha256 | privacy | Retained deliberately for C-12/back-compat; a keyed hash is an A2+ decision. |
| **R20** | **A container inside a nested-encoded string whose value contains a brace** (`{\\"v\\": \\"a}b\\"}`) cannot be measured exactly | fidelity | New in revision 4. The fallback detects escaped quotes and rejects a closer followed by a word character; if the closer is followed by `,` the region can end early. The remainder is still scanned, so a secret under a sensitive key is still redacted; a secret under a non-sensitive key after the early close can survive. Bounded and documented, not claimed fixed. |
| **R21** | **A container under bare `key`** (`{"key": {"value": "hunter2024"}}`) is not redacted wholesale | leak (weak tier) | New in revision 4. `key` is the documented weak tier; an inner *sensitive* key is still redacted by the inner match, an inner non-sensitive name is not. Deliberate: redacting every dict under `key` would destroy business payloads. |
| **R22** | **YAML block detection is a heuristic** | usability / leak | New in revision 4. `credentials:\n  value: x` is redacted; `credentials:\n  plain prose` keeps the revision-3 scalar rule (first token only). A non-YAML log line that happens to put `name:` on an indented line after a sensitive key is over-redacted. |
| **R23** | **Quoted values under a strong key lose their whole content** | fidelity | New in revision 4. `"credentials": "see the vault"` becomes `"credentials": "[REDACTED]"` instead of `"credentials": "see"`. Correct direction for a credential key; a deliberate widening. |
| **R24** | **Container scanning costs CPU on pathological lines** | cost | New in revision 4. 5 000 container values in one 105 KB line: 41–44 ms; 5 000 YAML blocks: 50–59 ms. Linear, bounded per container, but not free. Ordinary log lines (0–2 occurrences) are unaffected. |

R20–R24 exist **because** F-1 was fixed: every value-shape branch trades
fidelity or CPU for closing a leak. They are listed so the trade is visible.

## 16. Acceptance criteria for implementation  ⟦R4⟧

The design is ready to implement when the owner accepts the following. The
implementation PR is mergeable only when **all** of them hold.

**Design acceptance (this document)**

1. `redact_payload` is the single entry point for arbitrary payloads;
   `redact_text` and `redact_mapping` keep their signatures.
2. The vocabulary (§6) is the intended policy: the text tier is **derived**
   from `_SENSITIVE_KEY_EXACT` + suffixes minus bare `key` (C-9).
3. Bounded header values, with end-of-line redaction limited to
   `Authorization`/`Proxy-Authorization`, and the business-ID allowlist for
   bare `Bearer`, are the intended behaviour.
4. Deterministic fail-closed behaviour is acceptable for: unterminated PEM,
   PEM > 1 MiB, structured text > 256 KiB, escaped text > 256 KiB with a
   credential word, 17+ encoding levels, and unterminated/mismatched/oversized
   (> 64 KiB) or deeper-than-32 containers (R6, R14–R16, R20).
5. `max_bytes <= 0` raises `ValueError`; NFKC only when it changes the text.
6. The two existing tests in §12 are updated exactly as shown, and no other
   existing test is changed.
7. **F-1 policy:** a strong key's value is replaced wholesale when it is a
   container, a quoted string or a YAML block; a weak key keeps the scalar
   rule. Partial redaction of a strong-key value is a defect, not a trade-off.
8. **F-3 policy:** A2 edits `_ConfiguredModel` at class definition, leaves
   `Settings` and `DomainModel` alone, and treats `.errors()`/`.json()` as raw.

**Implementation acceptance (F3-A1)**

9. **C-8:** `_PEM_RE` does not exist; a 1 MiB repeated `BEGIN` input completes
   within the shipped budgets (< 0.5 s `redact_text`, < 1 s
   `summarize_payload`) and yields `[REDACTED]`.
10. **C-9:** every name in `_SENSITIVE_KEY_EXACT` redacts a non-provider secret
    in text and structure; no second strong-key list exists.
11. **C-10:** double/triple/4-level payloads redact; 17 levels yield
    `[REDACTED]`; benign escaped text without a credential word is unchanged.
12. **F-1:** the four review shapes, repr dicts, nested JSON strings, YAML
    blocks/lists/block scalars and pydantic dict/list `input_value` are
    redacted; malformed containers end in `[REDACTED]`; benign containers are
    byte-identical; the container redaction is idempotent.
13. **F-2:** escape runs 1–16 are redacted in place with a non-prefixed secret;
    17+ are contained; a benign escaped key survives.
14. `tests/unit/test_redaction_adversarial.py` is byte-identical to the QA
    commit and **89/89 pass** against the real module.
15. `tests/unit/test_redaction_regression.py` matches `f3a1-regression-r4.py`
    and **219/219 pass**; together with the gate that is **308 tests**.
16. `pytest -m "not live"` is green: **1887 passed, 3 skipped** (C-12).
17. `email_hash`, `REDACTED`, `redact_text`, `redact_mapping`, `redact_payload`,
    `summarize_payload`, `REDACTED_KEY` and the `INLINE`/`TRUNCATED`/`WITHHELD`
    vocabulary behave as §13.5 states. `email_hash` is unchanged.
18. `ruff check .` and `ruff format --check .` pass (line 100; `except
    Exception` carries `# noqa: BLE001`).
19. No new dependency; `redaction.py` imports only stdlib and
    `rfq_agent.domain.values`.
20. Idempotence holds for every test corpus.
21. The extended matrix passes unchanged: 101/101.
22. No file outside the §11.1 list is touched.
23. The F-1/F-2 regressions are falsifiable: 37 of the 219 fail against the
    revision-3 prototype (22 container, 15 escape-boundary).

**Implementation acceptance (F3-A2, separate PR — see
`f3a2-follow-up-contract.md`)**

24. `_ConfiguredModel.model_config` gains `hide_input_in_errors=True` **at class
    definition**; all eight section models inherit it.
25. `Settings` and `DomainModel` are not modified; `Settings` already had the
    flag.
26. `.errors()`/`.json()` remain raw; `safe_validation_report` is added by A2,
    not A1, and AC-A2-1 … AC-A2-12 pass.
27. No runtime mutation of `model_config` in source or tests.
28. Config rendering keeps host/port/path visible and the real values
    reachable.

**Reporting rule.** A green project suite is not evidence of safety: at
baseline 84 of the 89 adversarial tests fail, revision 2 passed 89/89 while
leaking containers and deep escapes, and revision 3 passed 89/89 + 101/101
while doing the same. The gate is gate **plus** regression suite.

## 17. Provenance  ⟦R4⟧

* Repository baseline `d5611eef` — verified clean before and after this
  session. No commit, no branch change, no tracked file modified.
* Design under revision: `e1494f8f0bd9328f55a8e0ef53b2e766dcaeb8ea`.
* **Revision 3, returned REVISE:** `bee310553fb88701d63d0c7a6571e4bb2e9f36c0`
  (`docs/reviews/f3a/revision-3/`, six artefacts). Extracted from the commit
  and SHA-256 checked against the delivered files before revision 4 began.
* Independent adversarial gate: `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`,
  executed unmodified.
* Source facts for F-3 read directly from `src/rfq_agent/config.py` and
  `src/rfq_agent/domain/values.py` at `d5611eef`.
* Artefacts delivered with this document (all outside the repository):
  * `f3a1-prototype-r4.py` — reference prototype revision 4
  * `f3a1-regression-r4.py` — 219-test regression suite (§13.4)
  * `f3a1-qa-coverage-matrix-r4.md` — 89 gate rows **plus** 219 regression rows
  * `f3a1-extended-matrix.py` — 101-assertion matrix (unchanged)
  * `f3a1-test-shim.py` — the `sitecustomize` used to inject the prototype
  * `f3a2-follow-up-contract.md` — the corrected, separate A2 contract
* Commands used: `git show`, `pytest -m "not live" -q`, both adversarial
  suites, `time.perf_counter()` benchmarks, and direct reads of the config
  classes.
* **No implementation, no commit, no PR, no claim of independent ACCEPT.**

**Status: application code modified NO · tracked files modified NO · tests added
to the repository NO · commits created NO · PR created NO · implementation
authorized NO · independent ACCEPT claimed NO.**
