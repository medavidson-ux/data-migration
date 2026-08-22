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
- **`_run_log.txt`** — a plain-text summary of the whole run, including three
  failure-visibility sections: **string truncations applied** (how many values
  were cut to SAP field lengths), **date parse failures** (count plus the
  distinct offending source values — an unparseable date becomes a null SAP
  date, so this is where you'd notice a format problem), and **null rates in
  output fields** (per target field, `nulls/rows_emitting_the_field`, with an
  `ALL NULL` marker when a transform produced nothing; fields a contributing
  spec doesn't emit are *absent*, not null, and don't inflate the rate). The
  null-rate section's first real run surfaced `ACDOCA.KOSTL` at 71% null —
  investigated and confirmed expected; see the cost-center finding in
  `sap_target_schema/DATA_QUALITY_FINDINGS.md`.

### What it currently covers

Seventeen specs. Five are the executable encoding of the first three worked
examples in `sap_target_schema/examples/`; the next six cover the 4th, 5th,
and 6th examples — `Customers`, the Material Master, and `Assets` — the
`Customers` address half came out of a live session with the agent below, see
§ "A real session, not a demo" for how it got there. The newest six cover the
**purchasing cycle** (`Vendors`, purchase orders and their lines, vendor
contracts) and the **sales billing leg** (customer invoices and their lines,
completing order → fulfillment → invoice). The billing mapping verified along
the way that the Sales-side `Invoices` sheet is a 1:1 duplicate of the
Finance-side `Customer Invoices` (53/53 rows identical on every shared
column) and mapped the Finance side as authoritative — see
`sap_target_schema/DATA_QUALITY_FINDINGS.md` for everything the purchasing
and billing batches surfaced (two vendor-less contracts, backward validity
dates passed through verbatim, and the resolved Business-Partner
even/odd staging-key convention).

| Spec | Source sheet(s) | Target tables |
|---|---|---|
| `employees.json` | Employees | `PA0001`, `PA0002`, `PA0008`, `PA0009`, `PA0105` (×2), `PA0185`, `HRP1001` |
| `sales_orders_header.json` | Sales Orders | `VBAK`, `VBKD`, `VBPA`, `VBFA` |
| `sales_order_lines.json` | Sales Order Lines | `VBAP` |
| `journal_entries_header.json` | Journal Entries | `BKPF` |
| `general_ledger_lines.json` | General Ledger | `ACDOCA` |
| `customers_addresses.json` | Customers (address fields) | `ADRC` (×2 rows per customer — billing + shipping) |
| `customers_core.json` | Customers (identity/company/sales-area fields) | `BUT000`, `KNB1`, `KNVV`, `BUT0ID`, `BUT0TX` |
| `inventory_items.json` | Inventory Items | `MARA`, `MAKT`, `MARC`, `MBEW` |
| `locations.json` | Locations | `T499S`, `ADRC` |
| `assets.json` | Assets | `ANLA`, `ANLZ`, `ANLB` |
| `depreciation.json` | Depreciation | `ACDOCA` (one leg only — see notes below) |
| `vendors_core.json` | Vendors | `BUT000`, `LFA1`, `LFB1` |
| `purchase_orders_header.json` | Purchase Orders | `EKKO` |
| `purchase_order_lines.json` | Purchase Order Lines | `EKPO`, `EKET` |
| `vendor_contracts.json` | Vendor Contracts | `EKKO` (`BSTYP=K`, contract category) |
| `customer_invoices_header.json` | Customer Invoices (Finance, authoritative — Sales `Invoices` verified a 1:1 duplicate) | `VBRK` |
| `customer_invoice_lines.json` | Customer Invoice Lines | `VBRP` |

Together the two `customers_*.json` specs are the executable side of the 4th
worked example, `sap_target_schema/examples/customers_field_mapping.csv` — see
§ "A real session, not a demo" below for how the address half came from a live
agent session, and note that a few fields on `Customers` (`Email`,
`Phone_Number`, `Is_Active`, `Account_Manager_ID`) are deliberately *not*
encoded in either spec because the mapping decision itself is still open, not
just an unresolved lookup — see `customers_core.json`'s `dropped_fields` and the
worked example for why.

`inventory_items.json` is the 5th worked example's only spec — its sibling
sheet, `Product Catalog`, is fully documented in
`sap_target_schema/examples/product_catalog_field_mapping.csv` but deliberately
**not** encoded as a runnable spec: its real target key needs a Distribution
Channel, and there's no source data for that at all, not even an unresolved
lookup. This is also the example that corrected a wrong assumption baked into
the original schema catalog — `Product Catalog` and `Inventory Items` were
flagged as duplicate tables before any real data had been checked; they aren't.
See `materials_worked_example.md` for what checking actually found.

`locations.json`/`assets.json`/`depreciation.json` are the 6th worked
example's specs (config-only `asset_config_field_mapping.csv` isn't
encoded — it's almost entirely customizing linkages, not flat fields). This
example checked three more "duplicate" flags the original catalog carried
into `Finance` and got a different answer for each: one confirmed exactly
right (`Asset Register`), one confirmed on amounts but not on classification
text (`Disposals`), and one **backwards** — `Depreciation Schedules` and
`Assets > Depreciation` are actually planned-vs-actual, not duplicates at
all, and the original catalog had even mislabeled *which one* was "planned."
`depreciation.json` also carries a known incompleteness in its own `notes`
field: it produces only one leg of what should be a balanced two-line
posting, because the engine has no "expand one row into a balanced
multi-line document" capability yet — flagged rather than worked around,
same as the `Weight_KG` gap in the Materials example.

Run against the real seed data, this produces 39 output tables from 2,192
distinct source rows, all 257 journal entries validate as balanced, every one
of the 90 `ADRC` rows resolves a non-null `COUNTRY` with no `ADDRNUMBER`
collisions, and every dropped/deferred field is accounted for in
`_skipped_fields.csv` — see `output/_run_log.txt` after running it yourself for
the exact numbers.

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

The model and endpoint are configurable via the same `.env`: `ANTHROPIC_MODEL`
(defaults to `claude-opus-5`) and `ANTHROPIC_BASE_URL`, so any
Anthropic-compatible endpoint works — e.g. Zhipu's GLM
(`ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic`,
`ANTHROPIC_MODEL=glm-4.5`, plus your GLM key as `ANTHROPIC_API_KEY`). The
agent prints the active model and endpoint at startup. On a recoverable API
error mid-session (rate limit, status error, network), the failed turn is
rolled back out of the message history entirely — including a turn that died
halfway through a tool call — so the conversation always resumes from a clean
role-alternating state instead of failing every subsequent request.

### What it does

An interactive REPL for talking through a mapping problem the worked examples
didn't cover. It's given five tools and instructed to use them instead of
recalling SAP structure from training data:

| Tool | Grounds the agent in |
|---|---|
| `search_sap_tables` | `sap_target_schema/sap_tables.json` — the real table catalog |
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

---

## 3. Quality control

Two scripts, checking two different questions, both meant to be re-run after
every future change rather than treated as one-off checks:

### `qc_check.py` — are the mapping artifacts internally consistent?

```
python qc_check.py
```

Checks the project's own output against itself: every example CSV parses
cleanly, every spec is valid JSON whose `source_sheet`/`source` columns are
real columns in the seed workbook (a wrong column name doesn't error at
runtime, it just quietly produces nulls — this is the check that would catch
that), every table/field a spec writes to is actually documented in
`sap_tables.json`, `legacy_to_sap_mapping.csv` has no duplicate rows and every
`MIGRATE` table matches a real sheet, every `see X.json`/`see Y.md`
cross-reference in a doc actually points at a file that exists, the engine
itself still runs clean with all validation rules passing, and the golden-value
unit tests in `tests/` all pass (run standalone with
`python -m unittest discover -s tests` from `etl/`). Those tests lock in each
transform's exact semantics — the `signed_amount_pair` `AMBIGUOUS` refusal,
the float-artifact cleanup, the `split_delimiter` fallbacks, the XREF
placeholder format — plus a spec-level end-to-end run (fan-out,
`skip_row_if_null`, constants) against a synthetic sheet, and a fully mocked
smoke test of the agent's tool loop and error rollback (no network, no API
key). Built after
`employees.json` turned out to reference two SAP tables (`PA0009`, `PA0185`)
and several fields (`PA0001-PERSK`, `PA0002-GESCH`/`NATIO`/`FAMST`) that had
never actually been added to the catalog — real gaps from early in this
project, before this check existed, found and fixed by the first run.

### `data_quality_check.py` — is the underlying legacy data itself trustworthy?

```
python data_quality_check.py
```

A different question: not "did we map it right" but "should it be trusted in
the first place." Profiles all 199 sheets in the seed workbook for duplicate
primary keys, fully-duplicate rows, null rates, constant (zero-information)
columns, foreign-key referential integrity (name-matched against every other
sheet's own primary key — 353 relationships / 34,670 values checked clean,
with an honestly-reported gap for the 40 `_ID` columns no name match could
resolve), and date-pair ordering (`Start`/`End`, `Created`/`Updated`). Full
findings, written up in prose rather than left as a raw CSV: see
`sap_target_schema/DATA_QUALITY_FINDINGS.md` — the headline result is that
structural integrity is genuinely strong (zero duplicate keys, zero orphaned
FKs), but ~70 sheets have `Updated_At` timestamps that precede their own
`Created_At`, a real defect that happens not to matter yet only because every
`Created_At`/`Updated_At` pair in this project's specs is already
`NOT_MIGRATED`.
