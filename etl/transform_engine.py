# -*- coding: utf-8 -*-
"""
transform_engine.py
====================
Applies the field-mapping *patterns* documented in sap_target_schema/examples/
programmatically against the real legacy seed data (ERP_Seed_Data (1).xlsx),
producing SAP-shaped staging tables plus reports on what still needs attention.

This is a PROTOTYPE, not a production load tool. It proves the transform logic
is executable and correct on real data; it does not connect to an SAP system,
does not know real SAP customizing values (currency keys, tax codes, GL account
numbers, etc.), and does not attempt every one of the 199 legacy tables.

--------------------------------------------------------------------------------
SPEC FORMAT
--------------------------------------------------------------------------------
Each JSON file in specs/ describes how to turn one legacy source sheet into one
or more SAP target tables. This is the machine-executable encoding of the same
decisions documented in sap_target_schema/examples/*_field_mapping.csv -- read
those first for the *why*; this format captures the *how*.

{
  "source_sheet": "Employees",              # sheet name in ERP_Seed_Data (1).xlsx
  "source_key": "Employee_ID",               # legacy primary key column, for logging
  "validations": [                           # optional pre-transform checks on the
    {                                         # RAW source data (run before any field
      "type": "balanced_pair",                # is transformed)
      "group_by": "Journal_Entry_ID",
      "debit_field": "Debit_Amount",
      "credit_field": "Credit_Amount"
    }
  ],
  "targets": [                               # one source row can fan out into
    {                                         # records in several target tables --
      "table": "PA0002",                      # this is the fan-out list
      "skip_row_if_null": "Manager_ID",        # optional: don't emit a row into this
                                                 # target if this source column is null
      "constants": {"BEGDA": "19000101"},      # target_field -> literal value, every row
      "fields": [                             # single-leg form: one row out per row in
        {
          "target_field": "VORNA",
          "sap_description": "First Name",
          "transform": "direct",
          "source": "First_Name"
        },
        {
          "target_field": "GBDAT",
          "sap_description": "Date of Birth",
          "transform": "date_yyyymmdd",
          "source": "Date_Of_Birth"
        },
        {
          "target_field": "FAMST",
          "sap_description": "Marital Status",
          "transform": "lookup_passthrough",
          "source": "Marital_Status_ID",
          "lookup_ref": "HR>Marital Status -> FAMST domain"
        },
        {
          "target_field": ["HSL", "DRCRK"],
          "sap_description": "Amount / Debit-Credit Indicator",
          "transform": "signed_amount_pair",
          "debit_source": "Debit_Amount",
          "credit_source": "Credit_Amount"
        }
      ]
    }
  ],
  "dropped_fields": [                        # informational only -- documents fields
    {"source": "Account_ID", "reason": "redundant with line-level detail"}
  ],
  "not_migrated_fields": [                   # informational only -- system-generated
    {"source": "Created_At", "reason": "system-generated on save"}
  ]
}

--------------------------------------------------------------------------------
TRANSFORM TYPES
--------------------------------------------------------------------------------
direct                copy the source value as-is (stringified)
date_yyyymmdd         parse a date string, emit SAP DATS format (YYYYMMDD)
truncate              cut a string to max_len, flag in the run log if it truncated
constant              a fixed literal, no source column
lookup_passthrough    the real SAP code isn't known without a live target system --
                       emit "XREF:<lookup_ref>:<raw legacy value>" and record the
                       (table, field, lookup_ref, distinct value count) in
                       output/_pending_lookups.csv so nothing fabricated leaks
                       into the output looking like a real SAP code
row_number_pad        legacy sequential line numbers (1,2,3) -> SAP-style padded
                       item numbers (10,20,30 or zero-padded), per `multiplier`/`width`.
                       Optional `offset` (default 0) adds a constant after the multiply --
                       e.g. multiplier=2/offset=0 vs multiplier=2/offset=1 generates two
                       disjoint even/odd key series from the same source column, useful
                       when one legacy row needs to become two distinct target keys
                       (e.g. a customer's billing vs shipping address as two ADRC rows).
signed_amount_pair     the ACDOCA collapse: two source columns (debit, credit) ->
                       one signed amount field + one 'S'/'H' indicator field.
                       `target_field` must be a 2-element list [amount_field, indicator_field].
value_map              maps a source value through a small, explicitly-known reference
                       table given in the spec as `mapping` (e.g. {"Amman": "Jordan"}) --
                       unlike lookup_passthrough, this is for facts the pipeline actually
                       knows to be true (geography, fixed business rules), not a stand-in
                       for an unknown SAP customizing value. An unmapped input returns
                       `spec.get("default")` (None if not set) rather than guessing.
                       Optionally splits the source on `delimiter`/`part_index` first
                       (same as split_delimiter) so it can key off a derived sub-value,
                       e.g. mapping the city parsed out of a free-text address to a
                       country, without needing a separate intermediate column.
split_delimiter        splits one free-text source field on `delimiter` (default ",")
                       and emits the piece at `part_index` (0-based), trimmed. If the
                       source has fewer parts than requested, `part_index` 0 falls back
                       to the whole trimmed string (nothing to split, treat it all as
                       the first part) and any other `part_index` returns None rather
                       than guessing. This is a heuristic, not a real address parser --
                       always confirm with a human/agent review before trusting it at
                       scale (see sap_target_schema/examples' NEEDS_PARSE notes).

Every transform function has the signature (row, field_spec) -> value, except
signed_amount_pair which returns a dict {amount_field: v, indicator_field: v}.

--------------------------------------------------------------------------------
ROW EXPANSION (legs)
--------------------------------------------------------------------------------
A target may instead declare "legs": a list of leg templates, each emitting its
own row per source row. Each leg has required "fields" (same shape as target
"fields"), optional "constants" (merged over the target's own constants, so a
leg can set e.g. a different movement type), and an optional "emit_if"
condition evaluated against the RAW source row:

  {"emit_if": {"field": "Quantity_Rejected", "op": "gt", "value": 0},
   "constants": {"BWART": "122"},
   "fields": [...]}

Ops: gt/ge/lt/le/eq/ne (numeric when both sides parse as numbers, else string)
and null/not_null (no "value"). Null values always fail the comparison ops.

This is how one source row becomes N SAP rows: a goods-receipt line with both
accepted and rejected quantities emits a 101 movement AND a 122 movement; an
inventory transfer emits a from-leg and a to-leg; a depreciation entry emits a
balanced expense/accumulated-depreciation pair. A leg whose condition fails
emits nothing.

--------------------------------------------------------------------------------
OUTPUTS (written to etl/output/)
--------------------------------------------------------------------------------
<SAP_TABLE>.csv            one file per target table, rows from every spec that
                            contributes to it (concatenated)
_pending_lookups.csv        every lookup_passthrough field, aggregated: how many
                            distinct legacy values still need a real SAP code
_skipped_fields.csv         every dropped/not-migrated field, aggregated across specs
_validation_report.csv      pass/fail results of each spec's `validations` block
_run_log.txt                human-readable summary of the run
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SEED_WORKBOOK = os.path.join(PROJECT_ROOT, "ERP_Seed_Data (1).xlsx")
SPECS_DIR = os.path.join(SCRIPT_DIR, "specs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

_sheet_cache: dict[str, pd.DataFrame] = {}


def load_sheet(sheet_name: str) -> pd.DataFrame:
    if sheet_name not in _sheet_cache:
        _sheet_cache[sheet_name] = pd.read_excel(SEED_WORKBOOK, sheet_name=sheet_name)
    return _sheet_cache[sheet_name]


def _is_null(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (v is pd.NaT)


def _clean(v) -> str:
    """Stringify a source value, stripping the '.0' pandas artifact that shows up
    whenever an integer-like ID column contains nulls elsewhere (forcing float64)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# --------------------------------------------------------------------------------
# Transform functions
# --------------------------------------------------------------------------------

def t_direct(row, spec):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    return _clean(v)


def t_date_yyyymmdd(row, spec, run_log):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    try:
        ts = pd.to_datetime(v)
        if pd.isna(ts):
            raise ValueError(f"unparseable date: {v!r}")
        return ts.strftime("%Y%m%d")
    except Exception:
        run_log["date_failures"] += 1
        run_log["date_failure_values"].add(_clean(v))
        return None


def t_truncate(row, spec, run_log):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    s = _clean(v)
    max_len = spec["max_len"]
    if len(s) > max_len:
        run_log["truncations"] += 1
    return s[:max_len]


def t_constant(row, spec):
    return spec["value"]


def t_lookup_passthrough(row, spec, pending_lookups, table_name, field_name):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    v = _clean(v)
    key = (table_name, field_name, spec["lookup_ref"])
    pending_lookups[key].add(v)
    return f"XREF:{spec['lookup_ref']}:{v}"


def t_row_number_pad(row, spec):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    multiplier = spec.get("multiplier", 1)
    offset = spec.get("offset", 0)
    width = spec.get("width", 0)
    n = int(v) * multiplier + offset
    return str(n).zfill(width) if width else str(n)


def t_split_delimiter(row, spec):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    delimiter = spec.get("delimiter", ",")
    idx = spec["part_index"]
    parts = [p.strip() for p in _clean(v).split(delimiter)]
    if idx < len(parts):
        return parts[idx]
    return parts[0] if idx == 0 else None


def t_value_map(row, spec):
    v = row.get(spec["source"])
    if _is_null(v):
        return None
    v = _clean(v)
    if "delimiter" in spec and "part_index" in spec:
        parts = [p.strip() for p in v.split(spec["delimiter"])]
        idx = spec["part_index"]
        if idx < len(parts):
            v = parts[idx]
        elif idx == 0:
            v = parts[0]
        else:
            return spec.get("default")
    return spec["mapping"].get(v, spec.get("default"))


def t_signed_amount_pair(row, spec):
    debit = row.get(spec["debit_source"])
    credit = row.get(spec["credit_source"])
    debit = 0.0 if _is_null(debit) else float(debit)
    credit = 0.0 if _is_null(credit) else float(credit)
    amount_field, indicator_field = spec["target_field"]
    if debit and credit:
        # Both sides non-zero: source invariant broken. Don't guess -- surface it.
        return {amount_field: None, indicator_field: "AMBIGUOUS"}
    if debit:
        return {amount_field: debit, indicator_field: "S"}
    if credit:
        return {amount_field: -credit, indicator_field: "H"}
    return {amount_field: 0.0, indicator_field: None}


# --------------------------------------------------------------------------------
# Validation rules (run on the raw source data, before transforms)
# --------------------------------------------------------------------------------

def validate_balanced_pair(df: pd.DataFrame, rule: dict) -> dict:
    g = df.groupby(rule["group_by"])[[rule["debit_field"], rule["credit_field"]]].sum()
    diff = (g[rule["debit_field"]] - g[rule["credit_field"]]).round(2)
    unbalanced = diff[diff != 0]
    return {
        "rule": f"balanced_pair({rule['group_by']}, {rule['debit_field']} vs {rule['credit_field']})",
        "groups_checked": len(g),
        "failures": len(unbalanced),
        "status": "PASS" if len(unbalanced) == 0 else "FAIL",
        "failure_detail": unbalanced.to_dict() if len(unbalanced) else {},
    }


def validate_one_sided(df: pd.DataFrame, rule: dict) -> dict:
    """Exactly one of debit_field/credit_field non-zero per row -- the invariant
    signed_amount_pair depends on."""
    debit = df[rule["debit_field"]].fillna(0)
    credit = df[rule["credit_field"]].fillna(0)
    both_nonzero = ((debit != 0) & (credit != 0)).sum()
    both_zero = ((debit == 0) & (credit == 0)).sum()
    return {
        "rule": f"one_sided({rule['debit_field']} xor {rule['credit_field']})",
        "rows_checked": len(df),
        "failures": int(both_nonzero + both_zero),
        "status": "PASS" if (both_nonzero + both_zero) == 0 else "FAIL",
        "failure_detail": {"both_nonzero": int(both_nonzero), "both_zero": int(both_zero)},
    }


VALIDATORS = {
    "balanced_pair": validate_balanced_pair,
    "one_sided": validate_one_sided,
}


# --------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------

def apply_field(row, field_spec, pending_lookups, run_log, table_name):
    transform = field_spec["transform"]
    if transform == "direct":
        return {field_spec["target_field"]: t_direct(row, field_spec)}
    if transform == "date_yyyymmdd":
        return {field_spec["target_field"]: t_date_yyyymmdd(row, field_spec, run_log)}
    if transform == "truncate":
        return {field_spec["target_field"]: t_truncate(row, field_spec, run_log)}
    if transform == "constant":
        return {field_spec["target_field"]: t_constant(row, field_spec)}
    if transform == "lookup_passthrough":
        return {field_spec["target_field"]: t_lookup_passthrough(
            row, field_spec, pending_lookups, table_name, field_spec["target_field"])}
    if transform == "row_number_pad":
        return {field_spec["target_field"]: t_row_number_pad(row, field_spec)}
    if transform == "split_delimiter":
        return {field_spec["target_field"]: t_split_delimiter(row, field_spec)}
    if transform == "value_map":
        return {field_spec["target_field"]: t_value_map(row, field_spec)}
    if transform == "signed_amount_pair":
        return t_signed_amount_pair(row, field_spec)
    raise ValueError(f"Unknown transform type: {transform!r}")


def _cond_met(row, cond: dict) -> bool:
    """Evaluate a leg's `emit_if` condition against the raw source row.

    Supported ops: not_null, null, gt, ge, lt, le, eq, ne. Comparisons try
    numeric first and fall back to string compare, so {"field": "Qty", "op":
    "gt", "value": 0} works on float64 columns and strings alike.
    """
    v = row.get(cond["field"])
    op = cond["op"]
    if op == "not_null":
        return not _is_null(v)
    if op == "null":
        return _is_null(v)
    if _is_null(v):
        return False
    val = cond["value"]
    try:
        a, b = float(v), float(val)
    except (TypeError, ValueError):
        a, b = str(v), str(val)
    return {"gt": a > b, "ge": a >= b, "lt": a < b,
            "le": a <= b, "eq": a == b, "ne": a != b}[op]


def run_spec(spec: dict, table_frames: dict, pending_lookups: dict,
             skipped_fields: list, validation_results: list, run_log: dict):
    sheet = spec["source_sheet"]
    df = load_sheet(sheet)
    run_log["sheets_processed"].append((sheet, len(df)))

    for rule in spec.get("validations", []):
        validator = VALIDATORS[rule["type"]]
        result = validator(df, rule)
        result["source_sheet"] = sheet
        validation_results.append(result)

    for f in spec.get("dropped_fields", []):
        skipped_fields.append({"source_sheet": sheet, "source_field": f["source"],
                                "disposition": "DROPPED_REDUNDANT", "reason": f["reason"]})
    for f in spec.get("not_migrated_fields", []):
        skipped_fields.append({"source_sheet": sheet, "source_field": f["source"],
                                "disposition": "NOT_MIGRATED", "reason": f["reason"]})

    for target in spec["targets"]:
        table_name = target["table"]
        out_rows = []
        skip_col = target.get("skip_row_if_null")
        # Row expansion: a target with `legs` emits one row per leg whose
        # `emit_if` condition holds -- e.g. a GR line with both accepted and
        # rejected quantities becomes a 101 movement AND a 122 movement, or a
        # stock transfer becomes a from-leg and a to-leg. Without `legs`,
        # the target is itself the single leg (previous behavior, unchanged).
        legs = target.get("legs") or [{"fields": target["fields"]}]
        for _, row in df.iterrows():
            if skip_col is not None and _is_null(row.get(skip_col)):
                continue
            for leg in legs:
                cond = leg.get("emit_if")
                if cond is not None and not _cond_met(row, cond):
                    continue
                out_row = dict(target.get("constants", {}))
                out_row.update(leg.get("constants", {}))
                for field_spec in leg["fields"]:
                    out_row.update(apply_field(row, field_spec, pending_lookups, run_log, table_name))
                out_rows.append(out_row)
        table_frames[table_name].extend(out_rows)
        run_log["targets_produced"].append((sheet, table_name, len(out_rows)))


def summarize_null_rates(table_frames: dict) -> dict:
    """Per table: {field: (null_count, rows_emitting_field)} for fields with nulls.

    Only counts rows that actually contain the field -- a field one contributing
    spec emits and another doesn't is absent, not null, and conflating the two
    would overstate the null rate. A field that is null in *every* row that
    emits it is the loudest signal (the transform produced nothing), so it gets
    its own entry in the all-null list for the run log.
    """
    summary = {}
    for table_name, rows in table_frames.items():
        fields = {}
        for field in {k for row in rows for k in row}:
            values = [row.get(field, _ABSENT) for row in rows]
            emitted = [v for v in values if v is not _ABSENT]
            nulls = sum(1 for v in emitted if v is None)
            if nulls:
                fields[field] = (nulls, len(emitted))
        if fields:
            summary[table_name] = fields
    return summary


_ABSENT = object()  # sentinel: field not emitted for this row at all


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    spec_files = sorted(
        f for f in os.listdir(SPECS_DIR) if f.endswith(".json")
    )

    table_frames: dict[str, list] = defaultdict(list)
    pending_lookups: dict[tuple, set] = defaultdict(set)
    skipped_fields: list = []
    validation_results: list = []
    run_log = {"sheets_processed": [], "targets_produced": [], "truncations": 0,
               "date_failures": 0, "date_failure_values": set()}

    for fname in spec_files:
        with open(os.path.join(SPECS_DIR, fname), encoding="utf-8") as f:
            spec = json.load(f)
        run_spec(spec, table_frames, pending_lookups, skipped_fields, validation_results, run_log)

    # --- write one CSV per SAP target table ---
    for table_name, rows in table_frames.items():
        out_df = pd.DataFrame(rows)
        out_path = os.path.join(OUTPUT_DIR, f"{table_name}.csv")
        out_df.to_csv(out_path, index=False)

    # --- pending lookups report ---
    pl_path = os.path.join(OUTPUT_DIR, "_pending_lookups.csv")
    with open(pl_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["sap_table", "sap_field", "lookup_ref", "distinct_legacy_values_needing_real_sap_code"])
        for (table_name, field_name, lookup_ref), values in sorted(pending_lookups.items()):
            w.writerow([table_name, field_name, lookup_ref, len(values)])

    # --- skipped fields report ---
    sf_path = os.path.join(OUTPUT_DIR, "_skipped_fields.csv")
    with open(sf_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["source_sheet", "source_field", "disposition", "reason"])
        w.writeheader()
        for row in skipped_fields:
            w.writerow(row)

    # --- validation report ---
    vr_path = os.path.join(OUTPUT_DIR, "_validation_report.csv")
    with open(vr_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["source_sheet", "rule", "status", "groups_checked",
                                           "rows_checked", "failures", "failure_detail"])
        w.writeheader()
        for r in validation_results:
            w.writerow({
                "source_sheet": r["source_sheet"], "rule": r["rule"], "status": r["status"],
                "groups_checked": r.get("groups_checked", ""), "rows_checked": r.get("rows_checked", ""),
                "failures": r["failures"], "failure_detail": json.dumps(r["failure_detail"]),
            })

    # --- run log ---
    log_path = os.path.join(OUTPUT_DIR, "_run_log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Transform engine run: {datetime.now().isoformat()}\n")
        f.write(f"Specs processed: {', '.join(spec_files)}\n\n")
        f.write("Source sheets read:\n")
        for sheet, n in run_log["sheets_processed"]:
            f.write(f"  {sheet}: {n} rows\n")
        f.write("\nTarget tables produced:\n")
        for sheet, table, n in run_log["targets_produced"]:
            f.write(f"  {sheet} -> {table}: {n} rows\n")
        f.write(f"\nString truncations applied: {run_log['truncations']}\n")
        f.write(f"\nDate parse failures (emitted as null): {run_log['date_failures']}\n")
        if run_log["date_failures"]:
            values = sorted(run_log["date_failure_values"])
            shown = ", ".join(values[:20]) + ("..." if len(values) > 20 else "")
            f.write(f"  Distinct offending values ({len(values)}): {shown}\n")

        f.write("\nNull rates in output fields (nulls / rows emitting the field):\n")
        null_summary = summarize_null_rates(table_frames)
        if null_summary:
            for table_name in sorted(null_summary):
                f.write(f"  {table_name}:\n")
                for field, (nulls, emitted) in sorted(null_summary[table_name].items(),
                                                      key=lambda kv: -kv[1][0]):
                    marker = "  <-- ALL NULL" if nulls == emitted else ""
                    f.write(f"    {field}: {nulls}/{emitted}{marker}\n")
        else:
            f.write("  (none -- every emitted field is fully populated)\n")
        f.write(f"\nValidation results:\n")
        for r in validation_results:
            f.write(f"  [{r['status']}] {r['source_sheet']}: {r['rule']} "
                     f"({r['failures']} failures)\n")
        f.write(f"\nPending lookups (need real SAP codes before load): {len(pending_lookups)} field(s)\n")
        f.write(f"Fields dropped/not migrated: {len(skipped_fields)}\n")

    print(f"Done. {len(table_frames)} target tables written to {OUTPUT_DIR}")
    print(f"See _run_log.txt, _validation_report.csv, _pending_lookups.csv, _skipped_fields.csv")


if __name__ == "__main__":
    main()
