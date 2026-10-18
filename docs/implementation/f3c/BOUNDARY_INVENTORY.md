# F3-C — Boundary Inventory (Phase 1: read-only audit)

Status: **finalized Phase 1 audit — architecture direction approved with conditions (2026-10-18 review); production implementation NOT authorized.** Baseline:
`7f32d5813a219e33e16903ae58e5cbfb8a370005` (merge of PR #9, F3-A2), verified
2026-10-18 with `git rev-parse HEAD` on a clean tree. No production code or
test was modified for this audit. **No tests were executed for this document set** (sandbox has no `.venv` and
dependencies are not installed). No test result is claimed anywhere in these
documents; every claim below is from source inspection and is labelled
accordingly. Any future statement of test results must be backed by an
executed run recorded in the implementation handoff.

## 1. Baseline verification

| Item | Verified state at baseline |
|---|---|
| `main` SHA | `7f32d5813a219e33e16903ae58e5cbfb8a370005` — matches the brief exactly. |
| F3-A1 | `src/rfq_agent/observability/redaction.py` (1,331 lines). Public: `REDACTED`, `REDACTED_KEY`, `email_hash`, `redact_mapping`, `redact_payload`, `redact_text`, `summarize_payload`. Handoff: `docs/implementation/f3a1/HANDOFF.md`. |
| F3-A2 | `src/rfq_agent/observability/validation_errors.py` (91 lines). Public: `render_validation_error`, `summarize_validation_error`. Reads **only** native `ValidationError.error_count(error)` (descriptor invoked directly, inside `try`); fixed 7-key schema `rfq.validation-diagnostic/v1`, count capped at 32, `locations: null` / `WITHHELD`. Handoff + independent review (incl. hostile `__class__` remediation) in `docs/implementation/f3a2/`. |
| F3-B (config) | `src/rfq_agent/config.py`: `SecretStr` + masking `field_serializer`s for `groq.api_key` / `http.operator_token`, `hide_input_in_errors=True` on root `Settings` only, `_as_bool` deliberately does not interpolate the offending value. **Not changed by F3-C.** |
| Architecture docs | There is **no** `docs/ARCHITECTURE.md` in the repo (README states it is "not committed yet"). Architecture exists only as section references in module docstrings (e.g. §10.2 in `spans.py`, §8 in `trust.py`). Do not cite a document that does not exist. |
| Existing tests | `tests/unit/test_observability.py`, `test_redaction_{adversarial,production,regression}.py`, `test_validation_errors.py`, `test_validation_errors_review.py`, `test_config.py`, `test_package_surface.py` (module/`__all__` registry — any new module must be added there). |
| Actual logging/tracing | **None in production code.** `grep` over `src/` finds no `import logging`/`getLogger`/`structlog`/`opentelemetry`/`sentry`, no traceback rendering, no log writer. Only `migrations/env.py` uses `logging.config.fileConfig` (Alembic's own config). `Settings.log_level`, `Settings.log_file`, `Settings.redact_logs` are validated but **consumed by nothing**. |

## 2. What actually exists vs. what is only planned

**Exists (production code):** redaction choke point (F3-A1, *no production
caller*), aggregate validation renderer (F3-A2, *no production caller*),
correlation-ID / span *schemas* (`observability/ids.py`: `TraceContext`,
`LogFields`; `observability/spans.py`: `LLMCallSpan`, `ToolCallSpan`),
`Settings` secret handling, persistence schema incl. trace tables
(`persistence/models/trace.py`, `run.py`, `rfq.py`), two CLIs
(`demo/__main__.py`, `seed/__main__.py`), test-only provider doubles
(`contracts/testing.py`), deterministic fingerprinting
(`domain/values.py: canonical_json`/`fingerprint`).

**Only planned / declared, not implemented:** any logging sink or formatter;
any trace/span *writer* (`contracts/ports.py` declares `TraceWriter`-style
ports with `TraceContext` parameters, no implementation); operator trace view
(docstring: "arrives in Phase 6"); HTTP/UI (config present, `ui/` is asserted
absent by `test_package_surface.py::test_no_layer_without_a_phase_exists_yet`);
LLM provider client (ports + contracts only, replay-by-default); workflow
engine / agent loop (state-machine contract only).

> Therefore F3-C is a **pre-integration** boundary: its job is to be the only
> door future sinks may use, before any sink exists. Almost every production
> risk below is currently *latent* (no caller) or located in the two CLIs.

## 3. Boundary inventory

Legend — Status: **C** = confirmed in production code at baseline,
**L** = latent (safe component exists but a future/bypassing caller defeats
it), **P** = planned only, **H** = hypothetical (no code path today).
Trust: T0 system / T1 business data / T2 untrusted customer / T3 model claim /
T4 operator (per `domain/trust.py` TrustTier).

### B-01 — F3-A2 validation renderer → any future consumer
1. `observability/validation_errors.py::render_validation_error`, `summarize_validation_error`.
2. Input: `ValidationError` (T0–T4 depending on validated object; settings errors may embed T0 secrets in messages/input).
3. Secret-bearing: Pydantic messages, `ctx` exception objects, `input`, `loc`, URLs — *none are read*.
4. Existing protection: only native `error_count()` via descriptor; no `str`/`repr`/`errors()`/`json()`; fixed keys; count ≤ 32; fail-closed `UNAVAILABLE`.
5. Bypass: **any caller that does not use it** — raw `str(error)` / `.errors()` / `.json()` anywhere else (see B-08, B-09, B-11). The renderer cannot protect upstream code that already formatted the exception (Pydantic itself may stringify a raised `ValueError` during construction).
6. Exists; **no production caller**.
7. Mitigation: F3-C boundary accepts validation failures *only* through this renderer (Invariant I-4); lint/test rule forbidding raw rendering in future sink code.

### B-02 — F3-A1 redactor → any future consumer
1. `observability/redaction.py::redact_text/redact_mapping/redact_payload/summarize_payload`.
2. Input: arbitrary `Json`/text (T2/T3 explicitly in scope).
3. Secret-bearing: keyed credentials, bearer/provider tokens, PEM, URL userinfo, e-mails, credential-record shapes.
4. Existing protection: fail-closed withholding at bounds (depth 32, 256 KiB structured parse, 64 KiB container, 32 records / 128 candidates), hostile-object/cycle guards in structural walk, redact-before-truncate in `summarize_payload`.
5. Bypass/disclosure: **R29** (decorated/overlong/zero-width-adjacent keys — `redact_text('log [api_key]: hunter2024')` leaks, pinned by tests) and **R32** (semantic association `{"a":"token","b":"hunter2024"}` unchanged) — see THREAT_MODEL. Also: redactor is opt-in per call; nothing forces a future logger through it. `summarize_payload`'s `_withheld` emits `type(error).__name__` (class name only — low risk, but F3-C must not copy this pattern for arbitrary objects). `summarize_payload(label=...)` interpolates a caller string into output keys.
6. Exists; **no production caller** outside tests.
7. Mitigation: F3-C treats redaction as defence-in-depth only; allowlisted values that do not match a safe pattern are **withheld**, not redacted-and-passed (this contains R29/R32 **inside boundary output only** — the secret-shaped value never enters a free-text field; R29/R32 are not globally resolved, see THREAT_MODEL §6).

### B-03 — Structured log payload shape (`LogFields`)
1. `observability/ids.py::LogFields`, `TraceContext`.
2. Input: caller-composed `event`, free `message: str` (unbounded), `attributes: dict[str, str|int|float|bool|None]`, `TraceContext`.
3. Secret-bearing: `message` and attribute *values* can carry anything the caller puts there; keys are unconstrained strings.
4. Existing protection: shape only — `level` enum pattern, `event` ≤ 120 chars, scalar-only attribute values. **No redaction, no size bound on `message`/attribute count, no allowlist.**
5. Bypass: a future `logger.info(LogFields(message=str(exc), ...))` is fully "compliant" with this schema while leaking. `float` attributes admit `inf`/`nan` (non-JSON).
6. **P** — schema exists, never instantiated outside tests.
7. Mitigation: F3-C supersedes `LogFields` as the sink-facing contract (does not modify it); sink-facing events have **no free-text message field** in v1.

### B-04 — Trace-span schemas (`LLMCallSpan` / `ToolCallSpan`)
1. `observability/spans.py`.
2. Input: caller-composed stage/model/purpose/error_code strings (≤ 32–120 chars), hashes, counters.
3. Secret-bearing: free-ish strings could smuggle content (bounded length only); design intent is hashes + byte counts, prompts never stored.
4. Existing protection: strict `DomainModel` (`extra=forbid`, frozen), sha256 patterns, outcome validators.
5. Bypass: caller stuffing a secret into `stage`/`model`/`error_code`; span → DB row (B-07) makes it persistent. No redaction hook on construction.
6. **P** — schemas + persistence tables exist, no writer.
7. Mitigation: F3-C string policy (pattern-allowlisted codes, else withheld) applies to any span field ever emitted to a sink; span persistence remains a separate, later decision.

### B-05 — RFQ ingestion → persistence (intentional raw store)
1. `persistence/models/rfq.py::RfqRow.body_text` (verbatim T2), `subject`, `sender_email` (plaintext, stored once), `RfqAttachmentRow.text_preview`, `filename`.
2. Input: T2 customer content. 3. Secret-bearing: entire body can embed credentials/PII (customer may paste an API key into an RFQ).
4. Existing protection: design rule "stored once, never logged; referenced by `rfq_id`/`body_sha256`/`sender_email_hash` elsewhere" (module docstrings). This is an *accepted, audited* store, not a leak.
5. Bypass: any future read path, operator view, demo report or diagnostic that selects `body_text`/`sender_email` into output; `text_preview` inherits the same risk with less visibility.
6. **C** (schema + writers exist; demo intake writes fixture rows).
7. Mitigation: out of F3-C's code scope; F3-C invariant: boundary must never accept RFQ body/subject/sender fields as diagnostic attributes (no attribute key exists for them).

### B-06 — LLM/tool contracts → future spans & repair loop
1. `contracts/llm.py::ParseFailure.raw` (model output retained "for debugging… persisted behind the redaction policy, never logged verbatim in production"), `ParseIssue{field,message,error_type}` ("safe to feed back to the model"), `CompletionResult.text`, `ToolCall.arguments`; `domain/trust.py::InjectionSuspicion.snippets` (verbatim T2 snippets, ≤ 5), `UntrustedText.text`.
2. Input: T3 model claims, T2 snippets. 3. Secret-bearing: raw model output can echo customer content or a canary; `ParseIssue.message` derives from Pydantic messages (see B-09).
4. Existing protection: docstring-level policy only; length caps on `ParseIssue` fields. **No code enforces "never logged".**
5. Bypass: first logging integration that logs a `ParseFailure` wholesale (plain `str()`/`model_dump()` of it).
6. **P/H** — contracts exist, no provider, no loop, no logger.
7. Mitigation: F3-C v1 emits none of these; any future inclusion requires a named allowlist entry + adversarial tests (contract §7, Q4).

### B-07 — Persistence trace/detail JSON columns
1. `persistence/models/trace.py::LlmCallRow.payload_json` ("Redacted request/response snapshot. Never raw prompt text"), `ToolCallRow.args_json`; `run.py::RunEventRow.detail_json`, `rfq.py::IntakeEventRow.detail_json`, each with `*_sha256` companions.
2. Input: whatever a future writer supplies (T2/T3 possible). 3. Secret-bearing: full payload snapshots if a writer is careless.
4. Existing protection: comments/docstrings + hash companions. **No writer exists; no redaction is enforced at the column.**
5. Bypass: a Phase-2 writer calling `summarize_payload` incorrectly, or writing raw payloads — database persistence is *worse* than a log line (durable, queryable, backed up).
6. **P** — tables exist (migrations 0001/0002), no writer in `src/`.
7. Mitigation: explicitly out of F3-C scope (no persistence changes); contract requires any future writer to persist only F3-C-boundary output or F3-A1 summaries, decided in its own phase.

### B-08 — Demo CLI stderr (confirmed unsafe rendering)
1. `demo/__main__.py::main` — `except ValidationError as error: print(f"…:\n{error}", file=sys.stderr)`; also `except ValueError as error: print(f"…: {error}")`, `except DemoError` similarly; `seed/__main__.py::main` prints `f"error: {error}"` for `SeedError`.
2. Input: `Settings.load()` `ValidationError` (T0: environment may contain `RFQ_GROQ__API_KEY`, `RFQ_HTTP__OPERATOR_TOKEN`, DB URL password), other `ValueError`s from contract validation over DB facts.
3. Secret-bearing: `str(ValidationError)` renders messages; root `hide_input_in_errors=True` suppresses input echo but **not message text** (config tests prove message-carried secrets are a real class: nested-section errors, `_as_bool`, model validators).
4. Existing protection: partial — `hide_input_in_errors`, `SecretStr` serializers (protect `model_dump`, not exception rendering), `Database.url.render_as_string(hide_password=True)` for the header line. Tests in `test_config.py::TestValidationErrorDisclosure` assert secret absence in *rendered settings errors they construct* — but the CLI interpolates the raw exception regardless.
5. Bypass: **this is the bypass.** Any settings error whose message embeds a secret reaches stderr (terminal, CI logs, shell history, crash reports).
6. **C** — the only production-adjacent exception-to-output path at baseline.
7. Mitigation: route through `summarize_validation_error` (F3-A2). **Not done in this audit**; CLI wiring is **outside F3-C** per decision Q2 and is registered as **SEC-PATCH-CLI-01** (§6). Note demo is fixture-facing, which lowers (does not remove) exposure.

### B-09 — Test provider double builds `ParseIssue` from raw Pydantic errors
1. `contracts/testing.py::RecordingLLMProvider.parse` (name in source: the scripted provider) — `exc.errors()` → `ParseIssue(field=".".join(str(p) for p in error["loc"]), message=error["msg"], error_type=error["type"])`, `raw=str(result.data)`.
2. Input: T3 scripted data failing validation. 3. Secret-bearing: `msg` can embed input/context via custom validators (F3-A2's core finding).
4. Existing protection: none needed at runtime — **test-support code only**, never imported by production paths.
5. Bypass: if this double's `ParseFailure` were ever logged, or if the pattern is copied into the real provider's repair loop (the obvious place to copy from).
6. **C in test-support code; H for production.**
7. Mitigation: contract §6 names this pattern explicitly as forbidden in sink-facing code; F3-C adds no change here (test code is out of scope) but the adversarial matrix includes it as a regression archetype.

### B-10 — Deterministic serialisation (`canonical_json` / `fingerprint`)
1. `domain/values.py::canonical_json` uses `json.dumps(..., default=str)`.
2. Input: any `Json`-typed value incl. arbitrary objects smuggled as `Any` (`Json = Any`).
3. Secret-bearing: `default=str` **invokes `str()` on unexpected objects** — property access, hostile `__str__`, secret-bearing reprs — silently, inside a "safe" fingerprint helper. Used by gating evidence fingerprints, quote fingerprints, `summarize_payload`.
4. Existing protection: none at this function; callers are expected to pass plain JSON.
5. Bypass: a future diagnostic that fingerprints an arbitrary payload gets representation-leak + possible exceptions/side effects from `__str__`, and non-determinism.
6. **C** (function), **L** (no observability caller passes hostile objects today).
7. Mitigation: F3-C output verification must serialise with `json.dumps` **without** `default=` and fail closed (Invariant I-7); F3-C never passes untrusted objects to `canonical_json`.

### B-11 — Exception construction across the domain layer
1. ~200 `raise ValueError(msg)` sites in `domain/*`, `contracts/*`; messages interpolate IDs, codes, dates, money (T1) — inspected samples do not interpolate secrets or T2 body text. `domain/workflow.py::TransitionError` carries `source`/`event` enums.
2. Input: mostly T0/T1. 3. Secret-bearing: low today; pattern risk if a future validator interpolates a value (the `config._as_bool` comment shows the project already treats this as a live hazard).
4. Existing protection: convention + `hide_input_in_errors` (root settings only; `DomainModel`/`_ConfiguredModel` do **not** set it, so nested/domain `ValidationError`s echo input via `str()`).
5. Bypass: generic `except Exception: log(str(exc))` in any future worker/API layer would harvest all of it.
6. **C** (construction), **H** (no such handler exists).
7. Mitigation: F3-C accepts **no generic exception** at all in v1 (only `ValidationError` via F3-A2, mapped to a fixed code); domain errors reach sinks only as caller-selected allowlisted codes.

### B-12 — Database engine / SQL echo
1. `persistence/engine.py`, `DatabaseSettings.echo=False` default, Alembic `migrations/env.py`.
2. Input: SQL + bound parameters (T1/T2). 3. Secret-bearing: echoed SQL parameters would include `body_text`, e-mails.
4. Existing protection: echo defaults off; no logging configured.
5. Bypass: `RFQ_DATABASE__ECHO=true` in a deployed env once any logging is configured; Alembic `fileConfig` could enable SQLAlchemy loggers.
6. **C** (config surface), **H** (no sink today).
7. Mitigation: out of F3-C code scope; recorded as a deployment-checklist item for the future sink phase (THREAT_MODEL T-09).

### B-13 — Operator/demo stdout reports
1. `demo/__main__.py::_format_report` prints sender e-mail (fixture), customer summary, money, blocked-reason messages; `demo/runner.py` captures `str(error)` into `stop_reason` → printed; `demo/intake.py` re-raises `DemoError(str(error)) from error` from `SeedSchemaError`.
2. Input: fixture T2 stand-ins + DB facts. 3. Secret-bearing: fixture data is synthetic; against a real DB the same code prints real sender e-mail and stop reasons derived from exception text.
4. Existing protection: demo is explicitly fixture-scoped ("untrusted fixture intake" label).
5. Bypass: running the demo against a non-fixture database, or copying its formatting into the Phase-6 operator UI.
6. **C** (demo scope only).
7. Mitigation: no change proposed in F3-C Phase 1; contract forbids reusing `str(error)`-into-report in future operator surfaces; CLI remediation itself is **SEC-PATCH-CLI-01** (§6), gated before any operator surface reuses CLI formatting.

### B-14 — Configuration secrets at rest in process memory
1. `config.py::GroqSettings.require_credentials()` returns the raw key (by design, for the future provider); `has_credentials` is the safe boolean.
2. Input T0. 3. The key itself.
4. Existing protection: `SecretStr`, masking serializers — `model_dump()`/`model_dump_json()` are log-safe (tested).
5. Bypass: future code logging `settings` via `repr()` (Pydantic `SecretStr` repr is masked — safe), logging `require_credentials()` result, or logging `os.environ`.
6. **C** (API), **H** (no caller logs it; no provider exists).
7. Mitigation: F3-C attribute allowlist contains no settings/secret-shaped key; contract restates "settings objects are never diagnostic attributes".

## 4. Sources, transformations, destinations — summary flow

```
T2 RFQ body/subject/sender ──► RfqRow (verbatim, accepted) ──► [no reader to logs today]
T2/T3 content ──► UntrustedText / ParseFailure.raw / snippets ──► [no logger; docstring policy only]
env secrets (T0) ──► Settings (SecretStr, hide_input) ──► str(ValidationError) ──► demo CLI stderr  ★ confirmed unsafe sink
domain ValidationErrors ──► str()/errors() ──► demo CLI stderr, contracts/testing ParseIssue (test-only)  ★/☆
arbitrary payload ──► redact_*/summarize_payload (F3-A1) ──► [no caller]   (R29/R32 residuals inside)
ValidationError ──► render_validation_error (F3-A2) ──► [no caller]
spans/LogFields ──► [no writer/sink]        trace tables ──► [no writer]
```

★ = exists in code today. Everything else is a door that a future sink,
worker, API or UI will open — F3-C exists to lock the door shape first.

## 5. Decision log (Phase 1 review, 2026-10-18)

| ID | Decision |
|---|---|
| Q1 | **Approved:** closed vocabulary of exactly three events. Exact names, purposes and per-event allowed attributes are normative in `IMPLEMENTATION_CONTRACT.md` §3. Changing them requires a schema-version bump (§3.4 of the contract). |
| Q2 | **C-1 is outside F3-C** but is registered below as required security patch **SEC-PATCH-CLI-01** — a tracked obligation with a blocking gate, not an indefinite follow-up. |
| Q3 | **Approved:** no free-text message and no generic exception field in F3-C v1. |
| Q4 | **Deferred:** trace persistence, span projection/writers, sinks, external integrations — none are in F3-C. |
| Q5 | **Approved as testable initial constraints:** ≤16 attributes, ≤1024 encoded output bytes, strict primitive types, deterministic constant fallback. Each limit must have a boundary test (contract A11/A15). |
| Q6 | **N-1/N-2/N-3 are unverified references** until original evidence is supplied (§7). No meaning is assigned to them. |
| Q7 | Future sink enforcement is **defined** in the contract (§8) and **not implemented** in F3-C. No sink is introduced. |

## 6. Registered follow-up items (tracked, not implemented here)

### SEC-PATCH-CLI-01 — CLI exception rendering (from C-1 / B-08) — REQUIRED, separate patch
- **Scope (only):** `demo/__main__.py::main` and `seed/__main__.py::main` stop
  interpolating raw exceptions (`f"…{error}"`). Validation failures render via
  F3-A2 `summarize_validation_error`; other failures render as fixed text plus
  an allowlisted code — never exception text. Add CLI regression tests with a
  synthetic secret-bearing settings error.
- **Blocking gate:** must land **before** (a) any logging/sink integration,
  (b) the demo or seed CLI is run against a non-fixture database or in CI with
  real credentials in the environment, or (c) the Phase-6 operator surface
  reuses any CLI formatting code — whichever comes first.
- **Not in F3-C:** F3-C remains caller-less; this patch is its own reviewed
  change with its own handoff. It is not optional and not "someday": the gate
  above makes it a precondition of the next integration phase.

### SEC-REVIEW-JSON-01 — `canonical_json(default=str)` (from C-3 / B-10) — separate security review
`domain/values.py::canonical_json` uses `json.dumps(..., default=str)`, which
invokes `str()` on any non-JSON value. The digest is not emitted, so the risk
is *representation invocation inside a trusted helper* (hostile `__str__`
execution, exceptions, non-determinism), not direct disclosure at today's
callers. Actual production callers and their trust boundaries:

| Caller | Input actually passed today | Trust boundary |
|---|---|---|
| `domain/quote.py::quote_inputs_fingerprint` | Dict built in-function from typed `Quote` fields (`str/int/isoformat` only) | T1 typed domain model; `default=str` unreachable for the constructed payload as written |
| `domain/gating.py::_evidence_fingerprint` | Dict of `model_dump(mode="json")` results + scalars | T1 typed models; JSON-mode dumps are primitives |
| `persistence/writers.py::_operation_key` | `{"quote": model_dump(mode="json"), "ledger": model_dump(mode="json")}` | T1 typed models; primitives |
| `observability/redaction.py` (`_redact_string`, `summarize_payload`) | Post-redaction structures / caller `payload: Json` | **Widest boundary:** `summarize_payload` accepts caller payloads; a non-JSON object smuggled as `Json` (= `Any`) reaches `default=str` before redaction |
| `domain/values.py::fingerprint` | No production caller found at baseline (tests only) | Whoever adopts it inherits `default=str`; its docstring advertises prompt/tool-result hashing (T2/T3-adjacent uses) |

- **Review questions for SEC-REVIEW-JSON-01:** (1) make `canonical_json` strict
  (no `default=`, raise on non-JSON) and fix any caller relying on coercion;
  (2) or split into a strict function + an explicitly-named coercing one.
  Changing it affects quote/gate/idempotency fingerprints, so it needs its own
  change, digest-stability analysis and full-suite evidence. **Explicitly not
  part of F3-C**, which instead never passes untrusted data to `canonical_json`
  and verifies its own output with `json.dumps` without `default=` (I-7).

## 7. N-1 / N-2 / N-3 traceability

The audit brief instructs tracing previously documented N-1/N-2/N-3 findings
to their original sources. **Result: no occurrence of `N-1`, `N-2` or `N-3`
exists anywhere in this repository** — searched `docs/`, `src/`, `tests/`,
evidence files and tooling at baseline (only R-series identifiers from the
F3-A1 review lineage exist: R27–R32, of which R28/R31 are recorded fixed in
production, R29/R32 open, R27 referenced as a limitation on escaped record
field names, R30 not mentioned). The F3-A review documents that define the
R-series (`docs/reviews/f3a/`, pinned by `tools/verify_f3a1.py`) are **not in
this repo** — the verifier expects an external `--review-root`. Therefore:

- N-1/N-2/N-3 most plausibly originate in that external review set or in a
  predecessor session's documents not carried into this repository.
- **No meaning is inferred for them here.** Any mapping of N-x to R-x would be
  fabrication. Approval Question Q6 asks the reviewer to supply the source
  documents or confirm the identifiers are superseded by R27–R32.

## 8. Addendum (PR #10 review, 2026-10-18)

The Phase 1 review identified that the draft contract's regex-based string
admission would itself have created a new boundary flaw (a secret-shaped
value such as `hunter2024` is pattern-valid). This does not change any
inventory finding above — no such field exists in production code — but it
corrects the *proposed* boundary: see `IMPLEMENTATION_CONTRACT.md` §3.2 for
the closed-vocabulary policy, the omission of `model`/`tool_name`/hash
attributes from v1, revised I-1/I-5, and tests A21–A23. Provenance facts used
there were verified at baseline: `GroqSettings.agent_model` accepts any
non-empty string, and tool names in `contracts/llm.py` are pattern-validated
only; no tool/model registry exists yet.
