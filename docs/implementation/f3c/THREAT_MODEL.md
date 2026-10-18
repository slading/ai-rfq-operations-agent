# F3-C — Threat Model: Safe Observability Boundary

Status: **finalized Phase 1 threat model — architecture direction approved with conditions (2026-10-18 review); production implementation NOT authorized.** Baseline
`7f32d5813a219e33e16903ae58e5cbfb8a370005`. Companion: `BOUNDARY_INVENTORY.md`
(boundary IDs B-01…B-14 referenced below), `IMPLEMENTATION_CONTRACT.md`.

## 1. Assets, adversaries, trust levels

**Assets.** (a) T0 secrets: `RFQ_GROQ__API_KEY`, `RFQ_HTTP__OPERATOR_TOKEN`,
database URL credentials, future provider keys. (b) T2 customer content: RFQ
body/subject/sender, attachment previews — may itself embed third-party
credentials a customer pasted in. (c) T1 business data when it becomes
diagnostic context (pricing, customer identity). (d) Integrity of diagnostics
themselves: an operator misled by attacker-shaped log content makes wrong
approval decisions. (e) Availability: diagnostics must not become a DoS or
exception source inside error handling.

**Adversaries / hostile sources.** (1) Customer (T2): controls RFQ text,
subjects, filenames; can embed secrets-that-aren't-theirs, prompt-injection,
oversized/deeply-nested payloads, Unicode tricks. (2) Model (T3): output can
echo customer content, canaries, or fabricated "error" text. (3) Hostile
in-process objects: subclassed exceptions/models with malicious
`__str__`/`__repr__`/`__class__`/properties/`model_dump` — the F3-A2 review
demonstrated this is a real, reproduced class, not paranoia. (4) Operator
error (T4): pasting a credential into the wrong env var/field — the exact case
`config._as_bool` was hardened against. (5) Future integrator (insider risk by
accident): adds `logger.exception(...)` or `print(payload)` at the first sink.

**Non-goals.** Protecting the intentional raw stores (`rfqs.body_text`,
outbound `rendered_text`) from someone with database access; detecting secrets
in arbitrary free prose (impossible in general — R32); defending a compromised
process.

## 2. Confirmed findings (exist in code at baseline)

| ID | Finding | Evidence |
|---|---|---|
| C-1 | Demo CLI renders raw `ValidationError`/`ValueError` to stderr (`f"…{error}"`). Root `hide_input_in_errors` suppresses input echo but not message text; nested/domain models don't set it at all. A message-carried secret (the class `test_config.py` already tests for) reaches terminal/CI logs. | `demo/__main__.py::main` (B-08); `seed/__main__.py` prints `SeedError` the same way. |
| C-2 | `contracts/testing.py` (test-support) builds `ParseIssue` from `exc.errors()` (`msg`/`loc`/`type`) and `raw=str(result.data)`. Safe only because it is test-only; it is the natural template a real provider repair-loop would copy. | B-09. |
| C-3 | `canonical_json(..., default=str)` silently invokes `str()` on unexpected objects inside the project's "safe fingerprint" helper. Any future diagnostic fingerprinting untrusted objects inherits representation invocation. | `domain/values.py` (B-10). |
| C-4 | Neither F3-A1 nor F3-A2 has a production caller. All current safety is *opt-in per future call site*. The first sink integration decides the real security posture unless a boundary is mandated before it. | Caller grep, B-01/B-02. |
| C-5 | `LogFields.message` is unbounded free text and `attributes` keys/values are unallowlisted; `LLMCallSpan`/`ToolCallSpan` string fields are length-bounded only. The existing schemas, used naively as the log contract, permit full secret carriage. | `observability/ids.py`, `spans.py` (B-03/B-04). |
| C-6 | Trace/detail persistence columns (`payload_json`, `args_json`, `detail_json`) carry a "redacted / never raw" policy in comments only. No writer exists to violate it yet — and none to enforce it. | B-07. |

## 3. Threats

Severity assumes a future sink exists (logs shipped off-box, retained,
searchable) unless stated. Likelihood is judged *given that integration
happens without F3-C*.

### T-01 — Raw exception rendering at the sink (from C-1, B-11)
`logger.exception`, `f"{exc}"`, traceback rendering, `str(exc)` interpolation.
Pydantic messages can carry input/context via custom validators (`ValueError`
message text is author-controlled and value-interpolated at ~200 domain raise
sites). **Impact: secret + T2 disclosure. The single most probable leak path.**

### T-02 — Unsafe Pydantic access (B-01, B-09)
`.errors()` formats `msg` even with `include_input/context/url=False`
(F3-A2's demonstrated finding: formatting invokes a ctx object's `__str__`);
`.errors(include_url=False)` still yields `loc` (field paths can encode
customer keys) and `input` by default; `.json()` same. Also `model_dump()` on
objects containing T2 fields, and `repr(model)` (Pydantic repr includes field
values; `SecretStr` is the exception, not the rule).

### T-03 — Nested/arbitrary-object traversal (B-10, B-02)
Serialising `dict[str, Any]` diagnostics with `default=str`, `repr`, or a
recursive walker that touches properties: hostile `__str__`/`__repr__`/
`__getattr__`, cycles, self-referential lists, objects whose equality/hash
raises. F3-A1's structural walk already defends itself; a new boundary must
not reintroduce naive traversal. Includes the F3-A2-remediated hostile
`__class__` property escaping `isinstance` — checks must not read instance
attributes on untrusted objects.

### T-04 — Decorated / disguised sensitive keys (R29, carried)
Free-text key recognition is bypassed by decoration (`[api_key]:`), overlong
keys (> 64 chars), zero-width characters adjacent to the key. **Status in
F3-A1: open, pinned by limitation tests** (`test_redaction_regression.py`,
`test_redaction_production.py::test_r29_and_r32_remain_explicit_text_disclosure_limits`).
Structural (mapping) path is safer because key normalisation strips
punctuation — but only for keys the *application* chose. **F3-C consequence:
any boundary that passes free text through `redact_text` and calls the result
safe inherits R29.** Mitigation must be architectural (no free-text fields),
not another regex.

### T-05 — Semantic secret relationships (R32, carried)
`{"a":"token","b":"hunter2024"}`: no generic redactor can know `b`'s value is
a credential. Also: prefixless prose secrets, secrets split across fields,
secrets that are also legitimate business values, canary/cross-customer values
(the trust layer's `CANARY_SCAN_STRICT` concern). **No redaction-based design
closes T-05.** Only allowlisting (values are never carried unless their *role*
is approved) closes it.

### T-06 — Unbounded diagnostic payloads
1 MB inputs, 128-deep nesting, 10⁵ attributes, long strings: memory/CPU in
the error path, log-ingestion cost, truncation that cuts mid-secret leaving a
usable prefix (F3-A1 already redacts before truncating; a new boundary must
bound *before* serialising and never truncate a value into a partial secret —
it withholds instead).

### T-07 — Fail-open on redaction/serialisation failure
"On exception, log the original" fallbacks; `try: safe(x) except: return x`;
partial output emitted before a late failure. Redaction today fails closed
(withholding); any composition layer can silently undo that. Requirement:
one constant fallback event, emitted whole, containing no input-derived data
— not even `type(exc).__name__` from an arbitrary object (cf. B-02 note on
`summarize_payload._withheld`; class names are low-risk but the boundary
takes the stricter line so the rule is exceptionless and testable).

### T-08 — Accidental persistence (B-05, B-06, B-07)
Diagnostics written to `payload_json`/`detail_json`/span tables become
durable, backed-up, operator-visible data. `ParseFailure.raw` ("persisted
behind the redaction policy") and `InjectionSuspicion.snippets` (verbatim T2)
are the two fields most likely to be persisted by a helpful implementer.

### T-09 — Sink-adjacent configuration (B-12)
SQLAlchemy echo, Alembic `fileConfig`, future `logging` dictConfig,
provider SDK debug logging: each can emit bound parameters/prompts below the
application layer. F3-C cannot fix these; the sink phase must ship a config
checklist (echo off, SDK debug off, no `print` in `src/` outside CLIs).

### T-10 — Diagnostic-content injection (integrity)
Even with no secret present, T2/T3-shaped strings in logs enable log forging
(newlines, fake `ERROR` lines), terminal escape sequences, and JSON breaking
in downstream parsers. Allowlisted-pattern strings + JSON encoding at the
boundary close this; free text never does.

### T-11 — Correlation-ID confusion
`TraceContext` IDs are pattern-constrained (`TraceId`/`RunId`/`SpanId`), safe
to emit. Risk is only a caller placing a non-ID string (e-mail, RFQ subject)
in an ID field — prevented if the boundary accepts `TraceContext` objects or
pattern-checked strings, never raw caller strings.

### T-12 — Hash-based correlation as a side channel (accepted, documented)
`email_hash`/payload sha256 are unsalted SHA-256 (F3-A1 handoff records this
as accepted for API compatibility). Low-entropy values are dictionary-
attackable by anyone with log read access. F3-C emits hashes only where a
span schema already requires them; it does not add new hash-of-secret fields.

## 4. Hypothetical / future risks (no code path today — do not report as findings)

- **H-1** FastAPI/operator UI (Phase 6) rendering trace rows or exception pages (debug middleware tracebacks). No UI exists; `test_package_surface` asserts it.
- **H-2** LLM provider client logging prompts/responses; SDK retries capturing headers (`Authorization`). No client exists; mode defaults to replay.
- **H-3** Workflow worker (Phase 2) adding `logger.exception` in the run loop; queue payloads logging RFQ content.
- **H-4** External observability SaaS (Sentry/OTel exporter) receiving whatever the first sink sends — the reason the boundary must be sink-agnostic and produce plain JSON-compatible dicts.
- **H-5** Recordings (`RFQ_RECORDINGS_DIR`, decision D3) capturing live prompts to disk in `record` mode.

Each becomes in-scope for review **when its phase is authorised**, with this
threat model as the checklist. None justifies code in F3-C beyond the
boundary itself.

## 5. Risk posture summary

| Threat | Today | After F3-C boundary (as contracted) |
|---|---|---|
| T-01/T-02 raw exception & Pydantic rendering | C-1 live in demo CLI; latent everywhere else | Closed *at the boundary*; C-1 itself closed only by the separate required patch **SEC-PATCH-CLI-01** (outside F3-C, gated — inventory §6) |
| T-03 hostile objects | Defended in F3-A1/A2 individually | Closed by no-representation invariant |
| T-04 (R29) / T-05 (R32) | **Open in F3-A1, by design** | **Contained inside the F3-C boundary only** (no free-text carriage). **F3-C does not globally resolve R29 or R32:** the F3-A1 redactor, and every present or future consumer that logs free text outside the boundary, remain exactly as exposed as today. Any wording elsewhere that could be read as "R29/R32 fixed" is incorrect. |
| T-06 unbounded | Bounded inside F3-A1/A2 | Bounded at boundary (fixed output ceiling) |
| T-07 fail-open | No composition layer exists | Closed by constant-fallback invariant |
| T-08 persistence | Tables unwritten | Unchanged — future writer phase must reuse boundary (Q4) |
| T-09/T-10/T-12 | Latent | Partially (T-10 closed at boundary; T-09 checklist; T-12 accepted) |

## 6. Carried statements (review conditions)

- **R29/R32 (T-04/T-05) are not resolved by F3-C** — globally, in the redactor,
  or for any other consumer. F3-C's claim is strictly local: data that passes
  through the boundary cannot carry free text, so those two bypass classes
  have no field to act on *inside boundary output*.
- **C-3 is tracked separately as SEC-REVIEW-JSON-01** (inventory §6) with its
  actual callers and trust boundaries. It is not fixed, mitigated or
  re-rated by F3-C beyond I-7's prohibition on feeding untrusted data to
  `canonical_json`.
- **C-1 is tracked separately as SEC-PATCH-CLI-01** (inventory §6) with a
  blocking gate. Until it lands, the demo/seed CLI stderr path remains a
  confirmed unsafe output.
- **Sink enforcement (Q7)** is a defined future requirement (contract §8):
  when a sink is authorised, sinks must accept only boundary output, enforced
  by a repo-wide source-scan test plus review. It is **not** implemented in
  F3-C and no sink exists to enforce against today.
- **No test execution is claimed** for this document set.
