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
        self.assertEqual(self.tickers("shortlist"), ["SMPB"])
        # SMPA dropped 14% and looks cheap on total FCF, but it dilutes shareholders 4%/yr,
        # so its per-share growth (the reference) is too low for the price to look cheap
        self.assertEqual(sorted(self.tickers("triggered_only")), ["SMPA", "SMPC"])
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

    def test_cyclical_judged_on_average(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        d = rows["SMPD"]
        self.assertTrue(d["volatile"])
        self.assertGreater(d["implied_g"], -0.02)  # on its lower 3-yr average FCF the price looks less cheap

    def test_stock_comp_subtracted(self):
        a = {r["ticker"]: r for r in self.data["all_quality"]}["SMPA"]
        self.assertGreater(a["sbc_bn"], 0)
        self.assertAlmostEqual(a["fcf_bn"], 1.70 * 1.06, places=2)  # fixtures set OCF so FCF after SBC is the target

    def test_per_share_growth(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        a, b = rows["SMPA"], rows["SMPB"]
        self.assertLess(a["fcf_ps_cagr"], a["fcf_cagr"])   # dilution lowers per-share growth
        self.assertGreater(b["fcf_ps_cagr"], b["fcf_cagr"])  # buybacks raise it
        self.assertAlmostEqual(a["share_cagr"], 0.04, places=2)

    def test_risk_premium(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        self.assertAlmostEqual(rows["SMPD"]["required_return"], 0.105, places=4)  # cyclical
        self.assertAlmostEqual(rows["SMPA"]["required_return"], 0.09, places=4)

    def test_explorer_written(self):
        html = (self.tmp / "out/explorer.html").read_text()
        self.assertNotIn("__COMPANIES_JSON__", html)
        self.assertIn('"id": "smpb"', html)

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
            for unit, vals in c["units"].items():
                c["units"][unit] = [f for f in vals if f["form"] != "10-Q"]
        fu = fundamentals(facts)
        self.assertEqual(fu["fcf_basis"], "FY")
        self.assertAlmostEqual(fu["fcf"] / 1e9, 1.4, places=6)  # FCF after stock comp
