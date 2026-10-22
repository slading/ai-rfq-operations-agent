# Raw RFQ → DRAFT — operator walkthrough (Project 04)

One command takes a raw RFQ body plus trusted metadata, asks the configured live
model for structured claims, and pushes those claims through the accepted
deterministic engine. The only quote it can produce is a `DRAFT` a human must
still review.

```bash
python -m rfq_agent.raw_acceptance --input docs/acceptance/raw-case-a.json [--url URL]
```

Everything below is the same command, explained.

## 1. Prerequisites

```bash
make venv install            # or: pip install -e ".[dev]"
export RFQ_LLM_MODE=live     # default is replay: no network, and the command refuses
export RFQ_GROQ__API_KEY=... # environment only; never in a file that is committed
make migrate && make seed
```

`RFQ_GROQ__AGENT_MODEL`, `RFQ_GROQ__REQUEST_TIMEOUT_SECONDS`,
`RFQ_GROQ__MAX_OUTPUT_TOKENS`, `RFQ_GROQ__MAX_RESPONSE_BYTES`,
`RFQ_GROQ__RETRY__MAX_ATTEMPTS` and the rest of the `RFQ_GROQ__*` block in
`.env.example` bound the call. Without the mode switch and a credential the
command exits `1` before reading a body or opening a socket.

## 2. The input file

`docs/acceptance/raw-case-a.json`:

```json
{
  "body": "Hello,\nplease quote 25 pcs of 100-ABC for our standard replenishment.\nNo specific delivery date is required.\nBest regards,\nNordwind Einkauf",
  "sender_email": "einkauf@nordwind-industrie.de",
  "sender_name": "Nordwind Einkauf",
  "received_at": "2026-10-06T09:00:00Z",
  "subject": "RFQ: 25 pcs 100-ABC",
  "source_channel": "EMAIL"
}
```

* `body` — the untrusted customer text, verbatim. It must be nonempty and at most
  65 536 characters. It must satisfy the closed source grammar documented in
  [`README.md`](README.md); anything else is refused.
* `sender_email`, `sender_name` — trusted metadata. The customer is bound only
  from a stored `EMAIL` alias that matches this address exactly; a name or a
  domain is evidence for a human, not a binding.
* `received_at` — trusted, timezone-aware. It is the arrival instant used for the
  pricing date and every "as of" question; the slice reads no clock.
* `subject`, `source_channel` — trusted. `FIXTURE` is refused: it names the
  demonstration's channel.

There is no extraction field in this file. That is the point: the claims come
from the model, and nothing about their provenance is taken on trust.

## 3. What runs

```
raw body ──▶ envelope ──▶ live provider (one call, bounded)
                                │
                    schema-valid claims? ── no ──▶ typed refusal, nothing stored
                                │ yes
              derive ids from metadata + body digest
                                │
        accept_rfq: store intake ──▶ load source back ──▶ bind customer
                    ──▶ resolve lines ──▶ verify claims vs stored source
                    ──▶ price ──▶ block ledger ──▶ gate ──▶ persist DRAFT
```

The identifiers in the report (`RFQ_…`, `RUN_…`, `QTE_…`, `Q-<year>-<digits>`,
`LI_…`) are derived from the sender, arrival instant, subject, channel and body
digest. Re-running the same file derives the same identities and reports
`DUPLICATE`, writing nothing new.

One cost note, stated rather than glossed: a replay still makes **one** model
call, because the identity is derived from the delivery and the accepted check
happens once the claim exists. Suppressing a re-delivery before the provider is
called would need an intake-first design with its own accepted phase; this slice
does not have one.

## 4. What a successful run reports

```
Raw RFQ acceptance (live extraction -> accepted engine): northwind-components 2026.10.1
database: configured database
input: raw delivery file
provider: live mode, credentials from the environment
extraction: openai/gpt-oss-120b - 1 line(s), 0 open question(s), 812 token(s), 640 ms, provider-constrained schema: no

=== fresh RFQ RFQ_1369BB071380D47B
intake    : run RUN_1369BB071380D47B - rows written
customer  : EXACT
line 1    : RESOLVED
quote     : Q-2026-06268373 (QTE_1369BB071380D47B) - status DRAFT
money     : 25 x PMP-A-100 @ 1150.0000 EUR = 28750.00
            subtotal 28750.00 EUR - discount 0.00 = total 28750.00 EUR
gate      : eligible for human review: YES / awaiting human approval: YES (printed, never stored)
blocked   : none
persisted : stored quote QTE_1369BB071380D47B: 1 line(s), 0 blocked reason(s), one transaction
outcome   : DRAFT_AWAITING_HUMAN_REVIEW
```

No live transcript is committed: no credential is checked in, and this
repository's CI has no network. The same path is asserted offline, end to end,
in `tests/acceptance/test_raw_acceptance.py` with the shipped scripted provider —
including the `28 750.00 EUR` total, the blocked variant, the duplicate replay and
every refusal below.

## 5. What a refusal looks like

Provider or output failure — nothing is written at all:

```
=== raw RFQ RFQ_1369BB071380D47B
intake    : nothing written - no claim reached the accepted pipeline
extraction: no claim was produced
stop      : extraction refused (PROVIDER_TIMEOUT: TIMEOUT)
retryable : YES
persisted : nothing - no intake row and no quote row
outcome   : PROVIDER_TIMEOUT
```

Closed refusal codes: `PROVIDER_NOT_CONFIGURED`, `PROVIDER_TIMEOUT`,
`PROVIDER_FAILURE`, `RESPONSE_NOT_JSON`, `RESPONSE_TOO_LARGE`,
`RESPONSE_SCHEMA_INVALID`, `NO_CLAIMED_LINES`.

A claim the source does not support — a quantity, evidence span, delivery date or
phrasing the deterministic verifier cannot ground — reaches the accepted engine
and is refused there instead:

```
=== fresh RFQ RFQ_…
intake    : run RUN_… - rows written
customer  : EXACT
line 1    : RESOLVED
stop      : claim verification refused the quote (codes: EVIDENCE_NOT_VERBATIM, QUANTITY_UNSUPPORTED)
persisted : nothing - no quote was prepared, so no quote row was stored
outcome   : HUMAN_REQUIRED_BEFORE_QUOTE
```

The intake rows exist because the request did arrive; the quotation does not.

## 6. Check the rows

```bash
python - <<'PY'
import sqlite3
db = sqlite3.connect("var/rfq_agent.db")
for row in db.execute(
    "SELECT rfq_id, source_channel, sender_email, subject, received_at FROM rfqs"
):
    print(row)
for row in db.execute(
    "SELECT quote_id, status, subtotal, total, approved_at, policy_allowed FROM quotes"
):
    print(row)
for table in ("run_events", "human_actions", "outbound_messages"):
    print(table, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
print("runs", db.execute("SELECT state, outcome FROM runs").fetchall())
PY
```

Expected: one `rfqs` row carrying the caller's metadata, one `runs` row still
`RECEIVED` with a `NULL` outcome, one `intake_events` row, one `quotes` row in
`DRAFT` with `approved_at` and `policy_allowed` both `NULL`, and **zero**
`run_events`, `human_actions` and `outbound_messages`.

## 7. Deliberately unsupported

* Mailbox, IMAP/Graph ingestion, threading and supersession: the delivery file is
  the ingress.
* Attachments: metadata is refused by the accepted source loader, so an RFQ with
  attachments cannot be verified — and this slice never writes one.
* Human approval, outbound send, run transitions, an HTTP service or a UI.
* Any phrasing outside the closed source grammar, including specifications,
  corrections, alternatives, pack language and multi-line prose.
* A second, different quotation for a run that already has one: `(run_id,
  revision)` is unique and no revision policy exists, so it is a `CONFLICT`.
