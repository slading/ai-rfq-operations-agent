# F3-A1 — Redaction hardening, revision 5 (design delta)

**Status: REVIEW READY — not ACCEPT.** Revision 5 closes the remaining
reproducible secret disclosures reported after revision 4 without expanding
F3-A1 into another architecture redesign.

This is a **new build**. The original revision-5 artifacts were lost when the
publication session was re-provisioned; this document and its companions were
reconstructed from the published revision-4 baseline
(`docs/reviews/f3a/revision-4/`, commit `50a71f17`) and the revision-5 review
report, then re-verified end to end. It is **not** a byte-exact recovery and
makes no claim about the original artifacts' checksums. Every number quoted
here was produced by the commands in `f3a1-r5-evidence.txt` against the files
shipped in `docs/reviews/f3a/revision-5/`.

Revision 4 remains authoritative for everything this document does not
replace. It is unchanged, published and green.

---

## 0. The review findings and the delta

| ID | Finding | Root cause in revision 4 | Revision-5 change | Proof |
| --- | --- | --- | --- | --- |
| **D-1** | A strong key followed by a *decorated* container leaked its leaf: `credentials=<{"value": "hunter2024"}>` → `credentials=[REDACTED]: "hunter2024"}>`. Also tuple reprs, YAML list markers, quoted wrappers and bracket-prefixed containers. Propagates through `redact_text`, `redact_mapping`, `redact_payload` and `summarize_payload`. | The value-shape dispatch tested `text[pos] in "{["` and `text[pos] in "\"'"` only. A leading `<` or `(` fell through to the scalar token rule, which skipped the opener and consumed the first token of the container. | A bounded **decorated-value** scanner: a run of `<`/`(` wrappers followed by a container or quote is consumed as **one value** and replaced whole. A wrapper that never closes around a container/quote withholds the tail; a mis-nested group whose content starts `key:`/`key=` withholds its line. Inline `- ` items and same-indent YAML sequences are separate dispatch cases. | 27 wrapper tests, **22 fail on the revision-4 prototype**; the 64-shape fuzz is leak-free and idempotent; revision 4 leaks 24 of the 64 (§2.6). |
| **D-2** | `{"name": "api_key", "value": "hunter2024"}` → `{"name": "api_key", "[REDACTED]": "hunter2024"}`. The secret survives and a *key* is destroyed. | Two defects. (a) `,` is a key separator, so a quoted value followed by `, "next":` was read as the next key and the token after it was redacted instead. (b) A record's `value` field is not itself in the sensitive vocabulary, so the secret was never the target. | A bounded **credential-record** pass that recognises `{name, value}` records structurally (JSON and repr), quoted/kwarg/bare pair patterns in either field order, and a comma guard that refuses a comma separator when the next item is another `ident:`/`ident=` field. | 21 record tests, **18 fail on revision 4**; 640 record shapes leak-free; non-sensitive names stay byte-identical (§3.6). |
| **D-3** | Decorated-key recognition limits were not documented. | — | §4: an explicit table of what is and is not recognised, with executable evidence. | 11 tests: 5 positive controls, 6 pinned limits. |
| **D-4** | "No new dependency" is imprecise. | — | §5.1: the statement becomes **"no new third-party dependency"**. | — |
| **D-5** | `ids.py` is in the A1 scope without a demonstrated requirement or an acceptance test. | — | §5.2: `ids.py` is **removed** from the F3-A1 implementation scope. | — |

Nothing else changes. F-2 and F-3 are preserved exactly (§1.3).

---

## 1. Scope of this delta

### 1.1 What was built

Revision 5 changes one module in the prototype — `redact_text`'s value
dispatch, one new pre-pass and one new probe — and adds 59 regression tests to
the 219-test revision-4 suite. No public name, signature or return shape
changes; no new module, no new dependency, no structural-walk redesign.

| Artifact | Lines | What it is |
| --- | --- | --- |
| `f3a1-prototype-r5.py` | 1 293 | reference prototype (revision-4 prototype + this delta) |
| `f3a1-regression-r5.py` | 827 | 278 tests (219 unchanged + 59 new) |
| `f3a1-fuzz-r5.py` | 98 | 64-shape wrapper grid and 640-shape record grid |
| `f3a1-qa-coverage-matrix-r5.md` | 418 | 89 gate rows + 278 regression rows |
| `f3a1-r5-evidence.txt` | 102 | verbatim run log (header, runs 1–10, summary) |

The **test shim is unchanged from revision 4** (`f3a1-test-shim.py`, 21 lines,
still published in `docs/reviews/f3a/revision-4/`): it installs the prototype's
four public functions and extends `__all__`, and it never writes to the
repository.

### 1.2 Sections of the revision-4 design this document replaces

| Revision-4 section | Status in revision 5 |
| --- | --- |
| §5.4 "Container, quoted and YAML values (F-1)" | **extended** by §2 (decorated values, inline `- `, same-indent sequences) |
| §11.1 file list | **corrected** by §5.2 (`ids.py` removed) |
| §11.1 dependency sentence | **corrected** by §5.1 ("third-party") |
| §13 results | **superseded** by §6 |
| §14 performance | **superseded** by §8 |
| §15 residual risks | **extended** by §9 (R25–R32) |
| §16 acceptance criteria | **superseded** by §10 |

### 1.3 Sections that remain authoritative and unchanged

§3 (C-1 … C-6), §4 (decisions), §5.1–5.3 and §5.5 (pipeline, vocabulary,
escape boundary), §6 (key vocabulary: 46 exact names, derived text tier),
§7 (structural walk), §8 (`summarize_payload`), §9 (F-2 escaped JSON, 1–16
levels, fail-closed probe), §10 (F-3 A1/A2 split), §12 (the two approved test
adaptations), §13.5 (public surface, `email_hash`).

In particular:

* **F-2 is untouched.** `_KEY_SEP_RE` is byte-identical; 1–16 escape levels
  redact in place, 17+ are probed or withheld. The revision-4 suite's 26
  escape-boundary tests and the revision-3 suite's 162 tests pass unmodified.
* **F-3 is untouched.** A2 still edits `_ConfiguredModel` at class definition,
  leaves `Settings` and `DomainModel` alone, and treats `.errors()`/`.json()`
  as raw. `safe_validation_report` remains A2 scope.
* **A2 stays separate and unimplemented.**
* `DomainModel` is not modified; `email_hash` and every public name in §13.5
  are unchanged.

---

## 2. D-1 — wrapper-decorated values

### 2.1 The defect, reproduced on revision 4

| Input | Revision-4 output | Revision-5 output |
| --- | --- | --- |
| `credentials=<{"value": "hunter2024"}>` | `credentials=[REDACTED]: "hunter2024"}>` | `credentials=[REDACTED]` |
| `credentials=({"value": "hunter2024"})` | `credentials=[REDACTED]: "hunter2024"})` | `credentials=[REDACTED]` |
| `credentials=({'value': 'hunter2024'},)` | `credentials=[REDACTED]: 'hunter2024'},)` | `credentials=[REDACTED]` |
| `credentials=('value', 'hunter2024')` | `credentials=[REDACTED], 'hunter2024')` | `credentials=[REDACTED]` |
| `credentials=({'a': 1}, {'b': 'hunter2024'})` | leaks the second leaf | `credentials=[REDACTED]` |
| `credentials=<<{"value": "hunter2024"}>>` | leaks | `credentials=[REDACTED]` |
| `api_key=<{"value": "hunter2024"}>` | `api_key=[REDACTED]: "hunter2024"}>` | `api_key=[REDACTED]` |
| `credentials=<[{"value": "hunter2024"}]>` | leaks the leaf | `credentials=[REDACTED]` |
| `credentials: - hunter2024` | `credentials: [REDACTED] hunter2024` | `credentials: [REDACTED]` |
| `token:\n- x: hunter2024` | `token:\n[REDACTED] x: hunter2024` | `token: [REDACTED]` |
| `password=({a: hunter2024})` | `password=[REDACTED] hunter2024})` | `password=[REDACTED]` |
| `password: (rotated 2026-01-01)` | `password: [REDACTED] 2026-01-01)` | `password: [REDACTED]` |
| `credentials=<value: hunter2024}` | leak | `credentials=[REDACTED]` |

The common mechanism: revision 4 dispatched on the character *immediately*
after the separator. A wrapper hid the real shape, the scalar rule skipped the
opener and consumed a token, and the rest of the container survived as
"ordinary" text.

### 2.2 Dispatch order (normative)

For a **strong** key, after the separator and its whitespace:

1. **YAML block** — separator crossed a newline, or the key line ends in a
   block-scalar marker (`|`, `>`, `|-`, …). Unchanged from revision 4, with one
   extension: a **sequence at the key's own indentation** is now a block
   (`token:` followed by `- x: …`), and the block ends at the first line that
   is not such an item.
2. **Container** — `{` or `[`: `_consume_container` (unchanged).
3. **Quoted value** — `"` or `'`: content replaced, quotes kept (unchanged).
4. **Decorated value** (new) — a run of `<`/`(` wrappers, bounded at 32, that
   leads to a container, a quote, **or closes as a group**. The whole group is
   replaced, wrappers included.
5. **Mis-nested group** (new) — a `<`/`(` whose matching closer never arrives
   **and** whose content starts with `ident:` or `ident=`: the group cannot be
   consumed as a scalar without leaving its tail behind, so the value is
   withheld to the end of the line (bounded by `_MAX_CONTAINER_CHARS`). This is
   the shape the 64-way fuzz found: `credentials=<value: hunter2024}` → a
   scalar rule leaves `credentials=[REDACTED] hunter2024}`. Ordinary prose such
   as `(rotated 2026-01-01)` does not start with a key and keeps the scalar
   rule.
6. **Inline YAML item** (new) — a `-` followed by space/tab: the item's line is
   the value scope.
7. **Scalar** — the revision-3/4 token rule (unchanged).

```python
_WRAPPER_PAIRS = {"<": ">", "(": ")", "{": "}", "[": "]"}

def _consume_decorated(text, pos):
    """Index past a complete wrapper-decorated value, or None."""
    limit = min(len(text), pos + _MAX_CONTAINER_CHARS)      # 64 KiB
    i, openers = _skip_wrappers(text, pos, limit)           # bounded at 32
    if not openers:
        return None
    stack = list(openers)
    quote = ""
    escaped_quotes = False
    while i < limit:
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = ""
        elif ch in "\"'":
            if i > pos and text[i - 1] == "\\":
                escaped_quotes = True
            else:
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
                end = i + 1
                if escaped_quotes:
                    j = end
                    while j < len(text) and text[j] in " \t":
                        j += 1
                    if j < len(text) and (text[j].isalnum() or text[j] == "_"):
                        return None  # early close inside a nested-encoded view
                return end
        i += 1
    return None
```

The group is replaced **whole**, wrappers included, so no tail can survive:

```text
credentials=<{"value": "hunter2024"}>   ->  credentials=[REDACTED]
```

### 2.3 Failure modes (all fail closed or preserve the scalar rule)

| Shape | Behaviour |
| --- | --- |
| Wrapper closes around a container or quote | whole group → `[REDACTED]` |
| Wrapper closes around a plain scalar | whole group → `[REDACTED]` (`password: (rotated 2026-01-01)`) |
| Wrapper around a container/quote never closes | `[REDACTED]` appended, **rest of text withheld** |
| Wrapper around a scalar never closes | scalar rule (`password: (rotated` → `password: [REDACTED]`); nothing can be partially exposed |
| Mismatched closer (`<{…})`) | withheld |
| Mis-nested group starting `key:`/`key=` (`<value: hunter2024}`) | withheld to the end of its line |
| Nested wrappers `<<…>>`, `(<…>)` | balanced scan |
| Multiple items in a group `({'a':1}, {'b':2})` | one group, one marker |
| Group larger than 64 KiB / deeper than 64 | withheld |
| Weak key (`key=…`) | unchanged weak-tier rules (revision-4 R21) |

### 2.4 Why this is not a new architecture

It is the **same** bounded bracket scan revision 4 already uses for containers,
generalised to the two punctuation pairs that can wrap a value. It reuses
`_MAX_CONTAINER_CHARS`, `_MAX_CONTAINER_DEPTH`, the escaped-quote rule and the
"withhold on doubt" contract. The only new state is a wrapper stack plus one
bounded lookahead that classifies a mis-nested group as `key:`-shaped.

### 2.5 Bounds

| Bound | Value | Applies to |
| --- | --- | --- |
| `_MAX_CONTAINER_CHARS` | 64 KiB | wrapper scan, mis-nested group, inline YAML item |
| `_MAX_CONTAINER_DEPTH` | 32 | wrapper stack (`× 2` for the group stack) |
| `_MAX_ESCAPE_DEPTH` | 8 | unchanged (F-2 probe) |

No unbounded loop was added; every new scan stops at a size or depth bound and
withholds when the bound is hit.

### 2.6 Evidence

* 27 tests in `TestWrapperContainers`: 8 decorated-container shapes, the review
  example verbatim, quoted wrapper, inline and same-indent YAML sequences,
  3 malformed wrappers, 3 mis-nested-group tests (6 shapes, following line, all
  entry points), wrapped scalar, dangling wrapper, harmless-text container,
  benign non-sensitive control, trailing fields, idempotence and all four
  public entry points.
* A 64-combination fuzz over `{ (<[` × `} )]>` × four inner shapes: revision 5
  **0 leaks, 0 idempotence violations**; revision 4 leaks **24** of 64.
* All four public entry points verified:

```text
redact_text("credentials=<{\"value\": \"hunter2024\"}>")            -> credentials=[REDACTED]
redact_payload({"log": text})                                       -> {'log': 'credentials=[REDACTED]'}
redact_mapping({"log": text})                                       -> {'log': 'credentials=[REDACTED]'}
summarize_payload({"log": text}, max_bytes=512)["payload"]          -> {'log': 'credentials=[REDACTED]'}
```

---

## 3. D-2 — credential records in text

### 3.1 The defect, reproduced on revision 4

| Input | Revision-4 output | Revision-5 output |
| --- | --- | --- |
| `{"name": "api_key", "value": "hunter2024"}` | `{"name": "api_key", "[REDACTED]": "hunter2024"}` | `{"name": "api_key", "value": "[REDACTED]"}` |
| `{'name': 'api_key', 'value': 'hunter2024'}` | `{'name': 'api_key', '[REDACTED]': 'hunter2024'}` | `{'name': 'api_key', 'value': '[REDACTED]'}` |
| `{"value": "hunter2024", "name": "api_key"}` | unchanged — leaks | `{"value": "[REDACTED]", "name": "api_key"}` |
| `Credential(name="api_key", value="hunter2024")` | `Credential(name="api_key", [REDACTED]"hunter2024")` | `Credential(name="api_key", value="[REDACTED]")` |
| `name=api_key, value=hunter2024` | `name=api_key, [REDACTED]` | `name=api_key, value=[REDACTED]` |
| `{"name": "api_key", "count": 3}` | `{"name": "api_key", "[REDACTED]": 3}` | unchanged |
| A `json.dumps` of a record | leaks | `[REDACTED]` |

### 3.2 The fix

Four cooperating parts, all bounded:

1. **Comma guard** (`_separator_is_real`). A match whose separator contains a
   comma is rejected when the key token is followed by a closing quote (one
   escape level per encoding allowed) or when the next item is a bare
   identifier followed by `:`/`=`. This is what stops `"api_key", "value":`
   from being read as key/value. It does **not** touch `api_key,SECRET` (the
   accepted CSV case: the token after the comma is not followed by `:`/`=`).
2. **Structural record walk** (`_redact_credential_records`). Scans `{…}`
   containers (at most 32 redactions over at most 128 candidates), parses the
   literal, and requires a name-like field (`name`/`key`/`field`/`type`) whose
   value is sensitive **and** a value-like field. The secret span is located by
   `_RECORD_VALUE_FIELD_RE` and replaced; everything else is preserved
   byte-for-byte.
3. **Pair patterns** (`_redact_record_pairs`). Quoted and bare `name … value`
   pairs in either field order, with `:` or `=`, catch records whose container
   is malformed (no closing brace) or whose quoting defeats the JSON parse.
4. **Record probe** (`_record_probe`). If a `json.dumps`-encoded view still
   hides a record after unescaping (bounded at 8 levels and 256 KiB), the whole
   string is withheld rather than returned partially redacted.

Ordering matters and is now explicit: the key rules run **first**, then the
record pass, so a record's own field names are still intact when the structure
is parsed. `_redact_string` also tries the raw shape before the text rules,
which is what fixes the original "`value` was rewritten before it could be
recognised" defect.

### 3.3 The distinction between a record and ordinary data

A literal is a credential record only when **both** hold:

* a name-like field exists and its value normalises to a sensitive key
  (`api_key`, `password`, `token`, … — the unchanged vocabulary);
* a value-like field exists.

Therefore these stay byte-identical (verified by tests):

| Input | Why it is ordinary data |
| --- | --- |
| `{"name": "username", "value": "hunter2024"}` | `username` is not a credential name |
| `{"name": "api_key", "count": 3}` | no value field |
| `{"a": "token", "b": "x"}` | no name field, and the comma guard keeps `b` intact |

### 3.4 Bounds

| Bound | Value | Applies to |
| --- | --- | --- |
| `_MAX_RECORDS` | 32 | redactions per string |
| `_MAX_RECORD_SCAN` | 128 | container candidates per string |
| `_MAX_RECORD_PAIR_CHARS` | 256 KiB | the quoted/bare pair patterns |
| `_MAX_CONTAINER_CHARS` | 64 KiB | one record literal |
| `_MAX_ESCAPE_DEPTH` | 8 | the record probe |

The pair patterns are skipped above 256 KiB and when the text contains no `:`
or `=`; a malformed record in a larger string is then covered only by the
container walk (R31), which keeps a 1 MB prose payload from being walked four
times by regexes that cannot match it.

### 3.5 Why the comma guard does not weaken C-9

The 89-test gate's CSV assertion (`api_key,<secret>`) still redacts: the token
after the comma is a credential, not a field name. The guard only rejects a
comma separator when the next item is **another named field** — precisely the
record shape. Both behaviours are pinned: gate row 63 and the new
`test_record_in_text_behind_a_comma_is_not_mistaken_for_a_key`.

### 3.6 Evidence

21 tests in `TestCredentialRecords` (18 fail on revision 4); a 640-shape fuzz
over 4 names × 5 values × 2 quote styles × 2 separators × 8 spacings: revision 5
**0 leaks**, revision 4 **128 leaks**; benign controls byte-identical;
idempotent; all four public entry points clean.

---

## 4. D-3 — decorated-key recognition limitations

The key token is `[A-Za-z0-9_.\-]{1,64}` with escaped `\uXXXX` runs, and the
separator must follow the token directly (optionally through one quote).

| Shape | Recognised | Evidence |
| --- | --- | --- |
| `api_key: hunter2024` | **yes** | positive control |
| `"api_key": "hunter2024"` | **yes** | positive control |
| `__api_key__: hunter2024` | **yes** | suffix rule |
| `x-api-key: hunter2024` | **yes** | suffix rule |
| `x.api_key: hunter2024` | **yes** | suffix rule |
| `[api_key]: hunter2024` | no | R29, pinned |
| `` `api_key`: hunter2024 `` | no | R29, pinned |
| `*api_key*: hunter2024` | no | R29, pinned |
| `/api_key/: hunter2024` | no | R29, pinned |
| key longer than 64 characters | no | R29, pinned |
| zero-width character after the key | no | R29, pinned |

These are **documented limitations**, not confirmed vulnerabilities in a
supported shape: configuration keys and log field names are produced by the
application, not by an attacker choosing punctuation. The tests pin both sides
so a future widening cannot silently narrow them.

---

## 5. D-4 and D-5 — documentation corrections

### 5.1 D-4 — dependency statement

Revision 4 §11.1 said "No new dependency (stdlib + pydantic, already imported
through `domain.values`)". Corrected:

> **No new third-party dependency.** The module imports only the standard
> library plus `pydantic`, which the project already depends on and which is
> reached through `rfq_agent.domain.values`.

`pydantic` is a third-party package; saying "no new dependency" while listing
it is what the review flagged. The prototype adds no import at all for the
delta: `ast`, `json`, `re`, `unicodedata` are stdlib; `pydantic` is pre-existing.

### 5.2 D-5 — `ids.py` leaves the F3-A1 scope

Revision 4 §11.1 listed `src/rfq_agent/observability/ids.py` ("`LogFields`
gains a wrap serializer and `__repr__`/`__str__`"). No acceptance test in the
89-test gate, the 101 assertions or the 278 regressions requires it, and
`redact_mapping` already covers the mapping case. It is **removed** from the A1
file list.

The F3-A1 implementation therefore touches exactly:

| File | Change |
| --- | --- |
| `src/rfq_agent/observability/redaction.py` | rules, structural walk, `redact_payload`, `summarize_payload`; `safe_validation_report` is **A2** |
| `tests/unit/test_observability.py` | the two approved §12 tests |
| `tests/unit/test_redaction_adversarial.py` | **new**, the 89-test gate verbatim |
| `tests/unit/test_redaction_regression.py` | **new**, the 278-test suite |

`ids.py` is not modified by either unit. If a future change needs a rendering
hook there, it must arrive with its own requirement and tests.

---

## 6. Results

### 6.1 Commands and counts (all executed; `f3a1-r5-evidence.txt`)

| Suite | Result |
| --- | --- |
| Session B gate, unmodified | **89 passed, 0 failed** |
| Revision-3 regression suite, unmodified | **162 passed, 0 failed** |
| Revision-4 regression suite, unmodified | **219 passed, 0 failed** |
| Revision-5 regression suite | **278 passed, 0 failed** |
| Extended matrix | **101/101** |
| Project suite, unmodified tests | 1881 passed, **3 failed**, 3 skipped — the same three IDs as revisions 3 and 4 |
| Project suite with §12.1–12.2 applied | **1887 passed, 3 skipped** |
| Idempotence corpus | 45 entries + 4 summarize payloads, **0 failures** |

The three failures are the same two functions the approved §12 edits replace;
no other test changed behaviour. The count identity is unchanged from revision
4: 1881 − 36 + 42 = 1887.

### 6.2 What did not change

No test in the gate, the extended matrix, the revision-3 suite or the 219-test
revision-4 suite was edited, skipped or relaxed. The revision-4 suite passes
**byte-for-byte** against the revision-5 prototype — the strongest available
statement that F-1, F-2, C-8, C-9, C-10 and C-12 are preserved.

---

## 7. Falsifiability

The 278-test suite run against the **revision-4** prototype:

| Class | Tests | Fail on revision 4 |
| --- | --- | --- |
| `TestWrapperContainers` (new) | 27 | **22** |
| `TestCredentialRecords` (new) | 21 | **18** |
| `TestDecoratedKeyLimits` (new) | 11 | 0 (documented limits, both revisions) |
| the 219 inherited tests | 219 | 0 |

**40 failed, 238 passed.** Every failure is a leak or a mangle the review named,
a mis-nested shape the fuzz found, or a preservation case the old comma rule
broke. The one difference from the original revision-5 report (39 failures) is
`test_structural_record_mapping`, which also fails on revision 4 in this build:
it exercises the text path through `redact_mapping`, which the comma rule
mangles there.

---

## 8. Performance

Minimum of five runs per case, three interleaved processes per variant on the
same runner. Revision-4 numbers come from the same loop.

| Case | Rev 4 | Rev 5 | Delta |
| --- | --- | --- | --- |
| 48 KB key-run | 1.51 ms | 1.54 ms | +2 % |
| 32 KB dash-key | 1.07 ms | 1.09 ms | +2 % |
| 48 KB e-mail dots | 0.46 ms | 0.48 ms | +4 % |
| 48 KB backslash-run | 1.51 ms | 1.56 ms | +3 % |
| 1 MB prose | 10.81 ms | 11.12 ms | +3 % |
| 1 MB `a=` | 10.86 ms | 11.27 ms | +4 % |
| 110 KB `password=…` × 5 000 | 32.68 ms | 33.22 ms | +2 % |
| 48 KB `BEGIN` | 0.51 ms | 0.51 ms | 0 % |
| 1 MB `BEGIN` | 10.83 ms | 10.85 ms | 0 % |
| 4 MB `BEGIN` | 43.77 ms | 43.74 ms | 0 % |
| `summarize_payload` 48 KB | 0.74 ms | 0.73 ms | −1 % |
| `summarize_payload` 1 MB | 14.44 ms | 14.54 ms | +1 % |
| 5 000 containers | 34.81 ms | 36.47 ms | +5 % |
| 5 000 unterminated | 8.95 ms | 8.99 ms | +1 % |
| 5 000 YAML blocks | 42.24 ms | 43.72 ms | +4 % |
| **90 KB of records (×2 000)** | 19.59 ms | **30.77 ms** | **+57 %** |
| 1 MB prose containing "name"/"value" | 10.30 ms | 10.63 ms | +3 % |
| deep record | 0.40 ms | 0.47 ms | +17 % |

Every pre-existing path moves by at most 5 %, and the sign varies between runs:
that is the runner's noise, not a cost of the change. The two material rows are
both **record-dense inputs** — shapes the new pass exists to handle — and both
stay far inside budget. The 4 MB row is dominated by PEM scanning in both
variants and is not used to support any claim.

All shipped budgets pass with wide margins:

| Budget | Limit | Measured |
| --- | --- | --- |
| 48 KB hostile shapes | 200 ms | **1.56 ms** |
| 1 MB `redact_text` | 500 ms | **11.27 ms** |
| `summarize_payload` at 1 MB | 1 000 ms | **14.54 ms** |

---

## 9. Residual risks

R1–R24 are unchanged; the table in revision-4 §15 remains authoritative. The
revision-5 additions:

| # | Risk | Direction | Status |
| --- | --- | --- | --- |
| **R25** | A wrapper around a container or quote that never closes withholds the rest of the string | fidelity | Fail-closed by design. A dangling wrapper around a plain scalar keeps the scalar rule, so prose is not truncated. |
| **R26** | A decorated value is replaced whole, so benign text inside a strong key's wrapper is lost (`credentials=(none)` → `credentials=[REDACTED]`) | fidelity | The same trade as R23 (quoted values). |
| **R27** | A record whose name field is not one of `name`/`key`/`field`/`type`, or whose secret is itself escaped, is not recognised; an encoded record is withheld whole | leak (exotic) / fidelity | Documented and tested (`test_encoded_record_fails_closed`). `SecretStr` still keeps such keys out of log records. |
| **R28** | More than 32 container-valued records in one string, or more than 128 candidates: records past the cap are covered only by the flat patterns, which do not reach container values | leak (pathological) | New bound, documented. |
| **R29** | Decorative punctuation between a key token and its separator (`[api_key]:`, backticks, `*`, `/`), keys > 64 characters, zero-width characters adjacent to a key | leak (exotic) | D-3. Documented and pinned by 6 tests; revision 4 behaved identically. |
| **R30** | A mis-nested `<`/`(` group starting `key:`/`key=` withholds the rest of its line (`credentials=<value: hunter2024} user=alice` loses `user=alice`) | fidelity | The alternative is leaving the secret behind. Line-scoped, bounded, idempotent. |
| **R31** | Above 256 KiB the quoted/bare pair patterns are skipped; a malformed record in such a string relies on the container walk | leak (pathological) | New bound; the container walk still covers well-formed records at any size. |
| **R32** | `{"a": "token", "b": "hunter2024"}` is unchanged: no name field, and the comma cannot be read as a separator without breaking the accepted CSV rule | leak (exotic) | Pre-existing documented limitation, now pinned by `test_record_in_text_behind_a_comma_is_not_mistaken_for_a_key`. |

Risk classification: R25, R26 and R30 are deliberate fidelity trades; R27–R29,
R31 and R32 are **documented limitations**, not confirmed vulnerabilities in a
supported shape.

---

## 10. Acceptance criteria for implementation

The implementation PR is mergeable when all of the following hold.

**Preserved from revision 4**

1. `redact_payload` remains the single entry point for arbitrary payloads;
   `redact_text` and `redact_mapping` keep their signatures.
2. The vocabulary is unchanged: 46 exact names, the suffix rule, bare `key`
   weak, the text tier derived.
3. The two existing test functions in §12 are updated exactly as shown, and no
   other existing test changes.
4. F-2: escape runs 1–16 redact in place; 17+ are contained; 26 tests pass.
5. F-3: A2 edits `_ConfiguredModel` at class definition, leaves `Settings` and
   `DomainModel` alone, `.errors()`/`.json()` stay raw, `safe_validation_report`
   is A2-only.
6. `email_hash` and every public name in §13.5 are unchanged.

**New in revision 5**

7. **D-1:** every wrapper family in §2.1 redacts to `[REDACTED]`; malformed
   wrappers around containers/quotes withhold; a dangling wrapper around a
   scalar keeps the scalar rule; the 64-combination fuzz is leak-free.
8. **D-1:** the fix applies through `redact_text`, `redact_mapping`,
   `redact_payload` and `summarize_payload`.
9. **D-2:** every record shape in §3.1 redacts; `{"name": "username", …}` and
   `{"name": "api_key", "count": 3}` stay byte-identical; a record with a
   missing closing brace still loses its secret.
10. **D-2:** a quoted value followed by `, "next":` is no longer turned into a
    redacted key (`{"a": "token", "b": "x"}` unchanged).
11. **D-3:** the six documented limits are tested and the five positive
    controls pass.
12. **D-4:** the design and the PR text say "no new **third-party**
    dependency".
13. **D-5:** `ids.py` is not modified.
14. **Tests:** gate **89/89**, revision-3 suite **162/162**, revision-4 suite
    **219/219**, revision-5 suite **278/278**, extended **101/101**, project
    suite **1887 passed, 3 skipped** (with the two documented adaptations),
    unmodified project suite 1881 passed / 3 failed / 3 skipped with exactly
    the same three IDs.
15. **Falsifiability:** at least the 40 new tests still fail against the
    revision-4 prototype.
16. **Performance:** the shipped budgets pass. No pre-existing path shows a
    reproducible regression: measured deltas are ≤ 5 % and change sign between
    runs. The record pass may cost up to 2× on record-dense text (measured
    1.57×) and must stay inside the 200 ms budget.
17. No new third-party dependency; `redaction.py` imports stdlib +
    `pydantic` only.
18. **D-1 malformed groups:** a mis-nested `<`/`(` group whose content starts
    with `ident:`/`ident=` redacts to the end of its line (6 shapes tested
    through all entry points); `password: (rotated 2026-01-01)` still follows
    the scalar rule.

**Reporting rule.** A green gate is not evidence of safety. Revision 4 passed
89/89 and 101/101 while leaking every wrapper shape and mangling every record;
the gate plus the 278-test regression suite is the unit of evidence.

---

## 11. Provenance

* Repository baseline `d5611eef`; revision 4 pinned at
  `50a71f17d5560a4383ffda84e21c17c31b788f77` (`docs/reviews/f3a/revision-4/`,
  eight artefacts). The working tree was restored to that commit after
  verifying all 21 tracked-document paths were byte-identical (a tar backup was
  taken first); no unknown file was overwritten.
* Independent adversarial gate `28af34cb…`, executed unmodified.
* Verification environment: Python 3.11.2, pytest 9.1.1, pydantic 2.14.1,
  SQLAlchemy 2.1.5, alembic 1.20.0.
* Artefacts delivered with this document:
  * `f3a1-prototype-r5.py` — reference prototype revision 5
  * `f3a1-regression-r5.py` — 278-test regression suite
  * `f3a1-fuzz-r5.py` — 64-shape wrapper and 640-shape record fuzz harness
  * `f3a1-qa-coverage-matrix-r5.md` — 89 gate rows + 278 regression rows
  * `f3a1-r5-evidence.txt` — verbatim run log and benchmark output
* Commands: `git cat-file`/`git show`, `pytest -q -p no:cacheprovider`, the gate
  and extended suites, `time.perf_counter()` benchmarks with interleaved
  variants.
* **New build.** The original revision-5 artifacts were destroyed with the
  previous sandbox; this set was reconstructed from the published revision-4
  baseline and the revision-5 report, then re-verified. It does **not** match
  the lost artifacts' checksums and no such match is claimed.
* **No implementation, no claim of independent ACCEPT.** Publication is
  review-artifact only.

**Status: application code modified NO · production tests modified NO ·
dependencies changed NO · `main` changed NO · implementation authorized NO ·
independent ACCEPT claimed NO.**
