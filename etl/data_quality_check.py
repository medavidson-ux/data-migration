# -*- coding: utf-8 -*-
"""
data_quality_check.py
======================
Profiles the underlying legacy data itself (ERP_Seed_Data (1).xlsx) -- a
different question from qc_check.py, which checks whether the *mapping
artifacts* (specs, catalog, worked examples) are internally consistent. This
script asks whether the *source data* is trustworthy in the first place,
systematically, across all ~199 sheets rather than only the handful this
project has investigated by hand so far (Customers.Country, Locations.Address,
the Materials/Assets duplicate-table checks).

Checks, per sheet:
  1. Duplicate primary key values (first column, if it looks like a *_ID key)
  2. Fully duplicate rows (every column identical)
  3. Null rate per column
  4. Constant columns (every non-null value identical -- zero information,
     and the exact pattern that turned out to matter in the Customers.Country
     investigation)
  5. Foreign-key referential integrity: for every "<Something>_ID" column that
     isn't the sheet's own primary key, if another sheet's primary key has that
     exact name, check what fraction of values actually exist there (orphaned
     FKs -- a real load-blocking problem, not a modeling question). Null FK
     values on such columns are reported as NULL_FK findings (INFO, WARN at
     majority-null rates) rather than silently skipped -- the check can't know
     which references are mandatory, so nulls are surfaced for human judgment
     instead of hiding behind an implied "clean"
  6. Date-pair sanity: for column pairs that look like a start/end or
     created/updated pair, flag rows where the end precedes the start

Run: python data_quality_check.py
Outputs to etl/output/:
  _data_quality_report.csv   -- one row per issue found, with enough detail to
                                 act on it
  _data_quality_summary.txt  -- counts by issue type and a top-offenders list
"""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SEED_WORKBOOK = os.path.join(PROJECT_ROOT, "ERP_Seed_Data (1).xlsx")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

ID_COL_RE = re.compile(r"^[A-Za-z0-9]+_ID$")

DATE_PAIR_HINTS = [
    ("Start_Date", "End_Date"), ("BEGDA", "ENDDA"),
    ("Created_At", "Updated_At"), ("Hire_Date", "Termination_Date"),
    ("Purchase_Date", "Warranty_Expiry"), ("Acquisition_Date", "Disposal_Date"),
    ("Order_Date", "Required_Delivery_Date"), ("Registration_Date", "Disposal_Date"),
]


def load_all_sheets():
    xls = pd.ExcelFile(SEED_WORKBOOK)
    sheets = {}
    for name in xls.sheet_names:
        if name == "Index":
            continue
        sheets[name] = pd.read_excel(SEED_WORKBOOK, sheet_name=name)
    return sheets


def find_pk_column(df: pd.DataFrame) -> str | None:
    if len(df.columns) == 0:
        return None
    first = df.columns[0]
    if ID_COL_RE.match(str(first)):
        return first
    return None


def build_pk_index(sheets: dict) -> dict:
    """Maps PK column name -> sheet name that owns it, for FK resolution."""
    idx = {}
    for name, df in sheets.items():
        pk = find_pk_column(df)
        if pk:
            idx[pk] = name
    return idx


issues = []  # list of dicts: sheet, column, issue_type, detail, severity


def add_issue(sheet, column, issue_type, detail, severity="INFO"):
    issues.append({"sheet": sheet, "column": column, "issue_type": issue_type,
                    "detail": detail, "severity": severity})


def check_duplicate_pk(name, df, pk):
    if pk is None:
        return
    dupes = df[pk][df[pk].duplicated(keep=False)]
    if len(dupes):
        add_issue(name, pk, "DUPLICATE_PK",
                  f"{dupes.nunique()} distinct value(s) duplicated, {len(dupes)} rows affected",
                  "FAIL")


def check_duplicate_rows(name, df):
    dupe_mask = df.duplicated(keep=False)
    if dupe_mask.any():
        add_issue(name, "(all columns)", "DUPLICATE_ROWS",
                  f"{dupe_mask.sum()} fully duplicate row(s)", "WARN")


def check_nulls(name, df):
    n = len(df)
    if n == 0:
        return
    for col in df.columns:
        null_count = df[col].isna().sum()
        if null_count == 0:
            continue
        pct = 100 * null_count / n
        # Only flag as WARN when a majority are null -- some nulls (e.g. Manager_ID
        # for a CEO, Termination_Date for an active employee) are expected and fine.
        severity = "WARN" if pct >= 50 else "INFO"
        add_issue(name, col, "NULLS", f"{null_count}/{n} rows null ({pct:.0f}%)", severity)


def check_constant_columns(name, df):
    n = len(df)
    if n < 2:
        return
    for col in df.columns:
        non_null = df[col].dropna()
        if len(non_null) >= 2 and non_null.nunique() == 1:
            add_issue(name, col, "CONSTANT_COLUMN",
                      f"every non-null value is {non_null.iloc[0]!r} ({len(non_null)} rows) -- "
                      f"zero information content, and a signal worth corroborating other "
                      f"findings against (see the Customers>Country investigation)",
                      "INFO")


fk_coverage = {"checked": 0, "unresolved": []}  # module-level, read by main() for the summary


def check_referential_integrity(name, df, pk_index, sheets):
    self_pk = find_pk_column(df)
    for col in df.columns:
        if not ID_COL_RE.match(str(col)) or col == self_pk:
            continue
        target_sheet = pk_index.get(col)
        if target_sheet is None or target_sheet == name:
            # No sheet has a primary key with this exact name -- NOT checked, not confirmed
            # clean. Record it so the summary is honest about coverage rather than implying
            # every _ID column was verified.
            fk_coverage["unresolved"].append(f"{name}.{col}")
            continue
        fk_coverage["checked"] += 1
        target_pk_values = set(sheets[target_sheet][col].dropna().unique())
        # Null FK values are not checked for existence (nothing to resolve) -- but
        # they are NOT silently skipped either: a null reference on a relationship
        # that turns out to be mandatory is a load blocker (e.g. Vendor Contracts
        # rows 1 and 3 with no Vendor_ID). The check can't know which FK columns are
        # mandatory -- that's schema knowledge -- so nulls are reported as NULL_FK,
        # INFO by default and WARN at a majority-null rate (matching the NULLS
        # check's convention), for a human to judge before mapping the sheet.
        null_fk = df[col].isna().sum()
        if null_fk:
            pct = 100 * null_fk / len(df)
            add_issue(name, col, "NULL_FK",
                      f"{null_fk}/{len(df)} rows null on FK to {target_sheet}.{col} "
                      f"({pct:.0f}%) -- legitimate for optional references "
                      f"(e.g. Manager_ID for a CEO), a missing mandatory "
                      f"relationship otherwise; confirm before mapping",
                      "WARN" if pct >= 50 else "INFO")
        fk_values = df[col].dropna()
        if len(fk_values) == 0:
            continue
        orphans = fk_values[~fk_values.isin(target_pk_values)]
        if len(orphans):
            pct = 100 * len(orphans) / len(fk_values)
            add_issue(name, col, "ORPHANED_FK",
                      f"{len(orphans)}/{len(fk_values)} values ({pct:.0f}%) don't exist in "
                      f"{target_sheet}.{col} -- e.g. {sorted(orphans.unique())[:5]}",
                      "FAIL" if pct >= 1 else "WARN")


def check_date_pairs(name, df):
    for start_col, end_col in DATE_PAIR_HINTS:
        if start_col not in df.columns or end_col not in df.columns:
            continue
        s = pd.to_datetime(df[start_col], errors="coerce")
        e = pd.to_datetime(df[end_col], errors="coerce")
        both = s.notna() & e.notna()
        backwards = both & (e < s)
        if backwards.any():
            add_issue(name, f"{start_col}/{end_col}", "DATE_ORDER",
                      f"{backwards.sum()} row(s) where {end_col} precedes {start_col}", "WARN")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("Loading all sheets...")
    sheets = load_all_sheets()
    print(f"Loaded {len(sheets)} sheets.\n")

    pk_index = build_pk_index(sheets)

    for name, df in sheets.items():
        pk = find_pk_column(df)
        check_duplicate_pk(name, df, pk)
        check_duplicate_rows(name, df)
        check_nulls(name, df)
        check_constant_columns(name, df)
        check_referential_integrity(name, df, pk_index, sheets)
        check_date_pairs(name, df)

    # --- write detailed report ---
    report_path = os.path.join(OUTPUT_DIR, "_data_quality_report.csv")
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["severity", "sheet", "column", "issue_type", "detail"])
        w.writeheader()
        for issue in sorted(issues, key=lambda x: (x["severity"] != "FAIL", x["severity"] != "WARN", x["sheet"])):
            w.writerow({"severity": issue["severity"], "sheet": issue["sheet"],
                        "column": issue["column"], "issue_type": issue["issue_type"],
                        "detail": issue["detail"]})

    # --- summary ---
    by_type = defaultdict(int)
    by_severity = defaultdict(int)
    sheets_with_fail = set()
    for issue in issues:
        by_type[issue["issue_type"]] += 1
        by_severity[issue["severity"]] += 1
        if issue["severity"] == "FAIL":
            sheets_with_fail.add(issue["sheet"])

    total_fk = fk_coverage["checked"] + len(fk_coverage["unresolved"])
    summary_path = os.path.join(OUTPUT_DIR, "_data_quality_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"Data quality profile: {len(sheets)} sheets checked\n")
        f.write(f"Total issues: {len(issues)}\n\n")
        f.write("By severity:\n")
        for sev in ("FAIL", "WARN", "INFO"):
            f.write(f"  {sev}: {by_severity.get(sev, 0)}\n")
        f.write("\nBy issue type:\n")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            f.write(f"  {t}: {c}\n")
        f.write(f"\nSheets with at least one FAIL-severity issue ({len(sheets_with_fail)}):\n")
        for s in sorted(sheets_with_fail):
            f.write(f"  {s}\n")
        f.write(f"\nReferential integrity coverage (name-based matching only -- see caveat below):\n")
        f.write(f"  {fk_coverage['checked']}/{total_fk} FK-shaped (_ID) columns matched to a real "
                f"primary key and checked ({100*fk_coverage['checked']/total_fk:.0f}%)\n")
        f.write(f"  {len(fk_coverage['unresolved'])} FK-shaped columns have NO matching primary-key "
                f"column name anywhere -- NOT checked, not confirmed clean, just unresolvable by\n"
                f"  this script's naming-based approach (e.g. Manager_ID doesn't literally match any\n"
                f"  sheet's own primary key name, even though it plausibly references Employees).\n"
                f"  Deliberately not fuzzy-matched further -- a generic name like 'Category_ID' means\n"
                f"  different things on different sheets, and a wrong guess here would produce a false\n"
                f"  finding that looks like a confirmed data problem. Full list:\n")
        for u in sorted(fk_coverage["unresolved"]):
            f.write(f"    {u}\n")

    print(f"Total issues found: {len(issues)}")
    print(f"  FAIL: {by_severity.get('FAIL', 0)}  WARN: {by_severity.get('WARN', 0)}  INFO: {by_severity.get('INFO', 0)}")
    print(f"Sheets with a FAIL-severity issue: {len(sheets_with_fail)}")
    print(f"\nWrote: {report_path}")
    print(f"Wrote: {summary_path}")


if __name__ == "__main__":
    main()
