# ETL Prototype: Rule-Based Engine + AI Mapping Agent

Two things live here, both grounded in `sap_target_schema/` (the schema catalog and
the three worked examples) rather than reinventing decisions from scratch:

1. **`transform_engine.py` + `specs/`** — a small rule-based engine that actually
   runs the field-mapping patterns from the worked examples against the real
   legacy data in `ERP_Seed_Data (1).xlsx`, and produces SAP-shaped staging tables.
2. **`agent/sap_migration_agent.py`** — an interactive Claude-powered agent that
   helps work through *new* mapping problems (tables the worked examples didn't
   cover), using the same tables, data, and transform vocabulary as grounding —
   and can test its own proposals against real rows before you commit to them.

Neither connects to a real SAP system. Both are prototypes: the engine proves the
transform logic is correct and executable; the agent proves an LLM can extend that
logic to new tables without hallucinating SAP structure, by being forced to look
things up and test them rather than recall them.

---

## 1. The transform engine

```
python transform_engine.py
```

Reads every `*.json` file in `specs/`, applies it to the matching sheet in
`ERP_Seed_Data (1).xlsx`, and writes to `output/`:

- **`<SAP_TABLE>.csv`** — one file per SAP target table (`PA0001.csv`, `VBAK.csv`,
  `ACDOCA.csv`, …), rows from every spec that contributes to it.
- **`_pending_lookups.csv`** — every field that needs a real SAP customizing value
  (a currency key, a GL account number, …) the engine doesn't have access to.
  Instead of fabricating one, the field is filled with a placeholder like
  `XREF:Foundation>Currencies -> TCURC:JOD` and logged here with a distinct-value
  count — this is the punch list for whoever has real access to the target system.
- **`_skipped_fields.csv`** — every legacy column that was deliberately dropped or
  marked not-migrated, with a one-line reason each. Nothing vanishes silently.
- **`_validation_report.csv`** — pass/fail results of the pre-transform checks each
  spec declares (currently: the `General Ledger` balanced-debit/credit check).
- **`_run_log.txt`** — a plain-text summary of the whole run.

### What it currently covers

Five of the six specs in `specs/` are the executable encoding of the three worked
examples in `sap_target_schema/examples/`; the sixth (`customers_addresses.json`)
came out of a live session with the agent below — see § "A real session, not a
demo" for how it got there:

| Spec | Source sheet(s) | Target tables |
|---|---|---|
| `employees.json` | Employees | `PA0001`, `PA0002`, `PA0008`, `PA0009`, `PA0105` (×2), `PA0185`, `HRP1001` |
| `sales_orders_header.json` | Sales Orders | `VBAK`, `VBKD`, `VBPA`, `VBFA` |
| `sales_order_lines.json` | Sales Order Lines | `VBAP` |
| `journal_entries_header.json` | Journal Entries | `BKPF` |
| `general_ledger_lines.json` | General Ledger | `ACDOCA` |
| `customers_addresses.json` | Customers (address fields only) | `ADRC` (×2 rows per customer — billing + shipping) |

Run against the real seed data, this produces 15 output tables from 1,282 source
rows, all 257 journal entries validate as balanced, every one of the 80 `ADRC`
rows resolves a non-null `COUNTRY` with no `ADDRNUMBER` collisions, and every
dropped/deferred field is accounted for in `_skipped_fields.csv` — see
`output/_run_log.txt` after running it yourself for the exact numbers.

### Extending it to the other ~130 `MIGRATE` tables

Each spec is the machine-readable twin of a `*_field_mapping.csv` like the ones in
`sap_target_schema/examples/`. To add one:

1. Pick a `MIGRATE` row from `sap_target_schema/legacy_to_sap_mapping.csv`.
2. Author the field-level mapping the way the three worked examples do (by hand,
   or via the agent below) — direct copies, lookups, splits, relationships.
3. Encode it as a spec JSON using the transform types documented in the
   `transform_engine.py` module docstring (`direct`, `date_yyyymmdd`, `truncate`,
   `constant`, `lookup_passthrough`, `row_number_pad`, `signed_amount_pair`,
   `split_delimiter`, `value_map`).
4. Drop the file in `specs/` and re-run — no code changes needed.

That vocabulary isn't fixed in stone: `split_delimiter` and `value_map` (and the
`offset` parameter on `row_number_pad`) didn't exist when this engine was first
built — they were added when a real mapping problem needed them, mid-session
with the agent below. See § "A real session, not a demo" for how that happened
and why it's a reasonable way for the vocabulary to keep growing.

### Honest limitations

- **No real SAP codes.** Every `lookup_passthrough` field is a labeled
  cross-reference placeholder, not a guessed value. Fabricating a plausible-looking
  currency key or GL account number would be actively worse than leaving it
  unresolved — check `_pending_lookups.csv` before treating any output as loadable.
- **Staging keys, not real document numbers.** `PERNR`, `VBELN`, `BELNR`, etc. carry
  the legacy row's own ID verbatim so header/item/relationship records stay
  cross-referenced against each other. A real load assigns SAP's own numbers from
  configured number ranges.
- **Condition-driven pricing and derived reports are out of scope.** Fields like
  `Unit_Price`, `Subtotal`, `Running_Balance`, `Status` are dropped with a reason,
  not approximated — see the worked examples for why they don't have a flat-field
  home in SAP at all.
- **Free-text parsing (addresses) is not attempted by hand-authored specs** — the
  original five specs flag it as a `NEEDS_PARSE`-style drop rather than guess.
  `customers_addresses.json` is the exception: it exists because the agent
  proposed, tested, and got a `split_delimiter`/`value_map`-based parse confirmed
  through the process below — the engine still never guesses on its own, but it
  can execute a parse once a human (or the agent, checked by a human) has decided
  one is sound enough to encode as a spec.

---

## 2. The AI mapping agent

```
cd agent
pip install anthropic python-dotenv pip-system-certs   # if not already installed
copy .env.example .env                                  # then fill in ANTHROPIC_API_KEY
python sap_migration_agent.py
```

Requires Claude API credentials. The script loads a `.env` file (this folder or
the project root — see `.env.example`) via `python-dotenv`; if no `.env` is
found, the SDK falls back to a real `ANTHROPIC_API_KEY` env var or an
`ant auth login` profile automatically. It prints a clear message if none of
those provide a key. `.gitignore` at the project root excludes real `.env`
files so a key never gets committed.

`pip-system-certs` is included above as a precaution: on some machines the
plain `anthropic` SDK call fails with `anthropic.APIConnectionError` /
`SSL: CERTIFICATE_VERIFY_FAILED`, because Python's bundled certificate store
(via `httpx`/`certifi`) doesn't trust whatever is terminating TLS on that
network (common behind a corporate proxy or a sandboxed dev environment doing
TLS interception) — even though the identical request succeeds from a tool
that uses the OS's own certificate store (e.g. PowerShell's
`Invoke-WebRequest`), which is exactly the symptom hit and fixed while testing
this agent. `pip-system-certs` patches Python's `ssl` module process-wide, at
import time, to trust the OS store instead of its own bundled one — no code
change needed. If installing it doesn't clear the error, the cause is more
likely the network itself, not certificates — check connectivity to
`api.anthropic.com` directly.

### What it does

An interactive REPL for talking through a mapping problem the worked examples
didn't cover. It's given five tools and instructed to use them instead of
recalling SAP structure from training data:

| Tool | Grounds the agent in |
|---|---|
| `search_sap_tables` | `sap_target_schema/sap_tables.json` — the real 119-table catalog |
| `lookup_legacy_mapping` | `sap_target_schema/legacy_to_sap_mapping.csv` — table-level routing already decided |
| `get_source_sample` | The real legacy data, so it reasons from actual values, not column names |
| `preview_transform` | **This project's own transform engine** — runs a draft mapping against real rows before presenting it as final |
| `save_mapping_decision` | Persists a confirmed mapping to `agent/mapping_decisions.csv`, in the same shape as the worked examples' CSVs |

The system prompt explicitly forbids inventing table/field names or SAP
customizing values — every claim has to come from a tool call, mirroring the same
discipline the worked examples and the transform engine both follow by hand.

### A real session, not a demo

This agent has actually been run against real credentials, on the real project
data, across four separate conversations that resolved one genuine mapping
problem end to end: `Customers.Billing_Address`/`Shipping_Address` → `ADRC`.

1. **First ask** — "How should I map the Billing_Address field on Customers?"
   The agent searched the catalog, found `BUT000` has no address fields,
   correctly redirected to `ADRC`, then previewed its own first attempt, caught
   that it had put the same string into both `STREET` and `CITY1`, and said so
   instead of presenting it as finished. It surfaced three open questions
   (street/city need splitting, billing vs. shipping need two `ADRC` rows,
   `ADDRNUMBER` is provisional) rather than picking answers to sound complete.
2. **Deciding the split** — told to split on the first comma, it discovered the
   transform vocabulary had no split operation. Rather than approximate with
   `truncate`, that gap became a real addition to `transform_engine.py`
   (`split_delimiter`), unit-tested before being wired back in. The agent then
   tested it against 20 real rows for both address columns and saved four
   confirmed decisions — all marked `APPROX`, with the exact reason why
   (`split_delimiter` splits on *every* comma, not just the first, so a
   multi-comma address would silently misparse) rather than rounding up to
   `HIGH` to look more finished.
3. **The Country problem** — while testing, it independently noticed
   `Customers.Country` frequently disagreed with the address's own city, and
   flagged it unprompted rather than mapping past it. Investigating the full
   40-row table (not just its 20-row sample cap) showed the mismatch was
   real and severe: 50%+ of rows, with currency, phone country code, and even
   company naming (6 of 9 "Jordan"-named companies had a non-Jordan `Country`)
   all pointing at `Country` being the unreliable field. Handed that evidence,
   the agent didn't just agree — it pushed back on treating uniform currency and
   phone codes as strong evidence (they're constant across all 40 rows, so they
   can't discriminate between hypotheses), proposed three named options with
   honest uncertainty about which was right, and refused to save a `COUNTRY`
   decision until the ambiguity was actually resolved (a follow-up check on
   `Tax_ID`, the one lead it suggested, also turned out uninformative). Deriving
   `COUNTRY` from the address city instead of the legacy column required a
   second new transform, `value_map` — deliberately built distinct from
   `lookup_passthrough`, because this is a fact the pipeline can independently
   verify (geography), not a placeholder for an unknown SAP code.
4. **Closing its own gaps** — confirming the exact ISO codes surfaced a second
   real bug the agent caught itself: two `ADRC` rows per customer (billing +
   shipping) would collide on the same `ADDRNUMBER`, since both were computed
   from `Customer_ID` alone. That became a third small engine addition — an
   `offset` parameter on `row_number_pad`, letting billing/shipping generate
   disjoint even/odd key series from one column. The agent also refused to
   silently overwrite its now-superseded first `ADDRNUMBER` decision, and
   refused to invent a "not mapped" convention for the legacy `Country` column
   that didn't already exist in the project — both were resolved by hand
   afterward: the superseded row was annotated in place rather than deleted,
   and `Country`'s exclusion was written up in `customers_addresses.json`'s
   `dropped_fields`, the same convention every other spec already uses.

The result, `etl/specs/customers_addresses.json`, ran clean against the full
40-row table: 80 `ADRC` rows, zero null `COUNTRY` values, zero `ADDRNUMBER`
collisions. The full back-and-forth — including the parts where the agent was
uncertain, wrong, or waiting on a human — is preserved in
`etl/agent/mapping_decisions.csv`.

### Why this shape, not a bigger one

This is deliberately the simplest tier that does the job — see the `claude-api`
skill's "Should I Build an Agent?" checklist. A manual tool-calling loop (not the
beta Tool Runner, not Managed Agents) was chosen because: the task is genuinely
open-ended (which of ~130 remaining tables, which columns, what ambiguity) so a
single call or fixed workflow doesn't fit; but it's a local, developer-facing
prototype with no need for hosted deployment, persisted agent configs, or a
sandboxed container — so Managed Agents would be more platform than the problem
needs. The five tools above are exactly the lookups a human doing this work by
hand would do (check the catalog, check what's already decided, look at real
data, test before committing); the agent doesn't get any capability the worked
examples in this project weren't already built with.
