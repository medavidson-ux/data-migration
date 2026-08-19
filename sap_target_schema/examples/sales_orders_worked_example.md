# Worked Example: `Sales Orders` + `Sales Order Lines` → SAP SD Documents

This is the **transactional** counterpart to the `Employees` worked example. Where
`Employees` showed a master-data record fanning out into dated infotypes, this one shows
a **header/item transaction** with a genuinely different set of transform problems:
condition-based pricing, partner determination, document flow, and status that's tracked
as multiple orthogonal indicators rather than one field. Read the `Employees` example
first if you haven't — this one builds directly on two of its patterns.

Source: `ERP_Seed_Data (1).xlsx`, sheets `Sales Orders` (70 rows, 20 columns) and
`Sales Order Lines` (192 rows, 18 columns) — a classic header/item pair, `Sales_Order_ID`
as the join key. Field-by-field detail:
[`sales_orders_header_field_mapping.csv`](./sales_orders_header_field_mapping.csv) and
[`sales_order_lines_field_mapping.csv`](./sales_order_lines_field_mapping.csv).

## The core shift: two flat tables → a header/item pair plus three satellite structures

`VBAK` (header) and `VBAP` (item) map fairly directly onto the source's own header/item
split — that part is the easy part. What the flat source *doesn't* show is that SAP spreads
several attributes you'd expect to find on the order itself into separate, purpose-built
tables:

| Target table | Holds | Legacy columns that land here instead of VBAK/VBAP |
|---|---|---|
| `VBAK` / `VBAP` | Core header/item data | Most fields, plus `NETWR`, `AUDAT`, `MATNR`, `KWMENG`, etc. |
| `VBKD` | Payment terms, currency, incoterms | `Payment_Terms` |
| `VBPA` | Partner functions (sold-to, ship-to, salesperson) | `Sales_Rep_ID`, part of `Delivery_Address` |
| `VBFA` | Document flow (links to preceding/subsequent docs) | `Quote_ID` |
| `VBUK` / `VBUP` | Header/item processing status | `Status` |
| `KONV` (S/4: `PRCD_ELEMENTS`) | Pricing condition results | `Unit_Price`, `Subtotal`, `Discount_Amount`/`Discount_Percent`, `Tax_Amount` |

Same lesson as `Employees`, restated for transactions: **the ETL's output unit is not
"one row per input row" — it's one header record, N item records, and a scatter of
satellite records across five more tables per order.**

## Patterns that repeat from the `Employees` example

- **Lookup resolution** — `Customer_ID`, `Item_ID`, `UOM_ID`, `Warehouse_ID`, `Tax_Code_ID`
  all resolve through the same master-data lookups already mapped elsewhere in
  `legacy_to_sap_mapping.csv`. `Sales_Rep_ID` specifically resolves through the
  **`Employees` mapping itself** — a concrete example of one worked example feeding another.
- **Relationship, not a field** — `Quote_ID` (link to the originating quotation) isn't a
  column on `VBAK`; it's a `VBFA` document-flow record connecting two documents. Structurally
  identical to `Manager_ID` in `Employees`. `Sales_Rep_ID` and part of `Delivery_Address` are
  the same pattern again, via `VBPA` partner functions.
- **System-generated audit columns** — `Created_At`, `Updated_At`, `Created_By_User_ID` are
  `NOT_MIGRATED` for the same reason as in `Employees`: SAP stamps these automatically.

## Patterns unique to this transactional shape

1. **Pricing is condition-driven, not stored** — this is the biggest new problem here.
   `Unit_Price`, `Subtotal`, `Discount_Amount`/`Discount_Percent`, and `Tax_Amount` all look
   like plain numeric columns in the source, but standard SD has **no flat field for most of
   them**. SAP's pricing procedure computes order value step-by-step from condition records
   (`KONV`, redesigned as `PRCD_ELEMENTS` in S/4HANA); a header discount or a line's unit
   price is one step's *result*, not a column you write to. To carry forward historical
   pricing exactly as it was, you generally have to **create condition records that
   reproduce the legacy numbers** (often via a manual/imported condition type), rather than
   populating a field — a materially different load mechanic than everything else in this
   table. `NETWR` (net value) and `MWSBK` (tax amount) are the two genuine stored exceptions
   on `VBAK`/`VBAP`, so anchor validation there.

2. **Status is several independent fields, not one** — the legacy `Status` column
   ("Invoiced", "Shipped") reads as one enum, but SAP tracks delivery status, billing
   status, and overall processing status **separately** (`VBUK` for the header, `VBUP` per
   item), normally maintained automatically as the order actually flows through delivery
   and billing. Translating one legacy status value into the right combination of these
   indicators — especially for orders migrated *mid-process* — needs explicit business
   rules and careful cutover review, not a simple value-lookup table.

3. **A derived rollup needs historical reconstruction** — `Quantity_Delivered` on the line
   item is, in live SAP, a rollup of linked delivery items (`LIPS-LFIMG`) reachable via
   `VBFA`, not a static field. For closed orders this may not matter for reporting purposes,
   but for **open/partially-delivered orders carried into cutover**, getting this number
   right requires either reconstructing the `VBFA` links to real delivery documents or
   accepting it as a one-time reporting snapshot rather than a live-maintained field.

4. **Denormalization to clean up** — `Company_ID` appears on both the header and every line
   in the source, but `VBAP` has no company-code field of its own (it's inherited from the
   header via the sales-organization assignment). Drop it at the item level rather than
   trying to map it twice.

## Sequencing implication for the ETL

Given the cross-table satellite structures and the position/employee dependency inherited
from `Sales_Rep_ID`, a workable load order is:

1. All referenced master data: `Sales > Customers` (→ Business Partner), `Purchasing >
   Inventory Items` / `Manufacturing > Product Catalog` (→ Material), `Inventory > Units of
   Measure`, `Inventory > Warehouses`, `Foundation > Payment Terms`, `Foundation > Tax
   Codes`, `Foundation > Currencies`.
2. `HR > Employees` (needed to resolve `Sales_Rep_ID` into a valid `PERNR` for the
   salesperson partner function) and the SD org structure (`TVKO` sales-org-to-company-code
   assignment).
3. `VBAK` header records, immediately followed by their `VBKD` (payment terms/currency) and
   `VBPA` (sold-to/ship-to/salesperson partner) satellite records — these three should load
   as one transactional unit per order, not as separate passes.
4. `VBAP` item records, plus the condition records (`KONV`) that reproduce each line's
   pricing.
5. A final pass to reconstruct `VBFA` document-flow links (`Quote_ID` → order, and order →
   any already-existing delivery/billing documents if this is a partial historical load).

Apply the same lens — direct copy / lookup resolution / relationship-not-a-field /
condition-driven-not-a-field / status-as-multiple-indicators — to the other header/item
transaction tables in `legacy_to_sap_mapping.csv` (Purchase Orders → `EKKO`/`EKPO`, Vendor
Invoices → `RBKP`/`RSEG`, Production Orders → `AFKO`/`AFPO`, and so on): they share the same
shape, and several — Purchase Orders and Invoices especially — are also priced via
condition records, so the pricing lesson from this example transfers directly.
