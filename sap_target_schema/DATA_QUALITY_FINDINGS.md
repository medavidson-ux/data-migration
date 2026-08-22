# Data Quality Findings: the Legacy Source Data

This is a different question from everything else in `sap_target_schema/` and
`etl/`. Those ask "is our *mapping* of the data correct?" This asks "is the
*data itself* trustworthy?" — checked systematically across all 199 sheets in
`ERP_Seed_Data (1).xlsx` with `etl/data_quality_check.py`, rather than only the
handful of tables this project happened to investigate by hand along the way
(`Customers.Country`, `Locations.Address`, the Materials/Assets duplicate-table
checks). Re-run it any time with `python etl/data_quality_check.py` — it
writes `etl/output/_data_quality_report.csv` (one row per issue) and
`_data_quality_summary.txt` (counts + the full unresolved-FK list).

## The good news, with real numbers behind it

- **Zero duplicate primary keys, zero fully-duplicate rows, anywhere in 199 sheets.**
- **Zero orphaned foreign keys** across every relationship the script could
  identify by name — 353 `_ID` columns matched to a real primary key
  elsewhere, 34,670 non-null values checked, all resolved. Every spot-check
  done by hand earlier in this project (e.g. `Product Catalog.Item_ID` always
  existing in `Inventory Items`) generalizes: this dataset's key structure is
  genuinely sound.

**Caveat on that second point, stated plainly rather than glossed over**: the
check only covers `_ID` columns whose name exactly matches another sheet's own
primary key name. **40 FK-shaped columns couldn't be resolved this way** —
things like `Employees.Manager_ID` (plausibly `Employees.Employee_ID`, but not
named that), `Purchase Orders.Requisition_ID`, four different sheets'
`Period_ID`, two sheets' `Tax_ID`. These are **not checked, not confirmed
clean** — just outside what a naming-based approach can resolve safely. A
generic name like `Category_ID` means a different thing on different sheets;
guessing which target it means and being wrong would manufacture a false
"orphaned FK" finding that looks like real evidence. The full list is in
`_data_quality_summary.txt`. If you need those relationships verified, they
need to be resolved by hand (the way `Manager_ID` already was, correctly, in
the `Employees` worked example) or with real schema documentation, not by
loosening this check's matching rule.

## The real finding: `Updated_At` before `Created_At`, pervasively

**78 instances across roughly 70 of the 199 sheets** have rows where
`Updated_At` is chronologically *before* `Created_At` — a logical
impossibility, not a judgment call like `Customers.Country` was. Counts run
from 1 row (`Employees`) to 76 rows (`Communication Logs`); this isn't a
handful of edge cases, it's systemic across the dataset. A handful of sheets
also have `Start_Date` after `End_Date`: `Campaigns` (2 rows), `Customer
Contracts` (3), `Vendor Contracts` (3), `Resource Allocation` (7).

**Why this doesn't block anything currently built**: every `Created_At`/
`Updated_At` pair in every worked example and spec in this project is already
marked `NOT_MIGRATED` — SAP stamps this metadata itself, so the defect never
reaches a mapping decision. But it's worth flagging on its own terms: if
anything downstream ever needs to trust these timestamps (audit review, a
future table's mapping that does try to use them), don't assume they're
sequential. The `Start_Date`/`End_Date` cases are more immediately relevant —
none of the four affected tables have been mapped yet, so whoever maps
`Campaigns`, `Customer Contracts`, `Vendor Contracts`, or `Resource Allocation`
next should check this before trusting date-range logic on those tables.

## This is a single-company dataset

`Company_ID` is a constant value on **71 of 199 sheets** — every table that
carries it has exactly one distinct value. Practically: every "needs a real
Plant/Sales-Org/Valuation-Area determination" `APPROX` flag scattered across
the worked examples (Sales Orders' `VKORG`, Materials' `WERKS`/`BWKEY`,
Assets' plant assignment, …) is a structurally correct concern for a *general*
migration approach, but in *this specific dataset* there's only ever one
company to resolve against — a real simplification for whoever actually runs
this migration, distinct from the underlying modeling problem still being
correct to flag.

## Two patterns already suspected, now confirmed at full scale

- **`Is_Active` is constant (always `True`) on 41 sheets.** This project's
  worked examples flagged this exact ambiguity by hand, repeatedly, every time
  an `Is_Active` column came up (`Customers`, `Inventory Items`, `Locations`,
  `Asset Categories`, `Depreciation Methods`) — "no evidence of what
  'inactive' would mean, since nothing in the data is ever inactive." This
  profiling run confirms that wasn't a coincidence of which tables got mapped
  first: it's a dataset-wide characteristic.
- **`Currency` is constant (always `JOD`) on 25 sheets** — the same finding
  that mattered in the `Customers.Country` investigation (where uniform
  currency was one of the corroborating-but-weak signals the AI agent
  correctly argued shouldn't carry too much weight on its own), confirmed here
  as a dataset-wide pattern rather than something specific to `Customers`.

## Columns that are entirely empty

A number of columns are **100% null** — not high-null, *entirely* empty:
`Work Orders.Asset_ID`/`Maintenance_Type_ID` (87/87), `Stock
Movements.Lot_Number`/`Serial_Number` (274/274), `Leave
Requests.Time_Log_ID`/`Comments` (120/120), `Project Budgets.Task_ID` (60/60),
several hierarchy self-references (`Chart of Accounts.Parent_Account_ID`,
`Item Categories.Parent_Category_ID`, `Work Breakdown.Parent_WBS_ID`), among
others — full list in `_data_quality_report.csv` (`issue_type=NULLS`,
`detail` shows `100%`). These aren't necessarily bugs — a synthetic dataset
plausibly generates some optional fields as always-empty by construction — but
they're worth a direct check before building a mapping that assumes a column
is populated. **`Work Orders.Asset_ID`** is the one worth a second look before
anyone maps that table: an asset-tracking work order with a wholly-empty asset
reference is either a deliberately-unused column or a sign the table isn't
what its name suggests.

## Cost centers on GL lines: 71% null, and that's correct (one anomaly)

The transform engine's null-rate report (a `_run_log.txt` section added while
hardening the engine) flagged `ACDOCA.KOSTL` (cost center) as null in 448 of
631 rows — 71%. Investigated, and the nulls trace one-for-one to null
`Cost_Center_ID` values in the `General Ledger` source sheet; the pattern
behind them matches SAP's own rules almost perfectly:

- **Every Revenue and Expense account** (Sales Revenue, COGS, Salaries, Rent,
  Utilities, Depreciation, Marketing, Freight, Professional Fees, Insurance)
  has a cost center on **all** of its lines — required in SAP, where P&L
  postings need a cost-object assignment.
- **Every balance-sheet account** (Bank, AR, AP, VAT receivable/payable,
  accruals, payables, PP&E, accumulated depreciation, finished-goods
  inventory) has a cost center on **zero** of its lines — also correct, since
  SAP rejects cost centers on balance-sheet postings.

No account appears on both lists — the split is perfectly account-exclusive,
so no mapping change is needed; the transform faithfully preserves source
semantics that were already SAP-shaped.

**The one anomaly worth a business follow-up: Account 6.** "Inventory — Raw
Materials" (an Asset account) carries a cost center on all 32 of its lines,
while its sibling "Inventory — Finished Goods" (Account 8) carries none on
any — the *only* balance-sheet account in the dataset with populated cost
centers. In SAP, inventory accounts post via material/valuation class rather
than cost center, so Account 6's values are unusual, and the inconsistency
between the two inventory accounts suggests either a legacy data-entry quirk
or an account-type mislabel in the source. It doesn't block the prototype,
but confirm with the business whether those 32 rows are correct before a real
load.

## Findings surfaced while mapping the purchasing cycle (Vendors, POs, Contracts)

Three issues found by the mapping work itself, recorded here alongside the
profiling findings:

- **Two vendor contracts have no vendor at all.** `Vendor Contracts` rows 1
  and 3 (`VC-2025-00001`, `VC-2025-00003`) have a **null `Vendor_ID`** — a
  contract with no counterparty. The name-based FK check didn't flag them
  because it drops null FK values before matching: a null reference is
  currently *invisible* to referential-integrity checking, not confirmed
  clean. A null FK on a supposedly-mandatory relationship is arguably a
  stronger finding than an orphaned value; a future version of
  `data_quality_check.py` should report null FKs on non-nullable-looking
  relationships separately. The transform emits these rows with a null
  `EKKO.LIFNR` — visible in the run log's null-rate section — and they'd be
  rejected at load.
- **Backward contract validity dates confirmed at row level.** The 3
  `Vendor Contracts` rows flagged by the DATE_ORDER check are
  `VC-2025-00007`, `VC-2025-00012`, `VC-2025-00014` (3 of 20 rows;
  `Customer Contracts` has the same defect in 3 of 25 rows). The
  `vendor_contracts` spec passes `KDATB`/`KDATE` through verbatim rather than
  guessing which date is wrong; SAP would reject these at load, so they need
  business review of the source records.
- **Customer and vendor staging keys collide in BUT000.** Both
  `customers_core` and `vendors_core` emit `PARTNER` = the raw legacy
  `Customer_ID`/`Vendor_ID` (1..40 and 1..25), so customer 1 and vendor 1
  land on the same Business Partner number. This is an open *numbering
  strategy* decision (disjoint odd/even series, offset ranges, or prefixed
  staging keys — every FK reference to PARTNER/KUNNR/LIFNR would need the
  same treatment consistently), deliberately not guessed in the specs. Until
  decided, BUT000 output must not be treated as loadable.

## How to use this alongside the rest of the project

This complements, rather than duplicates, `etl/qc_check.py` (which checks the
mapping artifacts themselves). Read this document *before* mapping a new
table, the way the `Customers`/`Materials`/`Assets` worked examples did after
the fact — checking `_data_quality_report.csv` for the sheet you're about to
map costs one filter operation and would have caught the `Country` mismatch,
the `Reorder Points` granularity difference, and the `Depreciation
Schedules`/`Depreciation` plan-vs-actual split *before* they needed a manual
investigation to discover.
