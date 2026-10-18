# F3-C — Implementation Contract: Safe Observability Boundary

Status: **Phase 1 contract, revised after security-design review of PR #10 (2026-10-18). The earlier pattern-based string admission is WITHDRAWN (see §3.2 correction note). Production implementation is NOT authorized; it requires separate explicit authorization after review of this revised contract.** Baseline `7f32d5813a219e33e16903ae58e5cbfb8a370005`.
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
    "DiagnosticErrorCode",
    "DiagnosticEvent",
    "DiagnosticPurpose",
    "DiagnosticResultStatus",
    "build_diagnostic_event",
    "summarize_diagnostic_event",
]
```

```python
DIAGNOSTIC_SCHEMA = "rfq.diagnostic/v1"
MAX_DIAGNOSTIC_BYTES = 1024  # hard ceiling on canonical output


class DiagnosticEvent(StrEnum):
    """Closed event vocabulary — the ONLY events a sink may emit in v1."""

    VALIDATION_FAILED = "validation_failed"
    OPERATION_FAILED = "operation_failed"  # generic, caller gives only a code
    DIAGNOSTIC_UNAVAILABLE = "diagnostic_unavailable"  # the fail-closed fallback


class DiagnosticErrorCode(StrEnum):
    """Closed failure-code vocabulary for OPERATION_FAILED (v1, exhaustive)."""

    WORKFLOW_ERROR = "WORKFLOW_ERROR"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    RUN_BUDGET_EXCEEDED = "RUN_BUDGET_EXCEEDED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TOOL_FAILURE = "TOOL_FAILURE"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    UNKNOWN = "UNKNOWN"


class DiagnosticPurpose(StrEnum):
    """Closed purpose vocabulary (values mirror contracts.llm.ModelPurpose)."""

    TRIAGE = "triage"
    EXTRACT = "extract"
    RESOLVE = "resolve"
    REPAIR = "repair"
    CLASSIFY_TRUST = "classify_trust"


class DiagnosticResultStatus(StrEnum):
    """Closed result vocabulary (values mirror observability.spans.ToolResultStatus)."""

    OK = "OK"
    ERROR = "ERROR"
    DENIED = "DENIED"
    TRUNCATED = "TRUNCATED"


def build_diagnostic_event(
    *,
    event: DiagnosticEvent,
    trace: TraceContext | None = None,
    validation_error: ValidationError | None = None,
    error_code: DiagnosticErrorCode | None = None,
    attributes: Mapping[str, object] | None = None,
) -> dict[str, str | int | bool | None]: ...


def summarize_diagnostic_event(event_payload: Mapping[str, object]) -> str: ...
```

Notes on deliberate narrowness (each is a decision §9 can veto):

- **No `exception` parameter.** The only exception-shaped input is
  `validation_error`, consumed exclusively via F3-A2
  (`render_validation_error`). Generic exceptions are represented by the
  caller choosing `OPERATION_FAILED` + a `DiagnosticErrorCode` **enum member**.
  A plain string — even one equal to a member's value — is not admitted.
  There is no parameter through which exception text *could* enter.
- **No `message` parameter.** Human text is derived from the event code by a
  fixed table inside `summarize_diagnostic_event` (which accepts only a
  payload previously produced by `build_diagnostic_event`; anything else
  yields the constant `"Diagnostic unavailable."`). Distinct API, like
  F3-A2's structured/summary split.
- **`event`/`error_code` are enum members, not strings.** Exact-type checks
  (`type(x) is DiagnosticEvent` / `DiagnosticErrorCode`) are required;
  lookalikes (plain `str` with an identical value, `StrEnum` impostors,
  hostile objects) fail closed. Syntactic equality is never admission.
- **Keyword-only arguments**, frozen output construction, fresh dict per call
  (no shared mutable state — same property F3-A2's review verified).

### 3.0 Normative event schema (stable, Q1)

Schema identifier: `rfq.diagnostic/v1`. The vocabulary is **closed**: exactly
three events. Names are lowercase snake_case, never reused with a different
meaning, and never renamed within `v1`.

| Event (`DiagnosticEvent`) | Purpose — when a caller may emit it | Allowed optional inputs | Fixed `error_code` behaviour |
|---|---|---|---|
| `validation_failed` | A Pydantic `ValidationError` was safely summarised via F3-A2. The only event that may embed a `validation` object. | `trace`, `validation_error` (required for `SAFE`; absent/invalid degrades the whole event to `diagnostic_unavailable` semantics: `status=UNAVAILABLE`, `validation=null`) | Always `"VALIDATION_ERROR"` (from F3-A2) |
| `operation_failed` | A non-validation failure the caller can name with an allowlisted code (e.g. a domain `ReasonCode`-style value). The escape hatch that removes any need to stringify an exception. | `trace`, `error_code` (required: a `DiagnosticErrorCode` member; a plain string, a pattern-valid secret-shaped string such as `hunter2024`, or any non-member degrades the event to `diagnostic_unavailable`), `attributes` | The member's value from the closed §3.2 vocabulary |
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
   attribute keys, no widened vocabularies/ranges, no new input parameters.
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
  "error_code": "VALIDATION_ERROR",      // closed-vocabulary value or null
  "trace_id": "…32 hex…", "rfq_id": null, "run_id": null, "span_id": null,
  "validation": { …the exact F3-A2 dict… } ,   // or null
  "attributes": { …admitted scalars / closed-vocabulary values… },
  "attributes_withheld": 0               // count of dropped attribute entries
}
```

Flat-or-one-level only: maximum depth 2 (3 counting `validation`), maximum
16 attributes, serialised size ≤ `MAX_DIAGNOSTIC_BYTES` verified before
return; on any violation the **whole event** is replaced by the constant
fallback (§5), never truncated.

### 3.2 String admission: syntactic validation ≠ semantic admission (CORRECTED)

> **Correction (PR #10 review).** The previous draft admitted any string
> matching `^[a-z][a-z0-9_.-]{0,63}$` into `error_code`, `stage`, `model`,
> `tool_name` and similar fields. That policy is **withdrawn**: `hunter2024`
> matches that pattern, so a caller could place a real secret in a
> "syntactically valid code". A regex proves only *shape*; it proves nothing
> about *trust*. v1 therefore uses two distinct gates, and only the second
> admits a string:
>
> 1. **Syntactic validation** — exact type, length, character shape. Necessary
>    for well-formedness; **never sufficient** for admission.
> 2. **Semantic admission** — the value must be a member of a **closed,
>    boundary-owned vocabulary** (below), supplied as the corresponding enum
>    member from an already-trusted, typed code path. Membership, not shape,
>    is the confidentiality mechanism.

#### 3.2.1 Closed string vocabularies admitted in v1 (exhaustive)

| Output position | Admitted type | Exhaustive values |
|---|---|---|
| `event` | `DiagnosticEvent` member | `validation_failed`, `operation_failed`, `diagnostic_unavailable` |
| `error_code` on `validation_failed` | (fixed by F3-A2) | `VALIDATION_ERROR` |
| `error_code` parameter / field on `operation_failed` | `DiagnosticErrorCode` member | `WORKFLOW_ERROR`, `ILLEGAL_TRANSITION`, `WORKFLOW_TIMEOUT`, `STEP_BUDGET_EXCEEDED`, `RUN_BUDGET_EXCEEDED`, `ATTEMPTS_EXHAUSTED`, `PROVIDER_ERROR`, `RATE_LIMITED`, `TIMEOUT`, `TOOL_FAILURE`, `TOOL_DENIED`, `TOOL_NOT_FOUND`, `TOOL_TIMEOUT`, `MALFORMED_MODEL_OUTPUT`, `PROVIDER_NOT_CONFIGURED`, `UNKNOWN` (16 values; drawn from the existing `ReasonCode` / `ProviderErrorCode` / `DomainError.code` vocabularies at baseline, frozen here — upstream additions do **not** auto-admit) |
| attribute `purpose` | `DiagnosticPurpose` member | `triage`, `extract`, `resolve`, `repair`, `classify_trust` |
| attribute `result_status` | `DiagnosticResultStatus` member | `OK`, `ERROR`, `DENIED`, `TRUNCATED` |
| `schema`, `status`, fallback/summary text | boundary constants | `rfq.diagnostic/v1`; `SAFE` / `UNAVAILABLE`; §5 constants |

That is **every** caller-influenced string that can appear in v1 output.
Enum members are admitted by exact type + membership; the boundary emits the
member's `.value` from its own table lookup, not by formatting the caller's
object. A plain `str` equal to a vocabulary value (e.g. `"triage"`,
`"TIMEOUT"`) is **rejected** (counted withheld / whole-event fallback for
`error_code`), because the boundary cannot distinguish it from a caller
string that happens to look safe.

#### 3.2.2 Dynamic identifiers — provenance decision: OMITTED from v1

| Field | Trusted provenance that *would* be required | v1 decision |
|---|---|---|
| `model` | Would need: value taken only from validated `Settings` (T0 config) or a provider response already parsed into a typed contract, with a closed provider registry to check against. Config strings are operator-controlled free strings (`.env`), and provider responses are T3 — neither gives a closed set. `config.GroqSettings.agent_model` accepts any non-empty string. | **Omitted.** No attribute key. Future admission needs a provider/model registry phase and a `v2` amendment. |
| `tool_name` | Would need: value taken only from the tool registry's own `ToolSpec.name` objects (registry is the trusted T0/T1 source), never from model-requested `ToolCall.name` (T3). No registry exists yet (Phase 2+); today a tool name is only pattern-validated (`^[a-z][a-z0-9_]{2,63}$` in `contracts/llm.py`) — syntactic only. | **Omitted.** Revisit when a registry exists to be the provenance; then admission = registry membership, still not a regex. |
| `stage`, `status_code`, `outcome`, `reason_code`, `finish_reason` | No closed, boundary-stable vocabulary exists at baseline for `stage`/`status_code`; `reason_code`/`finish_reason` exist as enums (`ReasonCode`, `FinishReason`) but are owned by workflow/provider phases that have no caller yet. | **Omitted from v1** (not pattern-admitted). Each may return only as a boundary-owned frozen vocabulary in a `v2` amendment, with its source enum pinned. |
| `*_sha256` hash strings | Format check cannot prove the string is a digest, nor what was digested; provenance would be "produced by `sha256_text`/`sha256_bytes` over approved data at a trusted call site", which the boundary cannot verify. Spans that need hashes are deferred (Q4). | **Omitted from v1 attributes.** Trace IDs (§3.3) are unaffected — they come from validated `TraceContext`, not caller strings. |

Callers needing a dynamic identifier in diagnostics must wait for its
provenance-owning phase; there is deliberately no "temporary" string field.

#### 3.2.3 Non-string attributes (unchanged, exhaustive)

Keys not listed are dropped and counted in `attributes_withheld`.

| Key | Admitted value |
|---|---|
| `purpose` | `DiagnosticPurpose` member (§3.2.1) |
| `result_status` | `DiagnosticResultStatus` member (§3.2.1) |
| `duration_ms`, `latency_ms`, `count`, `attempt`, `retries`, `repair_attempts`, `step`, `seq`, `error_count`, `validation_error_count`, `tokens_in`, `tokens_out`, `tokens_cached`, `request_bytes`, `result_bytes`, `args_bytes` | `type(v) is int` (bool excluded), `0 ≤ v ≤ 2**31 - 1` |
| `output_valid`, `truncated`, `duplicate`, `eligible_for_human_review`, `requires_human_approval` | `type(v) is bool` |

**Attributes are honoured only on `operation_failed` (§3.0); on other events
they are dropped and counted.** No floats, no `None` values, no nested
containers, no bytes, no dates/Decimals/UUIDs, no plain strings at all.

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
{
    "schema": "rfq.diagnostic/v1",
    "event": "diagnostic_unavailable",
    "status": "UNAVAILABLE",
    "error_code": None,
    "trace_id": None,
    "rfq_id": None,
    "run_id": None,
    "span_id": None,
    "validation": None,
    "attributes": {},
    "attributes_withheld": 0,
}
```

**Fallback/summary non-reflection:** the fallback above is a literal
constant — rejected inputs (including a pattern-valid rejected `error_code`
such as `hunter2024`) are never copied, truncated, hashed-into, or named in
it, and `type(exc).__name__` of a rejected object is never emitted.
`summarize_diagnostic_event` likewise derives text only from the §3.2.1
vocabulary via a fixed table: it first re-validates the payload's
`event`/`status`/`error_code` against the closed vocabularies and the §3.1
key set, and on *any* mismatch returns the one constant
`"Diagnostic unavailable."` — a tampered payload carrying a secret-shaped
string in any field yields that constant, never an interpolation of the
field (tests A21/A23).

Attribute-level problems are softer: drop the entry, increment
`attributes_withheld` (capped reporting at 16), keep the event. Whole-event
fallback is reserved for structural/hostile failures. Both paths are pure,
total (never raise — including for `None`, non-mapping `attributes`,
`Mapping` subclasses with hostile `items()`), idempotent, and independent of
input size (work is bounded by the caps before iteration: mappings reporting
`len > 64` are refused whole rather than iterated).

## 6. Security invariants (each must map to ≥ 1 test in §7)

- **I-1 Closed-vocabulary output (honest strength).** Output keys, event
  values and attribute keys/values come exclusively from §3.1–3.2. Every
  caller-influenced string in output is a member's value drawn from the
  finite §3.2.1 vocabularies (3 events, 17 error-code values incl.
  `VALIDATION_ERROR`, 5 purposes, 4 result statuses) — admitted by exact
  enum type + membership, never by pattern. This guarantees that **no
  caller-chosen string content** reaches output; it does *not* guarantee
  that a caller selected the *semantically correct* member, nor does it
  close the small covert channel of deliberately encoding information in
  *which* member/int/bool a caller picks. Pattern checks in the boundary
  are well-formedness only and are never cited as a confidentiality control.
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
- **I-5 No caller-controlled text (bounded claim).** No output field can
  contain a caller-supplied string: v1 output text is limited to boundary
  constants and §3.2.1 vocabulary values (plus pattern-validated trace IDs
  from `TraceContext`, §3.3). Within that bound, R29 (T-04) and R32 (T-05)
  have no field to act on in boundary output — while **remaining open in
  F3-A1 and for every other consumer, exactly as stated in §10**. I-5 does
  not claim that admitted metadata is meaningless: event/code/purpose
  choices and bounded ints reveal that *something* happened, and a hostile
  in-process caller could encode a few bits per event in those choices.
  Confidentiality against the *caller itself* is not a boundary property;
  the property is that untrusted *payload content* cannot pass through.
- **I-6 Bounded.** ≤ 16 attributes, vocabulary-bounded strings, ints ≤ 2³¹−1,
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
| A05 | Free-text smuggling: attributes `{"note": "api_key=hunter2024"}`, `{"message": …}`, unknown keys with secret values; **omitted v1 keys** `model`/`tool_name`/`stage`/`reason_code`/`*_sha256` offered with any value | I-1/I-5 (dropped + counted; key does not exist in v1) |
| A06 | R29 shapes as attribute values/keys: `[api_key]: hunter2024`, zero-width-adjacent keys, 10⁴-char key, decorated key | I-5 (never emitted) |
| A07 | R32 shape: `{"a": "token", "b": canary}` as attributes; nested dict/list/set/tuple values | I-1/I-5 |
| A08 | Hostile attribute values: objects with raising `__str__`/`__repr__`/`__eq__`/`__hash__`, property-raising mapping subclass, mapping whose `items()` raises, self-referential structures passed where containers attempted | I-2/I-3/I-8 |
| A09 | Scalar edge: `True` as int field, `1` as bool field, `float('inf'/'nan')`, `Decimal`, `UUID`, `datetime`, `bytes`, huge int `2**100`, negative counts | I-1/I-6 |
| A10 | String-shape edges (syntactic gate): uppercase, spaces, newline/ANSI-escape injection, `{}` format braces, `%s`, emoji, overlong strings, non-hex/short hashes in omitted `*_sha256` keys | I-1/T-10 — none admitted regardless of shape |
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
| A21 | **No-free-text / no-representation proof (revised):** a corpus of canary strings and untrusted objects is offered through *every* public input (`error_code`, every attribute key/value position, `validation_error`, `trace`, and `summarize_diagnostic_event`). Corpus must include **synthetic secrets that match the WITHDRAWN regex exactly** — `hunter2024`, `secret123`, `token2024x`, `a1b2c3d4` and `pass.word-2024` (each matches the withdrawn `^[a-z][a-z0-9_.-]{0,63}$` exactly) — offered as `error_code`, and under `stage`/`model`/`tool_name`/`purpose`/`result_status` and unknown keys; plus free prose with secrets, R29/R32 shapes, exception instances, `SecretStr`-like/model-like objects with counting/raising `__str__`/`__repr__`, arbitrary class instances. Assert across structured output, summary and their JSON serialisation: (a) no canary or ≥8-char fragment appears, (b) no `__str__`/`__repr__` counter incremented, (c) output is exactly the §3.1 key set with strings drawn only from §3.2.1, (d) result is the expected admitted payload or the §5 fallback — never a partial rendering, never the rejected value reflected in fallback, `attributes_withheld`, summary or error text. | I-1/I-2/I-5/I-8 |
| A22 | **Vocabulary & schema-stability snapshot:** literal assertion of (i) the §3.1 key set, (ii) the three `DiagnosticEvent` values, (iii) all 16 `DiagnosticErrorCode` values + fixed `VALIDATION_ERROR`, (iv) all 5 `DiagnosticPurpose` and 4 `DiagnosticResultStatus` values, (v) the §3.2.3 attribute key list by type category, (vi) that `model`/`tool_name`/`stage`/`status_code`/`outcome`/`reason_code`/`finish_reason` and `*_sha256` keys are **absent**, (vii) the §5 fallback dict literally. Any drift — including an upstream `ModelPurpose`/`ToolResultStatus` addition — fails pending a `v2` amendment (§3.4). Plain strings equal to vocabulary values (`"triage"`, `"TIMEOUT"`, `"VALIDATION_ERROR"` as `error_code`) and hostile enum lookalikes are rejected in the same test. | §3.2/§3.4 |
| A23 | **Fallback/summary non-reflection:** force whole-event fallback via each trigger (pattern-valid secret `error_code`, hostile objects, oversized attributes, tampered payload to `summarize_diagnostic_event` with secret-shaped strings planted in *every* field incl. `error_code`/`event`/`attributes`). Assert fallback and `"Diagnostic unavailable."` are byte-identical constants across all triggers and contain no input fragment, type name or count derived from rejected content (other than §5's fixed fields). | I-5/I-8 |

**Execution honesty:** no test in this matrix has been run for Phase 1 (documentation only). Results may be claimed only in the implementation handoff, from an executed run.

Plus happy-path tests: each event, each allowlisted attribute key, trace
propagation, `attributes_withheld` counting, F3-A2 `SAFE`/`UNAVAILABLE`
embedding.

## 8. Acceptance criteria

1. §2 file list is exact; `git diff --stat` against baseline shows nothing else.
2. New suite + F3-A1 + F3-A2 + config + observability + package-surface suites pass; full offline suite passes (`python -m pytest -q`, and `-m "not live"` per Makefile), no new skips.
3. `ruff check .` and `ruff format --check .` pass.
4. Every invariant I-1…I-10 has at least one named test; A01–A23 present or explicitly waived in the handoff with reason. A21 must include the regex-matching secret cases by name.
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
8. **Vocabulary-choice covert channel (I-1/I-5 bound):** closed vocabularies
   stop caller *content*, not caller *choices*. A hostile in-process caller
   can encode a few bits per event in which enum member / int / bool it
   selects. Preventing that is caller-trust policy, not a boundary property,
   and v1 does not claim it.
9. **Dynamic identifiers are simply absent** in v1 (`model`, `tool_name`,
   hashes): diagnostic utility is reduced until their provenance-owning
   phases exist. That loss is deliberate and preferable to pattern-admitted
   strings.

## 11. Out of scope (explicit)

New logging sinks/formatters/handlers; tracing/OTel; external services or
SaaS; databases/migrations/writers; HTTP/API/UI; provider clients; workflow
engine; settings changes; redactor changes (incl. any R29/R32 "fix");
F3-A2 per-field diagnostics; CLI wiring (separate SEC-PATCH-CLI-01, per Q2); deployment/CI changes;
new dependencies. Deterministic RFQ business logic is not touched in any way.
