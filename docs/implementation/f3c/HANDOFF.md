# F3-C — Safe Observability Boundary (Phase 2 Implementation Handoff)

Status: **implemented for independent Codex security review; do not merge or deploy.**
Implementation branch: `arena/fc8a8c42-ai-rfq-operations-agent`, branched from `main` at merge commit `c3509d29cfa2cc7c32952ba6c3ddb188c19ff6e0` (which merged PR #10, Phase 1 contract commit `dc318d10895bde59f8aec6a55de06621ffacc4d5`, on 2026-10-18T12:40:43Z). Resolve the immutable review SHA with `git rev-parse HEAD`.

## 1. Precondition and scope verification

| Item | Verified value |
| --- | --- |
| PR #10 state | `MERGED` into `main` (`2026-10-18T12:40:43Z`) |
| PR #10 head commit SHA | `dc318d10895bde59f8aec6a55de06621ffacc4d5` |
| `main` merge commit SHA (baseline) | `c3509d29cfa2cc7c32952ba6c3ddb188c19ff6e0` |
| Implementation branch | `arena/fc8a8c42-ai-rfq-operations-agent` |
| Behavior change vs. baseline | **Additive-only** (no production caller wired; existing modules byte-unchanged) |

### Exact changed-file list

```text
docs/implementation/f3c/HANDOFF.md
src/rfq_agent/observability/boundary.py
tests/unit/test_observability_boundary.py
tests/unit/test_package_surface.py
```

`src/rfq_agent/observability/redaction.py` (F3-A1), `src/rfq_agent/observability/validation_errors.py` (F3-A2), `src/rfq_agent/observability/ids.py`, `src/rfq_agent/observability/spans.py`, `src/rfq_agent/config.py` (F3-B), all `domain/*`, `contracts/*`, `persistence/*`, `demo/*`, `seed/*`, migrations, and `pyproject.toml` are untouched.

## 2. Implemented contract and security properties

- **Closed schema and three-event vocabulary (`I-1`, `§3.0`, `§3.1`, `§3.4`):** `DIAGNOSTIC_SCHEMA = "rfq.diagnostic/v1"`, `MAX_DIAGNOSTIC_BYTES = 1024`, and fixed 11-key output payload (`schema`, `event`, `status`, `error_code`, `trace_id`, `rfq_id`, `run_id`, `span_id`, `validation`, `attributes`, `attributes_withheld`). `DiagnosticEvent` admits only `validation_failed`, `operation_failed`, and `diagnostic_unavailable`.
- **Exact-type and singleton-identity enum admission (`I-1`, `I-3`, `§3.2.1`):** `DiagnosticEvent`, `DiagnosticErrorCode` (16 closed values), `DiagnosticPurpose` (5 closed values), and `DiagnosticResultStatus` (4 closed values) are validated by `type(candidate) is EnumCls` followed by singleton identity (`candidate is member`) against module-owned literal tables. Plain strings equal to vocabulary values (`"TIMEOUT"`, `"triage"`, `"OK"`), `StrEnum` lookalikes (`ModelPurpose`, `ToolResultStatus`), and `str.__new__(EnumCls, ...)` forged enum instances are rejected without invoking `__eq__`, `__hash__`, `__class__`, `str()`, or `repr()`.
- **Strict attribute admission (`I-1`, `I-5`, `I-6`, `§3.2.3`):** Attributes are honoured only on `operation_failed`; on `validation_failed` all supplied attributes are dropped and counted in `attributes_withheld` (capped at 16). Only the 23 allowlisted keys (2 enum, 16 `int` in `0..2**31 - 1` with `bool` excluded via `type(v) is int`, 5 `bool` with `int` excluded via `type(v) is bool`) are admitted. Dynamic identifiers omitted from v1 (`model`, `tool_name`, `stage`, `status_code`, `outcome`, `reason_code`, `finish_reason`, `*_sha256`) do not exist in the allowlist and are dropped and counted.
- **No representation or hostile attribute reads (`I-2`, `I-3`, `I-10`):** `src/rfq_agent/observability/boundary.py` contains zero calls to `str()`, `repr()`, `format()`, `ascii()`, `getattr()`, or `isinstance()`, zero f-strings (`ast.JoinedStr`), and zero accesses to `.errors()`, `.json()`, `.model_dump()`, `.args`, or `__dict__` (enforced by AST inspection in `test_a19_import_surface_and_ast_no_representation_proof`).
- **F3-A2-only validation rendering (`I-4`, `§4`):** `validation_failed` consumes `validation_error` exclusively through `render_validation_error(validation_error)` and verifies the returned 7-key `rfq.validation-diagnostic/v1` `SAFE` dictionary before copying its scalar fields into a fresh dict. Missing, non-`ValidationError`, or 0-error inputs degrade the whole event to the constant `diagnostic_unavailable` fallback. Cross-event arguments (`error_code` on `validation_failed`, or `validation_error` on `operation_failed`) also fail closed to the constant fallback.
- **Trace handling (`I-3`, `§3.3`):** `trace` is checked with `type(trace) is TraceContext`, its fields are read inside an exception handler and re-validated against `_TRACE_ID_RE` (`^[0-9a-f]{32}$`), `_IDENTIFIER_RE` (`^[A-Za-z][A-Za-z0-9_\-]{2,63}$`), and `_SPAN_ID_RE` (`^[0-9a-f]{16}$`) (including `parent_span_id`, which is verified if present and never emitted). Any invalid, uninitialized, or `model_construct`-tampered `TraceContext` degrades all four trace IDs to `None`.
- **Bounded work, no truncation, and strict serialisation (`I-6`, `I-7`, `I-8`, `§5`):** Attribute mappings check `len(attributes) <= 64` before calling `.items()`, enforce `seen == raw_len` during iteration to defeat mappings lying about `len`, and fail closed to `_unavailable_event()` if a 17th valid allowlisted attribute is supplied (never truncating). Every candidate payload is verified with `json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True)` (no `default=`) and checked against `MAX_DIAGNOSTIC_BYTES` (1024 bytes) before return.
- **Full-structure validation in `summarize_diagnostic_event()` (Additional Mandatory Security Requirement, `I-1`, `I-2`, `I-5`, `I-8`, `A13`, `A21`, `A23`):** `summarize_diagnostic_event()` rejects foreign or tampered payloads by validating the entire structure before returning a fixed summary string from `_SUMMARY_BY_EVENT_AND_CODE`:
  1. Requires `type(event_payload) is dict` (rejecting non-dicts and `dict` subclasses) and verifies all 11 keys have `type(k) is str` before key lookup (preventing colliding non-`str` keys from executing `__eq__`).
  2. Validates `schema == "rfq.diagnostic/v1"`, `event` in `{"validation_failed", "operation_failed"}`, `status == "SAFE"`, and `attributes_withheld` (`type is int`, `0 <= attributes_withheld <= 16`).
  3. Validates `trace_id`, `rfq_id`, `run_id`, and `span_id` against their exact domain patterns and enforces the structural invariant that `rfq_id`, `run_id`, and `span_id` must all be `None` whenever `trace_id` is `None`.
  4. Validates event-specific invariants: for `validation_failed`, `error_code == "VALIDATION_ERROR"`, `attributes == {}`, and `validation` satisfies the full 7-key F3-A2 `SAFE` schema (including `1 <= error_count <= 32` and `error_count == 32` whenever `error_count_truncated is True`); for `operation_failed`, `error_code` is a plain `str` in the 16-code vocabulary, `validation is None`, and `attributes` is a plain `dict` of `<= 16` allowlisted plain-`str` keys whose values satisfy exact type, range, and closed-vocabulary membership.
  5. Verifies `json.dumps` serialisability without `default=` and encoded length `<= MAX_DIAGNOSTIC_BYTES`. Any mismatch returns the constant `"Diagnostic unavailable."`.

## 3. Adversarial matrix and invariant coverage (`tests/unit/test_observability_boundary.py`)

| Case | Test function | Invariants verified | Result |
| --- | --- | --- | --- |
| Happy path (validation, operation, attributes, fallback, type hints) | `test_happy_path_*`, `test_runtime_type_annotations_resolve` (21 parametrized cases) | `§3.0`–`§3.3`, `I-1`, `I-4`, `I-7` | **21 passed** |
| **A01** | `test_a01_nested_settings_validation_error_with_secrets` | `I-4` | **Passed** |
| **A02** | `test_a02_hostile_ctx_object_in_validation_error_not_triggered` | `I-2`, `I-4` | **Passed** |
| **A03** | `test_a03_non_validation_error_and_hostile_class_hooks` (5 parametrized cases) | `I-3`, `I-8` | **5 passed** |
| **A04** | `test_a04_event_rejects_strings_ints_hostile_objects_and_lookalikes` | `I-1`, `I-3` | **Passed** |
| **A05** | `test_a05_free_text_smuggling_and_omitted_v1_keys_withheld` | `I-1`, `I-5` | **Passed** |
| **A06** | `test_a06_r29_shapes_as_attribute_keys_and_values_never_emitted` | `I-5` | **Passed** |
| **A07** | `test_a07_r32_semantic_shapes_and_nested_containers_withheld` | `I-1`, `I-5` | **Passed** |
| **A08** | `test_a08_hostile_attribute_values_and_mappings_fail_closed_without_repr` | `I-2`, `I-3`, `I-8` | **Passed** |
| **A09** | `test_a09_scalar_edges_rejected_by_exact_type_and_range` | `I-1`, `I-6` | **Passed** |
| **A10** | `test_a10_string_shape_edges_and_injection_strings_rejected` | `I-1`, `T-10` | **Passed** |
| **A11** | `test_a11_volume_bounds_lying_mappings_17th_attribute_and_byte_ceiling` | `I-6`, `I-8` | **Passed** |
| **A12** | `test_a12_trace_stand_ins_hostile_reads_and_valid_trace_context` | `I-3` | **Passed** |
| **A13** | `test_a13_summarize_diagnostic_event_rejects_foreign_and_tampered_payloads` | Fixed-text only, full-structure validation | **Passed** |
| **A14** | `test_a14_mutation_isolation_and_determinism` | `I-9` | **Passed** |
| **A15** | `test_a15_strict_json_serialisation_and_size_bounds` | `I-6`, `I-7` | **Passed** |
| **A16** | `test_a16_composition_regression_parse_failure_and_exc_errors_archetype` | `I-1`, `I-2` | **Passed** |
| **A17** | `test_a17_settings_shaped_mapping_and_secret_str_objects_withheld` | `I-1`, `I-5` | **Passed** |
| **A18** | `test_a18_idempotence_and_stability_across_f3a2_error_caps` (8 parametrized cases) | `I-4` | **8 passed** |
| **A19** | `test_a19_import_surface_and_ast_no_representation_proof` | `I-2`, `I-10` | **Passed** |
| **A20** | `test_a20_pathological_mapping_refused_without_iteration` | `I-6` | **Passed** |
| **A21** | `test_a21_no_free_text_and_no_representation_across_all_inputs` (incl. `hunter2024`, `secret123`, `token2024x`, `a1b2c3d4`, `pass.word-2024`) | `I-1`, `I-2`, `I-5`, `I-8` | **Passed** |
| **A22** | `test_a22_vocabulary_and_schema_stability_snapshot` | `§3.2`, `§3.4` | **Passed** |
| **A23** | `test_a23_fallback_and_summary_non_reflection_across_all_triggers` | `I-5`, `I-8` | **Passed** |

## 4. Verification evidence (executed runs)

Environment: Linux x86_64, Python 3.11.2, pytest 9.1.1, Pydantic 2.14.1, pydantic-settings 2.16.0, SQLAlchemy 2.1.5, Alembic 1.20.0, Ruff 0.17.1. All credentials in tests are synthetic.

| Check | Command | Executed result |
| --- | --- | --- |
| F3-C boundary suite (A01–A23 + happy path) | `.venv/bin/python -m pytest -v tests/unit/test_observability_boundary.py` | **55 passed** in 0.45 s (exit 0) |
| Combined observability, package-surface & security regressions (F3-C + F3-A2 + F3-B + F3-A1) | `.venv/bin/python -m pytest -q tests/unit/test_observability_boundary.py tests/unit/test_validation_errors.py tests/unit/test_validation_errors_review.py tests/unit/test_config.py tests/unit/test_observability.py tests/unit/test_package_surface.py tests/unit/test_redaction_adversarial.py tests/unit/test_redaction_production.py tests/unit/test_redaction_regression.py` | **729 passed, 3 skipped** in 5.20 s (exit 0) |
| Full offline suite (`pytest -q`) | `.venv/bin/python -m pytest -q` | **2424 passed, 3 skipped** in 82.25 s (exit 0) |
| Full offline suite (`pytest -m "not live" -q`) | `.venv/bin/python -m pytest -m "not live" -q` | **2424 passed, 3 skipped** in 81.85 s (exit 0) |
| Ruff lint | `.venv/bin/ruff check .` | `All checks passed!` (exit 0) |
| Ruff format (`src`, `tests`, `HANDOFF.md`) | `.venv/bin/ruff format --check src tests docs/implementation/f3c/HANDOFF.md` | `128 files already formatted` (exit 0) |
| Ruff format (repo-wide `.`) | `.venv/bin/ruff format --check .` | `1 file would be reformatted, 138 files already formatted` (exit 1; Ruff 0.17.1 with `docstring-code-format = true` flags the two fenced Python blocks in `docs/implementation/f3c/IMPLEMENTATION_CONTRACT.md` from PR #10, which was left untouched to preserve the exact 4-file authorized diff scope) |
| Git whitespace / conflict check | `git diff --check c3509d29cfa2cc7c32952ba6c3ddb188c19ff6e0..HEAD` | Clean (exit 0, no output) |

The three skips in `test_package_surface.py` are the pre-existing cases for `rfq_agent.contracts`, `rfq_agent.domain`, and `rfq_agent.observability` (which declare no `__all__`). No new skip was introduced.

## 5. Residual risks (restated from contract §10)

1. **R29 and R32 remain open outside the boundary:** F3-C does not modify `observability/redaction.py` or globally resolve R29 (decorated/overlong/zero-width-adjacent keys in free text) or R32 (semantic secret pairs such as `{"a": "token", "b": "hunter2024"}`). They are contained inside F3-C output only because the boundary admits no free-text field.
2. **C-1 remains live in the demo/seed CLIs:** `demo/__main__.py` and `seed/__main__.py` still interpolate raw exceptions until the separate required patch **SEC-PATCH-CLI-01** lands (blocking gate before any sink integration, non-fixture CLI use, or operator surface reuse).
3. **C-3 (`canonical_json(default=str)`) remains open as SEC-REVIEW-JSON-01:** `domain/values.py::canonical_json` still passes `default=str`; F3-C avoids passing untrusted data to it and validates its own output via `json.dumps` without `default=` (`I-7`).
4. **In-process bypass remains possible until sink enforcement (Q7):** Future code could still call `print` or `logging` directly until the first sink phase implements the repo-wide enforcement scan defined in `IMPLEMENTATION_CONTRACT.md` §9.1.
5. **Upstream Pydantic construction formatting:** Pydantic may format a raised `ValueError` while constructing a `ValidationError` before the boundary receives it; F3-C contains the result (never emitting it) but cannot prevent upstream construction behavior.
6. **Unsalted hashes (T-12):** Existing SHA-256 correlation helpers remain unsalted; F3-C emits no hash fields in v1.
7. **Aggregate-only validation diagnostics and existence metadata:** Per-field locations and error types remain withheld (`locations: null`, `locations_status: "WITHHELD"`), and bounded counts/event codes still reveal that a failure occurred.
8. **Vocabulary-choice covert channel (`I-1`/`I-5` bound):** Closed vocabularies prevent caller-chosen string content, not caller-chosen enum/int/bool selection by a hostile in-process caller.
9. **Dynamic identifiers omitted in v1:** `model`, `tool_name`, `stage`, and `*_sha256` digests are absent from v1 attributes until their provenance-owning registry phases and a `v2` contract amendment exist.

## 6. Independent review reproduction

```sh
git rev-parse HEAD
git diff --stat c3509d29cfa2cc7c32952ba6c3ddb188c19ff6e0..HEAD
git diff --check c3509d29cfa2cc7c32952ba6c3ddb188c19ff6e0..HEAD

.venv/bin/python -m pytest -v tests/unit/test_observability_boundary.py
.venv/bin/python -m pytest -q \
  tests/unit/test_observability_boundary.py \
  tests/unit/test_validation_errors.py \
  tests/unit/test_validation_errors_review.py \
  tests/unit/test_config.py \
  tests/unit/test_observability.py \
  tests/unit/test_package_surface.py \
  tests/unit/test_redaction_adversarial.py \
  tests/unit/test_redaction_production.py \
  tests/unit/test_redaction_regression.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests docs/implementation/f3c/HANDOFF.md
```

**Stop for independent Codex security review.** Do not merge or deploy.
