import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile  # noqa: E402
import signoff  # noqa: E402

GL = ROOT / "examples" / "gl_cash.csv"
SUB = ROOT / "examples" / "bank_statement.csv"
PERIOD_END = date(2026, 9, 30)


def run(gl=GL, sub=SUB, cfg=None, preparer="A. Preparer"):
    return reconcile.reconcile(str(gl), str(sub), cfg or reconcile.DEFAULT_CONFIG,
                               PERIOD_END, preparer)


def by_ref(res):
    return {(b["gl"] or b["sub"])["reference"]: b for b in res["breaks"]}


class ExampleReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = run()
        cls.breaks = by_ref(cls.res)

    def test_ties_out(self):
        s = self.res["summary"]
        self.assertTrue(s["tie_out"])
        self.assertEqual(Decimal(s["unexplained"]), 0)
        self.assertEqual(Decimal(s["variance"]), Decimal("-14486.74"))

    def test_every_break_type_detected(self):
        expected = {
            "PAY-307": "sign_flip",
            "PAY-305": "duplicate_posting",
            "INV-520": "transposition",
            "INV-525": "rounding",
            "INV-530": "amount_mismatch",
            "INV-540": "timing",
            "JE-ACCR-77": "missing_in_subledger",
            "INV-480": "missing_in_subledger",
            "FEE-0930": "missing_in_gl",
            "INT-0930": "missing_in_gl",
        }
        got = {ref: b["type"] for ref, b in self.breaks.items()}
        self.assertEqual(got, expected)

    def test_matching_methods(self):
        methods = sorted(m["method"] for m in self.res["matches"])
        self.assertEqual(methods, ["aggregate", "amount_date", "exact", "exact", "exact"])

    def test_timing_flags_cutoff(self):
        self.assertIn("cut-off", " ".join(self.breaks["INV-540"]["evidence"]))

    def test_routing(self):
        b = self.breaks
        self.assertEqual(b["FEE-0930"]["route_to"], "Preparer")
        self.assertEqual(b["INV-525"]["route_to"], "Preparer")
        self.assertEqual(b["INV-520"]["route_to"], "Reviewer")
        # type floor lifts a small duplicate to Reviewer
        self.assertEqual(b["PAY-305"]["route_to"], "Reviewer")
        # above Reviewer limit
        self.assertEqual(b["JE-ACCR-77"]["route_to"], "Controller")
        # aged 46 days: Reviewer escalated to Controller
        self.assertEqual(b["INV-480"]["route_to"], "Controller")
        self.assertIn("aged", b["INV-480"]["routing_reason"])

    def test_sorted_by_magnitude(self):
        mags = [Decimal(b["magnitude"]) for b in self.res["breaks"]]
        self.assertEqual(mags, sorted(mags, reverse=True))
        self.assertEqual(self.res["breaks"][0]["id"], "B-001")

    def test_json_serialisable_and_markdown(self):
        json.dumps(self.res)
        md = reconcile.render_markdown(self.res)
        self.assertIn("Tie-out:** PASS", md)
        self.assertIn("## Sign-off", md)


class Helpers(unittest.TestCase):
    def test_transposition(self):
        self.assertTrue(reconcile.is_transposition(Decimal("5420"), Decimal("5240")))
        self.assertFalse(reconcile.is_transposition(Decimal("8000"), Decimal("7650")))
        self.assertFalse(reconcile.is_transposition(Decimal("100"), Decimal("100")))

    def test_parse_amount(self):
        self.assertEqual(reconcile.parse_amount("(1,234.50)"), Decimal("-1234.50"))
        self.assertEqual(reconcile.parse_amount("$99"), Decimal("99"))

    def test_parse_date_formats(self):
        self.assertEqual(reconcile.parse_date("09/30/2026"), PERIOD_END)
        self.assertEqual(reconcile.parse_date("2026-09-30"), PERIOD_END)

    def test_column_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            gl, sub = Path(d, "gl.csv"), Path(d, "sub.csv")
            for p, rows in ((gl, [["DocNo", "PostDate", "Amt", "Ref"],
                                  ["J1", "2026-09-01", "100", "X1"]]),
                            (sub, [["DocNo", "PostDate", "Amt", "Ref"],
                                   ["S1", "2026-09-01", "100", "X1"]])):
                with open(p, "w", newline="") as fh:
                    csv.writer(fh).writerows(rows)
            cfg = reconcile.deep_merge(reconcile.DEFAULT_CONFIG, {"columns": {
                "id": "DocNo", "date": "PostDate", "amount": "Amt",
                "reference": "Ref", "description": "Memo"}})
            res = run(gl, sub, cfg)
            self.assertEqual(res["summary"]["break_count"], 0)
            self.assertTrue(res["summary"]["tie_out"])


class Signoff(unittest.TestCase):
    def setUp(self):
        self.res = json.loads(json.dumps(run()))
        self.ids = {(b["gl"] or b["sub"])["reference"]: b["id"]
                    for b in self.res["breaks"]}

    def test_approve(self):
        bid = self.ids["FEE-0930"]
        signoff.apply_signoff(self.res, [bid], "R. Reviewer", "Reviewer", "approve")
        b = next(x for x in self.res["breaks"] if x["id"] == bid)
        self.assertEqual(b["status"], "approved")
        self.assertEqual(self.res["audit_trail"][-1]["break"], bid)
        self.assertFalse(self.res["summary"]["signoff_complete"])

    def test_insufficient_authority(self):
        with self.assertRaisesRegex(signoff.SignoffError, "lacks authority"):
            signoff.apply_signoff(self.res, [self.ids["JE-ACCR-77"]],
                                  "R. Reviewer", "Reviewer", "approve")

    def test_segregation_of_duties(self):
        with self.assertRaisesRegex(signoff.SignoffError, "Segregation"):
            signoff.apply_signoff(self.res, [self.ids["FEE-0930"]],
                                  "a. preparer", "Controller", "approve")

    def test_reject_requires_note(self):
        with self.assertRaisesRegex(signoff.SignoffError, "note"):
            signoff.apply_signoff(self.res, [self.ids["FEE-0930"]],
                                  "C. Controller", "Controller", "reject")

    def test_complete(self):
        ids = [b["id"] for b in self.res["breaks"]]
        signoff.apply_signoff(self.res, ids, "C. Controller", "Controller", "approve")
        self.assertTrue(self.res["summary"]["signoff_complete"])
        with self.assertRaisesRegex(signoff.SignoffError, "already approved"):
            signoff.apply_signoff(self.res, ids[:1], "C. Controller",
                                  "Controller", "approve")


if __name__ == "__main__":
    unittest.main()
