# Worked Example: `Inventory Items` + `Product Catalog` → SAP Material Master

This is the **5th worked example**, and the first one built specifically to
resolve a claim the project itself had gotten wrong. The original schema catalog
(`legacy_to_sap_mapping.csv`) flagged `Purchasing > Inventory Items` and
`Manufacturing > Product Catalog` as duplicates of each other, to be
"consolidated before load" — a reasonable-sounding guess made from table names
and row-level descriptions alone, before any of the actual data had been
checked. It was wrong. Checking it properly is most of what this example is
about, and it's a useful reminder that "these two tables look like duplicates"
deserves the same scrutiny this project has already given "this column
disagrees with that column" (`Employees > Address`, `Customers > Country`).

Source: `ERP_Seed_Data (1).xlsx`, sheets `Inventory Items` (100 rows, 18
columns) and `Product Catalog` (15 rows, 19 columns). Field-by-field detail:
[`inventory_items_field_mapping.csv`](./inventory_items_field_mapping.csv) and
[`product_catalog_field_mapping.csv`](./product_catalog_field_mapping.csv).

## What checking the claim actually found

`Product Catalog` has an explicit `Item_ID` foreign key into `Inventory Items`
— it isn't a second copy of the same 100 items, it's a 15-row **extension** for
the subset that are sellable finished goods, adding attributes `Inventory Items`
doesn't have (list price, currency, sellable/purchasable flags). That's a real
`MARA` (base material data) vs. `MVKE` (sales-org-specific sales view) split,
which is exactly how SAP's own material master is structured — not redundant
data to dedupe.

But two fields genuinely *are* duplicates, and two more looked like duplicates
and turned out not to be, once actually checked row by row rather than assumed
from three samples:

| Field pair | Checked across all 15 overlapping rows | Verdict |
|---|---|---|
| `Product_Name` vs. `Inventory Items > Item_Name` | 0 mismatches | **Confirmed duplicate** — drop `Product_Name`, already migrated via `Item_Name` |
| `Standard_Cost` vs. `Inventory Items > Unit_Cost` | 0 mismatches | **Confirmed duplicate** — drop `Standard_Cost`, already migrated via `Unit_Cost` |
| `Lead_Time_Days` (both sheets) | 15 of 15 mismatches | **Not a duplicate** — two different values on every row |
| `Purchasing > Reorder Points` vs. `Inventory Items > Reorder_Point` | 93 of 93 overlapping rows mismatched, and `Reorder Points` is keyed by (Item, Warehouse) — 90 distinct combos across only 59 items, not one row per item | **Not a duplicate** — genuinely finer-grained (per-warehouse vs. per-item), originally miscategorized as one in the main schema catalog before this check |

The `Lead_Time_Days` case is deliberately handled differently from `Customers >
Country`, and the difference is worth naming: for `Country`, three *independent*
signals (currency, phone country code, company naming) all pointed the same
direction, so there was real evidence one side was noise. For `Lead_Time_Days`,
both sheets are consistently populated with plausible values that simply
differ — no independent corroboration either way. Treating that the same way as
`Country` (pick a winner) would be pattern-matching on the *shape* of the
earlier finding rather than the *evidence* — so this one stays an open question
for the business (procurement lead time vs. a customer-facing promised lead
time are both entirely plausible readings) rather than a resolved decision.

## Patterns that repeat from the other four examples

- **A clean field for once** — `Item_Code` ("ITM-00001") lands on `MARA-BISMT`
  (Old Material Number), a field that exists *specifically* for carrying a
  legacy identifier forward during a migration. Better fit than the
  cross-reference-only treatment `Employee_Code`/`Order_Number` got, similar to
  how `Customer_Code` found a real home in `BU_SORT1`.
- **Master data split across satellite tables** — `Item_Name` lives on `MAKT`
  (a separate, language-dependent text table), not on `MARA` itself, the same
  "one row fans out across several tables" pattern as `Employees` and
  `Customers`.
- **Condition-driven pricing, again** — `List_Price` has no flat field on
  `MVKE`; it's a pricing condition record, the identical finding from Sales
  Orders' `Unit_Price`/`Subtotal`.
- **The recurring ambiguous boolean** — `Is_Active` on both sheets hits the
  same wall as `Customers > Is_Active`: SAP has no single active/inactive flag,
  only block/deletion indicators at different scopes, and the data gives no
  evidence of which one "inactive" would need to mean.

## A genuinely new problem: the engine can't enrich a row from a second sheet

`Weight_KG` belongs on `MARA` (general material data), but it only exists in
`Product Catalog` — and `MARA` rows get built from the `Inventory Items` sheet.
Loading it would mean taking an `Inventory Items`-built `MARA` row for a given
`MATNR` and adding a field to it from a *different* source sheet keyed by the
same `MATNR`. `etl/transform_engine.py` doesn't support that: every spec's
target blocks write fresh rows, keyed independently, with no notion of merging
into a row another spec already produced. This is flagged rather than worked
around — the honest fix is a real engine capability (an "enrich existing row by
key" mode), not a same-target-different-source hack. Left as `dropped` in
`product_catalog_field_mapping.csv` with the reasoning spelled out, the same way
`split_delimiter`/`value_map`/the `row_number_pad` offset were flagged as real
gaps before they became real engine additions.

## Sequencing implication

`Inventory Items` (the base material master) must load before `Product
Catalog` (the sales-view extension), since the sales view's only join key is
the `Item_ID`/`MATNR` the base material mapping establishes — the reverse of
`Employees`-before-`Manager_ID`, but the same "the referenced side has to exist
first" rule.
