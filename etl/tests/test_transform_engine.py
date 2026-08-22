# -*- coding: utf-8 -*-
"""
test_transform_engine.py
========================
Golden-value regression tests for the transform engine's semantics. These lock in
the *behavior* of each transform (and the spec-level run loop) so future changes
-- new transform types, refactors, or the agent reusing these functions -- can't
silently alter produced values.

Two layers:
  1. Per-transform unit tests against synthetic rows (no workbook dependency).
  2. A spec-level test that monkeypatches load_sheet with a small synthetic
     DataFrame and runs the real run_spec() loop, covering fan-out,
     skip_row_if_null, constants, and validation wiring.

Run: python -m unittest discover -s tests   (from etl/)
     or: python -m unittest etl.tests.test_transform_engine (from project root)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from collections import defaultdict

import pandas as pd

ETL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ETL_DIR not in sys.path:
    sys.path.insert(0, ETL_DIR)

eng = importlib.import_module("transform_engine")


def fresh_run_log() -> dict:
    # Same shape transform_engine.main() and the agent's preview tool build --
    # t_date_yyyymmdd/t_truncate mutate these keys, so every test needs all of them.
    return {"truncations": 0, "date_failures": 0, "date_failure_values": set()}


class TestDirect(unittest.TestCase):
    def test_basic_and_null(self):
        self.assertEqual(eng.t_direct({"a": "hello"}, {"source": "a"}), "hello")
        self.assertIsNone(eng.t_direct({"a": None}, {"source": "a"}))

    def test_float_artifact_stripped(self):
        # Integer-like ID column forced to float64 by a null elsewhere: 42.0 -> "42"
        self.assertEqual(eng.t_direct({"a": 42.0}, {"source": "a"}), "42")
        # A real float with a fraction must NOT be truncated to an int
        self.assertEqual(eng.t_direct({"a": 42.5}, {"source": "a"}), "42.5")


class TestDateYyyymmdd(unittest.TestCase):
    def test_iso_date(self):
        row = {"d": "2024-03-15"}
        self.assertEqual(eng.t_date_yyyymmdd(row, {"source": "d"}, fresh_run_log()), "20240315")

    def test_datetime_object(self):
        row = {"d": pd.Timestamp("1990-01-31")}
        self.assertEqual(eng.t_date_yyyymmdd(row, {"source": "d"}, fresh_run_log()), "19900131")

    def test_null_returns_none_without_counting(self):
        log = fresh_run_log()
        self.assertIsNone(eng.t_date_yyyymmdd({"d": None}, {"source": "d"}, log))
        self.assertEqual(log["date_failures"], 0)

    def test_unparseable_counted_and_value_recorded(self):
        log = fresh_run_log()
        self.assertIsNone(eng.t_date_yyyymmdd({"d": "not-a-date"}, {"source": "d"}, log))
        self.assertIsNone(eng.t_date_yyyymmdd({"d": "31/02/2024!!"}, {"source": "d"}, log))
        self.assertEqual(log["date_failures"], 2)
        self.assertEqual(sorted(log["date_failure_values"]), ["31/02/2024!!", "not-a-date"])


class TestTruncate(unittest.TestCase):
    def test_within_limit_untouched(self):
        log = fresh_run_log()
        self.assertEqual(eng.t_truncate({"a": "short"}, {"source": "a", "max_len": 10}, log), "short")
        self.assertEqual(log["truncations"], 0)

    def test_over_limit_cut_and_counted(self):
        log = fresh_run_log()
        self.assertEqual(eng.t_truncate({"a": "abcdefgh"}, {"source": "a", "max_len": 5}, log), "abcde")
        self.assertEqual(log["truncations"], 1)


class TestConstant(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(eng.t_constant({}, {"value": "19000101"}), "19000101")


class TestLookupPassthrough(unittest.TestCase):
    def test_xref_placeholder_and_pending_recorded(self):
        pending = defaultdict(set)
        out = eng.t_lookup_passthrough(
            {"s": "Single"}, {"source": "s", "lookup_ref": "HR>Status -> FAMST domain"},
            pending, "PA0002", "FAMST")
        self.assertEqual(out, "XREF:HR>Status -> FAMST domain:Single")
        self.assertEqual(pending[("PA0002", "FAMST", "HR>Status -> FAMST domain")], {"Single"})

    def test_null_emits_nothing(self):
        pending = defaultdict(set)
        self.assertIsNone(eng.t_lookup_passthrough(
            {"s": None}, {"source": "s", "lookup_ref": "r"}, pending, "T", "F"))
        self.assertEqual(len(pending), 0)


class TestRowNumberPad(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(eng.t_row_number_pad({"n": 3}, {"source": "n"}), "3")

    def test_multiplier_offset_width(self):
        spec = {"source": "n", "multiplier": 2, "offset": 1, "width": 4}
        self.assertEqual(eng.t_row_number_pad({"n": 3}, spec), "0007")

    def test_disjoint_series(self):
        # The customers-addresses trick: same source column, two offset values
        even = eng.t_row_number_pad({"n": 5}, {"source": "n", "multiplier": 2, "offset": 0})
        odd = eng.t_row_number_pad({"n": 5}, {"source": "n", "multiplier": 2, "offset": 1})
        self.assertEqual((even, odd), ("10", "11"))


class TestSplitDelimiter(unittest.TestCase):
    def test_parts_trimmed(self):
        spec = {"source": "a", "delimiter": ",", "part_index": 1}
        self.assertEqual(eng.t_split_delimiter({"a": "Amman , Jordan"}, spec), "Jordan")

    def test_index0_falls_back_to_whole_string(self):
        spec = {"source": "a", "delimiter": ",", "part_index": 0}
        self.assertEqual(eng.t_split_delimiter({"a": "no delimiter here"}, spec), "no delimiter here")

    def test_missing_part_returns_none(self):
        spec = {"source": "a", "delimiter": ",", "part_index": 2}
        self.assertIsNone(eng.t_split_delimiter({"a": "one, two"}, spec))


class TestValueMap(unittest.TestCase):
    MAP = {"Amman": "JO", "Dubai": "AE"}

    def test_known_value(self):
        self.assertEqual(eng.t_value_map({"c": "Amman"}, {"source": "c", "mapping": self.MAP}), "JO")

    def test_unmapped_without_default_is_none_not_a_guess(self):
        self.assertIsNone(eng.t_value_map({"c": "Oslo"}, {"source": "c", "mapping": self.MAP}))

    def test_unmapped_with_default(self):
        spec = {"source": "c", "mapping": self.MAP, "default": "XX"}
        self.assertEqual(eng.t_value_map({"c": "Oslo"}, spec), "XX")

    def test_maps_derived_subvalue(self):
        # Key off the city parsed out of a free-text address
        spec = {"source": "addr", "mapping": self.MAP, "delimiter": ",", "part_index": 0}
        self.assertEqual(eng.t_value_map({"addr": "Amman, Something Street 1"}, spec), "JO")


class TestSignedAmountPair(unittest.TestCase):
    SPEC = {"debit_source": "dr", "credit_source": "cr", "target_field": ["HSL", "DRCRK"]}

    def test_debit_only(self):
        out = eng.t_signed_amount_pair({"dr": 100.0, "cr": 0.0}, self.SPEC)
        self.assertEqual(out, {"HSL": 100.0, "DRCRK": "S"})

    def test_credit_only_negated(self):
        out = eng.t_signed_amount_pair({"dr": 0.0, "cr": 250.5}, self.SPEC)
        self.assertEqual(out, {"HSL": -250.5, "DRCRK": "H"})

    def test_nulls_treated_as_zero(self):
        out = eng.t_signed_amount_pair({"dr": None, "cr": None}, self.SPEC)
        self.assertEqual(out, {"HSL": 0.0, "DRCRK": None})

    def test_both_nonzero_is_ambiguous_never_guessed(self):
        out = eng.t_signed_amount_pair({"dr": 10.0, "cr": 10.0}, self.SPEC)
        self.assertEqual(out, {"HSL": None, "DRCRK": "AMBIGUOUS"})


class TestValidators(unittest.TestCase):
    def test_balanced_pair_pass_and_fail(self):
        df = pd.DataFrame({
            "JE": ["J1", "J1", "J2"],
            "dr": [100.0, 0.0, 50.0],
            "cr": [0.0, 100.0, 40.0],  # J2 off by 10
        })
        rule = {"group_by": "JE", "debit_field": "dr", "credit_field": "cr"}
        res = eng.validate_balanced_pair(df, rule)
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["failures"], 1)
        self.assertEqual(res["failure_detail"], {"J2": 10.0})

    def test_one_sided(self):
        df = pd.DataFrame({
            "dr": [100.0, 0.0, 10.0, 0.0],
            "cr": [0.0, 100.0, 10.0, None],
        })
        rule = {"debit_field": "dr", "credit_field": "cr"}
        res = eng.validate_one_sided(df, rule)
        # row 2: both nonzero; row 3: both zero (None credit fills to 0)
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["failure_detail"], {"both_nonzero": 1, "both_zero": 1})


class TestApplyFieldDispatch(unittest.TestCase):
    def test_unknown_transform_raises(self):
        with self.assertRaises(ValueError):
            eng.apply_field({}, {"target_field": "X", "transform": "nope"}, {}, fresh_run_log(), "T")

    def test_signed_amount_pair_returns_both_fields(self):
        spec = {"target_field": ["HSL", "DRCRK"], "transform": "signed_amount_pair",
                "debit_source": "dr", "credit_source": "cr"}
        out = eng.apply_field({"dr": 5.0, "cr": 0.0}, spec, defaultdict(set), fresh_run_log(), "ACDOCA")
        self.assertEqual(out, {"HSL": 5.0, "DRCRK": "S"})


class TestNullRateSummary(unittest.TestCase):
    def test_absent_not_conflated_with_null(self):
        # TOPT.OPT is emitted by only one of two contributing specs; its null
        # rate must be measured against emitting rows only (1/2), not all rows.
        table_frames = {
            "TMIX": [
                {"A": None, "B": "x"},
                {"A": "v", "B": None, "OPT": None},
                {"A": "w", "B": "y"},
                {"A": "z", "B": "q", "OPT": "set"},
            ],
            "TFULL": [{"K": "1"}, {"K": "2"}],
        }
        s = eng.summarize_null_rates(table_frames)
        self.assertEqual(s, {"TMIX": {"A": (1, 4), "B": (1, 4), "OPT": (1, 2)}})
        self.assertNotIn("TFULL", s)  # fully populated table produces no entry

    def test_all_null_field_flagged(self):
        s = eng.summarize_null_rates({"T": [{"K": "1", "D": None}, {"K": "2", "D": None}]})
        nulls, emitted = s["T"]["D"]
        self.assertEqual((nulls, emitted), (2, 2))  # nulls == emitted -> run log marks ALL NULL


class TestCondMet(unittest.TestCase):
    ROW = {"qty": 5.0, "neg": -2.0, "zero": 0.0, "s": "abc", "n": None}

    def test_ops(self):
        c = eng._cond_met
        r = self.ROW
        self.assertTrue(c(r, {"field": "qty", "op": "gt", "value": 0}))
        self.assertTrue(c(r, {"field": "neg", "op": "lt", "value": 0}))
        self.assertTrue(c(r, {"field": "zero", "op": "le", "value": 0}))
        self.assertTrue(c(r, {"field": "s", "op": "eq", "value": "abc"}))
        self.assertTrue(c(r, {"field": "s", "op": "ne", "value": "xyz"}))
        self.assertTrue(c(r, {"field": "qty", "op": "not_null"}))
        self.assertTrue(c(r, {"field": "n", "op": "null"}))

    def test_null_fails_comparisons(self):
        c = eng._cond_met
        self.assertFalse(c({"n": None}, {"field": "n", "op": "gt", "value": 0}))
        self.assertFalse(c({"n": None}, {"field": "n", "op": "eq", "value": ""}))


class TestLegExpansion(unittest.TestCase):
    """Row expansion: one source row -> N target rows via `legs` + `emit_if`."""

    SPEC = {
        "source_sheet": "Synthetic",
        "targets": [{
            "table": "MSEG",
            "constants": {"MANDT": "100"},
            "legs": [
                {"constants": {"BWART": "101"},
                 "fields": [
                     {"target_field": "MBLNR", "transform": "direct", "source": "ID"},
                     {"target_field": "MENGE", "transform": "direct", "source": "Accepted"}]},
                {"emit_if": {"field": "Rejected", "op": "gt", "value": 0},
                 "constants": {"BWART": "122"},
                 "fields": [
                     {"target_field": "MBLNR", "transform": "direct", "source": "ID"},
                     {"target_field": "MENGE", "transform": "direct", "source": "Rejected"}]},
            ],
        }],
    }

    def setUp(self):
        self.df = pd.DataFrame({
            "ID": [1, 2],
            "Accepted": [10.0, 5.0],
            "Rejected": [0.0, 3.0],   # row 1: clean GR; row 2: partial rejection
        })
        self._orig = eng.load_sheet
        eng.load_sheet = lambda name: self.df
        eng._sheet_cache.pop("Synthetic", None)

    def tearDown(self):
        eng.load_sheet = self._orig

    def _run(self):
        table_frames = defaultdict(list)
        run_log = {"sheets_processed": [], "targets_produced": [], "truncations": 0,
                   "date_failures": 0, "date_failure_values": set()}
        eng.run_spec(self.SPEC, table_frames, defaultdict(set), [], [], run_log)
        return table_frames["MSEG"]

    def test_conditional_second_leg(self):
        rows = self._run()
        self.assertEqual(len(rows), 3)  # 101+101, 122 only for row 2
        by_bwart = {}
        for r in rows:
            by_bwart.setdefault(r["BWART"], []).append(r)
        self.assertEqual(len(by_bwart["101"]), 2)
        self.assertEqual(by_bwart["122"], [{"MANDT": "100", "BWART": "122", "MBLNR": "2", "MENGE": "3"}])
        # shared target constants reach both legs
        self.assertTrue(all(r["MANDT"] == "100" for r in rows))

    def test_leg_constants_override_target_constants(self):
        spec = json.loads(json.dumps(self.SPEC))
        spec["targets"][0]["constants"]["BWART"] = "999"
        rows = self._run()
        bwarts = {r["BWART"] for r in rows}
        self.assertEqual(bwarts, {"101", "122"})  # leg constants win over target's

    def test_null_condition_value_fails_leg(self):
        self.df.loc[1, "Rejected"] = None
        rows = self._run()
        self.assertEqual(len(rows), 2)  # only the unconditional 101 legs


class TestRunSpec(unittest.TestCase):
    """End-to-end through run_spec() with a synthetic source sheet, covering
    fan-out, skip_row_if_null, constants, and the pending-lookup side effects."""

    SPEC = {
        "source_sheet": "Synthetic",
        "source_key": "ID",
        "validations": [
            {"type": "one_sided", "debit_field": "dr", "credit_field": "cr"},
        ],
        "targets": [
            {
                "table": "TMAIN",
                "constants": {"MANDT": "100"},
                "fields": [
                    {"target_field": "KEY", "transform": "direct", "source": "ID"},
                    {"target_field": "AMT", "transform": "signed_amount_pair",
                     "debit_source": "dr", "credit_source": "cr", "target_field": ["AMT", "SH"]},
                ],
            },
            {
                "table": "TOPT",
                "skip_row_if_null": "OPT_ID",
                "fields": [
                    {"target_field": "REF", "transform": "direct", "source": "OPT_ID"},
                    {"target_field": "STATUS", "transform": "lookup_passthrough",
                     "source": "Status", "lookup_ref": "ref>status"},
                ],
            },
        ],
        "dropped_fields": [{"source": "Junk", "reason": "redundant"}],
        "not_migrated_fields": [{"source": "Created_At", "reason": "system-generated"}],
    }

    def setUp(self):
        self.df = pd.DataFrame({
            "ID": [1, 2],
            "dr": [100.0, 0.0],
            "cr": [0.0, 100.0],
            "OPT_ID": [None, 77.0],
            "Status": ["Active", "Active"],
            "Junk": ["x", "y"],
            "Created_At": ["2024-01-01", "2024-01-02"],
        })
        self._orig = eng.load_sheet
        eng.load_sheet = lambda name: self.df
        eng._sheet_cache.pop("Synthetic", None)

    def tearDown(self):
        eng.load_sheet = self._orig

    def test_fanout_skip_constants_and_reports(self):
        table_frames = defaultdict(list)
        pending = defaultdict(set)
        skipped, validations = [], []
        run_log = {"sheets_processed": [], "targets_produced": [], "truncations": 0,
                   "date_failures": 0, "date_failure_values": set()}
        eng.run_spec(self.SPEC, table_frames, pending, skipped, validations, run_log)

        self.assertEqual(len(table_frames["TMAIN"]), 2)
        self.assertEqual(table_frames["TMAIN"][0],
                         {"MANDT": "100", "KEY": "1", "AMT": 100.0, "SH": "S"})
        self.assertEqual(table_frames["TMAIN"][1]["SH"], "H")

        # Row 1 has null OPT_ID -> skipped; only row 2 lands in TOPT
        self.assertEqual(len(table_frames["TOPT"]), 1)
        self.assertEqual(table_frames["TOPT"][0]["REF"], "77")
        self.assertEqual(table_frames["TOPT"][0]["STATUS"], "XREF:ref>status:Active")
        self.assertEqual(pending[("TOPT", "STATUS", "ref>status")], {"Active"})

        self.assertEqual(validations[0]["status"], "PASS")
        self.assertEqual([s["disposition"] for s in skipped],
                         ["DROPPED_REDUNDANT", "NOT_MIGRATED"])


if __name__ == "__main__":
    unittest.main()
