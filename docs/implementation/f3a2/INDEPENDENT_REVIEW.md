# F3-A2 independent security review and remediation

Review date: 2026-10-18. Initial target:
`8100fd664a0e9e0ad4ee8952de1cd348e40bc959` on
`arena/724ad7f8-ai-rfq-operations-agent`. Remediation branch:
`codex/f3a2-security-remediation`. Do not merge or deploy without release authorization.

## Findings

### Medium: hostile class lookup escapes both supported renderers (fixed)

The original `isinstance(error, ValidationError)` check was outside the exception
handler. Python can read a non-ValidationError object's `__class__` property during
this check. A property raising `RuntimeError("SYNTHETIC_CLASS_SECRET")` escaped both
public APIs instead of returning the fixed unavailable diagnostic. A property
spoofing `ValidationError` was also invoked, violating the no-inspection boundary.
An upstream handler could subsequently expose the escaping exception text. This
is an in-process hostile-object path; no production renderer caller or remote
exploit was demonstrated.

Removed the preliminary check. The native `ValidationError.error_count(error)`
descriptor performs the actual type check inside the existing exception handler
without reading instance attributes. No fallback exception text is formatted.
Real ValidationError subclasses remain supported through the native descriptor.

### Low: short-secret test assertion was vacuous (fixed)

The synthetic short secret has seven characters, while the shared fragment helper
starts at eight. That test therefore checked no substring for the short secret.
Added an explicit whole-secret absence assertion before the existing fragment
checks. This finding concerns test coverage; it did not demonstrate a renderer
disclosure of the short secret.

No remaining critical, high or medium finding was identified within this scope.

## Contract and boundary review

- Source inspection confirms the renderer accesses only the native
  `ValidationError.error_count()` API. It never calls `errors()`, `json()`,
  `str(error)` or `repr(error)`, and never traverses messages, contexts, titles,
  locations, inputs or custom error types.
- The fixed seven-key schema uses `VALIDATION_ERROR`; locations are always null
  and marked `WITHHELD`. Positive counts are capped at 32, with truncation true
  only above 32. Non-positive/non-native-integer counts and native-call failures
  return the constant `UNAVAILABLE` shape.
- Both human summaries and structured results contain only controlled text and
  a bounded count. Repetition is deterministic, results do not share mutable
  dictionaries, and combined JSON/summary output stays below 512 characters in
  the exercised boundary cases.
- Module-level `__all__` exports exactly the two public functions. The package
  surface tests cover the module. Runtime annotations resolve and retain the
  existing ValidationError input and JSON-compatible scalar dictionary return
  contracts. No new parent-package re-export is required by the existing layout.
- The implementation is aggregate-only, as described in the handoff and the
  current review objective. It does not claim per-field diagnostics.
- The initial change against parent `e91514c021bffc502d2671c87e3fe8b9447a58ca`
  contains exactly the seven files listed in `HANDOFF.md`. Remediation changes
  only the renderer, its tests, and this review. F3-B configuration, F3-A1
  redaction, both ID modules, business rules, dependencies, logging sinks,
  database definitions and deployment configuration are unchanged by F3-A2.

## Independent verification

Git shell networking was unavailable. Source was obtained through the GitHub
connector, with every missing blob and tree checked against its Git object SHA.
The reconstructed initial commit hashes to the exact requested SHA; its tree is
`a588734af1f2564c0a19404bf96e38471460ab6a`. The remote branch was independently
confirmed at that commit.

Original commit, independently run on Python 3.11.17 / Pydantic 2.14.0 /
pydantic-settings 2.15.0 / pytest 9.1.1: **491 security cases passed**;
**2346 passed, 3 skipped** in the full suite (42.17 s); Ruff lint and format
passed (131 files). No Arena test result was used as proof.

The 21 independent adversarial cases were also run against the original code
with Pydantic 2.14.1: **2 failed, 19 passed**, reproducing both hostile class-hook
cases. After remediation, the same cases passed. They additionally challenge
subclass attribute/representation/method overrides, native-call exception
handling, malformed counts, 0/1/2/31/32/33/80/1024 error boundaries, output size,
JSON compatibility and mutation isolation.

Remediation candidate verification on Python 3.11.17 / Pydantic 2.14.1 /
pydantic-settings 2.16.0 / pytest 9.1.1 / Ruff 0.17.0:

| Check | Independent result |
| --- | --- |
| F3-A2 + independent cases + F3-B + F3-A1 | 512 passed (4.55 s) |
| Full offline suite | 2367 passed, 3 skipped (43.70 s) |
| Ruff lint | All checks passed |
| Ruff format | 132 files already formatted |

The three skips are unchanged package-surface cases for `contracts`, `domain`
and `observability`, which do not declare `__all__`. No skip was added.
After committing, repeat the following against the final immutable SHA and
record the SHA and results in the delivery evidence:

```sh
python -m pytest -q tests/unit/test_validation_errors.py \
  tests/unit/test_validation_errors_review.py tests/unit/test_config.py \
  tests/unit/test_redaction_adversarial.py \
  tests/unit/test_redaction_production.py tests/unit/test_redaction_regression.py
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

## Residual risks and release boundary

This API has no production caller or logging sink integration. Raw Pydantic
rendering remains potentially unsafe, and upstream construction may itself
format a raised exception before this renderer receives it. F3-A1 R29 and R32
remain open documented limitations; arbitrary free text is not universally
secret-free. Aggregate diagnostics withhold field names and individual types.
The broad declared dependency range is not exhaustively tested; local QA covers
Python 3.11, not the repository's Python 3.13 CI environment. No deployment or
production smoke test is part of this review. Merge and deployment require
separate explicit release authorization.

## Remediation changed files

```text
docs/implementation/f3a2/INDEPENDENT_REVIEW.md
src/rfq_agent/observability/validation_errors.py
tests/unit/test_validation_errors.py
tests/unit/test_validation_errors_review.py
```
