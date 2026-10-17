# F3-A2 Follow-Up Contract — ValidationError Rendering

**Project 04 / Session A — read-only design. Separate from F3-A1.**
Repository `slading/ai-rfq-operations-agent`, baseline `d5611eefab3b326e9ad5caa9789d892e69ce3875`.
Parent design: `f3a1-design-revision-r4.md` §10.

| Gate | Status |
| --- | --- |
| Application code modified | **NO** |
| Tests added to the repository | **NO** |
| Commits created | **NO** |
| PR created | **NO** |
| Implementation authorized | **NO** |

**This document corrects revision 3's A2 section. It is design only; none of it
is implemented by F3-A1 and none of it is part of the 89-test gate or the
219-test regression suite.**

---

## 1. What revision 3 got wrong (F-3)

| Claim in revision 3 | Fact |
| --- | --- |
| `Settings` derives from `_ConfiguredModel` | `class Settings(BaseSettings)` — `config.py:248` |
| `_ConfiguredModel` already has `hide_input_in_errors` | It does not: `ConfigDict(extra="forbid", frozen=True)` — `config.py:94` |
| `Settings` leaks its input in `str(e)` | It does **not**: `SettingsConfigDict(..., hide_input_in_errors=True)` — `config.py:263` |
| Eight section models inherit `_ConfiguredModel` | True: `RetryPolicy`, `GroqSettings`, `AgentSettings`, `TrustSettings`, `WorkflowSettings`, `BusinessSettings`, `HttpSettings`, `DatabaseSettings` |
| `DomainModel` is in scope | It is not: `DomainModel(BaseModel)` in `domain/values.py` is unrelated to configuration |
| Runtime mutation verifies the fix | Invalid: pydantic builds its core schema at class creation; mutating `model_config` afterwards does not rebuild it |

---

## 2. The exact change

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

That is the whole production change for the `str()`/`repr()` surface. It
covers all eight section models by inheritance.

### Do not change

* **`Settings`** — it already sets the flag in `SettingsConfigDict` and has
  different semantics (`env_prefix`, `env_nested_delimiter`, `extra="ignore"`).
  Leave it exactly as it is.
* **`DomainModel`** — domain schemas are not configuration and keep their
  current behaviour. Changing both bases would widen the blast radius for no
  security gain.
* **Field validators** — no message text may interpolate the offending value.
  `hide_input_in_errors` does not cover messages; the existing `_coerce_bool`
  comment already documents this.

---

## 3. Rendering policy

| Surface | After A2 | Rule |
| --- | --- | --- |
| `str(e)` / `repr(e)` | input-free | may be logged directly |
| `e.errors()["input"]` | **raw** | programmatic use only; never logged, never returned |
| `e.json()` | **raw** | never logged; render through the wrapper |
| `safe_validation_report(e)` | redacted | the only approved structured rendering |

`e.errors()` keeps the raw input **by design**: retry logic and field routing
need the value; the exception object is not a rendering boundary.

### Wrapper (A2 scope, not A1)

```python
# src/rfq_agent/observability/redaction.py — added by A2
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

`ValidationError` is imported from `pydantic`; `redaction.py` already imports
pydantic through `rfq_agent.domain.values`, so A2 adds no dependency.

---

## 4. Verification rules

1. **No runtime mutation.** `Model.model_config["hide_input_in_errors"] = True`
   is forbidden in source and tests. A test that mutates the dict proves
   nothing about the shipped class and must be rejected in review.
2. **Import-time assertions** on `_ConfiguredModel.model_config` and on all
   eight subclasses.
3. **Behavioural tests** construct each section model with a secret where an
   `int` is required and assert no ≥ 8-character fragment in `str(e)`.
4. **Raw-surface tests** assert that `e.errors()` still carries `input` and
   that the safe rendering is clean — the leak is pinned, not hidden.
5. **Source assertion** that no `logger.*` call receives `e.errors()` or
   `e.json()` in `config.py`, `persistence/engine.py` or `demo/`.
6. **Class-definition probe** (design evidence only): a subclass declared with
   `model_config = ConfigDict(**{**_ConfiguredModel.model_config,
   "hide_input_in_errors": True})` reproduces the post-patch class. It is a
   prediction, not an acceptance test.

---

## 5. Acceptance criteria

| ID | Criterion | Method |
| --- | --- | --- |
| AC-A2-1 | `_ConfiguredModel.model_config["hide_input_in_errors"] is True` | import-time assertion |
| AC-A2-2 | All eight section models inherit it | parametrised assertion |
| AC-A2-3 | `str(ValidationError)` from each of the eight models has no intact ≥ 8-char fragment of any input (3 secret shapes) | 24 constructions |
| AC-A2-4 | Diagnostics survive: field name, error type, message | assert `"busy_timeout_ms"`, `"int_parsing"` in `str(e)` |
| AC-A2-5 | `e.errors()` remains structured and still carries `input` (raw) | `errors()[0]["input"] == secret` |
| AC-A2-6 | `safe_validation_report(e)` and `json.loads(e.json())` through it are clean | 3 × 8 matrix |
| AC-A2-7 | `Settings` is unchanged: `str(e)` already clean; `env_prefix`/`frozen`/`extra` intact | existing config tests + one assertion |
| AC-A2-8 | `DomainModel.model_config` has no `hide_input_in_errors` | source assertion |
| AC-A2-9 | No runtime mutation of `model_config` in `src/` or `tests/` | grep assertion |
| AC-A2-10 | `Settings` production-invariant failures still print a usable message | existing `TestDisclosureFixKeepsDiagnostics` |
| AC-A2-11 | Demo CLI and alembic subprocess contracts unchanged (exit code 2, message shape) | existing F3-B subprocess tests |
| AC-A2-12 | Full suite green with A1 + A2 | `pytest -m "not live"` |

---

## 6. Evidence gathered this session (read-only)

| Model | Base | `str(e)` today | `.errors()` | `.json()` | `str(e)` with a class-level flag |
| --- | --- | --- | --- | --- | --- |
| `RetryPolicy` … `HttpSettings` (7) | `_ConfiguredModel` | leaks | leaks | leaks | clean |
| `DatabaseSettings` | `_ConfiguredModel` | leaks | leaks | leaks | clean |
| `GroqSettings` | `_ConfiguredModel` | leaks | leaks | leaks | clean |
| `Settings` | `BaseSettings` | **already clean** | leaks | leaks | unchanged |
| `DomainModel` subclass | `BaseModel` | leaks | leaks | leaks | clean only if its own config changes (out of scope) |

Measured with `hunter2024` in an integer field and a class-definition subclass of
the real model; `.errors()` and `.json()` stayed raw in every row, which is why
AC-A2-6 exists.

---

## 7. Boundary

* A2 is a separate PR; it is **not** merged before A1 and is **not** part of
  the F3-A1 acceptance gate.
* No logging, workflow, schema, migration or persistence change. The
  `create_engine(..., hide_parameters=True)` item from revision 2's A2 list
  is **not** part of this contract: A2 is the Pydantic change and the
  renderer only. If the owner wants the SQLAlchemy hardening, it is a
  separate, separately-authorized change.
* No `SecretStr` change, no `redact_logs` change, no demo CLI contract change.
* No claim of ACCEPT is made for A2 by this document.

**Status: application code modified NO · tests added NO · commits created NO ·
PR created NO · implementation authorized NO.**
