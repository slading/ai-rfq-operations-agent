# Fresh RFQ acceptance — runnable demonstration

Three declarations, one command, one migrated and seeded database. Each JSON
file states a complete fresh RFQ: the untrusted source text, the sender, the
arrival instant, the identifiers only a system may hold, the instant the
deterministic questions are asked at, and the extraction *claims* an LLM would
have produced. Nothing in these files is a business fact.

```bash
make migrate && make seed
python -m rfq_agent.acceptance --input docs/acceptance/case-a-clean.json
```

Use `--url` to point the command at a scratch database. Without it, the URL is
resolved exactly like `alembic` and the demo: `RFQ_DATABASE__URL`, then `.env`,
then `sqlite:///var/rfq_agent.db`.

## Cases

| File | What it states | Outcome | Exit |
| --- | --- | --- | --- |
| `case-a-clean.json` | 25 × `100-ABC` from `einkauf@nordwind-industrie.de` | `DRAFT_AWAITING_HUMAN_REVIEW` | 0 |
| `case-b-stock.json` | 200 × `100-ABC` — more than the 145 available | `DRAFT_BLOCKED` | 0 |
| `case-c-tampered.json` | source says 25, the claim asserts 26 | `HUMAN_REQUIRED_BEFORE_QUOTE` | 3 |

Case D is a replay of Case A: run the same file twice and the second run reports
`DUPLICATE` with exit 0 and writes nothing.

### Case A — clean RFQ

```
=== fresh RFQ RFQ_ACC_0001
intake    : run RUN_ACC_0001 - rows written
customer  : EXACT
line 1    : RESOLVED
quote     : Q-2026-8101 (QTE_ACC_0001) - status DRAFT
money     : 25 x PMP-A-100 @ 1150.0000 EUR = 28750.00
            subtotal 28750.00 EUR - discount 0.00 = total 28750.00 EUR
gate      : eligible for human review: YES / awaiting human approval: YES (printed, never stored)
blocked   : none
persisted : stored quote QTE_ACC_0001: 1 line(s), 0 blocked reason(s), one transaction
outcome   : DRAFT_AWAITING_HUMAN_REVIEW
```

### Case B — stock shortage

```
=== fresh RFQ RFQ_ACC_0002
intake    : run RUN_ACC_0002 - rows written
customer  : EXACT
line 1    : RESOLVED
quote     : Q-2026-8102 (QTE_ACC_0002) - status DRAFT
money     : 200 x PMP-A-100 @ 1150.0000 EUR = 230000.00
            subtotal 230000.00 EUR - discount 0.00 = total 230000.00 EUR
gate      : eligible for human review: NO / awaiting human approval: NO (printed, never stored)
blocked   : STOCK_INSUFFICIENT - line 1 PRD_0001: stock is PARTIAL, which must be reviewed before the quote may be sent (line 1)
persisted : stored quote QTE_ACC_0002: 1 line(s), 1 blocked reason(s), one transaction
outcome   : DRAFT_BLOCKED
```

### Case C — tampered extraction

```
=== fresh RFQ RFQ_ACC_0003
intake    : run RUN_ACC_0003 - rows written
customer  : EXACT
line 1    : RESOLVED
stop      : claim verification refused the quote (codes: EVIDENCE_NOT_VERBATIM, QUANTITY_UNSUPPORTED)
persisted : nothing - no quote was prepared, so no quote row was stored
outcome   : HUMAN_REQUIRED_BEFORE_QUOTE
```

Note what Case C *does* store: the RFQ, its run and one `RECEIVED` intake event.
The request arrived; only the quotation is absent. A pre-quote refusal is
returned as a typed outcome and **not persisted**, because no accepted state
records one.

### Case D — duplicate

```
intake    : run RUN_ACC_0001 - rows already stored, unchanged
persisted : identical quote QTE_ACC_0001 was already stored - nothing new written (an identical retry is free)
outcome   : DUPLICATE
```

## Check the rows, not just the text

```bash
python - <<'PY'
import sqlite3
db = sqlite3.connect("var/rfq_agent.db")
for row in db.execute(
    "SELECT quote_id, status, subtotal, total, revision, approved_at, policy_allowed "
    "FROM quotes ORDER BY quote_id"
):
    print(row)
for row in db.execute(
    "SELECT quote_id, code, line_ordinal, resolvable_by_human FROM quote_blocked_reasons"
):
    print(row)
for table in ("run_events", "human_actions", "outbound_messages"):
    print(table, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
PY
```

Expected after A, B and C (in that order):

| Table | Rows | Why |
| --- | --- | --- |
| `rfqs` | 3 | every declaration was stored, including the refused one |
| `runs` | 3, all `RECEIVED`, `outcome` `NULL`, `row_version` 0 | no transition was invented |
| `intake_events` | 3 × `RECEIVED`, `seq` 1 | one intake event per stored RFQ |
| `quotes` | 2, both `DRAFT`, `revision` 1, `approved_at`/`policy_*` `NULL` | Case C stored none |
| `quote_lines` | 2 | one per stored quote |
| `quote_blocked_reasons` | 1 × `STOCK_INSUFFICIENT`, `line_ordinal` 1 | Case B's evidence |
| `run_events` | 0 | this path takes no transition |
| `human_actions` | 0 | nothing is approved |
| `outbound_messages` | 0 | nothing is sent |

After Case D: every count above is unchanged.

## What this slice reuses, unchanged

`bind_customer` (exact alias identity only), `resolve_lines`, `to_quote_request`
(and the closed-grammar `verify_extraction_claims` behind it), `run_quote`,
`QuoteWriter.persist`, `BusinessReader`, the seed's schema check and
`Database`. No pricing, stock, gating or quoting rule is re-implemented, and
nothing imports `rfq_agent.demo`.

## Stated limitations

* **Input is a declaration, not an email.** No provider, model, parser or
  attachment handling: the extraction claims are supplied by the caller, exactly
  as a model's output would be, and are verified identically.
* **Refusals are not persisted.** `run_events` only accepts legal transition
  edges, `intake_events` has no refusal kind and `quotes.policy_*` belongs to a
  gate that never ran, so a refusal is reported and stored nowhere. Adding an
  audit state for it needs its own approved design and migration.
* **No approval, no send, no state change.** The run stays `RECEIVED`; the
  gate's verdict is printed and discarded; the quote is a `DRAFT`.
* **A different quotation for a run that already has one is a `CONFLICT`.**
  `(run_id, revision)` is unique and no revision policy exists, so a changed
  price between two runs is refused rather than silently becoming revision 2.
* **Exact-only binding.** A sender proven by a name or a mail domain still needs
  a human; this path has no operator binding, unlike the demo's fixtures.
