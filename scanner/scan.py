"""Daily cash-compounder scan.

Universe -> quality filter -> event triggers (sharp drop, insider cluster buys)
-> reverse-DCF gap -> shortlist. Writes report.html, results.csv and results.json.

Usage:
  SEC_USER_AGENT="Your Name you@example.com" python -m scanner.scan --out out/
  python -m scanner.scan --fixtures fixtures/ --asof 2026-11-06 --out out/   # offline test
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .analysis import fundamentals, implied_growth, parse_form4, price_stats
from .report import render_report
from .sources import (FetchError, FixtureFetcher, LiveFetcher, env_user_agent,
                      load_company_facts, load_filing_text, load_form4_index,
                      load_prices, load_tickers, refresh_facts_bulk, SourceBreaker)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def business_days_back(asof: date, n_calendar: int) -> list[str]:
    out = []
    for k in range(n_calendar):
        d = asof - timedelta(days=k)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


def run(fetcher, asof: date, out_dir: Path, cache_dir: Path | None, limit: int | None, tickers_only: list[str] | None):
    out_dir.mkdir(parents=True, exist_ok=True)
    funnel = {}

    # 1. Universe
    universe = load_tickers(fetcher)
    if tickers_only:
        want = {t.upper() for t in tickers_only}
        universe = [u for u in universe if u["ticker"].upper() in want]
    if limit:
        universe = universe[:limit]
    funnel["universe"] = len(universe)
    log(f"universe: {len(universe)} companies")

    # 2. Fundamentals + quality filter
    #    Full runs read a weekly bulk download; small --tickers runs call the per-company API.
    bulk = cache_dir is not None and not tickers_only and hasattr(fetcher, "download_to")
    if bulk:
        refresh_facts_bulk(fetcher, cache_dir, C.FUNDAMENTALS_CACHE_DAYS, log, only_ciks={u["cik"] for u in universe})
    quality = []
    for i, co in enumerate(universe, 1):
        if i % 250 == 0:
            log(f"  fundamentals {i}/{len(universe)}")
        facts = load_company_facts(fetcher, co["cik"], cache_dir, C.FUNDAMENTALS_CACHE_DAYS, allow_network=not bulk)
        if not facts:
            continue
        fu = fundamentals(facts)
        if not fu:
            continue
        reasons = quality_fail_reasons(fu)
        if reasons:
            continue
        quality.append({**co, **fu})
    funnel["has_data_and_quality"] = len(quality)
    log(f"quality pass (before market-cap check): {len(quality)}")

    # 3. Prices, market cap, reverse DCF
    priced = []
    breaker = SourceBreaker()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(load_prices, fetcher, co["ticker"], breaker): co for co in quality}
        closes_by = {}
        for k, fut in enumerate(as_completed(futures), 1):
            closes_by[futures[fut]["ticker"]] = fut.result()
            if k % 100 == 0:
                log(f"  prices {k}/{len(quality)}")
    missing = sum(1 for v in closes_by.values() if not v)
    log(f"prices: {len(quality) - missing} ok, {missing} missing; source failures: {breaker.fails}")
    funnel["price_missing"] = missing
    for co in quality:
        closes = closes_by.get(co["ticker"], [])
        ps = price_stats(closes, C.DROP_LOOKBACK_DAYS)
        if not ps or not co.get("shares"):
            continue
        mcap = ps["last"] * co["shares"]
        if mcap < C.MIN_MARKET_CAP_USD:
            continue
        g = implied_growth(co["fcf"], mcap - co["net_cash"], C.REQUIRED_RETURN, C.TERMINAL_GROWTH, C.FORECAST_YEARS)
        gap = (g - co["fcf_cagr"]) if (g is not None and math.isfinite(g) and co["fcf_cagr"] is not None) else None
        priced.append({**co, **ps, "mcap": mcap, "p_fcf": mcap / co["fcf"], "implied_g": g, "gap": gap})
    funnel["quality"] = len(priced)
    log(f"quality + priced + above market-cap floor: {len(priced)}")

    # 4. Insider cluster buys among quality names
    by_cik = {c["cik"]: c for c in priced}
    insider = defaultdict(lambda: {"buyers": {}, "by_buyer": defaultdict(float), "value": 0.0, "filings": set()})
    for ymd in business_days_back(asof, C.INSIDER_LOOKBACK_DAYS):
        idx = load_form4_index(fetcher, ymd)
        log(f"  form 4 index {ymd}: {len(idx)} lines, {sum(1 for c, _ in idx if c in by_cik)} for quality names")
        for cik, path in idx:
            if cik not in by_cik or path in insider[cik]["filings"]:
                continue
            txt = load_filing_text(fetcher, path)
            parsed = parse_form4(txt) if txt else None
            if not parsed or parsed["issuer_cik"] != cik:
                continue
            rec = insider[cik]
            rec["filings"].add(path)
            v = sum(b["value"] for b in parsed["buys"])
            for o in parsed["owners"]:
                rec["buyers"][o["name"]] = o["role"]
                rec["by_buyer"][o["name"]] += v / max(1, len(parsed["owners"]))
            rec["value"] += v

    # 5. Flags and shortlist
    for c in priced:
        ins = insider.get(c["cik"])
        c["insider_buyers"] = sorted((n, r) for n, r in ins["buyers"].items()
                                     if ins["by_buyer"][n] >= C.MIN_BUY_VALUE_USD) if ins else []
        c["insider_value"] = ins["value"] if ins else 0.0
        c["flag_drop"] = c["chg"] <= C.DROP_THRESHOLD
        c["flag_cluster"] = len(c["insider_buyers"]) >= C.MIN_CLUSTER_BUYERS
        c["flag_gap"] = c["gap"] is not None and c["gap"] <= C.GAP_THRESHOLD
        c["triggered"] = c["flag_drop"] or c["flag_cluster"]
        c["score"] = score(c)
    shortlist = sorted([c for c in priced if c["triggered"] and c["flag_gap"]], key=lambda c: -c["score"])
    triggered_only = sorted([c for c in priced if c["triggered"] and not c["flag_gap"]], key=lambda c: c["chg"])
    gap_only = sorted([c for c in priced if c["flag_gap"] and not c["triggered"]], key=lambda c: c["gap"])
    funnel["triggered"] = sum(1 for c in priced if c["triggered"])
    funnel["shortlist"] = len(shortlist)

    meta = {"asof": asof.isoformat(), "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "config": {k: getattr(C, k) for k in dir(C) if k.isupper()}, "funnel": funnel}
    write_outputs(out_dir, meta, shortlist, triggered_only, gap_only, priced)
    log(f"shortlist: {len(shortlist)}  -> {out_dir/'report.html'}")
    log(f"triggers: {sum(c['flag_drop'] for c in priced)} sharp drops, {sum(c['flag_cluster'] for c in priced)} insider clusters; "
        f"{sum(c['flag_gap'] for c in priced)} of {len(priced)} cheap vs track record")
    for c in shortlist[:60]:
        log(f"  {c['ticker']:<6} 5d {c['chg']*100:+6.1f}%  insiders {len(c['insider_buyers'])}  "
            f"implied {c['implied_g']*100:5.1f}%  track {c['fcf_cagr']*100:5.1f}%  P/FCF {c['p_fcf']:5.1f}  {c['fcf_basis']}")
    write_step_summary(meta, shortlist)
    return meta, shortlist


def write_step_summary(meta, shortlist):
    """A table on the GitHub Actions run page, readable without downloading anything."""
    import os
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    f = meta["funnel"]
    lines = [f"## Daily scan · {meta['asof']}", "",
             f"{f.get('universe',0):,} companies → {f.get('quality',0):,} quality → {f.get('triggered',0):,} triggered → **{f.get('shortlist',0):,} shortlist**", "",
             "| Ticker | 5-day | Insiders | Implied growth | Track record | P/FCF |", "|---|---:|---:|---:|---:|---:|"]
    for c in shortlist[:40]:
        lines.append(f"| {c['ticker']} | {c['chg']*100:+.1f}% | {len(c['insider_buyers']) or '–'} | {c['implied_g']*100:.1f}% | {c['fcf_cagr']*100:.1f}% | {c['p_fcf']:.0f}x |")
    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def quality_fail_reasons(fu) -> list[str]:
    r = []
    if not fu["revenue"] or fu["revenue"] < C.MIN_REVENUE_USD:
        r.append("revenue")
    if fu["fcf_margin"] is None or fu["fcf_margin"] < C.MIN_FCF_MARGIN:
        r.append("fcf_margin")
    if fu["positive_fcf_years"] < C.MIN_POSITIVE_FCF_YEARS:
        r.append("fcf_consistency")
    if fu["cash_roc"] is None or fu["cash_roc"] < C.MIN_CASH_RETURN_ON_CAPITAL:
        r.append("return_on_capital")
    if fu["capex_missing"]:
        r.append("capex_missing")
    return r


def score(c) -> float:
    """Higher = more interesting. Mostly the size of the gap, plus trigger strength."""
    s = 0.0
    if c["gap"] is not None and math.isfinite(c["gap"]):
        s += max(0.0, -c["gap"]) * 100
    s += max(0.0, -c["chg"]) * 50
    s += 3 * len(c["insider_buyers"])
    return round(s, 2)


def _row(c):
    fin = lambda x: (round(x, 4) if isinstance(x, float) and math.isfinite(x) else None)
    return {
        "ticker": c["ticker"], "name": c["name"], "cik": c["cik"],
        "price": round(c["last"], 2), "price_date": c["last_date"],
        "chg_5d": fin(c["chg"]), "off_52w_high": fin(c["off_high"]),
        "mcap_bn": round(c["mcap"] / 1e9, 2), "fcf_bn": round(c["fcf"] / 1e9, 3),
        "fy_end": c["fy_end"], "fcf_basis": c["fcf_basis"], "fcf_period_end": c["fcf_period_end"],
        "fy_fcf_bn": round(c["fy_fcf"] / 1e9, 3),
        "p_fcf": round(c["p_fcf"], 1), "net_cash_bn": round(c["net_cash"] / 1e9, 2),
        "fcf_margin": fin(c["fcf_margin"]), "cash_roc": fin(c["cash_roc"]),
        "fcf_cagr": fin(c["fcf_cagr"]), "implied_g": fin(c["implied_g"]) if c["implied_g"] is not None else None,
        "gap": fin(c["gap"]) if c["gap"] is not None else None,
        "insider_buyers": [f"{n} ({r})" for n, r in c["insider_buyers"]],
        "insider_value": round(c["insider_value"]),
        "flag_drop": c["flag_drop"], "flag_cluster": c["flag_cluster"], "flag_gap": c["flag_gap"],
        "score": c["score"], "spark": c["spark"],
        "fcf_history": [{"fy_end": h["fy_end"], "fcf_bn": round(h["fcf"] / 1e9, 3), "ttm": h.get("ttm", False)}
                        for h in c["history"]],
    }


def write_outputs(out_dir, meta, shortlist, triggered_only, gap_only, priced):
    data = {"meta": meta,
            "shortlist": [_row(c) for c in shortlist],
            "triggered_only": [_row(c) for c in triggered_only[:25]],
            "gap_only": [_row(c) for c in gap_only[:25]],
            "all_quality": [_row(c) for c in priced]}
    (out_dir / "results.json").write_text(json.dumps(data, indent=1))
    cols = ["ticker", "name", "price", "chg_5d", "mcap_bn", "fcf_bn", "p_fcf", "fcf_margin", "cash_roc",
            "fcf_cagr", "implied_g", "gap", "flag_drop", "flag_cluster", "flag_gap", "score"]
    with open(out_dir / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(data["all_quality"], key=lambda r: -r["score"]):
            w.writerow(r)
    (out_dir / "report.html").write_text(render_report(data))
    (out_dir / "explorer.html").write_text(render_explorer(data))


def render_explorer(data, max_companies=10) -> str:
    """Fill the reverse-DCF explorer with today's names: shortlist, then watchlist, then triggered."""
    picked, seen = [], set()
    for key in ("shortlist", "gap_only", "triggered_only"):
        for r in data[key]:
            if r["ticker"] not in seen and len(picked) < max_companies:
                picked.append(r)
                seen.add(r["ticker"])
    if not picked:  # quiet day: show the biggest quality names instead
        picked = sorted(data["all_quality"], key=lambda r: -r["mcap_bn"])[:max_companies]
    cos = []
    for r in picked:
        bases = [{"label": ("TTM to " if r["fcf_basis"] == "TTM" else "FY ending ") + r["fcf_period_end"], "fcf": r["fcf_bn"]}]
        if r["fcf_basis"] == "TTM":
            bases.append({"label": "FY ending " + r["fy_end"], "fcf": r["fy_fcf_bn"]})
        hist = [["TTM" if h["ttm"] else "FY" + h["fy_end"][2:4], h["fcf_bn"], 0] for h in r["fcf_history"]]
        first_fy = next((h for h in r["fcf_history"][-4:] if not h["ttm"]), r["fcf_history"][0])
        cos.append({
            "id": r["ticker"].lower(), "name": r["ticker"],
            "mcap": r["mcap_bn"], "mcapNote": f"{r['price_date']} close",
            "netCash": r["net_cash_bn"], "netNote": "cash + short-term investments − long-term debt",
            "bases": bases,
            "track": round((r["fcf_cagr"] or 0) * 100, 1),
            "trackNote": f"3 fiscal years ago → {'TTM' if r['fcf_basis'] == 'TTM' else 'latest FY'}",
            "hist": hist,
        })
    tpl = (Path(__file__).parent / "explorer_template.html").read_text()
    return (tpl.replace("__COMPANIES_JSON__", json.dumps(cos))
               .replace("__ASOF__", data["meta"]["asof"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--fixtures", type=Path, help="run offline against a fixtures folder")
    ap.add_argument("--cache", type=Path, default=Path(".cache/companyfacts"))
    ap.add_argument("--asof", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--limit", type=int, help="only scan the first N companies (testing)")
    ap.add_argument("--tickers", nargs="*", help="only scan these tickers")
    a = ap.parse_args(argv)
    if a.fixtures:
        fetcher, cache = FixtureFetcher(a.fixtures), None
    else:
        fetcher, cache = LiveFetcher(env_user_agent(), C.SEC_RATE_LIMIT_PER_SEC), a.cache
    run(fetcher, a.asof, a.out, cache, a.limit, a.tickers)


if __name__ == "__main__":
    main()
