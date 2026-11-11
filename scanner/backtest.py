"""Walk-forward backtest of the daily screen.

At each month-end from START to the latest month with data, rebuild what was knowable that day
(only SEC numbers filed by then, that day's prices, that day's 10-year Treasury yield), apply the
exact same rules as the daily scan, and record which companies each group would have flagged.
Then measure each flag's total return against the S&P 500 (SPY) 3, 6 and 12 months later.

Groups compared (all within the same quality universe, so the first one is the control group):
  quality        every high-quality cash generator above the market-cap floor
  gap            price implies growth well below the company's own record (the valuation gap)
  gap_trigger    gap + a sharp drop or 1-month drawdown vs the market (the shortlist, minus insiders)
  gap_quiet      gap without a drop (the watchlist)
  trigger_only   a sharp drop but no valuation gap
  gap_unadjusted gap computed without the cyclical / peak-margin adjustments (tests those rules)

Known limits, stated on the results page:
  * Survivorship: the universe is today's listed companies, so firms that went bust or were acquired
    are mostly missing. That flatters every group, including the control, so compare groups to each other.
  * Insider buying and earnings-date tags are not part of the backtest.
  * Prices are closes on the flag date; the scan runs after the close, so this assumes buying at that close.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .analysis import fundamentals, price_stats
from .scan import env_user_agent, gap_flag, price_flags, quality_fail_reasons, value_company
from .sources import FetchError, LiveFetcher, load_tickers, refresh_facts_bulk

START = "2014-01"
HORIZONS = {"3m": 63, "6m": 126, "12m": 252}       # trading days
HOLD_MONTHS = 6                                      # growth chart: hold each flag for 6 months
STALE_DAYS = 460                                     # ignore companies whose latest numbers are older than this
GROUPS = [
    ("quality", "All quality companies", "The control group: every company passing the quality screen"),
    ("gap", "Valuation gap", "Price implies growth at least 5 pts below the company's own record"),
    ("gap_trigger", "Gap + sharp drop", "Gap plus a 5-day drop or 1-month drawdown vs the market (the shortlist)"),
    ("gap_quiet", "Gap, no drop", "Gap without a recent drop (the watchlist)"),
    ("trigger_only", "Sharp drop, no gap", "A sharp drop vs the market but no valuation gap"),
    ("gap_unadjusted", "Gap, no cyclical/peak fixes", "Gap measured on latest cash flow for everyone"),
]
YAHOO_HIST = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}"
              "&interval=1d&events=div%7Csplit")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ point-in-time fundamentals

def facts_asof(facts: dict, day: str) -> dict:
    """Only the numbers that had been filed with the SEC by `day`."""
    out = {}
    for ns, concepts in facts.get("facts", {}).items():
        o = {}
        for k, v in concepts.items():
            units = {}
            for u, rows in v.get("units", {}).items():
                keep = [r for r in rows if r.get("filed", "9999") <= day]
                if keep:
                    units[u] = keep
            if units:
                o[k] = {"units": units}
        out[ns] = o
    return {"facts": out}


def pit_fundamentals(facts: dict, month_ends: list[str]) -> dict[str, dict]:
    """{month_end: fundamentals} for months where the company passed the quality screen.
    Recomputed only when a new filing arrives, which is what makes this fast."""
    filed = sorted({r["filed"] for concepts in facts.get("facts", {}).values() for v in concepts.values()
                    for rows in v.get("units", {}).values() for r in rows if r.get("filed")})
    if not filed:
        return {}
    memo, out = {}, {}
    for d in month_ends:
        i = bisect.bisect_right(filed, d) - 1
        if i < 0:
            continue
        key = filed[i]
        if key not in memo:
            try:
                fu = fundamentals(facts_asof(facts, key))
            except Exception:
                fu = None
            memo[key] = fu if (fu and not quality_fail_reasons(fu)) else None
        fu = memo[key]
        if not fu:
            continue
        if (date.fromisoformat(d) - date.fromisoformat(fu["fcf_period_end"])).days > STALE_DAYS:
            continue  # stopped filing; don't value it on years-old numbers
        out[d] = fu
    return out


def _pit_worker(args):
    path, month_ends = args
    try:
        facts = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    res = pit_fundamentals(facts, month_ends)
    return {d: {k: v for k, v in fu.items() if k != "history"} for d, fu in res.items()}


# ------------------------------------------------------------------ prices

class Series:
    """Daily prices for one ticker: split-adjusted closes (for moves), total-return closes
    (for performance), and split events (to recover the real price for market cap)."""

    def __init__(self, dates, close, adj, splits):
        self.dates, self.close, self.adj, self.splits = dates, close, adj, sorted(splits)  # splits: [(date, ratio)]

    def idx(self, day: str):
        i = bisect.bisect_right(self.dates, day) - 1
        return i if i >= 0 else None

    def split_factor_after(self, day: str) -> float:
        """Product of split ratios after `day`: share counts filed before a 2-for-1 split are half of today's."""
        f = 1.0
        for d, r in self.splits:
            if d > day:
                f *= r
        return f

    @classmethod
    def from_yahoo(cls, js):
        res = js["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]["close"]
        adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q
        dates, close, tr = [], [], []
        for t, c, a in zip(ts, q, adj):
            if c is None or a is None or c <= 0 or a <= 0:
                continue
            d = time.strftime("%Y-%m-%d", time.gmtime(t))
            if dates and d == dates[-1]:
                continue
            dates.append(d); close.append(float(c)); tr.append(float(a))
        splits = []
        for ev in (res.get("events") or {}).get("splits", {}).values():
            if ev.get("denominator"):
                splits.append((time.strftime("%Y-%m-%d", time.gmtime(ev["date"])), ev["numerator"] / ev["denominator"]))
        return cls(dates, close, tr, splits)

    def to_json(self):
        return {"d": self.dates, "c": self.close, "a": self.adj, "s": self.splits}

    @classmethod
    def from_json(cls, j):
        return cls(j["d"], j["c"], j["a"], [tuple(x) for x in j["s"]])


def load_history(fetcher, sym: str, start: str, cache: Path | None, max_age_days=6.0) -> Series | None:
    p = cache / f"{sym.replace('^', '_')}.json" if cache else None
    if p and p.exists() and time.time() - p.stat().st_mtime < max_age_days * 86400:
        try:
            return Series.from_json(json.loads(p.read_text()))
        except ValueError:
            pass
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    url = YAHOO_HIST.format(sym=sym.replace("^", "%5E").replace(".", "-"), p1=p1, p2=int(time.time()))
    try:
        s = Series.from_yahoo(json.loads(fetcher.get(url, retries=2, timeout=20)))
    except (FetchError, ValueError, KeyError, TypeError, IndexError):
        return None
    if not s.dates:
        return None
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s.to_json(), separators=(",", ":")))
    return s


# ------------------------------------------------------------------ the walk-forward

def month_ends(spy: Series, start: str) -> list[str]:
    out = []
    for i, d in enumerate(spy.dates):
        if d[:7] >= start and (i + 1 == len(spy.dates) or spy.dates[i + 1][:7] != d[:7]):
            out.append(d)
    if out and out[-1] == spy.dates[-1]:
        out.pop()  # the current month isn't over yet
    return out


def risk_free(tnx: Series | None, day: str) -> float | None:
    if tnx:
        i = tnx.idx(day)
        if i is not None:
            v = tnx.close[i]
            return (v / 10 if v > 20 else v) / 100
    return None


def hurdle(tnx: Series | None, day: str) -> float:
    rf = risk_free(tnx, day)
    rf = C.RISK_FREE_FALLBACK if rf is None else rf
    return min(C.REQUIRED_RETURN_MAX, max(C.REQUIRED_RETURN_MIN, rf + C.EQUITY_PREMIUM))


def _window(s: Series, i: int, n=260):
    lo = max(0, i - n + 1)
    return list(zip(s.dates[lo:i + 1], s.close[lo:i + 1]))


def forward(s: Series, spy: Series, i_spy: int, h: int):
    """Total return over h trading days from SPY index i_spy, and SPY's over the same span.
    If the stock stops trading early (acquired, delisted), exit at its last price."""
    if i_spy + h >= len(spy.dates):
        return None
    d0, d1 = spy.dates[i_spy], spy.dates[i_spy + h]
    j0, j1 = s.idx(d0), s.idx(d1)
    if j0 is None or j1 is None or s.dates[j0] < (date.fromisoformat(d0) - timedelta(days=7)).isoformat():
        return None
    early = s.dates[j1] < (date.fromisoformat(d1) - timedelta(days=10)).isoformat()
    k1 = spy.idx(s.dates[j1]) if early else i_spy + h
    r = s.adj[j1] / s.adj[j0] - 1
    m = spy.adj[k1] / spy.adj[i_spy] - 1
    return r, m, early


def walk(pit: dict[str, dict[str, dict]], meta: dict[str, dict], prices: dict[str, Series], spy: Series,
         tnx: Series | None, months: list[str]) -> dict:
    """pit: {ticker: {month_end: fundamentals}}. Returns flags and forward results per month."""
    rows = []          # one row per (month, ticker) in the priced quality universe
    for d in months:
        i_spy = spy.idx(d)
        mkt = price_stats(_window(spy, i_spy), C.DROP_LOOKBACK_DAYS, C.DRAWDOWN_DAYS)
        base_r = hurdle(tnx, d)
        for t, by_month in pit.items():
            fu = by_month.get(d)
            s = prices.get(t)
            if not fu or not s:
                continue
            j = s.idx(d)
            if j is None or s.dates[j] < (date.fromisoformat(d) - timedelta(days=7)).isoformat():
                continue
            ps = price_stats(_window(s, j), C.DROP_LOOKBACK_DAYS, C.DRAWDOWN_DAYS)
            if not ps or not fu.get("shares"):
                continue
            # filings report share counts before any later split; restate them in today's split-adjusted terms
            co = {**fu, **meta[t], "shares": fu["shares"] * s.split_factor_after(fu.get("shares_asof") or d)}
            v = value_company(co, ps, mkt["chg"], mkt["off_1m_high"], base_r)
            if not v:
                continue
            pf = price_flags(v)
            trig = pf["flag_drop"] or pf["flag_drawdown"]
            gap = gap_flag(v["gap"], v["implied_g"])
            gap_raw = gap_flag(v["gap_ttm"], v["implied_g_ttm"])
            fw = {h: forward(s, spy, i_spy, n) for h, n in HORIZONS.items()}
            # 12-month price momentum, skipping the latest month (the standard way, avoids short-term bounce noise)
            mom = (s.adj[j - 21] / s.adj[j - 252] - 1) if j >= 252 else None
            rows.append({
                "d": d, "t": t, "groups": [g for g, on in (("quality", True), ("gap", gap), ("gap_trigger", gap and trig),
                                                           ("gap_quiet", gap and not trig), ("trigger_only", trig and not gap),
                                                           ("gap_unadjusted", gap_raw)) if on],
                "implied_g": v["implied_g"], "ref_g": v["ref_g"], "mcap": v["mcap"], "hurdle": base_r,
                "gap": v["gap"] if v["gap"] is not None and math.isfinite(v["gap"]) else None,
                "fcf_yield": (1 / v["p_fcf"]) if v["p_fcf"] else None, "mom": mom,
                "fcf_margin": fu.get("fcf_margin"), "roc": fu.get("cash_roc"),
                "fw": {h: x for h, x in fw.items() if x is not None},
            })
    rates = {d: risk_free(tnx, d) for d in months}
    return {"rows": rows, "rf": rates}


def monthly_returns(s: Series, months: list[str]) -> dict[str, float]:
    """Total return from each month-end to the next."""
    out = {}
    for a, b in zip(months, months[1:]):
        i, j = s.idx(a), s.idx(b)
        if i is None or j is None or j == i or s.dates[i] < (date.fromisoformat(a) - timedelta(days=7)).isoformat():
            continue
        out[a] = s.adj[j] / s.adj[i] - 1
    return out


def summarize(walked: dict, prices: dict[str, Series], spy: Series, months: list[str], meta: dict) -> dict:
    rows = walked["rows"]
    # 1. Per-flag results by group and horizon
    table = []
    for g, label, desc in GROUPS:
        gr = [r for r in rows if g in r["groups"]]
        per_month = {}
        for r in gr:
            per_month.setdefault(r["d"], 0)
            per_month[r["d"]] += 1
        h_out = {}
        for h in HORIZONS:
            xs = [(r["fw"][h][0] - r["fw"][h][1]) for r in gr if h in r["fw"]]
            early = sum(1 for r in gr if h in r["fw"] and r["fw"][h][2])
            h_out[h] = {"n": len(xs), "mean": _r(statistics.fmean(xs)) if xs else None,
                        "median": _r(statistics.median(xs)) if xs else None,
                        "beat": _r(sum(1 for x in xs if x > 0) / len(xs)) if xs else None, "ended_early": early}
        table.append({"id": g, "label": label, "desc": desc, "flags": len(gr),
                      "companies": len({r["t"] for r in gr}), "avg_per_month": round(len(gr) / max(1, len(months)), 1),
                      "months_with_flags": len(per_month), "h": h_out})

    # 2. Growth of $1: each month hold every name the group flagged in the last HOLD_MONTHS month-ends
    mret = {t: monthly_returns(s, months) for t, s in prices.items()}
    spy_m = monthly_returns(spy, months)
    flagged = {g: {} for g, _, _ in GROUPS}
    for r in rows:
        for g in r["groups"]:
            flagged[g].setdefault(r["d"], set()).add(r["t"])
    growth = {"dates": [], "series": {"spy": [1.0]}, "holdings": {}}
    for g, _, _ in GROUPS:
        growth["series"][g] = [1.0]
        growth["holdings"][g] = []
    growth["dates"].append(months[0])
    for k, d in enumerate(months[:-1]):
        nxt = months[k + 1]
        if d not in spy_m:
            continue
        growth["dates"].append(nxt)
        growth["series"]["spy"].append(growth["series"]["spy"][-1] * (1 + spy_m[d]))
        for g, _, _ in GROUPS:
            hold = set()
            for dd in months[max(0, k - HOLD_MONTHS + 1):k + 1]:
                hold |= flagged[g].get(dd, set())
            rs = [mret[t][d] for t in hold if d in mret.get(t, {})]
            r = statistics.fmean(rs) if rs else spy_m[d]   # nothing held: sit in the index
            growth["series"][g].append(growth["series"][g][-1] * (1 + r))
            growth["holdings"][g].append(len(rs))
    for k in growth["series"]:
        growth["series"][k] = [round(x, 4) for x in growth["series"][k]]

    # 3. Calendar-year returns of those portfolios
    years = {}
    for i in range(1, len(growth["dates"])):
        y = growth["dates"][i][:4]
        for k, ser in growth["series"].items():
            years.setdefault(y, {}).setdefault(k, 1.0)
            years[y][k] *= ser[i] / ser[i - 1]
    year_rows = [{"year": y, **{k: _r(v - 1) for k, v in vals.items()}} for y, vals in sorted(years.items())]

    # 4. Case studies: the shortlist group's best and worst 12-month outcomes (first flag of each episode)
    seen, cases = {}, []
    for r in sorted((r for r in rows if "gap_trigger" in r["groups"] and "12m" in r["fw"]), key=lambda r: r["d"]):
        last = seen.get(r["t"])
        if last and (date.fromisoformat(r["d"]) - date.fromisoformat(last)).days < 180:
            continue
        seen[r["t"]] = r["d"]
        ret, m, early = r["fw"]["12m"]
        cases.append({"d": r["d"], "t": r["t"], "name": meta.get(r["t"], {}).get("name", ""), "implied_g": _r(r["implied_g"]),
                      "ref_g": _r(r["ref_g"]), "ret": _r(ret), "spy": _r(m), "excess": _r(ret - m), "early": early})
    cases.sort(key=lambda c: c["excess"])
    flag_counts = {g: [sum(1 for r in rows if r["d"] == d and g in r["groups"]) for d in months] for g, _, _ in GROUPS}
    return {"table": table, "growth": growth, "years": year_rows, "baskets": baskets(rows, months, walked.get("rf")),
            "cases": {"best": cases[-8:][::-1], "worst": cases[:8], "n": len(cases)},
            "counts": {"dates": months, **flag_counts}}


BASKETS = [
    # id, label, question, sort ascending?, end labels (first fifth, last fifth)
    ("gap", "Valuation gap", "Priced for less growth than its record vs more", True, ("Cheapest", "Priciest")),
    ("fcf_yield", "Free-cash-flow yield", "FCF as a % of market value, highest to lowest", False, ("Highest yield", "Lowest yield")),
    ("mom", "12-month momentum", "Past 12-month price change, best to worst", False, ("Biggest winners", "Biggest losers")),
    ("mcap", "Size", "Market value, largest to smallest", False, ("Largest", "Smallest")),
    ("fcf_margin", "FCF margin", "Free cash flow as a % of revenue, highest to lowest", False, ("Highest", "Lowest")),
    ("roc", "Return on capital", "Cash return on capital, highest to lowest", False, ("Highest", "Lowest")),
]
SPLIT_YEAR = "2020"   # check each result holds in both halves of the period


RATE_CUTOFFS = (0.025, 0.03, 0.035)
MAIN_CUTOFF = 0.03


def regime_split(spreads: list[tuple[str, float]], rf: dict, months: list[str]) -> dict:
    """Average first-minus-last-fifth spread in high- vs low-rate months (10-year yield on the ranking date),
    at several cutoffs, and in months when rates had risen vs fallen over the prior year."""
    def stats(xs):
        return {"mean": _r(statistics.fmean(xs)), "months": len(xs), "won": _r(sum(1 for x in xs if x > 0) / len(xs))} if xs else None
    out = {"level": {}}
    for cut in RATE_CUTOFFS:
        hi = [x for d, x in spreads if rf.get(d) is not None and rf[d] >= cut]
        lo = [x for d, x in spreads if rf.get(d) is not None and rf[d] < cut]
        out["level"][f"{cut:.3f}"] = {"high": stats(hi), "low": stats(lo)}
    idx = {d: i for i, d in enumerate(months)}
    up, down = [], []
    for d, x in spreads:
        i = idx[d]
        if i >= 12 and rf.get(d) is not None and rf.get(months[i - 12]) is not None:
            (up if rf[d] > rf[months[i - 12]] else down).append(x)
    out["rising"], out["falling"] = stats(up), stats(down)
    return out


def baskets(rows: list[dict], months: list[str], rf: dict | None = None) -> dict:
    """Each month, rank the quality universe on a measure, split it into fifths, and average each fifth's
    return beyond the S&P 500. Months are weighted equally so no single crash or boom dominates."""
    by_month = {}
    for r in rows:
        by_month.setdefault(r["d"], []).append(r)

    def xs(r, h):
        return r["fw"][h][0] - r["fw"][h][1] if h in r["fw"] else None

    out = []
    for key, label, q, asc, ends in BASKETS:
        res = {"id": key, "label": label, "question": q, "ends": ends, "h": {}}
        for h in ("6m", "12m"):
            per_q = [[] for _ in range(5)]
            spreads = []          # (month, first fifth minus last fifth)
            for d in months:
                rs = [r for r in by_month.get(d, []) if r.get(key) is not None and xs(r, h) is not None]
                if len(rs) < 25:
                    continue
                rs.sort(key=lambda r: r[key], reverse=not asc)
                n = len(rs)
                means = []
                for k in range(5):
                    chunk = rs[k * n // 5:(k + 1) * n // 5]
                    m = statistics.fmean(xs(r, h) for r in chunk)
                    per_q[k].append(m)
                    means.append(m)
                spreads.append((d, means[0] - means[4]))
            if not spreads:
                continue
            early = [x for d, x in spreads if d < SPLIT_YEAR]
            late = [x for d, x in spreads if d >= SPLIT_YEAR]
            res["h"][h] = {
                "q": [_r(statistics.fmean(v)) for v in per_q],
                "spread": _r(statistics.fmean(x for _, x in spreads)),
                "consistency": _r(sum(1 for _, x in spreads if x > 0) / len(spreads)),
                "early": _r(statistics.fmean(early)) if early else None,
                "late": _r(statistics.fmean(late)) if late else None,
                "months": len(spreads),
                "regime": regime_split(spreads, rf or {}, months),
            }
        out.append(res)

    # Value x momentum: split each month at the median of each, giving four baskets
    two = {}
    for h in ("6m", "12m"):
        cells = {"cheap_up": [], "cheap_down": [], "pricey_up": [], "pricey_down": []}
        for d in months:
            rs = [r for r in by_month.get(d, []) if r.get("gap") is not None and r.get("mom") is not None and xs(r, h) is not None]
            if len(rs) < 25:
                continue
            mg = statistics.median(r["gap"] for r in rs)
            mm = statistics.median(r["mom"] for r in rs)
            for name, cond in (("cheap_up", lambda r: r["gap"] <= mg and r["mom"] > mm),
                               ("cheap_down", lambda r: r["gap"] <= mg and r["mom"] <= mm),
                               ("pricey_up", lambda r: r["gap"] > mg and r["mom"] > mm),
                               ("pricey_down", lambda r: r["gap"] > mg and r["mom"] <= mm)):
                c = [xs(r, h) for r in rs if cond(r)]
                if c:
                    cells[name].append((d, statistics.fmean(c)))
        two[h] = {k: {"mean": _r(statistics.fmean(x for _, x in v)) if v else None,
                      "early": _r(statistics.fmean([x for d, x in v if d < SPLIT_YEAR] or [float("nan")])) if v else None,
                      "late": _r(statistics.fmean([x for d, x in v if d >= SPLIT_YEAR] or [float("nan")])) if v else None,
                      "months": len(v)} for k, v in cells.items()}
    return {"factors": out, "value_momentum": two, "split_year": SPLIT_YEAR,
            "rates": {"dates": months, "rf": [_r(rf.get(d)) if rf else None for d in months]},
            "cutoff": MAIN_CUTOFF}


def _r(x):
    return None if x is None or not math.isfinite(x) else round(float(x), 4)


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default=START, help="first month, YYYY-MM")
    ap.add_argument("--out", type=Path, default=Path("backtest"))
    ap.add_argument("--cache", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, help="only the first N companies (for a quick trial)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args(argv)
    t0 = time.time()
    fetcher = LiveFetcher(env_user_agent(), C.SEC_RATE_LIMIT_PER_SEC)
    facts_dir = a.cache / "companyfacts"
    price_dir = a.cache / "bt_prices"

    universe = load_tickers(fetcher)
    if a.limit:
        universe = universe[:a.limit]
    log(f"universe: {len(universe)} companies listed today")
    refresh_facts_bulk(fetcher, facts_dir, 7.0, log, only_ciks={u["cik"] for u in universe})

    hist_start = f"{int(a.start[:4]) - 1}-{a.start[5:7]}-01"
    spy = load_history(fetcher, "SPY", hist_start, price_dir)
    tnx = load_history(fetcher, "^TNX", hist_start, price_dir)
    if not spy:
        sys.exit("could not load SPY history")
    months = month_ends(spy, a.start)
    log(f"{len(months)} month-ends from {months[0]} to {months[-1]}; 10-yr yield history: {'ok' if tnx else 'missing (fallback rate)'}")

    # 1. Point-in-time fundamentals for every company, in parallel
    jobs = [(str(facts_dir / f"CIK{u['cik']:010d}.json"), months) for u in universe
            if (facts_dir / f"CIK{u['cik']:010d}.json").exists()]
    pit, meta = {}, {}
    by_path = {str(facts_dir / f"CIK{u['cik']:010d}.json"): u for u in universe}
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        for k, (job, res) in enumerate(zip(jobs, pool.map(_pit_worker, jobs, chunksize=40)), 1):
            if res:
                u = by_path[job[0]]
                pit[u["ticker"]], meta[u["ticker"]] = res, {"ticker": u["ticker"], "name": u["name"], "cik": u["cik"]}
            if k % 1000 == 0:
                log(f"  fundamentals {k}/{len(jobs)} ({len(pit)} ever pass quality)")
    log(f"point-in-time fundamentals: {len(jobs)} companies with SEC data, {len(pit)} pass quality in at least one month")

    # 2. Daily price history for those companies
    prices, missing = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, (t, s) in enumerate(zip(sorted(pit), pool.map(lambda t: load_history(fetcher, t, hist_start, price_dir), sorted(pit))), 1):
            if s:
                prices[t] = s
            else:
                missing.append(t)
            if k % 250 == 0:
                log(f"  prices {k}/{len(pit)}")
    log(f"prices: {len(prices)} ok, {len(missing)} missing")

    # 3. Walk forward month by month
    walked = walk(pit, meta, prices, spy, tnx, months)
    log(f"walk-forward: {len(walked['rows'])} company-months in the priced quality universe")
    res = summarize(walked, prices, spy, months, meta)
    res["meta"] = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "start": months[0], "end": months[-1],
        "months": len(months), "universe": len(universe), "with_sec_data": len(jobs), "ever_quality": len(pit),
        "priced": len(prices), "price_missing": len(missing), "company_months": len(walked["rows"]),
        "hold_months": HOLD_MONTHS, "hurdle_source": "Yahoo ^TNX + premium" if tnx else "fallback",
        "rules": {k: getattr(C, k) for k in ("MIN_REVENUE_USD", "MIN_FCF_MARGIN", "MIN_MARKET_CAP_USD", "GAP_THRESHOLD",
                                              "MAX_IMPLIED_GROWTH", "DROP_THRESHOLD", "DRAWDOWN_THRESHOLD", "EQUITY_PREMIUM")},
        "seconds": round(time.time() - t0),
    }
    write_results(a.out, res)
    for row in res["table"]:
        h = row["h"]["12m"]
        log(f"  {row['label']:<30} flags {row['flags']:>6}  12m excess mean {_pct(h['mean'])} median {_pct(h['median'])} beat {_pct(h['beat'])}")
    for f in res["baskets"]["factors"]:
        h = f["h"].get("12m")
        if h:
            log(f"  basket {f['label']:<22} 12m by fifth {' '.join(_pct(x) for x in h['q'])}  spread {_pct(h['spread'])} "
                f"({_pct(h['consistency'])} of months; before {SPLIT_YEAR} {_pct(h['early'])}, after {_pct(h['late'])})")
    for f in res["baskets"]["factors"]:
        g = (f["h"].get("12m") or {}).get("regime")
        if g:
            lv = " | ".join(f"{float(c)*100:.1f}%: hi {_pct((v['high'] or {}).get('mean'))} lo {_pct((v['low'] or {}).get('mean'))}"
                            for c, v in g["level"].items())
            log(f"  rates {f['label']:<22} {lv} | rising {_pct((g['rising'] or {}).get('mean'))} falling {_pct((g['falling'] or {}).get('mean'))}")
    vm = res["baskets"]["value_momentum"].get("12m", {})
    log("  value x momentum 12m: " + ", ".join(f"{k} {_pct(v['mean'])}" for k, v in vm.items()))
    log(f"done in {res['meta']['seconds']}s -> {a.out}/")


def _pct(x):
    return "   n/a" if x is None else f"{x*100:+6.1f}%"


def write_results(out: Path, res: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(res, separators=(",", ":")))
    tpl = (Path(__file__).parent / "backtest_template.html").read_text()
    (out / "index.html").write_text(tpl.replace("__DATA__", json.dumps(res, separators=(",", ":")).replace("</", "<\\/")))


if __name__ == "__main__":
    main()
