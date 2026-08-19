# Worked Example: `Employees` → SAP HCM Infotypes

This is a column-by-column mapping for one legacy table, meant as a **pattern your AI ETL
step can replicate** for the other 134 `MIGRATE` tables in `legacy_to_sap_mapping.csv`. It's
also the single most structurally revealing example in the dataset — HR exposes almost every
kind of transform challenge you'll hit elsewhere (lookups, splits, derived fields, relationship
modeling, system-generated metadata), so it's worth reading even if HR isn't your first
migration wave.

Source: `ERP_Seed_Data (1).xlsx`, sheet `Employees` (29 columns, one flat row per employee).
Field-by-field detail: [`employees_field_mapping.csv`](./employees_field_mapping.csv).

## The core shift: one flat row → many dated infotype records

The legacy table is one row per employee. SAP HCM has **no equivalent single "employee"
table** — the record set in `sap_target_schema/README.md` (`PA0000`, `PA0001`, `PA0002`,
`PA0006`, `PA0008`, `PA0009`, `PA0105`, `PA0185`) is a family of **infotypes**, each with its
own validity period (`BEGDA`/`ENDDA`), so one legacy `Employees` row becomes roughly:

| Target infotype | Holds |
|---|---|
| `PA0000` Actions | Hiring/termination events, employment status |
| `PA0001` Org Assignment | Company, org unit, position, employee subgroup |
| `PA0002` Personal Data | Name, DOB, gender, nationality, marital status |
| `PA0006` Addresses | Structured address |
| `PA0008` Basic Pay | Pay scale group |
| `PA0009` Bank Details | Bank account, IBAN |
| `PA0105` Communication | Phone, e-mail (one record per subtype) |
| `PA0185` Personal IDs | National ID (country-dependent) |

This is the single biggest thing to design for before writing transform code: **the ETL's
output unit is not "one row per input row" — it's "N dated records across M target tables,
per input row."**

## Four transform patterns this example demonstrates

1. **Simple 1:1 field copy** — `First_Name` → `PA0002-VORNA`. No logic beyond type/format
   conversion (see `Date_Of_Birth` → `GBDAT`, string date → SAP `DATS`).

2. **Lookup resolution** — `Marital_Status_ID`, `Salary_Grade_ID`, `Department_Type_ID` are
   legacy surrogate keys. Each requires joining to *its own* mapped lookup table first
   (`HR > Marital Status`, `HR > Salary Grades`, `HR > Departments Types` in the main
   mapping CSV) before the SAP-side value is known. **Load/lookup order matters**: these
   reference tables must be migrated (or at least mapped) before `Employees`.

3. **Field decomposition** — `Address` is one free-text string in the source
   (`"51 Zahran St, Jerash"`) but `PA0006` wants structured `STRAS`/`ORT01`/`PSTLZ`/`LAND1`.
   This needs a parsing step — a good candidate for AI-assisted extraction given the
   free-text, multi-locale format, but flag it for spot-checking rather than trusting it
   blindly, since address parsing errors are easy to miss at scale.

4. **Relationship modeling, not a field** — `Manager_ID` is a flat self-referencing FK in
   the source. SAP has no such column; "reports to" is a *relationship* (`HRP1001`) between
   Org Management objects (Positions), not a field on the employee. Converting this
   correctly requires the **Position catalog to exist first** (see `Job_Title` below), then
   translating each `Manager_ID` FK into a relationship edge. This is a materially harder
   transform than everything else in the table — budget extra review time for it.

## Fields with no clean SAP home

Three columns don't map to a standard field at all:

- **`Profile_Photo_URL`** — SAP stores photos as an *attached document* (ArchiveLink), not
  a URL string. Migrating this means uploading/linking the actual image, not copying text.
- **`Notes`** — no canonical free-text HR infotype. Needs a custom infotype, a GOS
  attachment, or a decision to drop it — confirm with the business owner rather than
  guessing it's disposable.
- **`Created_At` / `Updated_At` / `Created_By_User_ID`** — SAP infotypes stamp their own
  creation/change metadata automatically. These legacy audit columns are **not loaded**;
  at most `Created_By_User_ID` survives as a migration cross-reference attribute, not a
  standard field.

## Sequencing implication for the ETL

Given the lookups and the Position/Org-unit dependency, `Employees` cannot be migrated in
isolation. A workable load order for this one table is:

1. Foundation/HR config & lookups: `Companies`, `Departments Types`, `Sections`,
   `Marital Status`, `Salary Grades` (all in the main mapping CSV).
2. Org Management structure: `Hierarchy` → `HRP1000`/`HRP1001` (org units, and a Position
   catalog derived from distinct `Job_Title` values, since SAP has no free-text job title).
3. `Employees` itself, fanning out into `PA0000/0001/0002/0006/0008/0009/0105/0185`.
4. A second pass over the now-loaded `Employees` to resolve `Manager_ID` into `HRP1001`
   "reports to" relationships, since both ends of that edge must already exist.

Apply the same four-part lens (direct copy / lookup resolution / decomposition /
relationship-not-a-field) plus a same-style dependency pass to the rest of the `MIGRATE`
rows in `legacy_to_sap_mapping.csv`.
