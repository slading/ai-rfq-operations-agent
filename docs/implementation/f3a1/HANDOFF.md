# F3-A1 production implementation — Session D handoff

Status: implemented and locally verified; independent acceptance is pending.
Branch: `implement/f3a1-production`. Resolve its immutable review SHA with
`git rev-parse HEAD`; the delivery message supplies that SHA. No PR, push,
merge, or deployment was performed.

Model: GPT-6 Codex. Reasoning: high. Session D owns independent acceptance.

## Source of truth and scope

Production parent: `d5611eefab3b326e9ad5caa9789d892e69ce3875`.
Review source: `66de32c55994144c21df4a9af8b3d1edae1635ad`.
Revision 4: `50a71f17d5560a4383ffda84e21c17c31b788f77`.
Session B gate: `28af34cb5e3cdb936d087a0df5b536dc23ee40c4`.

The original local checkout was clean on `fix/f3b-sensitive-config-errors`,
`7a5f5e58aadfbcc635298b55b547b9e632b2f5dd`. Its tree matched the production
baseline tree `e81f46763bbb13cb8be48be9d06f9b6910f6e43f`. Shell Git fetch failed
with DNS resolution failure. The GitHub connector supplied the pinned Git
metadata and artifact contents. The baseline commit object was reconstructed
from that metadata and verified to hash to the exact supplied SHA; its tree
and both parents were already local. A separate worktree was created at that
commit. No Arena branch was merged or used as the implementation parent.

All 27 published review-document blobs were checked against the pinned
recursive Git tree, including all six Revision 5 artifacts. Inspection covered
the prototype, regressions, fuzz harness, 367-row coverage matrix, evidence,
Revision 5 design delta, relevant Revision 4 design, and the Session D findings
recorded there (C-8 through C-12 and F-1 through F-3). The executable verifier
pins ten relevant blob identities and the exact Session B gate checksum.

Only `src/rfq_agent/observability/redaction.py` required a production change.
The implementation uses the reviewed pipeline and vocabulary, with targeted
repairs for additional disclosures reproduced during implementation.
`email_hash`, existing public signatures, summary metadata and digest identity
are preserved. `redact_payload` and `REDACTED_KEY` are added to `__all__`.
There is no new third-party dependency.

F3-A2 is unimplemented. `config.py`, `DomainModel`, and both `ids.py` files are
byte-unchanged. Raw Pydantic `.errors()` and `.json()` remain raw; A1 sanitizes
them only when its caller explicitly uses the redactor. No logging sink was
added. At the pinned baseline, source inspection found no production callers
of these redaction functions outside their own module. This limits evidence
about deployed exposure; it does not make a reproduced disclosure harmless.

## Changes and confirmed repairs

- Strong keys consume whole containers, quoted values, decorated groups,
  inline YAML items, and detected YAML blocks. Malformed or oversized groups
  and wrapper-depth overflow fail closed.
- Credential records preserve their field names and remove the secret value
  in either order, including JSON, repr, kwargs and bare pairs. Commas between
  fields no longer turn a credential name into a false key match.
- Additional D-2 probes demonstrated that the prototype redacted a nested
  decoy instead of the root value, retained duplicate values, missed malformed
  container-valued records, and skipped some nested records in other fields.
  Production locates top-level spans, retains duplicate fields for inspection,
  removes every value field of a sensitive record, and inspects other nested
  containers. Text and structural name-field aliases now agree.
- Bounds do not silently bypass redaction: reaching record/candidate budgets
  withholds the whole input. This also prevents the cutoff moving between
  successive redaction passes. Oversized suspicious record-pair and encoded
  inputs are withheld rather than returned unchecked.
- Quote handling checks backslash parity. Container scans enforce size and
  depth bounds before decoding; ambiguous quotes from nested encoding withhold
  the remainder instead of trusting an early brace. A quoted literal beginning
  with `[REDACTED]` is replaced unless it is exactly the marker.
- The public mapping entry point uses the hostile-object and cycle guards;
  key collisions preserve all entries rather than overwriting an existing
  reserved-looking name.

These changes remain inside the existing redaction module. They do not alter
business rules, configuration classes, tracing models or persistence.

## Before / after reproduction

All values below are synthetic. The production baseline returns these inputs
unchanged, disclosing `hunter2024`.

| Input | Revision 4 output | Production output |
| --- | --- | --- |
| `credentials=<{"value": "hunter2024"}>` | `credentials=[REDACTED]: "hunter2024"}>` | `credentials=[REDACTED]` |
| `{"name": "api_key", "value": "hunter2024"}` | `{"name": "api_key", "[REDACTED]": "hunter2024"}` | `{"name": "api_key", "value": "[REDACTED]"}` |
| `{"value": "hunter2024", "name": "api_key"}` | unchanged | `{"value": "[REDACTED]", "name": "api_key"}` |

The verifier prints these comparisons from the actual baseline Git object,
pinned Revision 4 reference, and production module. Tests also exercise
`redact_mapping`, `redact_payload`, and inline/truncated summaries, checking
eight-character secret fragments and repeat application.

## Verification and the three observability failures

Environment: Python 3.11.17, macOS 26.5 arm64, pytest 9.1.1, Pydantic 2.14.1,
pydantic-settings 2.16.0, SQLAlchemy 2.1.4, Alembic 1.20.0, Ruff 0.17.0.
The existing virtual environment was reused; dependency manifests were not
changed except for Ruff settings that preserve the verbatim review tests.
CI's Python 3.13/Linux environment was not executed in this session.

| Check | Actual result |
| --- | --- |
| Unmodified production baseline, `python -m pytest -q` | **1884 passed, 3 skipped**, 40.70 s |
| Original baseline tests unchanged, final production implementation | **1881 passed, 3 failed, 3 skipped**, 43.78 s |
| Final project suite, `python -m pytest -q` | **2335 passed, 3 skipped**, 45.26 s |
| Session B gate, verbatim | **89 passed** |
| Revision 3 / 4 / 5 published regressions, directly against production | **162 / 219 / 278 passed**; **659 total** |
| Additional production security tests | **81 passed** |
| Updated observability tests | **42 passed** |
| Combined security / observability command | **490 passed**, 2.25 s |
| Original extended matrix | **101/101** |
| Published fuzz grids | **64 wrappers + 640 records**, zero leaks or idempotence failures |
| Expanded unique-secret record grid | **192 shapes**, zero leaks or idempotence failures |
| Seeded production fuzz tests | 800 complete/malformed wrappers and 288 complete/malformed record shapes through all rendering paths |
| Ruff lint / format | Passed |
| Revision 5 tests against Revision 4, separate reference diagnostic | **40 failed, 238 passed**: 22 wrapper and 18 credential-record failures |

The three historical failures are **not failures of the production baseline**.
They appear when the hardened behavior is run against the original tests:

1. The two parametrizations of
   `TestRedactText::test_prose_secrets_are_out_of_scope_for_the_pattern_rules`
   require a `gsk_` token to remain visible. The provider-prefix backstop now
   removes it, as required by the reviewed contract.
2. `TestRedactText::test_a_header_rule_redacts_to_the_end_of_the_line` requires
   an `api_key` assignment to erase the rest of its line. The reviewed contract
   bounds each assignment value and preserves trailing business identifiers;
   real Authorization headers still redact to end of line.

The two permanent test migrations are exactly Revision 4 §12: retain the
prefixless-prose limitation, add positive provider-token cases, and assert
both bounded assignment redaction and true-header redaction. No security
assertion was removed to conceal a failure; every secret remains required to
disappear in the supported cases. The gate and 278-test suite are byte-exact.
Their narrow Ruff style exemptions do not affect test execution.

The green final suite is **not equivalent to the unchanged-suite run**. Both
results and the unchanged-suite failure trace are retained. No temporary test
adaptation was used for that run: a baseline worktree retained every original
test and received only the production redaction module. The only reference
shim was used in the separately labeled, intentionally failing falsifiability
diagnostic. It never supplied evidence for production correctness.

The full suite also exercised package exports, config validation, Pydantic
errors, persistence/migrations, seeded RFQ flow, and real demo/seed CLI child
processes. The demo tests verify a persisted draft, blocking paths, retry
idempotence, and that nothing is approved or sent. These are local integration
checks. No deployed production smoke test or external logging sink check is
claimed; this session did not deploy a service.

## Performance and deviations requiring review

Five samples per variant were interleaved, then repeated in three fresh
processes. The full raw measurements are in `evidence/performance.txt`.
Evidence transcripts have trailing whitespace normalized for clean diffs.
Times below are production medians across those processes, not a comparison
with the slower production-baseline regexes.

| Case | Production | Budget |
| --- | --- | --- |
| 48 KB key-run | 1.46–1.59 ms | 200 ms |
| 1 MB prose | 18.77–19.04 ms | 500 ms |
| 1 MB summary | 10.58–10.74 ms | 1000 ms |
| 5000 container values | 36.54–36.73 ms | 200 ms |

All absolute budgets passed. Dense container input is **16–17% slower than
Revision 5** (about 31.4 ms there), because production enforces depth/size
bounds and quote handling in its scanner instead of first asking the JSON
decoder to consume an unbounded remaining input. This is a deliberate security
tradeoff and a deviation from Revision 5's “no pre-existing path >5%” criterion.
That criterion is **not claimed satisfied**. Session D should review the cost
and the decision to keep strict bounds. Ordinary text paths remain inside
budget; no claim of identical timing across hosts is made.

Record-dense text may become a single marker at its operation budget, so its
faster timing is accompanied by reduced retained context. It is not evidence
that the implementation preserves every record of a large batch.

Other deviations are the demonstrated D-1/D-2 repairs above, aligned structural
record aliases, the hostile-mapping/collision guards, and the two style-only
Ruff exclusions needed to adopt the original suites byte-for-byte. F-2's
1–16 escape-run and deeper fail-closed tests are unchanged and pass. F-3's
Pydantic ownership boundary is unchanged.

## Residual security review items

| Item | Production assessment |
| --- | --- |
| **R28** | Confirmed disclosure in Revision 5: the 33rd container-valued record survives. Production withholds the whole input at 32 changed records or 128 container candidates. Pair scans also have a 128-candidate budget. Boundary and idempotence tests pass. Context loss at the cap is intentional. |
| **R29** | **Open disclosure limitation.** Decorated keys, overlong text keys and adjacent zero-width characters can bypass free-text recognition. `redact_text('log [api_key]: hunter2024')` leaks; the same string in a mapping also leaks. Structural sensitive fields are safer because their normalization removes punctuation. A naked bracket-prefixed malformed string may be withheld by the structural-string parser, but a log prefix bypasses that accidental protection. Application-owned field names do not constrain arbitrary customer/tool text. Vocabulary expansion needs an explicit contract; this implementation preserves the pinned limitation tests. |
| **R31** | Confirmed disclosure in Revision 5: padding a malformed record beyond 256 KiB bypasses pair redaction. Production uses a linear sensitive-name probe and withholds suspicious overflow; encoded record-probe overflow also withholds. Tests cover both field orders, container values and encoding above the cap. Unknown/escaped record field names remain limited by R27. |
| **R32** | **Open semantic limitation.** `{"a":"token","b":"hunter2024"}` stays unchanged, including through mapping rendering. No supported name/value relationship binds the secret to `b`; the comma cannot safely act as a separator here. Real secrets in this shape can reach a consumer that relies only on the redactor. Structured credential records or explicit sensitive keys are required; arbitrary text is not guaranteed secret-free. |

Other carried risks remain: prefixless prose secrets, localized/unknown keys,
unquoted multiword values, bearer credentials that resemble accepted business
IDs, weak bare `key` containers, heuristic YAML detection, unknown/escaped
credential-record field spellings (R27), and literal-marker ambiguity. Raw
Pydantic errors still disclose input if callers bypass A1; F3-A2 owns that
future rendering work. `email_hash` remains an unsalted SHA-256 for API
compatibility. NFKC normalization and fail-closed withholding can lose benign
context. Passing the security gates is not a claim of universal redaction.

No deployment or new sink exposes these functions in this change. There is no
evidence here to quantify live production exploitability. R29/R32 must remain
visible before a future integration accepts untrusted free text into logs.

## Exact changed-file list

```text
pyproject.toml
src/rfq_agent/observability/redaction.py
tests/unit/test_observability.py
tests/unit/test_redaction_adversarial.py
tests/unit/test_redaction_production.py
tests/unit/test_redaction_regression.py
tools/verify_f3a1.py
docs/implementation/f3a1/HANDOFF.md
docs/implementation/f3a1/evidence/baseline.txt
docs/implementation/f3a1/evidence/unmodified-suite.txt
docs/implementation/f3a1/evidence/verification.txt
docs/implementation/f3a1/evidence/security-and-falsifiability.txt
docs/implementation/f3a1/evidence/performance.txt
docs/implementation/f3a1/evidence/sha256.json
```

## Reproduce and review independently

Use the immutable delivered SHA in a separate checkout based on the stated
production parent. Fetch the review commit separately; do not merge it. The
review-root argument expects a directory containing `docs/reviews/f3a/`.
The local verified artifact snapshot is `/Users/yauhenibuka/f3a1-review-source`;
the implementation worktree is `/Users/yauhenibuka/f3a1-production`.

```sh
git rev-parse HEAD
git log --oneline d5611eefab3b326e9ad5caa9789d892e69ce3875..HEAD
git diff --stat d5611eefab3b326e9ad5caa9789d892e69ce3875..HEAD
git diff --check d5611eefab3b326e9ad5caa9789d892e69ce3875..HEAD

F3A_PY=/Users/yauhenibuka/ai-rfq-operations-agent/.venv/bin/python
F3A_REVIEW_ROOT=/Users/yauhenibuka/f3a1-review-source
"$F3A_PY" tools/verify_f3a1.py --review-root "$F3A_REVIEW_ROOT" --benchmark --full
"$F3A_PY" tools/verify_f3a1.py --review-root "$F3A_REVIEW_ROOT" --falsifiability
"$F3A_PY" -m ruff check .
"$F3A_PY" -m ruff format --check .
```

To reproduce the **original unchanged suite** in isolation, create a detached
worktree at the production parent, copy only this commit's `redaction.py` into
the corresponding source path, then run `python -m pytest -q` there. Expect
1881 passed / the three named failures / 3 skipped. Running the baseline
worktree without the copied module instead reproduces 1884 passed / 3 skipped.

Review priorities: independently reproduce D-1/D-2 and the extra production
probes; challenge record association, malformed-value withholding, bounds and
idempotence; inspect R29/R32; assess the measured container cost; verify the
three old failures against §12 rather than treating either suite result as the
other; confirm no config/ids/business-rule changes. Review the exact SHA, not a
moving branch name. Linux/Python 3.13 CI and any future deployed sink still
need their own evidence. Freeze after review except for demonstrated failures.
