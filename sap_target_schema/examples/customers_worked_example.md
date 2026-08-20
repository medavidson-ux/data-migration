# Worked Example: `Customers` → SAP Business Partner

This is the **4th worked example**, and the first one that started as a live AI
agent session rather than a hand-authored mapping — the address portion
(`Billing_Address`/`Shipping_Address`/`Country`) was decided and tested through
several real conversations with `etl/agent/sap_migration_agent.py`, documented in
full in `etl/agent/mapping_decisions.csv` and `etl/README.md` § "A real session,
not a demo". This document completes the table by mapping the remaining columns
the agent sessions didn't cover, following the same discipline.

Source: `ERP_Seed_Data (1).xlsx`, sheet `Customers` (40 rows, 20 columns).
Field-by-field detail: [`customers_field_mapping.csv`](./customers_field_mapping.csv).

## The core shift: one master-data row → one BP spread across six tables

Like `Employees` fanning out into HCM infotypes, a `Customers` row fans out into
the Business Partner model's own scatter of tables — but wider, because BP
splits identity, communication, and sales-area data into separate concerns:

| Target table | Holds |
|---|---|
| `BUT000` | Core identity: partner number, organization name, search term |
| `KNB1` | Company-code-specific data (posting block, deletion flag) |
| `KNVV` | Sales-area data: classification, currency, payment terms |
| `BUT0ID` | Industry classification |
| `BUT0TX` | Tax numbers |
| `ADRC` / `ADR6` / `ADR2` | Address, e-mail, phone (already decided for `ADRC`) |
| `BUT050` | Relationships — account manager, same pattern as `Manager_ID` |

## Patterns that repeat from the other three examples

- **A clean field this time, not a workaround** — `Customer_Code` ("CUS-0001")
  hits the same shape of problem as `Employee_Code`/`Order_Number` (a legacy
  business key vs. SAP's own assigned number), but for once there's a proper
  standard home for it: `BUT000-BU_SORT1` (Search Term) is designed exactly for
  a human-readable lookup key, so it doesn't need external number-range
  configuration the way the earlier examples' IDs did.
- **Relationship, not a field** — `Account_Manager_ID` is a flat FK in the
  source, same as `Manager_ID` (`Employees`) and `Sales_Rep_ID` (`Sales
  Orders`). It becomes a `BUT050` relationship to an employee, not a column.
- **A duplicate to flag, not map** — `Contact_Person` is free text on this row,
  but `CRM > Contact Persons` is a whole separate legacy table already routed to
  its own `BUT050` relationship. Mapping the text field directly would create a
  second, disconnected representation of the same fact — dropped here with a
  pointer to the real source instead.
- **System-generated audit columns** — `Created_At`/`Updated_At`/
  `Created_By_User_ID`, same as every other example.

## What's different here: real ambiguity about which field is even right

Three fields don't have one obviously-correct target, which is a different kind
of problem than "the target exists but needs a lookup":

- **`Customer_Type`** (SME/Corporate/Government/Distributor) could reasonably be
  `KNVV-KUKLA` (Customer Classification) *or* `BUT000-BU_GROUP` (BP Account
  Group, which drives number ranges and screen layout and is normally set at
  creation, not treated as a business attribute afterward). Which one is right
  depends on how the target system actually uses the distinction — flagged
  `APPROX` for that reason, not for lack of a lookup.
- **`Is_Active`** is a boolean, but SAP has no matching boolean — inactivity is
  expressed through block/deletion indicators (`KNB1-LOEVM`, a usually-permanent
  deletion mark, vs. `KNB1-SPERR`, a reversible posting block). The source data
  doesn't say which the legacy system meant, and picking the wrong one is a real
  functional difference, not a cosmetic one.
- **`Email`/`Phone_Number`** land in address-linked communication tables
  (`ADR6`/`ADR2`), keyed by `ADDRNUMBER` — but the address mapping already
  produced *two* `ADDRNUMBER` series (billing, shipping). A customer's own
  contact info typically belongs on a third, "standard" address, not either of
  those — an open item the address work didn't need to resolve but this one does.

## Two table names carried at lower confidence than usual

`BUT0ID` (industry) and `BUT0TX` (tax numbers) are marked `APPROX` not because
the *concept* is uncertain, but because these are less universally-referenced
table names than `BUT000`/`ADRC`/`KNVV` — confirm they exist as named in the
target release via `SE11` before relying on them, same caveat the main schema
catalog gives for any `APPROX`-confidence entry.

## Sequencing implication

Company/currency/payment-term lookups first (same master data every other
example already depends on), then `HR > Employees` again (for
`Account_Manager_ID`, mirroring `Sales_Rep_ID`'s dependency), then the BP core
(`BUT000`) before its satellites (`KNB1`, `KNVV`, `BUT0ID`, `BUT0TX`) — and only
after the address work, since e-mail/phone need to know which `ADDRNUMBER` they
attach to.
