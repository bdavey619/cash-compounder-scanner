"""End-to-end check against the fictional fixtures. Run: python -m unittest discover tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scanner.analysis import implied_growth, dcf_value

ROOT = Path(__file__).resolve().parents[1]


class TestReverseDCF(unittest.TestCase):
    def test_round_trip(self):
        v = dcf_value(10.0, 0.12, 0.09, 0.03, 10)
        self.assertAlmostEqual(implied_growth(10.0, v, 0.09, 0.03, 10), 0.12, places=6)

    def test_bad_inputs(self):
        self.assertIsNone(implied_growth(-1, 100, 0.09, 0.03, 10))


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        subprocess.run([sys.executable, str(ROOT / "tests/make_fixtures.py"), str(cls.tmp / "fx")], check=True, cwd=ROOT)
        subprocess.run([sys.executable, "-m", "scanner.scan", "--fixtures", str(cls.tmp / "fx"),
                        "--asof", "2026-11-06", "--out", str(cls.tmp / "out")], check=True, cwd=ROOT)
        cls.data = json.loads((cls.tmp / "out/results.json").read_text())

    def tickers(self, key):
        return [r["ticker"] for r in self.data[key]]

    def test_quality_filter(self):
        quality = set(self.tickers("all_quality"))
        self.assertNotIn("SMPE", quality)  # 4% FCF margin
        self.assertNotIn("SMPF", quality)  # negative FCF year
        self.assertNotIn("SMPH", quality)  # too small
        self.assertIn("SMPG", quality)

    def test_buckets(self):
        self.assertEqual(self.tickers("shortlist"), ["SMPA", "SMPB"])
        self.assertEqual(self.tickers("triggered_only"), ["SMPC"])
        self.assertEqual(self.tickers("gap_only"), ["SMPD"])

    def test_insider_cluster_counts_buys_not_sales(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        self.assertEqual(len(rows["SMPB"]["insider_buyers"]), 3)
        self.assertEqual(rows["SMPC"]["insider_buyers"], [])  # its insider filed a sale

    def test_ttm_from_quarterly(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        a = rows["SMPA"]
        self.assertEqual(a["fcf_basis"], "TTM")
        self.assertEqual(a["fcf_period_end"], "2026-09-30")
        self.assertAlmostEqual(a["fcf_bn"], 1.70 * 1.06, places=2)  # FY25 + 0.81 YTD - 0.75 YTD

    def test_explorer_written(self):
        html = (self.tmp / "out/explorer.html").read_text()
        self.assertNotIn("__COMPANIES_JSON__", html)
        self.assertIn('"id": "smpa"', html)

    def test_annual_filter_ignores_quarterly(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        self.assertLess(rows["SMPA"]["fy_fcf_bn"], 100)  # the planted stale 10-Q value is $9.99T
        self.assertLess(rows["SMPA"]["fcf_bn"], 100)


if __name__ == "__main__":
    unittest.main()


class TestFallbackToAnnual(unittest.TestCase):
    def test_no_quarterly_uses_fy(self):
        from scanner.analysis import fundamentals
        fx = json.loads((ROOT / "tests/_tmp_fx.json").read_text()) if (ROOT / "tests/_tmp_fx.json").exists() else None
        sys.path.insert(0, str(ROOT / "tests"))
        import make_fixtures as mf
        facts = mf.company_facts(1, "X", [1.0, 1.2, 1.4], 0.3, 5.0, 0.0, 1e8)
        for c in facts["facts"]["us-gaap"].values():
            c["units"]["USD"] = [f for f in c["units"]["USD"] if f["form"] != "10-Q"]
        fu = fundamentals(facts)
        self.assertEqual(fu["fcf_basis"], "FY")
        self.assertAlmostEqual(fu["fcf"] / 1e9, 1.4, places=6)
