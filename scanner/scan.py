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
import os
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from . import track as T
from .analysis import fundamentals, implied_growth, parse_form4, price_stats
from .sources import (load_risk_free, FetchError, FixtureFetcher, LiveFetcher, env_user_agent,
                      load_company_facts, load_filing_text, load_form4_index,
                      load_prices, load_tickers, refresh_facts_bulk, SourceBreaker, load_submissions, sector_for)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def business_days_back(asof: date, n_calendar: int) -> list[str]:
    out = []
    for k in range(n_calendar):
        d = asof - timedelta(days=k)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


def run(fetcher, asof: date, out_dir: Path, cache_dir: Path | None, limit: int | None, tickers_only: list[str] | None,
        ledger_path: Path | None = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    funnel = {}

    # 0. The hurdle: 10-year Treasury yield + equity premium, so rate moves flow into every valuation
    rf = load_risk_free(fetcher, asof)
    rf_rate = rf["rate"] if rf["rate"] is not None else C.RISK_FREE_FALLBACK
    C.REQUIRED_RETURN = round(min(C.REQUIRED_RETURN_MAX, max(C.REQUIRED_RETURN_MIN, rf_rate + C.EQUITY_PREMIUM)), 4)
    rates = {"rf": rf_rate, "rf_date": rf["date"], "rf_source": rf["source"] or "fallback (lookup failed)",
             "premium": C.EQUITY_PREMIUM, "base": C.REQUIRED_RETURN}
    log(f"hurdle: 10-yr Treasury {rf_rate*100:.2f}% ({rates['rf_source']}, {rf['date']}) + {C.EQUITY_PREMIUM*100:.1f}% premium "
        f"= {C.REQUIRED_RETURN*100:.2f}% base required return")

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
    spy_closes = load_prices(fetcher, "SPY", breaker)
    mkt = price_stats(spy_closes, C.DROP_LOOKBACK_DAYS, C.DRAWDOWN_DAYS) if C.DROP_RELATIVE_TO_MARKET else None
    mkt_chg = mkt["chg"] if mkt else 0.0
    mkt_dd = mkt["off_1m_high"] if mkt else 0.0
    log(f"market (SPY): {C.DROP_LOOKBACK_DAYS}-day change {mkt_chg*100:+.1f}%, {mkt_dd*100:+.1f}% from its 1-month high"
        if mkt else "market: SPY unavailable, using absolute moves")
    meta_market = {"chg": mkt_chg, "off_1m_high": mkt_dd} if mkt else {}
    missing = sum(1 for v in closes_by.values() if not v)
    log(f"prices: {len(quality) - missing} ok, {missing} missing; source failures: {breaker.fails}")
    funnel["price_missing"] = missing
    for co in quality:
        v = value_company(co, price_stats(closes_by.get(co["ticker"], []), C.DROP_LOOKBACK_DAYS, C.DRAWDOWN_DAYS),
                          mkt_chg, mkt_dd, C.REQUIRED_RETURN)
        if v:
            priced.append(v)
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

    for c in priced:   # diagnostic: raw share inputs for names set in SHARES_DEBUG (comma-separated tickers)
        if c["ticker"] in os.environ.get("SHARES_DEBUG", "").split(","):
            log(f"shares debug {c['ticker']}: using {c.get('shares'):,.0f} as of {c.get('shares_asof')}; price {c.get('last')}")
            raw = _raw_facts(cache_dir, c["cik"])
            for ns, k, u in (("dei", "EntityCommonStockSharesOutstanding", "shares"), ("us-gaap", "EarningsPerShareDiluted", "USD/shares"),
                             ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")):
                rows = sorted(raw.get(ns, {}).get(k, {}).get("units", {}).get(u, []), key=lambda x: x["end"])[-6:]
                log(f"   {k}: " + "; ".join(f"{x.get('start','')}..{x['end']} {x['val']:,} {x.get('form','')} {x.get('accn','')[-8:]}" for x in rows))

    # 5. Industry codes and earnings dates (SEC submissions API, one call per quality name)
    for c in priced:
        sub = load_submissions(fetcher, c["cik"])
        c["sector"], c["sic_desc"] = sector_for(sub["sic"]), sub["sic_desc"]
        c["earnings_dates"] = sub["earnings_dates"]
        c["sic"] = sub["sic"]
    n_fin = sum(1 for c in priced if is_financial(c.get("sic")))
    priced = [c for c in priced if not is_financial(c.get("sic"))]
    funnel["financials_excluded"] = n_fin
    funnel["quality"] = len(priced)
    log(f"excluded {n_fin} banks, lenders, brokers and insurers -> {len(priced)} in the quality universe")

    # 6. Flags and shortlist
    for c in priced:
        ins = insider.get(c["cik"])
        c["insider_buyers"] = sorted((n, r) for n, r in ins["buyers"].items()
                                     if ins["by_buyer"][n] >= C.MIN_BUY_VALUE_USD) if ins else []
        c["insider_value"] = ins["value"] if ins else 0.0
        c.update(price_flags(c))
        # Did it fall on its own earnings report, or with the market?
        since = min([c["chg_start_date"]] + ([c["high_1m_date"]] if c["flag_drawdown"] else []))
        hits = sorted(d for d in c["earnings_dates"] if d >= since)
        c["earnings_in_window"] = hits[-1] if hits else None
        c["flag_cluster"] = len(c["insider_buyers"]) >= C.MIN_CLUSTER_BUYERS
        c["flag_gap"] = gap_flag(c["gap"], c["implied_g"])
        c["price_trigger"] = c["flag_drop"] or c["flag_drawdown"]
        c["triggered"] = c["price_trigger"] or c["flag_cluster"]
        c["score"] = score(c)
    shortlist = sorted([c for c in priced if c["triggered"] and c["flag_gap"]], key=lambda c: -c["score"])
    triggered_only = sorted([c for c in priced if c["triggered"] and not c["flag_gap"]], key=lambda c: c["chg"])
    gap_only = sorted([c for c in priced if c["flag_gap"] and not c["triggered"]], key=lambda c: c["gap"])
    funnel["triggered"] = sum(1 for c in priced if c["triggered"])
    funnel["modest"] = sum(1 for c in priced if c["flag_gap"])
    funnel["shortlist"] = len(shortlist)

    funnel["peak_margin"] = sum(1 for c in priced if c.get("peak_margin"))
    # Cash-yield leaders: the top fifth of the quality universe by free-cash-flow yield (FCF on the screen's
    # own base, so cyclicals and peak-margin names use their normalized figure). The backtest found this
    # measure did best when the 10-year yield was 3% or higher; see meta["regime"].
    leaders = sorted(priced, key=lambda c: -c["fcf_yield"])[:max(1, len(priced) // 5)] if priced else []
    funnel["yield_leaders"] = len(leaders)
    regime = load_regime(rates["rf"])

    # 7. Track record: log today's flags, then measure every open entry against the S&P 500
    ledger = T.load_ledger(ledger_path)
    groups = {"yield": leaders, "shortlist": shortlist, "watchlist": gap_only, "triggered": triggered_only}
    changes = T.list_changes(ledger, asof, groups)
    for g, cs in groups.items():
        for c in cs:
            if c["ticker"] in changes["new"][g]:
                c.setdefault("new_in", []).append(g)
    if changes["prev_date"]:
        log("since " + changes["prev_date"] + ": " + "; ".join(
            f"{g} +{len(changes['new'][g])} new, -{len(changes['dropped'][g])} dropped" for g in groups))
    added = T.add_flags(ledger, asof, groups, spy_closes)
    need = T.tickers_needing_prices(ledger, asof) - {t for t, v in closes_by.items() if v}
    if need:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for t, closes in zip(sorted(need), pool.map(lambda t: load_prices(fetcher, t, breaker), sorted(need))):
                closes_by[t] = closes
    T.update_returns(ledger, closes_by, spy_closes)
    if ledger_path:
        T.save_ledger(ledger_path, ledger)
    track = T.summarize(ledger, asof)
    log(f"track record: +{added} new entries today, {track['n']} total since {track['started']}"
        + (f" ({len(need)} extra price lookups)" if need else ""))

    meta = {"asof": asof.isoformat(), "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "config": {k: getattr(C, k) for k in dir(C) if k.isupper()}, "funnel": funnel, "market": meta_market,
            "rates": rates, "track": track, "changes": changes, "regime": regime}
    write_outputs(out_dir, meta, shortlist, triggered_only, gap_only, priced, leaders)
    (out_dir / "track").mkdir(exist_ok=True)
    T.write_csv(out_dir / "track" / "track_record.csv", ledger)
    T.save_ledger(out_dir / "track" / "ledger.json", ledger)
    log(f"shortlist: {len(shortlist)}  -> {out_dir/'report.html'}")
    log(f"triggers: {sum(c['flag_drop'] for c in priced)} sharp drops, {sum(c['flag_drawdown'] for c in priced)} 1-month drawdowns "
        f"({sum(1 for c in priced if c['price_trigger'] and c['earnings_in_window'])} on their own earnings), {sum(c['flag_cluster'] for c in priced)} insider clusters; "
        f"{sum(c['flag_gap'] for c in priced)} of {len(priced)} with modest expectations vs reference growth")
    for c in shortlist[:60]:
        log(f"  {c['ticker']:<6} 5d {c['chg']*100:+6.1f}%  insiders {len(c['insider_buyers'])}  "
            f"1m {c['off_1m_high']*100:+6.1f}%  {('EARNINGS ' + c['earnings_in_window'] if c['earnings_in_window'] else 'with market') if c['price_trigger'] else 'no price drop'}  "
            f"implied {c['implied_g']*100:5.1f}%  ref {c['ref_g']*100:5.1f}%  r {c['required_return']*100:.1f}%  P/FCF {c['p_fcf']:5.1f}  {c['fcf_basis']}")
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
             "| Ticker | 5-day | Insiders | Implied growth | Reference growth | P/FCF |", "|---|---:|---:|---:|---:|---:|"]
    for c in shortlist[:40]:
        lines.append(f"| {c['ticker']} | {c['chg']*100:+.1f}% | {len(c['insider_buyers']) or '–'} | {c['implied_g']*100:.1f}% | {c['ref_g']*100:.1f}% | {c['p_fcf']:.0f}x |")
    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def risk_premium(co) -> tuple[float, list[str]]:
    """Riskier businesses have to clear a higher bar: a cheap price is often fair pay for risk."""
    prem, why = 0.0, []
    if co.get("volatile"):
        prem += C.RISK_PREMIUM_CYCLICAL
        why.append("cyclical")
    net_debt = -co.get("net_cash", 0.0)
    if co.get("fcf", 0) > 0 and net_debt > C.LEVERAGE_NET_DEBT_TO_FCF * co["fcf"]:
        prem += C.RISK_PREMIUM_LEVERAGE
        why.append("leveraged")
    return prem, why


def reference_growth(co):
    """A conservative growth yardstick: the lower of FCF and revenue growth per share
    (so buybacks help and dilution hurts), capped."""
    cands = [x for x in (co.get("fcf_ps_cagr"), co.get("rev_ps_cagr")) if x is not None]
    if not cands:
        return None
    return min(min(cands), C.TRACK_CAP)


def value_company(co, ps, mkt_chg, mkt_dd, base_r):
    """Market cap, risk-adjusted required return, implied growth and the gap to reference growth.
    Shared by the daily scan and the backtest so both apply identical rules. None if unpriced or too small."""
    if not ps or not co.get("shares"):
        return None
    mcap = ps["last"] * co["shares"]
    if mcap < C.MIN_MARKET_CAP_USD:
        return None
    prem, why = risk_premium(co)
    r = base_r + prem
    g = implied_growth(co["fcf"], mcap - co["net_cash"], r, C.TERMINAL_GROWTH, C.FORECAST_YEARS)
    g_ttm = g
    # Cyclicals are judged on a 3-year average and peak-margin names on normalized cash flow
    # (revenue x their own average margin), not one boom year. If both apply, the lower base wins.
    alt = [x for x, use in ((co["fcf_avg3"], co["volatile"]), (co.get("fcf_normalized"), co.get("peak_margin")))
           if use and x and x > 0]
    if alt:
        g = implied_growth(min(alt), mcap - co["net_cash"], r, C.TERMINAL_GROWTH, C.FORECAST_YEARS)
    fcf_base = min(alt) if alt else co["fcf"]
    base_kind = "latest"
    if alt:
        base_kind = "avg3" if (co["volatile"] and co["fcf_avg3"] == fcf_base) else "normalized"
    ref = reference_growth(co)
    ok = lambda x: x is not None and math.isfinite(x)
    gap = (g - ref) if (ok(g) and ref is not None) else None
    return {**co, **ps, "rel_chg": ps["chg"] - mkt_chg, "rel_dd": ps["off_1m_high"] - mkt_dd, "mcap": mcap,
            "p_fcf": mcap / co["fcf"], "fcf_yield": fcf_base / mcap, "fcf_base": fcf_base, "base_kind": base_kind, "implied_g": g, "implied_g_ttm": g_ttm, "ref_g": ref, "gap": gap,
            "gap_ttm": (g_ttm - ref) if (ok(g_ttm) and ref is not None) else None,
            "risk_premium": prem, "risk_why": why, "required_return": r}


def price_flags(c) -> dict:
    return {"flag_drop": c["rel_chg"] <= C.DROP_THRESHOLD, "flag_drawdown": c["rel_dd"] <= C.DRAWDOWN_THRESHOLD}


def gap_flag(gap, implied_g) -> bool:
    return bool(gap is not None and implied_g is not None and math.isfinite(implied_g)
                and gap <= C.GAP_THRESHOLD and implied_g <= C.MAX_IMPLIED_GROWTH)


def load_regime(rf, path: Path = Path("backtest/results.json")) -> dict | None:
    """What the backtest says about today's rate environment, for the note on the home page."""
    try:
        b = json.loads(path.read_text())["baskets"]
    except (OSError, ValueError, KeyError):
        return None
    cut = b.get("cutoff", 0.03)
    f = next((f for f in b["factors"] if f["id"] == "fcf_yield"), None)
    lv = (((f or {}).get("h", {}).get("12m") or {}).get("regime") or {}).get("level", {}).get(f"{cut:.3f}")
    if not lv:
        return None
    hi_years = sorted({d[:4] for d, v in zip(b["rates"]["dates"], b["rates"]["rf"]) if v is not None and v >= cut})
    try:
        tbl = {r["id"]: r["h"]["12m"] for r in json.loads(path.read_text())["table"]}
    except (OSError, ValueError, KeyError):
        tbl = {}
    pick = lambda g, k: (tbl.get(g) or {}).get(k)
    return {"rf": rf, "cutoff": cut, "high": rf is not None and rf >= cut, "hi": lv["high"], "lo": lv["low"],
            "hi_years": hi_years, "start": b["rates"]["dates"][0][:4],
            "drop_only_median": pick("trigger_only", "median"), "drop_only_beat": pick("trigger_only", "beat"),
            "gap_drop_median": pick("gap_trigger", "median"), "gap_drop_mean": pick("gap_trigger", "mean")}


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
    if fu["fcf_margin"] is not None and fu["fcf_margin"] > C.MAX_FCF_MARGIN:
        r.append("implausible_fcf_margin")
    return r


def _raw_facts(cache_dir, cik):
    try:
        return json.loads((Path(cache_dir) / f"CIK{cik:010d}.json").read_text()).get("facts", {})
    except Exception:
        return {}


def is_financial(sic) -> bool:
    return sic is not None and any(lo <= sic <= hi for lo, hi in C.EXCLUDED_SIC)


def score(c) -> float:
    """Higher = more interesting. Mostly the size of the gap, plus trigger strength."""
    s = 0.0
    if c["gap"] is not None and math.isfinite(c["gap"]):
        s += max(0.0, -c["gap"]) * 100
    s += max(0.0, -c["rel_chg"], -c["rel_dd"]) * 50
    s += 3 * len(c["insider_buyers"])
    return round(s, 2)


def _row(c):
    fin = lambda x: (round(x, 4) if isinstance(x, float) and math.isfinite(x) else None)
    return {
        "ticker": c["ticker"], "name": c["name"], "cik": c["cik"], "new_in": c.get("new_in", []),
        "fcf_yield": fin(c.get("fcf_yield")), "fcf_base_bn": round(c["fcf_base"] / 1e9, 4) if c.get("fcf_base") else None,
        "base_kind": c.get("base_kind", "latest"), "math": c.get("math"), "mcap": c.get("mcap"),
        "sector": c.get("sector", "Other"), "sic_desc": c.get("sic_desc", ""),
        "price": round(c["last"], 2), "price_date": c["last_date"],
        "chg_5d": fin(c["chg"]), "rel_chg_5d": fin(c["rel_chg"]),
        "off_1m_high": fin(c["off_1m_high"]), "rel_dd": fin(c["rel_dd"]), "high_1m_date": c["high_1m_date"],
        "flag_drawdown": c["flag_drawdown"], "earnings_in_window": c["earnings_in_window"], "off_52w_high": fin(c["off_high"]),
        "mcap_bn": round(c["mcap"] / 1e9, 2), "fcf_bn": round(c["fcf"] / 1e9, 3),
        "fy_end": c["fy_end"], "fcf_basis": c["fcf_basis"], "fcf_period_end": c["fcf_period_end"],
        "fy_fcf_bn": round(c["fy_fcf"] / 1e9, 3), "fcf_avg3_bn": round(c["fcf_avg3"] / 1e9, 3), "volatile": c["volatile"],
        "peak_margin": bool(c.get("peak_margin")), "margin_avg": fin(c.get("margin_avg")), "margin_years": c.get("margin_years", 0),
        "fcf_norm_bn": round(c["fcf_normalized"] / 1e9, 3) if c.get("fcf_normalized") else None,
        "p_fcf": round(c["p_fcf"], 1), "net_cash_bn": round(c["net_cash"] / 1e9, 2),
        "fcf_margin": fin(c["fcf_margin"]), "cash_roc": fin(c["cash_roc"]),
        "fcf_cagr": fin(c["fcf_cagr"]), "rev_cagr": fin(c["rev_cagr"]),
        "fcf_ps_cagr": fin(c["fcf_ps_cagr"]), "rev_ps_cagr": fin(c["rev_ps_cagr"]), "share_cagr": fin(c["share_cagr"]),
        "sbc_bn": round((c["sbc"] or 0) / 1e9, 3), "sbc_pct_rev": fin(c["sbc_pct_rev"]),
        "risk_premium": c["risk_premium"], "risk_why": c["risk_why"], "required_return": round(c["required_return"], 4), "ref_g": fin(c["ref_g"]) if c["ref_g"] is not None else None,
        "implied_g": fin(c["implied_g"]) if c["implied_g"] is not None else None,
        "implied_g_ttm": fin(c["implied_g_ttm"]) if c["implied_g_ttm"] is not None else None,
        "gap": fin(c["gap"]) if c["gap"] is not None else None,
        "insider_buyers": [f"{n} ({r})" for n, r in c["insider_buyers"]],
        "insider_value": round(c["insider_value"]),
        "flag_drop": c["flag_drop"], "flag_cluster": c["flag_cluster"], "flag_gap": c["flag_gap"],
        "score": c["score"], "spark": c["spark"],
        "fcf_history": [{"fy_end": h["fy_end"], "fcf_bn": round(h["fcf"] / 1e9, 3), "ttm": h.get("ttm", False)}
                        for h in c["history"]],
    }


def write_outputs(out_dir, meta, shortlist, triggered_only, gap_only, priced, leaders=()):
    data = {"meta": meta, "yield_leaders": [_row(c) for c in leaders],
            "shortlist": [_row(c) for c in shortlist],
            "triggered_only": [_row(c) for c in triggered_only[:25]],
            "gap_only": [_row(c) for c in gap_only[:25]],
            "all_quality": [_row(c) for c in priced]}
    (out_dir / "results.json").write_text(json.dumps(data, indent=1))
    cols = ["ticker", "name", "price", "chg_5d", "mcap_bn", "fcf_bn", "p_fcf", "fcf_margin", "cash_roc",
            "fcf_cagr", "rev_cagr", "fcf_ps_cagr", "rev_ps_cagr", "share_cagr", "sbc_bn", "required_return", "ref_g", "implied_g", "gap", "flag_drop", "flag_cluster", "flag_gap", "score"]
    with open(out_dir / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(data["all_quality"], key=lambda r: -r["score"]):
            w.writerow(r)
    from .site import write_site
    write_site(out_dir, data, explorer_company)


def explorer_company(r, group):
    bases = [{"label": ("TTM to " if r["fcf_basis"] == "TTM" else "FY ending ") + r["fcf_period_end"], "fcf": r["fcf_bn"]}]
    if r["fcf_basis"] == "TTM":
        bases.append({"label": "FY ending " + r["fy_end"], "fcf": r["fy_fcf_bn"]})
    if r["fcf_avg3_bn"] and r["fcf_avg3_bn"] > 0:
        bases.append({"label": "3-year average (smooths the cycle)", "fcf": r["fcf_avg3_bn"]})
    if r.get("fcf_norm_bn") and r["fcf_norm_bn"] > 0 and r.get("margin_avg") is not None:
        bases.append({"label": f"Normalized: revenue × {r['margin_years']}-yr avg margin ({r['margin_avg']*100:.0f}%)", "fcf": r["fcf_norm_bn"]})
    # Default view matches how the screen judged it: the lowest of the alternative bases that applies
    alts = [i for i, b in enumerate(bases) if (b["label"].startswith("3-year") and r["volatile"])
            or (b["label"].startswith("Normalized") and r.get("peak_margin"))]
    default_base = min(alts, key=lambda i: bases[i]["fcf"]) if alts else 0
    badges = []
    if r["flag_drop"]:
        badges.append({"k": "drop", "t": f"Down {abs(r['chg_5d'])*100:.0f}% in 5 days ({r['rel_chg_5d']*100:+.0f} pts vs. S&P 500)",
                       "s": f"▼{abs(r['chg_5d'])*100:.0f}% wk"})
    if r["flag_drawdown"]:
        badges.append({"k": "drop", "t": f"{abs(r['off_1m_high'])*100:.0f}% below its 1-month high of {r['high_1m_date']} ({r['rel_dd']*100:+.0f} pts vs. S&P 500)",
                       "s": f"▼{abs(r['off_1m_high'])*100:.0f}% mo"})
    if r["flag_drop"] or r["flag_drawdown"]:
        if r["earnings_in_window"]:
            badges.append({"k": "earn", "t": f"Fell on its own earnings: results filed {r['earnings_in_window']} (8-K Item 2.02)", "s": "on earnings"})
        else:
            badges.append({"k": "mkt", "t": "Fell with the market: no earnings report in the window", "s": "with market"})
    if r["flag_cluster"]:
        badges.append({"k": "ins", "t": f"{len(r['insider_buyers'])} insiders bought ${r['insider_value']/1e6:,.1f}M: " + "; ".join(r["insider_buyers"]),
                       "s": f"{len(r['insider_buyers'])} insiders"})
    if r["volatile"]:
        badges.append({"k": "vol", "t": "Volatile cash flow (cyclical): judged on 3-yr average, +1.5 pts required return", "s": "cyclical"})
    if r.get("peak_margin"):
        badges.append({"k": "vol", "t": f"Peak margins: FCF margin {r['fcf_margin']*100:.0f}% vs. its {r['margin_years']}-yr average of {r['margin_avg']*100:.0f}%; judged on normalized cash flow",
                       "s": "peak margins"})
    if "leveraged" in (r.get("risk_why") or []):
        badges.append({"k": "vol", "t": f"Leveraged: net debt ${-r['net_cash_bn']:.1f}B, +1 pt required return", "s": "leveraged"})
    badges.append({"k": "link", "t": "Insider filings ↗", "u": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r['cik']}&type=4&owner=include&count=40"})
    badges.append({"k": "link", "t": "10-K / 10-Q ↗", "u": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r['cik']}&type=10-&count=40"})
    return {
        "id": f"{group}-{r['ticker'].lower()}", "name": r["ticker"], "fullName": r["name"], "group": group, "badges": badges,
        "isNew": group in (r.get("new_in") or []), "fcfYield": r.get("fcf_yield"),
        "defaultBase": default_base,
        "sector": r["sector"], "score": r["score"], "gap": r["gap"],
        "mcap": r["mcap_bn"], "mcapNote": f"{r['price_date']} close · {r['name']}" + (f" · {r['sic_desc'].title()}" if r["sic_desc"] else ""),
        "netCash": r["net_cash_bn"], "netNote": "cash + short-term investments − long-term debt",
        "bases": bases,
        "track": round((r["ref_g"] or 0) * 100, 1),
        "trackNote": f"per share: lower of FCF ({(r['fcf_ps_cagr'] or 0)*100:.0f}%) and revenue ({(r['rev_ps_cagr'] or 0)*100:.0f}%) growth, capped at {int(C.TRACK_CAP*100)}%"
                     + (f"; share count {(r['share_cagr'] or 0)*100:+.1f}%/yr" if r.get("share_cagr") is not None else ""),
        "riskPremium": r["risk_premium"], "riskWhy": r["risk_why"],
        "sbcNote": (f"after ${r['sbc_bn']:.2f}B stock comp ({(r['sbc_pct_rev'] or 0)*100:.0f}% of revenue)" if r["sbc_bn"] else "after stock comp"),
        "hist": [["TTM" if h["ttm"] else "FY" + h["fy_end"][2:4], h["fcf_bn"], 0] for h in r["fcf_history"]],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--fixtures", type=Path, help="run offline against a fixtures folder")
    ap.add_argument("--cache", type=Path, default=Path(".cache/companyfacts"))
    ap.add_argument("--asof", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--limit", type=int, help="only scan the first N companies (testing)")
    ap.add_argument("--tickers", nargs="*", help="only scan these tickers")
    ap.add_argument("--ledger", type=Path, help="track-record file to read and update (e.g. track/ledger.json)")
    a = ap.parse_args(argv)
    if a.fixtures:
        fetcher, cache = FixtureFetcher(a.fixtures), None
    else:
        fetcher, cache = LiveFetcher(env_user_agent(), C.SEC_RATE_LIMIT_PER_SEC), a.cache
    run(fetcher, a.asof, a.out, cache, a.limit, a.tickers, a.ledger)


if __name__ == "__main__":
    main()
