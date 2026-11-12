"""End-to-end check against the fictional fixtures. Run: python -m unittest discover tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
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
                        "--asof", "2026-11-06", "--out", str(cls.tmp / "out"),
                        "--ledger", str(cls.tmp / "ledger.json")], check=True, cwd=ROOT)
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
        self.assertAlmostEqual(rows["SMPD"]["required_return"], 0.0884 + 0.015, places=4)  # 10-yr 4.84% + 4% + cyclical
        self.assertAlmostEqual(rows["SMPA"]["required_return"], 0.0884, places=4)

    def test_earnings_vs_market(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        self.assertEqual(rows["SMPA"]["earnings_in_window"], "2026-11-04")  # 8-K Item 2.02 during the drop
        self.assertIsNone(rows["SMPC"]["earnings_in_window"])               # only an older 8-K
        html = (self.tmp / "out/company.html").read_text()
        self.assertIn("Fell on its own earnings", html)
        self.assertIn("Fell with the market", html)

    def test_drawdown_fields(self):
        a = {r["ticker"]: r for r in self.data["all_quality"]}["SMPA"]
        self.assertLess(a["off_1m_high"], -0.10)
        self.assertIn("flag_drawdown", a)

    def test_site_written(self):
        for page in ("index.html", "company.html", "track.html", "method.html", "learn.html"):
            html = (self.tmp / "out" / page).read_text()
            for ph in ("__CSS__", "__NAV__", "__COMPANIES__", "__META__", "__MODEJS__", "__GLOSS__"):
                self.assertNotIn(ph, html, page)
            self.assertIn('aria-label="Main"', html)
        home = (self.tmp / "out/index.html").read_text()
        self.assertIn('"t":"SMPB"', home)
        self.assertIn('"name":"Sample Beta Payments Inc"', home)

    def test_hurdle_from_treasury(self):
        r = self.data["meta"]["rates"]
        self.assertAlmostEqual(r["rf"], 0.0484)          # latest non-blank FRED value
        self.assertAlmostEqual(r["base"], 0.0884)        # + 4% premium
        self.assertEqual(r["rf_source"], "FRED DGS10")

    def test_ledger_logs_todays_flags(self):
        led = json.loads((self.tmp / "ledger.json").read_text())
        got = {(e["ticker"], e["group"]) for e in led["entries"]}
        self.assertEqual(got, {("SMPB", "shortlist"), ("SMPD", "watchlist"), ("SMPA", "triggered"), ("SMPC", "triggered"),
                               ("SMPD", "yield")})  # yield = top fifth by FCF yield
        self.assertTrue((self.tmp / "out/track/track_record.csv").exists())
        self.assertIn('"track"', (self.tmp / "out/index.html").read_text())

    def test_lookup_covers_companies_off_the_list(self):
        idx = json.loads((self.tmp / "out/c/index.json").read_text())
        on = {t: flag for t, _, flag in idx}
        self.assertEqual(on.get("SMPB"), 1)
        self.assertEqual(on.get("SMPE"), 0)   # 4% margin: not on the list, but can be looked up
        e = json.loads((self.tmp / "out/c/SMPE.json").read_text())
        self.assertFalse(e["onList"])
        self.assertIn("fcf_margin", e["why"])
        self.assertNotIn("SMPE", self.tickers("all_quality"))

    def test_quarterly_cash_history_ends_at_ttm(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        q = rows["SMPA"]["fcf_q"]
        self.assertGreaterEqual(len(q), 2)
        self.assertEqual(q[-1]["end"], "2026-09-30")
        self.assertAlmostEqual(q[-1]["fcf_bn"], rows["SMPA"]["fcf_bn"], places=3)
        self.assertEqual(sorted(x["end"] for x in q), [x["end"] for x in q])

    def test_annual_filter_ignores_quarterly(self):
        rows = {r["ticker"]: r for r in self.data["all_quality"]}
        self.assertLess(rows["SMPA"]["fy_fcf_bn"], 100)  # the planted stale 10-Q value is $9.99T
        self.assertLess(rows["SMPA"]["fcf_bn"], 100)


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


def _closes(start, n, p0, daily):
    out, d, p = [], start, p0
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d.isoformat(), round(p, 4)))
            p *= 1 + daily
        d += timedelta(days=1)
    return out


class TestTrackMath(unittest.TestCase):
    def test_horizons_excess_and_freeze(self):
        from scanner import track as T
        d0 = date(2026, 1, 5)
        spy = _closes(d0, 120, 100.0, 0.0)          # flat market
        stock = _closes(d0, 120, 50.0, 0.001)        # +0.1% a day
        led = {"started": None, "entries": []}
        co = {"ticker": "X", "name": "X Co", "last": 50.0, "last_date": d0.isoformat(), "flag_gap": True}
        self.assertEqual(T.add_flags(led, d0, {"shortlist": [co]}, spy), 1)
        self.assertEqual(T.add_flags(led, d0 + timedelta(days=10), {"shortlist": [co]}, spy), 0)  # repeat window
        T.update_returns(led, {"X": stock}, spy)
        e = led["entries"][0]
        self.assertIsNotNone(e.get("x1m")); self.assertIsNotNone(e.get("x3m")); self.assertIsNone(e.get("x6m"))
        self.assertGreater(e["x1m"], 0)
        self.assertGreater(e["x3m"], e["x1m"])
        frozen = e["x1m"]
        T.update_returns(led, {"X": [(d, p * 2) for d, p in stock]}, spy)   # later prices don't rewrite history
        self.assertEqual(led["entries"][0]["x1m"], frozen)
        s = T.summarize(led, d0 + timedelta(days=100))
        g = next(g for g in s["groups"] if g["group"] == "shortlist")
        self.assertEqual(g["h"]["3m"]["n"], 1)
        self.assertEqual(g["h"]["3m"]["beat"], 1.0)


class TestListChanges(unittest.TestCase):
    def test_new_and_dropped(self):
        from scanner import track as T
        led = {"started": "2026-11-09", "entries": [
            {"date": "2026-11-09", "ticker": "A", "group": "shortlist"},
            {"date": "2026-11-09", "ticker": "B", "group": "shortlist"}]}
        mk = lambda *ts: {"shortlist": [{"ticker": t} for t in ts]}
        ch = T.list_changes(led, date(2026, 11, 10), mk("B", "C"))      # seeded from the ledger's first day
        self.assertEqual((ch["new"]["shortlist"], ch["dropped"]["shortlist"]), (["C"], ["A"]))
        ch = T.list_changes(led, date(2026, 11, 10), mk("B", "C", "D"))  # same-day rerun still compares to Nov 9
        self.assertEqual(ch["new"]["shortlist"], ["C", "D"])
        ch = T.list_changes(led, date(2026, 11, 11), mk("D"))
        self.assertEqual((ch["prev_date"], ch["new"]["shortlist"], ch["dropped"]["shortlist"]), ("2026-11-10", [], ["B", "C"]))


class TestShares(unittest.TestCase):
    def test_share_classes_add_up(self):
        from scanner.analysis import shares_outstanding
        f = {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-10-20", "val": 800e6, "accn": "A2", "filed": "2026-10-28"},   # class A
            {"end": "2026-10-20", "val": 7e6, "accn": "A2", "filed": "2026-10-28"},     # class B
            {"end": "2026-07-20", "val": 810e6, "accn": "A1", "filed": "2026-07-28"}]}}}}
        self.assertEqual(shares_outstanding(f), (807e6, "2026-10-20"))

    def test_net_income_over_eps_wins_when_classes_convert(self):
        from scanner.analysis import shares_outstanding
        f = {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-10-20", "val": 1.7e9, "accn": "A2", "filed": "2026-10-28"}]}}},
             "us-gaap": {"NetIncomeLoss": {"units": {"USD": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 5.85e9, "filed": "2026-10-28"}]}},
                         "EarningsPerShareDiluted": {"units": {"USD/shares": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 3.0, "filed": "2026-10-28"}]}}}}
        self.assertAlmostEqual(shares_outstanding(f)[0], 1.95e9)

    def test_cover_page_kept_when_close(self):
        from scanner.analysis import shares_outstanding
        f = {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-10-20", "val": 1.0e9, "accn": "A2", "filed": "2026-10-28"}]}}},
             "us-gaap": {"NetIncomeLoss": {"units": {"USD": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 2.06e9, "filed": "2026-10-28"}]}},
                         "EarningsPerShareDiluted": {"units": {"USD/shares": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 2.0, "filed": "2026-10-28"}]}}}}
        self.assertEqual(shares_outstanding(f)[0], 1.0e9)     # 3% apart: cover page is more current

    def test_split_after_eps_keeps_cover_page(self):
        from scanner.analysis import shares_outstanding
        f = {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-10-20", "val": 800e6, "accn": "A2", "filed": "2026-10-28"}]}}},   # after a 25:1 split
             "us-gaap": {"NetIncomeLoss": {"units": {"USD": [
                            {"start": "2026-04-01", "end": "2026-06-30", "val": 3.2e9, "filed": "2026-07-28"}]}},
                         "EarningsPerShareDiluted": {"units": {"USD/shares": [
                            {"start": "2026-04-01", "end": "2026-06-30", "val": 100.0, "filed": "2026-07-28"}]}}}}
        self.assertEqual(shares_outstanding(f)[0], 800e6)

    def test_stale_counts_ignored(self):
        from scanner.analysis import shares_outstanding
        f = {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2010-01-27", "val": 469e6, "accn": "A1", "filed": "2010-02-01"}]}}}}
        self.assertEqual(shares_outstanding(f, "2026-09-30"), (None, None))   # Visa's 2010 cover page

    def test_financials_excluded(self):
        from scanner.scan import is_financial
        self.assertTrue(is_financial(6021))      # national commercial bank
        self.assertTrue(is_financial(6331))      # insurance
        self.assertTrue(is_financial(6282))      # asset managers
        self.assertTrue(is_financial(6200))      # exchanges
        self.assertFalse(is_financial(7389))     # business services (Visa, Mastercard)
        self.assertFalse(is_financial(None))


class TestPeakMargin(unittest.TestCase):
    def _facts(self, margins):
        sys.path.insert(0, str(ROOT / "tests"))
        import make_fixtures as mf
        g = {"NetCashProvidedByUsedInOperatingActivities": [], "PaymentsToAcquirePropertyPlantAndEquipment": [],
             "Revenues": []}
        for i, m in enumerate(margins):
            y = 2020 + i
            rev = 10e9
            g["Revenues"].append(mf.fact(f"{y}-01-01", f"{y}-12-31", rev, y, f"{y+1}-02-15"))
            g["PaymentsToAcquirePropertyPlantAndEquipment"].append(mf.fact(f"{y}-01-01", f"{y}-12-31", 0.5e9, y, f"{y+1}-02-15"))
            g["NetCashProvidedByUsedInOperatingActivities"].append(mf.fact(f"{y}-01-01", f"{y}-12-31", rev * m + 0.5e9, y, f"{y+1}-02-15"))
        return {"facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in g.items()},
                          "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [mf.fact(None, "2025-12-31", 1e9, 2025, "2026-02-15")]}}}}}

    def test_flags_margin_spike(self):
        from scanner.analysis import fundamentals
        fu = fundamentals(self._facts([0.15, 0.16, 0.15, 0.16, 0.17, 0.30]))
        self.assertTrue(fu["peak_margin"])
        self.assertAlmostEqual(fu["fcf_normalized"], 10e9 * fu["margin_avg"])

    def test_steady_margin_not_flagged(self):
        from scanner.analysis import fundamentals
        self.assertFalse(fundamentals(self._facts([0.25, 0.26, 0.27, 0.28, 0.29, 0.30]))["peak_margin"])


if __name__ == "__main__":
    unittest.main()


class TestBacktest(unittest.TestCase):
    """Synthetic end-to-end: one quality company with a 2-for-1 split and a sharp drop."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        import make_fixtures as mf
        from scanner import backtest as B
        cls.B = B
        cls.facts = mf.company_facts(9000002, "Sample Beta Payments Inc", [2.0, 2.4, 2.9], 0.41, 12.0, 1.0, 1e9)
        days, d = [], date(2024, 1, 2)
        while d <= date(2027, 12, 31):
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += timedelta(days=1)
        spy = [500 * 1.0003 ** i for i in range(len(days))]
        stock = []
        for i, day in enumerate(days):
            p = 21.5 * 1.0002 ** i                      # split-adjusted: raw price was 2x before the split
            if "2026-08-25" <= day <= "2026-08-31":
                p *= 0.78                               # sharp drop in the last week of August 2026
            elif day > "2026-08-31":
                p *= 0.9
            stock.append(p)
        cls.spy = B.Series(days, spy, spy, [])
        cls.stock = B.Series(days, stock, stock, [("2026-06-01", 2.0)])
        cls.months = B.month_ends(cls.spy, "2025-01")

    def test_facts_asof_hides_later_filings(self):
        early = self.B.facts_asof(self.facts, "2025-03-01")
        rows = early["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
        self.assertTrue(rows and all(r["filed"] <= "2025-03-01" for r in rows))
        self.assertNotIn("EntityCommonStockSharesOutstanding", early["facts"]["dei"])

    def test_walk_forward(self):
        B = self.B
        pit = B.pit_fundamentals(self.facts, self.months)
        self.assertNotIn("2025-03-31", pit)                 # only two years filed: fails the 3-year rule
        self.assertIn("2026-03-31", pit)
        meta = {"SMPB": {"ticker": "SMPB", "name": "Sample Beta", "cik": 9000002}}
        walked = B.walk({"SMPB": pit}, meta, {"SMPB": self.stock}, self.spy, None, self.months)
        rows = {r["d"]: r for r in walked["rows"]}
        # market cap uses the real (pre-split) price: 2x the adjusted 21.5ish, times 1B shares
        i = self.stock.idx("2026-03-31")
        self.assertAlmostEqual(rows["2026-03-31"]["mcap"], 2 * self.stock.close[i] * 1e9, delta=1e6)
        self.assertIn("quality", rows["2026-03-31"]["groups"])
        self.assertIn("12m", rows["2026-03-31"]["fw"])
        aug = rows["2026-08-31"]["groups"]
        self.assertTrue("gap_trigger" in aug or "trigger_only" in aug)   # the drop fires a trigger
        res = B.summarize(walked, {"SMPB": self.stock}, self.spy, self.months, meta)
        self.assertEqual(res["table"][0]["id"], "quality")
        self.assertEqual(len(res["growth"]["dates"]), len(res["growth"]["series"]["spy"]))
        out = Path(tempfile.mkdtemp())
        res["meta"] = {"generated": "test", "start": self.months[0], "end": self.months[-1], "months": len(self.months),
                       "company_months": len(walked["rows"]), "ever_quality": 1, "price_missing": 0, "hold_months": 6,
                       "rules": {"EQUITY_PREMIUM": 0.04}}
        B.write_results(out, res)
        self.assertIn('"table"', (out / "index.html").read_text())
        type(self).html = out / "index.html"
