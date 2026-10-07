# AI RFQ Operations Agent

An agentic B2B RFQ (request-for-quotation) processing system. Unstructured
customer requests go in; verified, approval-ready quotations come out.

The LLM interprets and orchestrates. It never invents prices, stock, SKUs,
customer records, totals or delivery dates — those come from deterministic tools
and business data, and every consequential action requires a human.

## Status

**Phase 0 — skeleton & contracts.** Schemas, enums, value objects, the workflow
state machine contract and the provider-neutral interfaces.

**Phase 1A — persistence.** SQLite through synchronous SQLAlchemy 2.0, with the
schema versioned by Alembic: 26 tables, append-only audit triggers, and the run
state machine enforced by the database itself. Migrations are the only way a
schema is created — there is no `create_all()` in production code.

**Phase 1B — demo dataset.** The Northwind Components business data: product
families, catalogue, customers and aliases, price books, stock, carriers and a
holiday calendar, applied by one deterministic, idempotent seed command.

**Phase 1C — read boundary.** The read repositories through which the rest of
the system sees business data. Callers receive domain value objects or explicit
read models — never SQLAlchemy rows — with money, enums, dates and UTC instants
converted explicitly at the seam. Reads report what the data says and decide
nothing: an ambiguous part number comes back as two matches, an expired price
entry comes back like any other, and no price is preferred over another.

**Phase 1D — pricing resolution.** The deterministic rule that turns the stored
price entries for one already-resolved product into a single applied price:
customer contract price, then tier price, then the public list tier; then the
highest reachable quantity break, the latest start date, and finally the entry
id, which makes the order total. Validity windows and quantity thresholds are
honoured exactly, an expired entry is never used, another customer's contract is
never offered and nothing is converted or invented. Every outcome is explicit: a
found price with its entry id as provenance, or `MISSING` / `EXPIRED` with a
machine-readable reason.

**Phase 1E — stock availability.** The deterministic answer to "can we cover
this quantity": unreserved stock (`on_hand − reserved`) is the only thing that
counts, inbound shipments are reported but never counted, and the outcome is
`SUFFICIENT` / `PARTIAL` / `NONE` with a machine-readable reason when the request
is not covered. A warehouse that can cover the request alone is reported as a
fact; when only the combined stock covers it, that is stated with a split
proposal as data — no shipping decision is made, nothing is reserved, and stale
or future-dated stock facts are reported rather than silently trusted.

**Phase 1F — delivery and calendar.** The earliest ship and delivery dates for a
request stock covers, computed from the carrier services and the seeded holiday
calendar: the cut-off hour decides whether an order leaves today, transit is
counted in working days, weekends are skipped unless the service runs on them,
and a public holiday stops the clock in the origin *or* the destination country.
A request covered by two warehouses is scheduled leg by leg and the later leg
sets the promise — reported as data, with no shipping decision attached. When a
fact is missing (no carrier, an unknown destination country, a country outside
the loaded calendar) the answer is `UNKNOWN` and no date at all, never a guess.

**Phase 1G — discount rules.** Which single discount rule applies to a quotation
is a lookup, not a negotiation: the seeded rules document the order (highest
priority, then the narrowest scope — customer, then tier, then everyone), and the
code follows it. Applicability is decided before precedence, so a switched-off,
out-of-window or below-floor rule at a higher precedence never blocks a lower one;
the rate is never a tie-break, and the rule id settles a total order. A rule
whose rate is zero is an applied rule, and a rule that needs sign-off is selected
and reported as such — this phase names the rule and the rate, and no percentage
touches money here.

**Phase 1H — quote arithmetic.** The money: every line is extended as
`quantity × unit_price` and quantised to cents with half-up rounding, the
subtotal is the sum of the extensions, the selected rule's percentage is applied
to that subtotal, and `total = subtotal − discount_amount`. All of it is exact
`Decimal` — no float ever touches a price. Every amount keeps its provenance:
the price entry id on the line, the rule id, scope and rate on the quote, plus
the calculation version and a fingerprint of the inputs, so the same facts always
produce the same money and the same digest. A line whose price lookup did not
return `FOUND` gets no price and no amount: it is blocked, machine-readably, it
is never totalled, and the quote that contains it can never be sent. The
calculator applies the discount it is given, reports `requires_approval` as a
fact, and decides nothing — no gate, no approval, no write.

**Phase 1I — the blocking ledger.** Why a quotation cannot go out is a fact, not
a summary written by a model. The projection takes what the calculation already
established — which lines priced, what each line's stock position is, what the
delivery promise says, whether the selected rule needed sign-off, and whether the
customer is on credit hold — and turns it into the ledger the contract already
defined: a machine-readable code, a sentence an operator can act on, and the
offending line where there is one. Five facts map onto five codes and nothing
else is invented: an un-priced line is `PRICE_MISSING`, a stock status the
contract calls blocking is `STOCK_INSUFFICIENT`, a promise that is infeasible *or
unknown* is `DELIVERY_INFEASIBLE`, a rule that exceeds the delegated limit is
`DISCOUNT_OVER_POLICY`, and a credit hold is `CREDIT_HOLD`. Unknown is never
softened into "probably fine" and stale is never relabelled — it is reported as
what it is, by the same wording the gate uses. Entries are deduplicated by code
(the contract's gate input refuses duplicate codes) with the per-line detail kept
on the line, ordered by the contract's own code order, and identical facts always
produce an identical ledger. Nothing is approved, rejected or transitioned: this
projects the reasons a human will see, and gives them nothing to argue with.

**Phase 1J' — the write path.** Where the accepted output becomes rows. Two
decisions settled Phase 1J's stop. A line with no usable price is now storable:
`quote_lines.price_entry_id` is nullable, the foreign key stays (a price entry
that *is* named must exist), and the pairing `(price_status = 'FOUND') =
(price_entry_id IS NOT NULL)` is enforced in the schema - so a refused line is
stored blocked, with no price entry and no money, and the domain's
`PRICE_MISSING` sentinel is translated to SQL `NULL` at this boundary and
nowhere else. And the projected ledger gets a table of its own,
`quote_blocked_reasons`: one append-only row per reason in the order it was
projected, with the line it is about, whether a human can resolve it and the
ledger's flags, guarded by the same `UPDATE`/`DELETE` triggers as the audit
tables and written by nothing else. A quote's header, its lines in ordinal order
and that evidence are written in **one transaction**: a failure anywhere leaves
nothing behind, including the idempotency claim, which is taken and released in
the same unit of work - so a retry after a crash is free rather than half
applied. Repeating an identical write stores nothing new and is not a new quote
revision, and a *different* quotation for the same run is refused by
`(run_id, revision)` instead of quietly becoming revision 2. Nothing is
recalculated, no gate runs, no status changes and no approval is recorded.

**Phase 1K — the gate decides.** The deterministic gate stops being a function
nobody calls and answers exactly one question: *is this quotation eligible for a
future human review step?* The answer
is deliberately narrow — not an approval, not sendability and not a workflow
state; a quotation can be eligible for review and never leave the building, and in
V1 the human approval is still outstanding even when the answer is yes. It is one
pure pass over facts that already exist (the quote, the projected ledger, the
caller's credit-hold and delivery facts, the approval policy), and it decides
*about* the evidence rather than taking its word: a blocking fact the quote
itself proves must appear in the ledger, or the evidence is `INCOMPLETE`; a
ledger entry no supplied fact witnesses is `CONTRADICTORY` and is **not** recorded
as a fact — the gate will not launder a denied claim into the ledger; two
delivery assessments that disagree are `CONTRADICTORY`; one condition reported as
both a blocking reason and a non-blocking flag is `CONTRADICTORY`; a quote that
is already terminal, or a policy that does not require human approval, is
`UNSUPPORTED`; a blocked line no code accounts for, or a credit-hold status
nobody established, is `INCOMPLETE`. Only `COMPLETE` evidence with no asserted
code, under the V1 rule that a human approves, makes a quotation eligible — so
every missing, unsupported or contradictory fact fails closed. The decision says
what it decided over: the quote's and run's identity, the rule set (`gate-v1`),
the quote's own input fingerprint and a fingerprint of the exact facts, stable
across runs and input order. The credit-hold fact is now explicit — `None` means
"not established" and fails closed, so "nobody checked" can never read as "the
account is fine" — while `project_blocked_ledger`'s accepted five-fact mapping is
unchanged. Nothing is approved, sent, transitioned or promoted: the quote stays a
draft, no gate outcome is persisted yet, and no row changes.

No provider calls, agent loop, worker, approval, outbound message or UI yet; the
gate decision is not persisted yet either — Phase 1K decides, and the write path
for its evidence is the next phase.

The approved V1 design (state machine, failure model, trust boundary, evaluation
strategy) is not committed yet — it lands as `docs/ARCHITECTURE.md` alongside the
Phase 1 work. Until then the schemas in `src/rfq_agent/domain/` are the
authoritative statement of the contract, and `src/rfq_agent/domain/workflow.py`
holds the state machine as data.

## Setup

```bash
make venv PYTHON_VERSION=3.13   # or: python3 -m venv .venv
make install
```

## Checks

```bash
make lint    # ruff check + ruff format --check
make test    # pytest (offline only)
make check   # both - the phase exit check
```

`make` always uses `.venv`. A bare `pytest` also works from the repo root:
`[tool.pytest.ini_options].pythonpath = ["src"]` puts the package on `sys.path`,
provided the interpreter running pytest has `pydantic` and `pydantic-settings`
installed (`pip install -e ".[dev]"` does that).

Live Groq tests are opt-in and never run by `make test`:

```bash
.venv/bin/pytest -m live
```

## Demo data

```bash
make migrate      # alembic upgrade head — creates var/rfq_agent.db
make seed         # write the Northwind Components dataset (idempotent)
make seed-reset   # delete the dataset's rows and write them again from scratch
```

The dataset is literals in `src/rfq_agent/seed/dataset.py`, versioned by
`SEED_VERSION`, and deliberately not generated: no clock, no randomness, no
streaming from a remote source. `make seed` *converges* the database to the
dataset — a missing row is inserted, a row edited by hand is corrected, a row
that is not part of the dataset is left alone — and reports what it did.
`make seed-reset` is the local-development path: it deletes exactly the dataset's
rows, children before parents, then writes them again, and refuses (rather than
cascading) if business records such as quotations reference them.

Contents: 3 product families, 18 products, 8 customers with aliases, 2 warehouses
(Warsaw and Berlin), 2 price books — a public list and negotiated contracts —
27 price entries, 6 discount rules, 27 stock rows, 4 carrier services, and the
2026 public-holiday calendar for the seven countries involved. Currency is EUR
throughout and there is no tax logic. A few rows exist on purpose to make the
hard cases demonstrable: a discontinued product whose only price has expired, a
contract-only item with no list price, a customer on credit hold, a deactivated
account, a stock line with nothing available but stock inbound, and one part
number that genuinely resolves to two products.

## Configuration

Copy `.env.example` to `.env`. Every variable is prefixed `RFQ_`; nested
sections use `__` (e.g. `RFQ_GROQ__AGENT_MODEL`).
