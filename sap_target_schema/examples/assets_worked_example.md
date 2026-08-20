# Worked Example: `Assets` → SAP Asset Accounting (FI-AA)

This is the **6th worked example**, and the one with the most ground to cover:
19 legacy tables in the `Assets` domain, plus three more in `Finance` flagged
against it as possible duplicates. It follows directly from the `Materials`
example's lesson — check duplicate claims against real data, don't inherit them
— and this time the results split both ways: one flag confirmed exactly right,
one confirmed with an important caveat, and one **backwards**.

Source: `ERP_Seed_Data (1).xlsx`, sheets `Assets` (60 rows), `Locations` (10
rows), `Depreciation` (318 rows), `Depreciation Schedules` (293 rows), `Asset
Categories` (8 rows), `Depreciation Methods` (4 rows), plus the three
Finance-domain tables checked below. Field-by-field detail:
[`assets_field_mapping.csv`](./assets_field_mapping.csv),
[`locations_field_mapping.csv`](./locations_field_mapping.csv),
[`depreciation_field_mapping.csv`](./depreciation_field_mapping.csv),
[`asset_config_field_mapping.csv`](./asset_config_field_mapping.csv).

## Checking three "duplicate" flags: one right, one right-with-a-catch, one backwards

The original schema catalog flagged `Finance > Asset Register`, `Finance >
Depreciation Schedules`, and `Finance > Disposals` as duplicates of
`Assets`-domain tables, "consolidate before load" — the same kind of
table-name-level guess that turned out wrong for `Product Catalog`. This time,
checking every overlapping row (not a sample) gave three different answers:

| Claim | Checked | Verdict |
|---|---|---|
| `Finance > Asset Register` duplicates `Assets > Assets` | Joined on `Asset_ID`, compared 6 shared fields across all 60 rows | **Confirmed exactly right** — 60/60 match on every field. Drop `Asset Register`, consolidate onto `Assets`. |
| `Finance > Disposals` duplicates `Assets > Asset Disposals` | Joined on `Asset_ID`, compared 4 quantitative fields across all 4 rows | **Confirmed on amounts, wrong on classification** — dates/NBV/gain-loss/proceeds match 4/4, but `Disposal_Type`/`Reason` use a different vocabulary than `Disposal_Method`/`Disposal_Reason` for the *same* disposal event (one row: "Scrap"/"Beyond economic repair" vs. "Trade-in"/"Sold to third party"). Consolidate the amounts; the classification text needs a business decision on which side is authoritative, not a silent pick. |
| `Finance > Depreciation Schedules` duplicates `Assets > Depreciation` | Compared row counts, grain, and a joined-by-asset spot check | **Backwards.** 293 rows at *annual* grain (`Schedule_Year`, `Is_Posted` flag) vs. 318 rows at *monthly* grain (`Period_Date`, `GL_Journal_Reference`, `Posted_By`/`Posted_At`) — these are **planned** vs. **actual** depreciation, a real and necessary distinction in SAP (`ANLP` planned periodic values vs. `ACDOCA` actual postings), not one table copying another. The original catalog even had the *target* backwards: it called `Assets > Depreciation` "planned periodic values (ANLP)," which actually describes `Depreciation Schedules`. Both tables need migrating, to different targets — see below. |

The `Asset Register` case is worth sitting with for a moment: it would have
been easy, after finding `Product Catalog` wasn't a duplicate, to start
assuming *no* flagged duplicate is real. It's not "duplicates are never real,"
it's "check every one" — and this round two of the three legacy-catalog notes
needed fixing while one didn't.

## The same city/address problem as `Customers`, opposite conclusion

`Locations.Address` ("33 King Abdullah II St, Mafraq") is a free-text
street+city string, and its embedded city disagrees with the declared `City`
column on 9 of 10 rows — structurally the identical shape of problem as
`Customers > Country`. But a third, independent field settles it the other way
this time: `Location_Name` ("Zarqa Plant — Line A") embeds the same city word
as `City` on **10 of 10** rows. Here the standalone classification column is
the reliable one and the free-text address is the noisy one — the reverse of
`Customers`, and a useful reminder that "the structured field vs. the
free-text field" isn't a rule with a fixed winner; it's evidence, checked each
time. (`Locations.Country` was checked too, for completeness — it agrees with
`City` on 10/10 rows, so it's actually usable directly here, another contrast
with `Customers.Country`.)

The resolution combines both fields rather than picking one wholesale:
`ADRC-STREET` still comes from parsing `Address` (only its trailing city token
was shown unreliable, not the street portion), while `ADRC-CITY1`/`COUNTRY`
come from the separately-verified `City` column.

## A genuinely new problem: one row needs to become two balanced posting lines

`Depreciation.Depreciation_Amount` is a single number per row. A real SAP
depreciation posting is double-entry, like every other financial posting in
this project (`Journal Entries`/`General Ledger`) — debit depreciation
expense, credit accumulated depreciation. But unlike `General Ledger`, which
had *two* source columns (`Debit_Amount`/`Credit_Amount`) to collapse into one
signed field, this source has only *one* column, and a complete posting needs
*two* target lines with opposite signs. `transform_engine.py` has no "expand
one source row into a balanced multi-line document" capability — every
transform so far maps one source row to one row per target block. Flagged as a
real gap in `depreciation_field_mapping.csv` rather than worked around by
inventing an offsetting account or line — the same discipline as the
`Weight_KG` cross-sheet-enrichment gap found in the `Materials` example.

## Patterns that repeat from the other five examples

- **Business codes with a real standard home, twice** — `Asset_Code` lands on
  `ANLA-INVNR` (Inventory Number), a field that exists specifically to carry a
  physical/legacy asset tag forward, the same shape of good outcome as
  `Customer_Code`→`BU_SORT1` and `Item_Code`→`MARA-BISMT`.
- **Values that live in a posting, not the master record** — `Purchase_Cost`,
  `Current_Book_Value`, and `Accumulated_Depreciation` all turn out to be
  either transaction-derived or calculated running balances, not flat master
  fields — the same finding as `Sales Orders > Unit_Price`/`Subtotal` and
  `General Ledger > Running_Balance`. Loading historical asset values like
  these is normally its own special one-time process in SAP (`AS91`/legacy
  data transfer), not an ordinary field mapping — worth flagging for whoever
  runs the actual cutover, not just noting as "dropped."
- **Status is structural, not a field** — `Assets.Status` ("In Storage", "In
  Use") hits the same wall as `Sales Orders.Status`: SAP expresses this via a
  deactivation date and posting-driven indicators, not one column.
- **The recurring ambiguous boolean** — every `Is_Active` column encountered
  in this project so far (`Customers`, `Inventory Items`, `Locations`, `Asset
  Categories`, `Depreciation Methods`) hits the identical wall: no single SAP
  flag, only block/deletion indicators at different scopes, and every single
  instance in this dataset is uniformly `True`, giving zero evidence of what
  "inactive" would even mean. This has come up often enough it's worth
  treating as a standing rule for this dataset, not a one-off surprise.
- **Relationship, not a field, again** — `Assigned_To_ID` on `Assets` is the
  same shape as `Sales_Rep_ID`/`Manager_ID`/`Account_Manager_ID`.

## Sequencing implication

Config first (`Depreciation Methods`, `Asset Categories` — though both are
mostly customizing-linkage open items, not flat-field loads), then `Locations`
(needed for `Assets.Location_ID`), then `Assets` itself, then the posting
history (`Depreciation` for actuals, `Depreciation Schedules` for the planned
side) — each of which needs the asset master to exist first. `Asset Register`
and `Disposals` don't get their own load step at all; they're confirmed
duplicates of `Assets` and `Asset Disposals` respectively.
