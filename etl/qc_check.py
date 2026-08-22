# -*- coding: utf-8 -*-
"""
qc_check.py
===========
Systematic, repeatable quality control across the whole project -- replaces
the ad-hoc spot-checks done while building it with one script that checks
everything at once and can be re-run after every future change.

Checks:
  1.  CSV integrity        -- every sap_target_schema/examples/*.csv parses with a
                               consistent column count (catches unquoted-comma bugs).
  2.  JSON spec validity    -- every etl/specs/*.json parses and has the required
                               top-level shape (source_sheet, targets).
  3.  Spec source sheets    -- every spec's source_sheet is a real sheet in the
                               seed workbook.
  4.  Spec source columns   -- every "source"/"debit_source"/"credit_source" a spec
                               reads actually exists as a column on that sheet
                               (catches silent typos -- a wrong column name doesn't
                               error, it just quietly produces nulls).
  5.  Spec target tables    -- every table a spec writes to is cataloged in
                               sap_tables.json (or is an explicitly-known exception).
  6.  Spec target fields    -- every target_field a spec writes is a field the
                               catalog actually documents for that table.
  7.  Legacy table coverage -- every row in legacy_to_sap_mapping.csv
                               matches either a real sheet in the seed workbook or is
                               explicitly a rollup/summary row, and there are no
                               duplicate (component, legacy_table) rows.
  8.  Cross-references      -- every "see X.json" / "see Y_worked_example.md" /
                               "see Z_field_mapping.csv" mentioned in a doc or CSV
                               note points at a file that actually exists.
  9.  Engine run            -- transform_engine.py runs clean and every declared
                               validation rule passes.
  10. Table-name catalog use -- every SAP table name mentioned in a
                               sap_target_table CSV column resolves to a real
                               catalog entry (accounting for compound "A / B" and
                               parenthetical "(not mapped)"-style non-table values).

Run: python qc_check.py
Exit code is non-zero if any check reports a FAIL.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SEED_WORKBOOK = os.path.join(PROJECT_ROOT, "ERP_Seed_Data (1).xlsx")
SCHEMA_DIR = os.path.join(PROJECT_ROOT, "sap_target_schema")
EXAMPLES_DIR = os.path.join(SCHEMA_DIR, "examples")
SPECS_DIR = os.path.join(SCRIPT_DIR, "specs")
SAP_TABLES_JSON = os.path.join(SCHEMA_DIR, "sap_tables.json")
LEGACY_MAPPING_CSV = os.path.join(SCHEMA_DIR, "legacy_to_sap_mapping.csv")

results = []  # list of (check_name, status, detail)


def report(name, status, detail=""):
    results.append((name, status, detail))
    marker = {"PASS": "OK ", "FAIL": "!! ", "WARN": "?? "}[status]
    print(f"{marker}[{status}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. CSV integrity
# ---------------------------------------------------------------------------

def check_csv_integrity():
    bad_total = 0
    for fn in sorted(glob.glob(os.path.join(EXAMPLES_DIR, "*.csv"))):
        with open(fn, encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            header = next(r)
            n = len(header)
            bad_lines = [i for i, row in enumerate(r, start=2) if len(row) != n]
        if bad_lines:
            bad_total += len(bad_lines)
            report(f"CSV integrity: {os.path.basename(fn)}", "FAIL",
                   f"{len(bad_lines)} malformed row(s) at lines {bad_lines}")
    if bad_total == 0:
        report("CSV integrity (all examples/*.csv)", "PASS",
               f"{len(glob.glob(os.path.join(EXAMPLES_DIR, '*.csv')))} files, 0 malformed rows")


# ---------------------------------------------------------------------------
# 2/3/4. JSON spec validity + source sheet/column existence
# ---------------------------------------------------------------------------

def load_seed_columns():
    """Returns {sheet_name: set(column_names)} without loading full data (header only)."""
    import pandas as pd
    xls = pd.ExcelFile(SEED_WORKBOOK)
    cols = {}
    for sheet in xls.sheet_names:
        df = pd.read_excel(SEED_WORKBOOK, sheet_name=sheet, nrows=0)
        cols[sheet] = set(df.columns)
    return cols


def check_specs(seed_columns, catalog):
    spec_files = sorted(glob.glob(os.path.join(SPECS_DIR, "*.json")))
    invalid = 0
    for fn in spec_files:
        base = os.path.basename(fn)
        try:
            with open(fn, encoding="utf-8") as f:
                spec = json.load(f)
        except Exception as e:
            report(f"Spec JSON validity: {base}", "FAIL", str(e))
            invalid += 1
            continue

        sheet = spec.get("source_sheet")
        if sheet not in seed_columns:
            report(f"Spec source_sheet: {base}", "FAIL",
                   f"sheet {sheet!r} not found in seed workbook")
            invalid += 1
            continue

        cols = seed_columns[sheet]
        bad_cols = []
        bad_tables = []
        bad_fields = []

        for target in spec.get("targets", []):
            table = target.get("table")
            if table not in catalog:
                bad_tables.append(table)
            cataloged_fields = {f["field"] for f in catalog.get(table, {}).get("fields", [])}

            for field_spec in target.get("fields", []):
                for src_key in ("source", "debit_source", "credit_source"):
                    src = field_spec.get(src_key)
                    if src is not None and src not in cols:
                        bad_cols.append((field_spec.get("target_field"), src_key, src))

                tf = field_spec.get("target_field")
                tf_list = tf if isinstance(tf, list) else [tf]
                for f in tf_list:
                    if table in catalog and f not in cataloged_fields:
                        bad_fields.append((table, f))

            skip_col = target.get("skip_row_if_null")
            if skip_col is not None and skip_col not in cols:
                bad_cols.append((None, "skip_row_if_null", skip_col))

        if bad_cols:
            report(f"Spec source columns: {base}", "FAIL",
                   f"{len(bad_cols)} reference(s) to columns not on sheet {sheet!r}: {bad_cols}")
            invalid += 1
        if bad_tables:
            report(f"Spec target tables: {base}", "FAIL",
                   f"table(s) not in sap_tables.json catalog: {sorted(set(bad_tables))}")
            invalid += 1
        if bad_fields:
            report(f"Spec target fields: {base}", "FAIL",
                   f"field(s) not documented for their table in the catalog: {bad_fields}")
            invalid += 1

    if invalid == 0:
        report(f"All {len(spec_files)} specs: JSON validity + source/target consistency", "PASS")


# ---------------------------------------------------------------------------
# 7. Legacy table coverage + duplicate check
# ---------------------------------------------------------------------------

def check_legacy_mapping(seed_columns):
    import pandas as pd
    df = pd.read_csv(LEGACY_MAPPING_CSV)

    dupes = df[df.duplicated(subset=["component", "legacy_table"], keep=False)]
    if len(dupes):
        report("legacy_to_sap_mapping.csv: duplicate rows", "FAIL",
               f"{len(dupes)} duplicated (component, legacy_table) rows: "
               f"{dupes[['component', 'legacy_table']].values.tolist()}")
    else:
        report("legacy_to_sap_mapping.csv: no duplicate rows", "PASS", f"{len(df)} rows checked")

    migrate_rows = df[df["status"] == "MIGRATE"]
    missing_sheet = [t for t in migrate_rows["legacy_table"] if t not in seed_columns]
    if missing_sheet:
        report("legacy_to_sap_mapping.csv: MIGRATE rows have a real source sheet", "FAIL",
               f"{len(missing_sheet)} MIGRATE table(s) with no matching sheet in the seed workbook: {missing_sheet}")
    else:
        report("legacy_to_sap_mapping.csv: MIGRATE rows have a real source sheet", "PASS",
               f"{len(migrate_rows)} MIGRATE rows checked")


# ---------------------------------------------------------------------------
# 10. sap_target_table values in example CSVs resolve to real catalog entries
# ---------------------------------------------------------------------------

NON_TABLE_PATTERNS = re.compile(
    r"^\(|^not mapped$|^NOT_MIGRATED$|^--$|^\s*$", re.IGNORECASE
)


def extract_table_tokens(value: str):
    """Split a sap_target_table cell like 'MARA / MARC / MBEW' or '(not mapped)'
    into individual candidate table names, skipping obvious non-table placeholders."""
    if not isinstance(value, str) or NON_TABLE_PATTERNS.match(value.strip()):
        return []
    tokens = []
    for part in re.split(r"\s*/\s*|\s*\+\s*", value):
        part = part.strip()
        # strip trailing parenthetical notes, e.g. "T499S (Location)" -> "T499S"
        part = re.split(r"\s*\(", part)[0].strip()
        if part and part.isupper() and re.match(r"^[A-Z0-9_]+$", part):
            tokens.append(part)
    return tokens


def check_example_csv_table_names(catalog):
    unresolved = {}
    for fn in sorted(glob.glob(os.path.join(EXAMPLES_DIR, "*.csv"))):
        with open(fn, encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            if "sap_target_table" not in (r.fieldnames or []):
                continue
            for row in r:
                for tok in extract_table_tokens(row["sap_target_table"]):
                    if tok not in catalog:
                        unresolved.setdefault(os.path.basename(fn), set()).add(tok)
    if unresolved:
        for fn, toks in unresolved.items():
            report(f"Example CSV table names: {fn}", "WARN",
                   f"token(s) not found in catalog (may be legitimate shorthand, review): {sorted(toks)}")
    else:
        report("Example CSV sap_target_table values resolve to the catalog", "PASS")


# ---------------------------------------------------------------------------
# 8. Cross-reference existence
# ---------------------------------------------------------------------------

# Matches a real-looking filename reference, e.g. "employees.json" or "customers_field_mapping.csv".
# Excludes: a glob wildcard immediately before it (*_field_mapping.csv is a pattern, not a pointer),
# and bare uppercase SAP-table-style names (PA0001.csv, VBAK.csv) which in this project are always
# illustrative examples of engine-generated output filenames, never a committed file to point at.
CROSS_REF_PATTERN = re.compile(r"(?<![*\w])([A-Za-z][A-Za-z0-9_]*\.(?:json|csv|md))\b")


def is_illustrative_output_name(ref: str) -> bool:
    stem = ref.rsplit(".", 1)[0]
    return stem.isupper()  # SAP table names (PA0001, VBAK, ACDOCA, ...) vs. our lowercase_snake_case files


def check_cross_references():
    all_files = set()
    for root, _, files in os.walk(PROJECT_ROOT):
        if ".git" in root or "output" in root or "__pycache__" in root:
            continue
        for f in files:
            all_files.add(f)

    missing = {}
    search_paths = glob.glob(os.path.join(EXAMPLES_DIR, "*.csv")) + \
        glob.glob(os.path.join(EXAMPLES_DIR, "*.md")) + \
        glob.glob(os.path.join(SPECS_DIR, "*.json")) + \
        [os.path.join(SCRIPT_DIR, "README.md"), os.path.join(SCHEMA_DIR, "README.md")]

    for fn in search_paths:
        with open(fn, encoding="utf-8") as f:
            text = f.read()
        for ref in set(CROSS_REF_PATTERN.findall(text)):
            if ref not in all_files and not is_illustrative_output_name(ref):
                missing.setdefault(os.path.basename(fn), set()).add(ref)

    if missing:
        for fn, refs in missing.items():
            report(f"Cross-references: {fn}", "WARN", f"mentions file(s) not found anywhere in the project: {sorted(refs)}")
    else:
        report("Cross-references (see X.json/.csv/.md mentions)", "PASS")


# ---------------------------------------------------------------------------
# 9. Engine run
# ---------------------------------------------------------------------------

def check_engine_run():
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "transform_engine.py")],
        cwd=SCRIPT_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        report("Engine run", "FAIL", f"exit code {result.returncode}: {result.stderr[-500:]}")
        return

    log_path = os.path.join(SCRIPT_DIR, "output", "_validation_report.csv")
    import pandas as pd
    vr = pd.read_csv(log_path)
    failures = vr[vr["status"] != "PASS"]
    if len(failures):
        report("Engine run: validation rules", "FAIL", f"{len(failures)} rule(s) not passing: {failures['rule'].tolist()}")
    else:
        report("Engine run: validation rules", "PASS", f"{len(vr)} rule(s), all PASS")


# ---------------------------------------------------------------------------
# 11. Unit tests (transform-engine golden values)
# ---------------------------------------------------------------------------

def check_unit_tests():
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=SCRIPT_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        # unittest prints failures to stderr
        report("Unit tests (tests/)", "FAIL", result.stderr.strip().splitlines()[-1])
    else:
        n = sum(1 for line in result.stderr.splitlines() if line.startswith("test_"))
        report("Unit tests (tests/)", "PASS", f"{n} tests, all passing")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 78)
    print("QC CHECK")
    print("=" * 78)

    with open(SAP_TABLES_JSON, encoding="utf-8") as f:
        catalog = json.load(f)

    check_csv_integrity()
    seed_columns = load_seed_columns()
    check_specs(seed_columns, catalog)
    check_legacy_mapping(seed_columns)
    check_example_csv_table_names(catalog)
    check_cross_references()
    check_engine_run()
    check_unit_tests()

    print()
    print("=" * 78)
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    print(f"SUMMARY: {len(results)} checks, {len(fails)} FAIL, {len(warns)} WARN, "
          f"{len(results) - len(fails) - len(warns)} PASS")
    if fails:
        print("\nFAILURES:")
        for name, status, detail in fails:
            print(f"  - {name}: {detail}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
