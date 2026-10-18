# F3-C — Implementation Contract: Safe Observability Boundary

Status: **FINALIZED Phase 1 contract — architecture direction approved with conditions (2026-10-18 review). Production implementation is NOT authorized by this document; it requires a separate explicit authorization after review of this finalized contract.** Baseline `7f32d5813a219e33e16903ae58e5cbfb8a370005`.
Companions: `BOUNDARY_INVENTORY.md` (B-/C- IDs), `THREAT_MODEL.md` (T- IDs).
Design rule throughout: **the narrowest boundary that makes a future sink
safe by construction — allowlist, never denylist; withhold, never
redact-and-hope (that is what finally contains R29/R32 at this layer).**

## 1. Purpose and non-purpose

F3-C creates one pure, I/O-free module through which every future log line,
trace payload and operator diagnostic must pass. It introduces **no sink**:
no logging configuration, no handler, no file/network output, no database
write, no external service, no new dependency, no deployment change. Wiring
any caller (worker, API, UI, CLI) to it is a separate, later authorisation
(decisions Q2/Q4) — mirroring how F3-A1/F3-A2 shipped caller-less safety primitives.

## 2. Exact proposed files

| File | Change | Notes |
|---|---|---|
| `src/rfq_agent/observability/boundary.py` | **Create** (est. 200–280 lines) | The entire implementation. No other production file is touched. |
| `tests/unit/test_observability_boundary.py` | **Create** | Unit + adversarial suite (§7). |
| `tests/unit/test_package_surface.py` | **Modify — one line** | Add `"rfq_agent.observability.boundary"` to `MODULES` (the registry test requires it for every new module). |
| `docs/implementation/f3c/HANDOFF.md` | **Create at delivery** | In the style of the F3-A1/A2 handoffs, with evidence under `docs/implementation/f3c/evidence/`. (This Phase-1 set — INVENTORY/THREAT_MODEL/CONTRACT — is already drafted.) |

**Explicitly unchanged:** `observability/redaction.py` (F3-A1),
`observability/validation_errors.py` (F3-A2), `observability/ids.py`,
`observability/spans.py`, `config.py` (F3-B), all `domain/*`, `contracts/*`,
`persistence/*`, migrations, `pyproject.toml`/dependencies, both `ids.py`
files, demo/seed CLIs (their remediation is the separate SEC-PATCH-CLI-01, per Q2).

## 3. Proposed API (exact)

Module `rfq_agent.observability.boundary`, `__all__` exactly:

```python
__all__ = [
    "DIAGNOSTIC_SCHEMA",
    "MAX_DIAGNOSTIC_BYTES",
    "DiagnosticEvent",
    "build_diagnostic_event",
    "summarize_diagnostic_event",
]
```

```python
DIAGNOSTIC_SCHEMA = "rfq.diagnostic/v1"
MAX_DIAGNOSTIC_BYTES = 1024          # hard ceiling on canonical output

class DiagnosticEvent(StrEnum):
    """Closed event vocabulary — the ONLY events a sink may emit in v1."""
    VALIDATION_FAILED = "validation_failed"
    OPERATION_FAILED  = "operation_failed"     # generic, caller gives only a code
    DIAGNOSTIC_UNAVAILABLE = "diagnostic_unavailable"  # the fail-closed fallback

def build_diagnostic_event(
    *,
    event: DiagnosticEvent,
    trace: TraceContext | None = None,
    validation_error: ValidationError | None = None,
    error_code: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, str | int | bool | None]: ...

def summarize_diagnostic_event(event_payload: Mapping[str, object]) -> str: ...
```

Notes on deliberate narrowness (each is a decision §9 can veto):

- **No `exception` parameter.** The only exception-shaped input is
  `validation_error`, consumed exclusively via F3-A2
  (`render_validation_error`). Generic exceptions are represented by the
  caller choosing `OPERATION_FAILED` + an allowlisted `error_code`. There is
  no parameter through which exception text *could* enter.
- **No `message` parameter.** Human text is derived from the event code by a
  fixed table inside `summarize_diagnostic_event` (which accepts only a
  payload previously produced by `build_diagnostic_event`; anything else
  yields the constant `"Diagnostic unavailable."`). Distinct API, like
  F3-A2's structured/summary split.
- **`event` is an enum member, not a string.** `type(event) is DiagnosticEvent`
  is required; lookalikes (plain `str`, hostile objects) fail closed.
- **Keyword-only arguments**, frozen output construction, fresh dict per call
  (no shared mutable state — same property F3-A2's review verified).

### 3.0 Normative event schema (stable, Q1)

Schema identifier: `rfq.diagnostic/v1`. The vocabulary is **closed**: exactly
three events. Names are lowercase snake_case, never reused with a different
meaning, and never renamed within `v1`.

| Event (`DiagnosticEvent`) | Purpose — when a caller may emit it | Allowed optional inputs | Fixed `error_code` behaviour |
|---|---|---|---|
| `validation_failed` | A Pydantic `ValidationError` was safely summarised via F3-A2. The only event that may embed a `validation` object. | `trace`, `validation_error` (required for `SAFE`; absent/invalid degrades the whole event to `diagnostic_unavailable` semantics: `status=UNAVAILABLE`, `validation=null`) | Always `"VALIDATION_ERROR"` (from F3-A2) |
| `operation_failed` | A non-validation failure the caller can name with an allowlisted code (e.g. a domain `ReasonCode`-style value). The escape hatch that removes any need to stringify an exception. | `trace`, `error_code` (required matching the §3.2 pattern; otherwise the event degrades to `diagnostic_unavailable`), `attributes` | The supplied allowlisted code |
| `diagnostic_unavailable` | The deterministic fail-closed fallback. Emitted only by the boundary itself, never requested because something "specific" happened. | None are honoured — all caller inputs are ignored/discarded | Always `null` |

Common to all events: the fixed key set of §3.1 (no key is ever added,
omitted or renamed in `v1`); `schema` is always `rfq.diagnostic/v1`;
attributes are admitted only per §3.2 and only for `operation_failed`
(`validation_failed` accepts the §3.2 telemetry keys too *except*
`error_code`-typed keys, which are reserved — see §3.2 note; simplest stable
rule adopted here: **attributes are honoured for `operation_failed` only**,
and counted as withheld on other events).

### 3.4 Schema stability rules

1. Within `rfq.diagnostic/v1`: no new events, no new output keys, no new
   attribute keys, no widened patterns/ranges, no new input parameters.
2. Any such change requires a new schema identifier (`rfq.diagnostic/v2`), a
   contract amendment, adversarial-test additions and fresh review.
3. The fallback payload (§5) is part of the stable schema: sink consumers may
   rely on its exact key set.

### 3.1 Output schema (fixed keys, always present)

```json
{
  "schema": "rfq.diagnostic/v1",
  "event": "validation_failed",          // enum value only
  "status": "SAFE",                      // or "UNAVAILABLE"
  "error_code": "VALIDATION_ERROR",      // allowlisted code or null
  "trace_id": "…32 hex…", "rfq_id": null, "run_id": null, "span_id": null,
  "validation": { …the exact F3-A2 dict… } ,   // or null
  "attributes": { …allowlisted scalars… },
  "attributes_withheld": 0               // count of dropped attribute entries
}
```

Flat-or-one-level only: maximum depth 2 (3 counting `validation`), maximum
16 attributes, serialised size ≤ `MAX_DIAGNOSTIC_BYTES` verified before
return; on any violation the **whole event** is replaced by the constant
fallback (§5), never truncated.

### 3.2 Attribute allowlist (v1 — exhaustive)

Keys not listed are dropped and counted in `attributes_withheld`. Values are
admitted by **exact type + range/pattern**, never by inspection:

| Key | Admitted value |
|---|---|
| `stage`, `tool_name`, `outcome`, `status_code`, `model`, `purpose`, `finish_reason`, `reason_code` | `str` matching `^[a-z][a-z0-9_.\-]{0,63}$` (else dropped) |
| `error_code` *(also top-level param)* | same pattern, ≤ 64 |
| `duration_ms`, `latency_ms`, `count`, `attempt`, `retries`, `repair_attempts`, `step`, `seq`, `error_count`, `validation_error_count`, `tokens_in`, `tokens_out`, `tokens_cached`, `request_bytes`, `result_bytes`, `args_bytes` | `type(v) is int` (bool excluded), `0 ≤ v ≤ 2**31 - 1` |
| `output_valid`, `truncated`, `duplicate`, `eligible_for_human_review`, `requires_human_approval` | `type(v) is bool` |
| `*_sha256` keys explicitly named: `inputs_sha256`, `messages_sha256`, `args_sha256`, `result_sha256`, `body_sha256` | `str` matching `^[0-9a-f]{64}$` |

**Attributes are honoured only on `operation_failed` (§3.0); on other events they are dropped and counted.** No floats in v1 (excludes `inf`/`nan` and float-formatting surprises), no
`None` attribute values (drop the key), no nested containers, no bytes, no
dates/Decimals/UUIDs (callers pass the allowlisted string/int forms above).
This mirrors the *spirit* of `LLMCallSpan`/`ToolCallSpan` fields so spans can
be projected through the boundary later without new keys (Q4).

### 3.3 Trace handling

`trace` must be a `TraceContext` (already pattern-validated by `DomainModel`
at construction). The boundary copies the four ID fields via attribute reads
on that trusted model only — it never accepts raw ID strings, never
constructs IDs, and on *any* exception (hostile stand-in object) emits the
event with all IDs `null`. `parent_span_id` is not emitted in v1 (sink can
derive nesting from `span_id` chains when spans are wired — Q4).

## 4. Interaction with existing modules

- **F3-A2** is the sole validation-error path:
  `validation = render_validation_error(validation_error)` embedded verbatim.
  If `event is VALIDATION_FAILED` but no/invalid `validation_error` is given,
  status degrades to `UNAVAILABLE` with `validation: null` (never fabricated).
- **F3-A1** is *not* called in v1. Rationale: with no free-text field there is
  nothing to redact, and routing allowlisted codes through `redact_text`
  would re-import R29/R32 semantics for zero benefit. F3-A1 remains the
  boundary for the separate problem "an approved text field is added later" —
  any such addition requires amending this contract and re-review (Q3).
- **`ids.py`/`spans.py`** are read-only inputs (types only); not modified.
  `LogFields` is *not* reused as the output type (B-03: unbounded `message`),
  and is not modified; the contract notes it as superseded-for-sinks.
- **`domain/values.canonical_json`** is used only on the boundary's own
  already-primitive output for the size check — never on caller data (B-10);
  the authoritative serialisation check uses `json.dumps` **without**
  `default=` (§5, I-7).
- **No imports** of `logging`, persistence, config, providers or domain
  business modules; deterministic RFQ business logic is untouched by
  construction (pure function, no I/O, no clocks, no randomness).

## 5. Fail-closed behaviour (deterministic)

Any of: wrong-typed/unexpected argument, hostile object behaviour, disallowed
attribute overflow beyond recovery, size/depth violation, internal exception
at any step → return the one constant payload:

```python
{"schema": "rfq.diagnostic/v1", "event": "diagnostic_unavailable",
 "status": "UNAVAILABLE", "error_code": None, "trace_id": None,
 "rfq_id": None, "run_id": None, "span_id": None, "validation": None,
 "attributes": {}, "attributes_withheld": 0}
```

Attribute-level problems are softer: drop the entry, increment
`attributes_withheld` (capped reporting at 16), keep the event. Whole-event
fallback is reserved for structural/hostile failures. Both paths are pure,
total (never raise — including for `None`, non-mapping `attributes`,
`Mapping` subclasses with hostile `items()`), idempotent, and independent of
input size (work is bounded by the caps before iteration: mappings reporting
`len > 64` are refused whole rather than iterated).

## 6. Security invariants (each must map to ≥ 1 test in §7)

- **I-1 Allowlist-only output.** Output keys, event values and attribute
  keys/values come exclusively from §3.1–3.2. No caller string reaches output
  unless it matches its key's pattern.
- **I-2 No representation.** The module never calls `str()`, `repr()`,
  `format()`, `ascii()`, f-strings, `%`-formatting or `getattr`-traversal on
  caller-supplied objects, and never touches `.errors()/.json()/
  model_dump()`/`.args`/`__dict__`. Type admission uses `type(v) is …`.
- **I-3 No hostile-attribute reads.** No `isinstance` against untrusted
  stand-ins where a `__class__` property could execute or escape (the F3-A2
  review finding); exception containment wraps every boundary crossing, as in
  `render_validation_error`'s descriptor pattern. Enums/TraceContext inputs
  are validated by identity/exact-type + guarded reads.
- **I-4 Validation via F3-A2 only.** Validation detail enters solely as the
  verbatim F3-A2 dict; the boundary adds no location/message/type from any
  other source.
- **I-5 No free text.** No output field can contain arbitrary text; therefore
  R29 (T-04) and R32 (T-05) **cannot trigger through this boundary**, while
  remaining open in F3-A1 for other consumers — stated, not hidden.
- **I-6 Bounded.** ≤ 16 attributes, ≤ 64-char code strings, ints ≤ 2³¹−1,
  depth ≤ 3, output ≤ 1024 bytes; caps checked before expensive work.
- **I-7 Serialisation honesty.** Returned payload must satisfy
  `json.dumps(payload)` with **no** `default=`; verified internally before
  return (failure → fallback). This proves no object leaked into output.
- **I-8 Fail-closed totality.** Never raises, never returns partial output,
  fallback contains zero input-derived data (including exception type names).
- **I-9 Determinism & isolation.** Same inputs → equal outputs; outputs share
  no mutable objects with inputs or with previous outputs; no I/O, time,
  randomness, global state, or import side effects.
- **I-10 No sink, no scope creep.** Module imports nothing that can perform
  I/O; it does not read settings/env, does not persist, does not log.

## 7. Adversarial test matrix (`tests/unit/test_observability_boundary.py`)

Secret canary convention as in F3-A1/A2 suites: assert the whole secret and
every ≥ 8-char fragment absent from structured output, summary, and
`json.dumps` of both. Synthetic secrets only.

| # | Case | Invariant |
|---|---|---|
| A01 | `ValidationError` from nested `Settings` with synthetic API key / operator token / DB password in message, input, ctx | I-4: only aggregate F3-A2 dict appears |
| A02 | Hostile ctx object in ValidationError (`__str__`/`__repr__` counters) — renderer not triggered via boundary | I-2/I-4 |
| A03 | Non-`ValidationError` passed as `validation_error` (incl. object with `__class__` property raising/spoofing, per F3-A2 review cases) | I-3/I-8 |
| A04 | `event` as plain string / int / hostile object / `DiagnosticEvent` lookalike | I-1/I-3 |
| A05 | Free-text smuggling: attributes `{"note": "api_key=hunter2024"}`, `{"message": …}`, unknown keys with secret values | I-1/I-5 (dropped + counted) |
| A06 | R29 shapes as attribute values/keys: `[api_key]: hunter2024`, zero-width-adjacent keys, 10⁴-char key, decorated key | I-5 (never emitted) |
| A07 | R32 shape: `{"a": "token", "b": canary}` as attributes; nested dict/list/set/tuple values | I-1/I-5 |
| A08 | Hostile attribute values: objects with raising `__str__`/`__repr__`/`__eq__`/`__hash__`, property-raising mapping subclass, mapping whose `items()` raises, self-referential structures passed where containers attempted | I-2/I-3/I-8 |
| A09 | Scalar edge: `True` as int field, `1` as bool field, `float('inf'/'nan')`, `Decimal`, `UUID`, `datetime`, `bytes`, huge int `2**100`, negative counts | I-1/I-6 |
| A10 | String pattern edges: uppercase, spaces, newline/ANSI-escape injection, `{}` format braces, `%s`, emoji, 65-char code, sha256 uppercase/short/non-hex | I-1/T-10 |
| A11 | Volume: 10⁵-entry mapping, mapping lying about `len`, 17th attribute, attributes exactly at cap, oversized event construction → byte ceiling | I-6/I-8 |
| A12 | `trace` stand-ins: wrong type, `TraceContext` copy with hostile extra attrs impossible (frozen model) — instead pass mock raising on attribute read; valid `TraceContext` happy path incl. `None` fields | I-3 |
| A13 | `summarize_diagnostic_event` fed foreign/tampered dicts, the fallback dict, dicts containing canary strings | fixed-text only |
| A14 | Mutation isolation: mutate input mapping & returned dict; re-call; outputs independent & deterministic (repeat ×3) | I-9 |
| A15 | Serialisation: `json.dumps(out)` without `default=` succeeds; length ≤ 1024 for all A-cases; combined structured+summary bounded | I-6/I-7 |
| A16 | Composition regression (C-2 archetype): a `ParseFailure`-shaped mapping / `exc.errors()` list offered as attributes | I-1/I-2 |
| A17 | Settings-shaped mapping containing `SecretStr`-like objects / `model_dump()` of real `Settings` offered as attributes (canary key/token) | I-1/I-5 |
| A18 | Idempotence/stability across F3-A2 caps: 0/1/32/33/1024-error ValidationErrors through boundary | I-4 |
| A19 | Import-surface: module imports no `logging`/I/O modules (assert via `sys.modules` delta or source scan, following repo test conventions); `__all__` accuracy via existing package-surface test | I-10 |
| A20 | Performance sanity (non-flaky): pathological mapping refused without iterating it (assert via counting `items()` calls ≤ bound) | I-6 |
| A21 | **No-free-text / no-representation proof (required by Phase 1 review):** a corpus of canary strings and untrusted objects (free prose containing synthetic secrets, R29/R32 shapes, exception instances, `SecretStr`-like and model-like objects with counting/raising `__str__`/`__repr__`, arbitrary class instances) is offered through *every* public input (`error_code`, every attribute key/value position, `validation_error`, `trace`, and `summarize_diagnostic_event`). Assert across structured output, summary and their JSON serialisation: (a) no canary or ≥8-char fragment appears, (b) no `__str__`/`__repr__` counter was incremented by the boundary, (c) output conforms exactly to the §3.1 key set and §3.2 patterns, (d) result is the expected allowlisted payload or the §5 fallback — never a partial rendering. | I-1/I-2/I-5/I-8 |
| A22 | Schema-stability snapshot: the exact §3.1 key set, the three event values, and §3.2 attribute key list are asserted literally, so any accidental schema drift fails the suite pending a `v2` bump (§3.4). | §3.4 |

**Execution honesty:** no test in this matrix has been run for Phase 1 (documentation only). Results may be claimed only in the implementation handoff, from an executed run.

Plus happy-path tests: each event, each allowlisted attribute key, trace
propagation, `attributes_withheld` counting, F3-A2 `SAFE`/`UNAVAILABLE`
embedding.

## 8. Acceptance criteria

1. §2 file list is exact; `git diff --stat` against baseline shows nothing else.
2. New suite + F3-A1 + F3-A2 + config + observability + package-surface suites pass; full offline suite passes (`python -m pytest -q`, and `-m "not live"` per Makefile), no new skips.
3. `ruff check .` and `ruff format --check .` pass.
4. Every invariant I-1…I-10 has at least one named test; A01–A20 present or explicitly waived in the handoff with reason.
5. No production caller is wired; no dependency/config/migration diff.
6. Handoff in F3-A1/A2 style with before/after note that behaviour is additive-only, verification table from **actually executed** runs (no result may be stated without execution), residual risks (§10) restated.
7. Independent review reproduces from the immutable SHA before any merge discussion — **this contract authorises no merge, PR, or deployment.**

## 9. Decisions recorded (Phase 1 review, 2026-10-18) and remaining gates

- **Q1 — Decided.** Closed three-event vocabulary; exact names, purposes and
  allowed attributes are normative in §3.0/§3.2, stabilised by §3.4.
- **Q2 — Decided.** C-1 CLI remediation is **outside F3-C** and registered as
  required patch **SEC-PATCH-CLI-01** (inventory §6) with a blocking gate —
  it must land before any sink integration, any non-fixture/CI use of the
  demo/seed CLIs, or any reuse of CLI formatting by an operator surface.
- **Q3 — Decided.** No free-text message and no generic exception field in v1.
  Adding either later requires a `v2` contract amendment and new review.
- **Q4 — Decided.** Trace persistence, span projection/writers, sinks and
  external integrations are deferred to their own phases.
- **Q5 — Decided.** Limits accepted as testable initial constraints: ≤16
  attributes, ≤1024 encoded output bytes, strict primitive types (no floats),
  int range `0..2**31-1`, code strings ≤64 chars, deterministic fallback.
- **Q6 — Decided.** N-1/N-2/N-3 are **unverified references** (inventory §7);
  nothing in F3-C depends on them.
- **Q7 — Defined, not implemented.** See §8.1. No sink is introduced by F3-C.

**Remaining gate:** production implementation of this contract requires a
separate explicit authorization after review of this finalized document set.

### 9.1 (Q7) Future sink-enforcement requirement — defined here, implemented later
When the first sink phase is authorised, all of the following are mandatory
acceptance criteria for *that* phase (none is implemented in F3-C):
1. Sinks accept only payloads produced by `build_diagnostic_event` (or a `v2`
   successor); sink APIs must not accept raw strings/exceptions/payloads.
2. A repo-wide source-scan test (extending the A19 pattern) fails if
   production code outside an allowlisted set imports `logging`, renders
   tracebacks, or calls `str()`/`repr()`/`.errors()`/`.json()` on exceptions
   in sink-adjacent modules.
3. `SEC-PATCH-CLI-01` has landed (its gate is a precondition).
4. Sink configuration review covers T-09 (SQL echo, SDK debug logging off).

## 10. Known residual risks (unchanged by F3-C, stated plainly)

1. **R29/R32 remain open — F3-C does not globally resolve them.** Not in
   F3-A1, not for any other consumer, not for any free text logged outside
   the boundary. F3-C's claim is strictly local: boundary *output* carries
   no free text, so those bypass classes have no field to act on in that
   output. Any reading of F3-C as an R29/R32 fix is incorrect.
2. **C-1 remains live** in the demo/seed CLIs until **SEC-PATCH-CLI-01** lands (gate defined in inventory §6).
2a. **C-3 (`canonical_json(default=str)`) remains open** as SEC-REVIEW-JSON-01 (inventory §6); F3-C only avoids feeding it untrusted data (I-7).
3. **Bypass is always possible in-process:** nothing prevents future code
   from calling `print`/`logging` directly. The boundary is a safe door, not
   a wall; Q7 owns enforcement.
4. **Upstream formatting:** Pydantic may stringify a raised `ValueError`
   while constructing a `ValidationError` before the boundary sees it
   (F3-A2 handoff) — contained (result never emitted), not preventable here.
5. **Unsalted hashes** (T-12) remain where span schemas already require them.
6. **Aggregate-only diagnostics** (F3-A2's accepted reduction) still withhold
   per-field locations/types; operators get counts, not fields.
7. Aggregate counts/patterns can still leak *existence* metadata (e.g. that
   validation failed N times) — accepted diagnostic utility, bounded by caps.

## 11. Out of scope (explicit)

New logging sinks/formatters/handlers; tracing/OTel; external services or
SaaS; databases/migrations/writers; HTTP/API/UI; provider clients; workflow
engine; settings changes; redactor changes (incl. any R29/R32 "fix");
F3-A2 per-field diagnostics; CLI wiring (separate SEC-PATCH-CLI-01, per Q2); deployment/CI changes;
new dependencies. Deterministic RFQ business logic is not touched in any way.
