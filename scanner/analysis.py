"""Turn raw SEC facts, prices and Form 4 filings into screen metrics."""
from __future__ import annotations

from . import config as C

import math
import re
import xml.etree.ElementTree as ET
from datetime import date

OCF = ["NetCashProvidedByUsedInOperatingActivities",
       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]
REVENUE = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
SBC = ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]
DILUTED = ["WeightedAverageNumberOfDilutedSharesOutstanding"]


def _usd(facts, ns, concept, unit="USD"):
    return facts.get(ns, {}).get(concept, {}).get("units", {}).get(unit, [])


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def annual_series(facts: dict, concepts: list[str], unit: str = "USD") -> dict[str, float]:
    """{fiscal-year-end: value} from 10-K full-year duration facts. Latest filing wins;
    the first concept that reports a given year wins over later fallbacks."""
    out: dict[str, tuple[str, float]] = {}
    for rank, c in enumerate(concepts):
        for f in _usd(facts, "us-gaap", c, unit):
            if not f.get("form", "").startswith("10-K") or "start" not in f:
                continue
            if not 330 <= _days(f["start"], f["end"]) <= 400:
                continue
            key = f["end"]
            prev = out.get(key)
            cand = (rank, f.get("filed", ""), f["val"])
            if prev is None or (cand[0], _neg(cand[1])) < (prev[0], _neg(prev[1])):
                out[key] = cand
    return {k: v[2] for k, v in out.items()}


def _neg(s: str) -> str:  # sort helper: later filed date ranks first
    return "".join(chr(255 - ord(ch)) for ch in s)


def _shift_year(d: str, years: int) -> date:
    x = date.fromisoformat(d)
    try:
        return x.replace(year=x.year + years)
    except ValueError:  # Feb 29
        return x.replace(year=x.year + years, day=28)


def ttm_value(facts: dict, concepts: list[str], parts: dict | None = None):
    """Trailing twelve months from 10-Q year-to-date facts:
    TTM = last fiscal year + this year's YTD - last year's matching YTD.
    Returns (value, period_end, fiscal_year_end_used) or (None, None, None)."""
    for c in concepts:
        annual = annual_series(facts, [c])
        if not annual:
            continue
        ytd = [f for f in _usd(facts, "us-gaap", c)
               if f.get("form", "").startswith("10-Q") and "start" in f and 60 <= _days(f["start"], f["end"]) <= 300]
        if not ytd:
            continue
        latest = max(ytd, key=lambda f: (f["end"], f.get("filed", "")))
        fy_end = max((e for e in annual if e < latest["end"]), default=None)
        if fy_end is None or abs(_days(fy_end, latest["start"]) - 1) > 10:
            continue  # YTD must start right after the last fiscal year ended
        dur = _days(latest["start"], latest["end"])
        target_end = _shift_year(latest["end"], -1)
        prior = [f for f in ytd if abs((date.fromisoformat(f["end"]) - target_end).days) <= 10
                 and abs(_days(f["start"], f["end"]) - dur) <= 10]
        if not prior:
            continue
        p = max(prior, key=lambda f: f.get("filed", ""))
        if parts is not None:
            parts.update({"fy": annual[fy_end], "fy_end": fy_end, "ytd": latest["val"], "ytd_start": latest["start"],
                          "ytd_end": latest["end"], "prior": p["val"], "prior_start": p["start"], "prior_end": p["end"]})
        return annual[fy_end] + latest["val"] - p["val"], latest["end"], fy_end
    return None, None, None


def latest_instant(facts: dict, concepts: list[str], ns="us-gaap", unit="USD"):
    best = None
    for c in concepts:
        for f in facts.get(ns, {}).get(c, {}).get("units", {}).get(unit, []):
            if "start" in f:
                continue
            if best is None or (f["end"], f.get("filed", "")) > (best["end"], best.get("filed", "")):
                best = f
        if best is not None:
            return best["val"], best["end"]
    return None, None


def fundamentals(facts: dict) -> dict | None:
    """Free cash flow here = operating cash flow - capex - stock-based compensation.
    Stock comp is a real cost to shareholders (it dilutes them) even though no cash leaves."""
    f = facts.get("facts", {})
    ocf, capex, rev, sbc = (annual_series(f, OCF), annual_series(f, CAPEX),
                            annual_series(f, REVENUE), annual_series(f, SBC))
    dil = annual_series(f, DILUTED, unit="shares")
    years = sorted(ocf)[-4:]
    if len(years) < 3:
        return None
    rows = []
    for y in years:
        cx, sb = capex.get(y), sbc.get(y, 0.0)
        rows.append({"fy_end": y, "ocf": ocf[y], "capex": cx if cx is not None else 0.0, "sbc": sb,
                     "capex_missing": cx is None, "revenue": rev.get(y), "diluted": dil.get(y),
                     "fcf": ocf[y] - (cx or 0.0) - sb})
    last = rows[-1]
    # Trailing twelve months from quarterly filings, when they're newer than the last 10-K
    tp = {k: {} for k in ("ocf", "capex", "rev", "sbc")}
    t_ocf, t_end, _ = ttm_value(f, OCF, tp["ocf"])
    t_cx, t_cx_end, _ = ttm_value(f, CAPEX, tp["capex"])
    t_rev, t_rev_end, _ = ttm_value(f, REVENUE, tp["rev"])
    t_sbc, t_sbc_end, _ = ttm_value(f, SBC, tp["sbc"])
    if t_ocf is not None and t_end > last["fy_end"] and t_cx_end == t_end:
        ttm_sbc = t_sbc if t_sbc_end == t_end else last["sbc"]  # fall back to the fiscal-year figure
        cur = {"end": t_end, "fcf": t_ocf - t_cx - ttm_sbc, "sbc": ttm_sbc, "basis": "TTM",
               "revenue": t_rev if t_rev_end == t_end else None, "ocf": t_ocf, "capex": t_cx,
               "parts": {"ocf": tp["ocf"], "capex": tp["capex"], "sbc": tp["sbc"] if t_sbc_end == t_end else None,
                         "rev": tp["rev"] if t_rev_end == t_end else None},
               "rev_estimated": t_rev_end != t_end}
        if cur["revenue"] is None:  # keep the margin like-for-like
            cur["revenue"] = last["revenue"] * (cur["fcf"] / last["fcf"]) if last["fcf"] and last["revenue"] else None
    else:
        cur = {"end": last["fy_end"], "fcf": last["fcf"], "sbc": last["sbc"], "basis": "FY", "revenue": last["revenue"],
               "ocf": last["ocf"], "capex": last["capex"], "parts": None, "rev_estimated": False}
    equity, equity_d = latest_instant(f, ["StockholdersEquity"])
    ltd, ltd_d = latest_instant(f, ["LongTermDebt", "LongTermDebtNoncurrent"])
    cash, cash_d = latest_instant(f, ["CashAndCashEquivalentsAtCarryingValue"])
    sti, sti_d = latest_instant(f, ["MarketableSecuritiesCurrent", "ShortTermInvestments"])
    shares, shares_asof, shares_src = shares_outstanding(f, cur["end"], with_source=True)
    capital = (equity or 0) + (ltd or 0)
    first = rows[-3]
    yrs = (date.fromisoformat(cur["end"]) - date.fromisoformat(first["fy_end"])).days / 365.25
    cagr = None
    if first["fcf"] > 0 and cur["fcf"] > 0 and yrs > 0.5:
        cagr = (cur["fcf"] / first["fcf"]) ** (1 / yrs) - 1
    rev_cagr = None
    if first.get("revenue") and cur.get("revenue") and first["revenue"] > 0 and yrs > 0.5:
        rev_cagr = (cur["revenue"] / first["revenue"]) ** (1 / yrs) - 1
    # Share count change per year (diluted, fiscal years): buybacks shrink it, stock issuance grows it
    share_cagr = None
    sh_first, sh_last = first.get("diluted"), last.get("diluted")
    sh_yrs = (date.fromisoformat(last["fy_end"]) - date.fromisoformat(first["fy_end"])).days / 365.25
    if sh_first and sh_last and sh_first > 0 and sh_yrs > 0.5:
        share_cagr = (sh_last / sh_first) ** (1 / sh_yrs) - 1
    per_share = lambda g: None if g is None else (1 + g) / (1 + (share_cagr or 0.0)) - 1
    fy3 = [r["fcf"] for r in rows[-3:]]
    avg3 = sum(fy3) / 3
    volatile = avg3 > 0 and (max(fy3) - min(fy3)) / avg3 >= 0.6
    history = rows + ([{"fy_end": cur["end"], "fcf": cur["fcf"], "ttm": True}] if cur["basis"] == "TTM" else [])
    # Peak-margin check: today's FCF margin against the company's own multi-year average
    margins = []
    for y in sorted(ocf)[-C.PEAK_MARGIN_YEARS:]:
        if rev.get(y) and rev[y] > 0:
            margins.append((ocf[y] - (capex.get(y) or 0.0) - sbc.get(y, 0.0)) / rev[y])
    margin_avg = sum(margins) / len(margins) if len(margins) >= 4 else None
    margin_now = (cur["fcf"] / cur["revenue"]) if cur["revenue"] else None
    peak_margin = bool(margin_avg is not None and margin_now is not None and margin_avg > 0
                       and margin_now >= C.PEAK_MARGIN_RATIO * margin_avg
                       and margin_now - margin_avg >= C.PEAK_MARGIN_MIN_PTS)
    math = {
        "basis": cur["basis"], "end": cur["end"], "ocf": cur["ocf"], "capex": cur["capex"], "sbc": cur["sbc"],
        "revenue": cur["revenue"], "rev_estimated": cur["rev_estimated"], "parts": cur["parts"],
        "cash": cash, "cash_date": cash_d, "sti": sti, "sti_date": sti_d, "ltd": ltd, "ltd_date": ltd_d,
        "equity": equity, "equity_date": equity_d, "shares": shares, "shares_date": shares_asof, "shares_src": shares_src,
        "first_fy": first["fy_end"], "first_fcf": first["fcf"], "first_rev": first.get("revenue"), "yrs": yrs,
        "sh_first": sh_first, "sh_last": sh_last, "sh_first_fy": first["fy_end"], "sh_last_fy": last["fy_end"], "sh_yrs": sh_yrs,
        "rows": [{k: r.get(k) for k in ("fy_end", "ocf", "capex", "sbc", "revenue", "fcf", "diluted")} for r in rows],
        "fcf_avg3": avg3, "margin_avg": margin_avg, "margin_years": len(margins),
    }
    return {
        "math": math,
        "history": history,
        "fy_end": last["fy_end"],
        "fcf_basis": cur["basis"],
        "fcf_period_end": cur["end"],
        "fy_fcf": last["fcf"],
        "revenue": cur["revenue"],
        "fcf": cur["fcf"],
        "sbc": cur["sbc"],
        "sbc_pct_rev": (cur["sbc"] / cur["revenue"]) if cur["revenue"] else None,
        "fcf_margin": (cur["fcf"] / cur["revenue"]) if cur["revenue"] else None,
        "positive_fcf_years": sum(1 for r in rows[-3:] if r["fcf"] > 0) - (0 if cur["fcf"] > 0 else 1),
        "fcf_cagr": cagr,
        "rev_cagr": rev_cagr,
        "share_cagr": share_cagr,
        "fcf_ps_cagr": per_share(cagr),
        "rev_ps_cagr": per_share(rev_cagr),
        "fcf_avg3": avg3,
        "margin_avg": margin_avg,
        "margin_years": len(margins),
        "peak_margin": peak_margin,
        "fcf_normalized": (cur["revenue"] * margin_avg) if (margin_avg and cur["revenue"]) else None,
        "volatile": volatile,
        "cash_roc": (cur["fcf"] / capital) if capital > 0 else None,
        "net_cash": (cash or 0) + (sti or 0) - (ltd or 0),
        "shares": shares,
        "shares_asof": shares_asof,
        "capex_missing": last["capex_missing"],
    }


def shares_outstanding(f, ref_end: str | None = None, max_age_days: int = 400, with_source: bool = False):
    """Total shares across all classes. The cover page reports each class separately (Mastercard, Visa,
    CME, Alphabet...), so add up every class from the latest filing. Then cross-check against diluted
    shares, which convert every class into common-equivalent shares: if that's materially larger (Visa's
    class B and C convert into several A shares each), use it."""
    rows = f.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    total, asof = None, None
    if rows:
        best = max(rows, key=lambda x: (x["end"], x.get("filed", "")))
        same = {x["val"] for x in rows if x["end"] == best["end"] and x.get("accn") == best.get("accn")}
        total, asof = float(sum(same)), best["end"]
    # Cover pages sometimes list only some classes (undercounting); net income / EPS and diluted shares can
    # lag a stock split (also undercounting). So keep the cover page unless another measure is 10%+ higher.
    # Ignore any count much older than the latest financials: some companies stopped using the standard
    # tags years ago (Visa's cover-page count stops in 2010), and a 2010 share count is worse than none.
    fresh = lambda c: bool(c[0]) and (ref_end is None or (date.fromisoformat(ref_end) - date.fromisoformat(c[1])).days <= max_age_days)
    if not fresh((total, asof)):
        total, asof = None, None
    named = [(implied_shares(f), "net income ÷ diluted EPS"), (latest_diluted_shares(f), "diluted weighted-average shares")]
    cands = [(c, n) for c, n in named if fresh(c)]
    best, src = max(cands, key=lambda x: x[0][0]) if cands else ((None, None), None)
    out = (total, asof, "cover page of the latest filing")
    if total is None or (best[0] and best[0] > 1.10 * total):
        out = (best[0], best[1], src)
    return out if with_source else out[:2]


def implied_shares(f):
    """Net income / diluted EPS for the latest period reporting both: the company's own share count in
    common-equivalent units, whatever its class structure (Visa's B and C shares convert into several A)."""
    g = f.get("us-gaap", {})
    ni = {(x.get("start"), x["end"]): x for x in g.get("NetIncomeLoss", {}).get("units", {}).get("USD", []) if "start" in x}
    best = None
    for x in g.get("EarningsPerShareDiluted", {}).get("units", {}).get("USD/shares", []):
        n = ni.get((x.get("start"), x["end"]))
        if not n or abs(x["val"]) < 0.05 or n["val"] == 0 or (n["val"] > 0) != (x["val"] > 0):
            continue
        key = (x["end"], x.get("filed", ""))
        if best is None or key > best[0]:
            best = (key, n["val"] / x["val"], x["end"])
    return (best[1], best[2]) if best else (None, None)


def latest_diluted_shares(f):
    best = None
    for x in f.get("us-gaap", {}).get("WeightedAverageNumberOfDilutedSharesOutstanding", {}).get("units", {}).get("shares", []):
        if best is None or x["end"] > best["end"]:
            best = x
    return (best["val"], best["end"]) if best else (None, None)


# ------------------------------------------------------------- reverse DCF

def dcf_value(f0, g, r, gt, n):
    v, f = 0.0, f0
    for t in range(1, n + 1):
        f *= 1 + g
        v += f / (1 + r) ** t
    return v + f * (1 + gt) / (r - gt) / (1 + r) ** n


def implied_growth(fcf, target_value, r, gt, n):
    """Constant FCF growth over n years that makes the DCF equal target_value."""
    if fcf is None or fcf <= 0 or target_value is None or target_value <= 0:
        return None
    lo, hi = -0.6, 3.0
    if dcf_value(fcf, hi, r, gt, n) < target_value:
        return math.inf
    if dcf_value(fcf, lo, r, gt, n) > target_value:
        return -math.inf
    for _ in range(100):
        mid = (lo + hi) / 2
        if dcf_value(fcf, mid, r, gt, n) < target_value:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ------------------------------------------------------------- prices

def price_stats(closes: list[tuple[str, float]], lookback: int, month: int = 21) -> dict | None:
    if len(closes) <= lookback:
        return None
    last = closes[-1][1]
    prior = closes[-1 - lookback][1]
    hi52 = max(c for _, c in closes)
    window = closes[-month - 1:]
    hi_date, hi_1m = max(window, key=lambda dc: dc[1])
    return {
        "last": last, "last_date": closes[-1][0],
        "chg": last / prior - 1,
        "chg_start_date": closes[-1 - lookback][0],
        "off_1m_high": last / hi_1m - 1,
        "high_1m_date": hi_date,
        "off_high": last / hi52 - 1,
        "spark": [round(c, 4) for _, c in closes[-90:]],
    }


# ------------------------------------------------------------- Form 4

def parse_form4(text: str) -> dict | None:
    """Open-market purchases (transaction code P) in one Form 4 submission."""
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text, re.S)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return None

    def txt(node, path):
        el = node.find(path)
        return el.text.strip() if el is not None and el.text else None

    issuer_cik = txt(root, "issuer/issuerCik")
    owners = []
    for ro in root.findall("reportingOwner"):
        name = txt(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        role = []
        if rel is not None:
            if (txt(rel, "isDirector") or "") in ("1", "true"):
                role.append("Director")
            if (txt(rel, "isOfficer") or "") in ("1", "true"):
                role.append(txt(rel, "officerTitle") or "Officer")
            if (txt(rel, "isTenPercentOwner") or "") in ("1", "true"):
                role.append("10% owner")
        owners.append({"name": name, "role": ", ".join(role) or "Insider"})
    buys = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if txt(tx, "transactionCoding/transactionCode") != "P":
            continue
        try:
            sh = float(txt(tx, "transactionAmounts/transactionShares/value") or 0)
            px = float(txt(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        buys.append({"date": txt(tx, "transactionDate/value"), "shares": sh, "value": sh * px})
    if not buys or not issuer_cik:
        return None
    return {"issuer_cik": int(issuer_cik), "owners": owners, "buys": buys}
