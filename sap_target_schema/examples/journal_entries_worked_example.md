# Worked Example: `Journal Entries` + `General Ledger` → SAP Universal Journal (`ACDOCA`)

This is the **financial posting** worked example, alongside `Employees` (master data) and
`Sales Orders`/`Sales Order Lines` (transactional header/item with condition pricing). It has
the same header/item shape as Sales Orders on the surface, but the target architecture is
different enough — and consequential enough to get wrong — to warrant its own walkthrough.
Read the other two first if you haven't; this one assumes the "one row → many target
records," "relationship, not a field," and "lookup resolution" patterns are already familiar.

Source: `ERP_Seed_Data (1).xlsx`, sheets `Journal Entries` (257 rows, 19 columns, one row per
posting document) and `General Ledger` (631 rows, 17 columns, the posting lines — 2 to 4 per
journal entry). Field-by-field detail:
[`journal_entries_field_mapping.csv`](./journal_entries_field_mapping.csv) and
[`general_ledger_field_mapping.csv`](./general_ledger_field_mapping.csv).

## The headline lesson: no separate debit/credit columns

`General Ledger` has `Debit_Amount` and `Credit_Amount` as two columns, exactly one non-zero
per line — completely ordinary double-entry bookkeeping shape. **`ACDOCA` has no equivalent
pair of columns.** It stores one signed amount field (`HSL`, amount in company code currency)
plus a separate debit/credit indicator (`DRCRK`, `'S'`/`'H'` — Soll/Haben, German for
debit/credit): a debit line becomes `HSL = +Debit_Amount, DRCRK = 'S'`; a credit line becomes
`HSL = -Credit_Amount, DRCRK = 'H'`. Two source columns collapse into one target field plus a
flag. This is the single most important transform in this table — get the sign convention
wrong and every downstream balance is wrong, silently, since nothing about a wrongly-signed
but still-present number looks broken on inspection.

**Before transforming, validate the invariant the collapse depends on**: exactly one of
`Debit_Amount`/`Credit_Amount` is non-zero per row. It held for all 631 rows checked here, but
treat that as a pre-load data-quality gate for the real migration, not an assumption.

## The second lesson: ACDOCA denormalizes header data onto every line

Classic SAP FI split header attributes (`BKPF`: company code, document type, currency,
posting date) from line items (`BSEG`), requiring a join to answer most reporting questions.
S/4HANA's Universal Journal deliberately reverses this for HANA's columnar engine: `ACDOCA`
**repeats** `RBUKRS` (company code), `BUDAT` (posting date), and `XBLNR` (reference) on every
single line, alongside the header table's own copies in `BKPF`. This is why
`Company_ID`/`Transaction_Date`/`Reference` show up mapped on *both*
`journal_entries_field_mapping.csv` and `general_ledger_field_mapping.csv` — that's not
duplication to clean up, it's how the target table is designed to work. Don't dedupe it away.

## The third lesson: verify before you trust a header field — this one turned out redundant

`Journal Entries` carries its own `Account_ID`, `Debit_Total`, and `Credit_Total` at the
header level, which looks like a summary of the entry. Checking it against the actual lines
(e.g. Journal Entry #1: header `Account_ID=4`; its `General Ledger` lines are accounts `4`,
`23`, `17`) shows the header's `Account_ID` **consistently equals the account of that entry's
first line**, and `Debit_Total`/`Credit_Total` are just the balanced total every entry already
has by construction (0 of 257 entries unbalanced when checked). None of the three has a home
in `BKPF`/`ACDOCA` — standard SAP doesn't store a single "the account" on a multi-line
document, and totals are computed from the lines, not stored. **The lesson generalizes**: when
a legacy header field looks redundant with its own detail lines, verify empirically (as done
here) before deciding to drop it — don't assume redundancy, and don't carry it forward
unexamined either.

## Patterns that repeat from the earlier two examples

- **Business number vs. system-assigned number** — `Entry_Number` ("JE-2025-00001") is the
  same shape of problem as `Employee_Code` and `Order_Number`: SAP assigns `BELNR`
  internally unless external number ranges are configured for the document type.
- **Lookup resolution** — `Account_ID`, `Period_ID`, `Currency`, `Cost_Center_ID` all resolve
  through master data already mapped elsewhere (`Chart of Accounts`, `Accounting Periods`,
  `Currencies`, `Cost Centers`).
- **Workflow field, not migrated** — `Approved_By_User_ID` follows the same reasoning as
  `Foundation > Approval Requests`: SAP document release/approval is rebuilt via workflow
  configuration in the target system, not carried as a stored field.
- **Status is structural, not a value** — simpler than the Sales Orders case, but the same
  idea: every source row here is `'Posted'`, and in SAP that's not a field value at all —
  it's *which table the document lives in* (`BKPF` once posted, `VBKPF` if only parked). If
  the source ever includes unposted entries, they target a different table entirely, not a
  status column on the same one.
- **System-generated audit columns** — `Created_At`/`Updated_At`/`Created_By_User_ID` are
  `NOT_MIGRATED`, as in both earlier examples. This table adds a wrinkle worth flagging on
  its own: `Prepared_By_User_ID` and `Created_By_User_ID` look like they might record the
  same fact under two different names — worth a business conversation before deciding which
  one (if either) is worth preserving as a cross-reference attribute.

## One new, legitimate kind of null

`Cost_Center_ID` is populated on some `General Ledger` lines and null on others (e.g. an
expense line carries a cost center, the matching cash/AR line doesn't). That's correct
accounting behavior — cost objects apply to P&L-relevant postings, not balance-sheet ones —
not a data-quality problem to "fix" by inferring a value. Preserve the nulls as-is.

## Sequencing implication for the ETL

1. Master data this table depends on: `Finance > Chart of Accounts`, `Foundation >
   Accounting Periods`, `Foundation > Currencies`, `Foundation > Cost Centers`, `Foundation >
   Companies`.
2. **Pre-load validation gate**: confirm every `Journal_Entry_ID`'s lines are balanced
   (`sum(Debit_Amount) == sum(Credit_Amount)`) and that each line has exactly one non-zero
   side, *before* attempting the `HSL`/`DRCRK` transform — SAP will reject an unbalanced
   document at posting time anyway, so catching it earlier saves a failed-load cycle.
3. Load `BKPF` header records (dropping the redundant `Account_ID`/`Debit_Total`/
   `Credit_Total`), then their `ACDOCA` line items, repeating the header's denormalized
   fields onto each line per the target design.
4. Treat `Running_Balance` as out of scope for the load entirely — it's a reporting artifact,
   the same category as the already-`NOT_MIGRATED` `Trial Balance`, `Balance Sheet`, `Cash
   Flow`, and `Income Statement` tables in the main mapping. If it's needed post-migration,
   it comes from a report/CDS view over the loaded `ACDOCA` lines, not from loading this
   column anywhere.

This same signed-amount-plus-indicator collapse applies to every other table in
`legacy_to_sap_mapping.csv` that ultimately posts to the Universal Journal —
`Payments`, `Vendor Invoice Lines`, `Customer Invoice Lines`, and the `Assets` acquisition/
disposal/transfer postings all carry the same debit/credit-to-signed-amount transform under
the hood, even though their line items land via different intermediate posting tables
(`REGUP`, `RSEG`, `VBRP`, `ANEP`) before flowing into `ACDOCA`.
