# SAP S/4HANA Target Schema — Migration Deliverable

Generated for the legacy → SAP migration project. This is the **end-state schema**: what
the data should look like once it lands in SAP, derived table-by-table from your actual
source catalog (`ERP_Schema_v2_Corrected (1).csv`, 199 tables across 10 domains) rather
than a generic template.

## Files

| File | Purpose |
|---|---|
| `legacy_to_sap_mapping.csv` | One row per legacy table (199 total). Says whether it migrates, and if so, to which standard SAP table(s). **This is the primary driver for your AI-assisted ETL** — a per-table transform/routing rule set. |
| `sap_tables.json` | Catalog of every unique SAP target table referenced by the mapping (119 tables). Machine-readable: description, module, table type, and key fields with SAP data types. |
| `sap_tables.csv` | Same catalog, flattened to one row per field — easier to skim or load into a spreadsheet. |
| `examples/employees_field_mapping.csv` + `examples/employees_worked_example.md` | Worked column-level mapping for a **master-data** table (`Employees` → SAP HCM infotypes): lookups, field splits, relationship modeling, system-generated fields. |
| `examples/sales_orders_header_field_mapping.csv` + `examples/sales_order_lines_field_mapping.csv` + `examples/sales_orders_worked_example.md` | Worked column-level mapping for a **transactional header/item** table (`Sales Orders`/`Sales Order Lines` → SAP SD documents): condition-based pricing, partner determination, document flow, multi-field status. |
| `examples/journal_entries_field_mapping.csv` + `examples/general_ledger_field_mapping.csv` + `examples/journal_entries_worked_example.md` | Worked column-level mapping for a **financial posting** table (`Journal Entries`/`General Ledger` → SAP `BKPF`/`ACDOCA` Universal Journal): debit/credit columns collapsing into one signed amount + indicator, header-field denormalization onto every line, verifying a header field is genuinely redundant before dropping it. |

## Scope decisions (confirmed with you before building)

- **Target generation: S/4HANA**, not classic ECC. That means Business Partner (`BUT000`)
  replaces separate Customer (`KNA1`) / Vendor (`LFA1`) masters, and the Universal Journal
  (`ACDOCA`) replaces `BSEG`/`BSIS`/`BSAS`/`COEP` as the single financial line-item table.
  Classic tables (`BKPF`, `EKKO`, `VBAK`, the `PA0xxx` HR infotypes, etc.) are still shown
  where S/4 keeps them as compatibility views (CDS-based) — the field shapes still apply.
- **Only tables with a real SAP target are mapped.** 135 of 199 legacy tables (68%) migrate
  to a standard SAP table. The other 64 are flagged `NOT_MIGRATED` with a reason — mostly:
  - **Analytics/forecast/report tables** (Churn Analysis, OEE Metrics, Sales Forecasts,
    Balance Sheet, Trial Balance, etc.) — these are *derived* from migrated transactional
    data via SAP reporting/CDS views, not stored as their own source table in SAP.
  - **Front-office CRM/CX objects** (Leads, Opportunities, Campaigns, Service Tickets'
    close cousins) — these belong to SAP Sales/Service/Marketing Cloud, not core S/4HANA.
  - **Security/workflow scaffolding** (Users, Roles, Approval Requests) — rebuilt natively
    in SAP (SU01/PFCG/Business Workflow), not data-migrated.
  - **No standard SAP equivalent exists** (Risk Register, Lessons Learned, Checkouts) —
    these stay as custom (Z) tables or a bolt-on tool if you still need them.

## Confidence flag

Each `MIGRATE` row is marked `HIGH` or `APPROX` in the `confidence` column:

- **HIGH** — canonical, well-known standard SAP table for that business object.
- **APPROX** — a defensible best-fit mapping, but the exact table/field can vary by SAP
  release, industry solution, or configuration (e.g. "Milestones", "Vendor Ratings",
  "Capacity Plans"). **Verify these against the actual target system's data dictionary
  (transaction `SE11`) before building load programs** — don't treat them as final.

## Known duplicates in your source data

Several legacy tables across different domains map to the *same* SAP target — this mirrors
real duplication in the source (e.g. `Finance > Asset Register` and `Assets > Assets` both
→ `ANLA`; `Purchasing > Inventory Items` and `Manufacturing > Product Catalog` both →
`MARA`/`MARC`/`MBEW`; `CRM > Customer Profiles` and `Sales > Customers` both → `BUT000`).
The mapping notes flag every case. **Resolve these at the data-quality/dedup step of your
ETL, before load** — don't run two independent loads into the same SAP object.

## Field lists: what's included and what isn't

`sap_tables.json`/`.csv` list the **primary key plus the handful of most important standard
business fields** for each table — not the full DDIC field list (real SAP tables often carry
50–150+ fields, many client-specific). Treat this as a structural skeleton for designing your
transform logic and target validation rules, and pull the complete, authoritative field list
for whichever fields you actually need to populate directly from the target system (`SE11`/
`SE16`) or your SAP Data Dictionary access, since exact field sets vary by release and by
what's been custom-extended in your specific SAP instance.

## Suggested use in the AI-assisted ETL

1. **Routing** — for each source sheet in `ERP_Seed_Data (1).xlsx`, look up its row in
   `legacy_to_sap_mapping.csv` by (`component`, `legacy_table`) to get `status` and
   `sap_target`. Skip `NOT_MIGRATED` rows (or redirect them to a reporting/staging area).
2. **Target shape** — for each `sap_target` table, pull its field list from `sap_tables.json`
   to drive column-mapping/transformation prompts (e.g., "map these legacy columns onto
   `ANLA`'s key + business fields") and to validate output types (`DATS`, `CURR`, `CHAR(n)`, …).
3. **Dedup pass** — before loading, merge legacy tables that share a target (see the notes
   column) so you don't create duplicate master records in SAP.
4. **Confidence-based review** — route `APPROX` mappings to a human-in-the-loop review step;
   auto-proceed `HIGH`-confidence ones.

## Implementation: `../etl/`

The `etl/` folder (sibling to this one) turns the three worked examples above into a
runnable prototype: `etl/transform_engine.py` executes the same transform patterns against
the real seed data and produces SAP-shaped staging tables, and `etl/agent/` is an
interactive Claude-powered agent that extends the same patterns to new tables, grounded in
this schema catalog and tested against real data rather than recalled from memory. See
`etl/README.md`.
