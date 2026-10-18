# F3-A2 — Safe Validation Error Rendering

Status: implemented for independent review; **do not merge**. The implementation is on the Arena session branch `arena/724ad7f8-ai-rfq-operations-agent`, based on `e91514c021bffc502d2671c87e3fe8b9447a58ca`. Resolve the committed handoff SHA with `git rev-parse HEAD`. No push, pull request, merge, deployment, or live provider call was performed.

## Scope decision and Pydantic API constraint

Implementation followed the user's authorization for an **aggregate-only** renderer. During inspection with Pydantic 2.14.1, `ValidationError.errors(include_input=False, include_context=False, include_url=False)` still formats each `msg`. A synthetic custom error context demonstrated that formatting invokes the context object's `__str__`, even though context is not included in the returned mapping. The public `ValidationError` API exposes no message-free per-error type/location accessor.

To preserve the strict rendering boundary, the supported API reads only the native `ValidationError.error_count()` method. It does not call `str(error)`, `repr(error)`, `.errors()`, `.json()`, or traverse inputs, locations, contexts, URLs, or exception objects. This means **per-error codes and field locations are intentionally unavailable**. The structured result uses the one controlled code `VALIDATION_ERROR`, a count capped at 32, and `locations: null` with `locations_status: "WITHHELD"`. The human summary is separately generated from fixed text and the bounded count. A malformed or non-ValidationError value fails closed to an `UNAVAILABLE` result.

This is an explicit functional reduction from the initial proposal: it provides a safe aggregate diagnostic, not field-level diagnostics. A future Pydantic message-free metadata API or a separately authorized safe diagnostic carrier would be needed to restore field-level detail without weakening the no-untrusted-representation rule.

## Implementation and security properties

- `src/rfq_agent/observability/validation_errors.py` is a standalone module; F3-A1 redaction behavior is untouched.
- Output has a fixed schema and fixed keys. Its size is bounded independently of input size or error count; counts above 32 are represented as `32` with `error_count_truncated: true`.
- No user-controlled string, error type, message, field location, input, context, URL, or exception representation is copied into output.
- `summarize_validation_error()` is a distinct string API; it does not stringify or serialize the exception.
- There are no logging, telemetry, UI, HTTP, provider, or persistence side effects. No production caller or sink was added.
- Synthetic tests cover nested `Settings` errors containing API keys, operator tokens, database URLs/passwords, short and long secrets, a custom error message and hostile exception context, a malicious location, an unknown custom error type, a 1 MB-plus input, a 128-level nested input/location, and an 80-error case capped at 32.
- The hostile-context test clears the `__str__` counter after Pydantic constructs the exception and confirms the renderer itself does not trigger another conversion. Pydantic may itself stringify a raised `ValueError` while constructing a `ValidationError`; this renderer cannot prevent that upstream behavior, but it does not expose its result.

## Existing guarantees and limitations

- F3-B configuration validation code and tests were not changed. F3-A1 code and redaction semantics were not changed.
- Raw Pydantic `.errors()`, `.json()`, `str(error)`, and exception messages remain potentially unsafe when accessed directly. `hide_input_in_errors` is not a substitute for this API.
- R29 and R32 remain documented F3-A1 limitations; this work neither changes nor claims to fix them. This API is not universal secret detection for arbitrary free text.
- No logging sink currently uses the F3-A1 redactor, and this change adds no sink. Supported-renderer results do not establish safety for callers that bypass it.
- The renderer withholds all field names and error types. Operators receive only the generic validation code and a bounded count.
- Verification ran locally on Python 3.11.2, Pydantic 2.14.1, pydantic-settings 2.16.0, and Ruff 0.17.1. The repository CI uses Python 3.13; that environment was not run here.

## Verification evidence

All credentials used by the added tests are synthetic. No test migration or skip was added.

| Check | Result |
| --- | --- |
| F3-A2 + F3-B + F3-A1 security regressions | **491 passed**, 5.28 s |
| Full offline suite (`python -m pytest -q`) | **2346 passed, 3 skipped**, 91.02 s |
| Ruff lint | Passed: `All checks passed!` |
| Ruff format | Passed: `131 files already formatted` |

The three skips are existing `test_package_surface.py` cases for `rfq_agent.contracts`, `rfq_agent.domain`, and `rfq_agent.observability`, which do not declare `__all__`. No new skips were introduced. Reproducible commands and concise results are recorded in `evidence/security-tests.txt`, `evidence/full-suite.txt`, and `evidence/lint.txt`.

## Exact changed-file list

```text
docs/implementation/f3a2/HANDOFF.md
docs/implementation/f3a2/evidence/full-suite.txt
docs/implementation/f3a2/evidence/lint.txt
docs/implementation/f3a2/evidence/security-tests.txt
src/rfq_agent/observability/validation_errors.py
tests/unit/test_package_surface.py
tests/unit/test_validation_errors.py
```

## Independent review instructions

Review the immutable commit reported by `git rev-parse HEAD`; do not rely only on the moving branch name. Confirm that the diff is limited to the seven files listed above and that `config.py`, the F3-A1 redactor, both `ids.py` modules, business logic, dependencies, schemas, and migrations are unchanged.

Reproduce offline from the repository root:

```sh
python -m pytest -q tests/unit/test_validation_errors.py \
  tests/unit/test_config.py \
  tests/unit/test_redaction_adversarial.py \
  tests/unit/test_redaction_production.py \
  tests/unit/test_redaction_regression.py
python -m pytest -q
ruff check .
ruff format --check .
```

Review priorities:

1. Confirm through a hostile custom error context that repeated rendering does not call its `__str__`/`__repr__`, `.errors()`, `.json()`, or `str(error)`.
2. Challenge the count-only design against the stated security requirement; verify output keys, count cap, generic fallback, fixed summary, JSON compatibility, and deterministic repetition.
3. Verify synthetic secret fragments, messages, locations, and large/deep input remain absent from both supported outputs.
4. Confirm the aggregate-only deviation is acceptable for this phase. Per-field error codes/locations are intentionally not claimed.
5. Re-run the F3-B and F3-A1 tests and inspect the residual limitations above. Do not treat direct raw Pydantic APIs or arbitrary free text as protected.

**Stop before merge.** No deployment, sink integration, per-field metadata access, or scope expansion is authorized by this handoff.
