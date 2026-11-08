"""Turn raw SEC facts, prices and Form 4 filings into screen metrics."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from datetime import date

OCF = ["NetCashProvidedByUsedInOperatingActivities",
       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]
REVENUE = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]


def _usd(facts, ns, concept):
    return facts.get(ns, {}).get(concept, {}).get("units", {}).get("USD", [])


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def annual_series(facts: dict, concepts: list[str]) -> dict[str, float]:
    """{fiscal-year-end: value} from 10-K full-year duration facts. Latest filing wins;
    the first concept that reports a given year wins over later fallbacks."""
    out: dict[str, tuple[str, float]] = {}
    for rank, c in enumerate(concepts):
        for f in _usd(facts, "us-gaap", c):
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


def ttm_value(facts: dict, concepts: list[str]):
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
    f = facts.get("facts", {})
    ocf, capex, rev = annual_series(f, OCF), annual_series(f, CAPEX), annual_series(f, REVENUE)
    years = sorted(ocf)[-4:]
    if len(years) < 3:
        return None
    rows = []
    for y in years:
        cx = capex.get(y)
        rows.append({"fy_end": y, "ocf": ocf[y], "capex": cx if cx is not None else 0.0,
                     "capex_missing": cx is None, "revenue": rev.get(y), "fcf": ocf[y] - (cx or 0.0)})
    last = rows[-1]
    # Trailing twelve months from quarterly filings, when they're newer than the last 10-K
    t_ocf, t_end, _ = ttm_value(f, OCF)
    t_cx, t_cx_end, _ = ttm_value(f, CAPEX)
    t_rev, t_rev_end, _ = ttm_value(f, REVENUE)
    if t_ocf is not None and t_end > last["fy_end"] and t_cx_end == t_end:
        cur = {"end": t_end, "fcf": t_ocf - t_cx, "basis": "TTM",
               "revenue": t_rev if t_rev_end == t_end else None}
        if cur["revenue"] is None:  # keep the margin like-for-like
            cur["revenue"] = last["revenue"] * (cur["fcf"] / last["fcf"]) if last["fcf"] and last["revenue"] else None
    else:
        cur = {"end": last["fy_end"], "fcf": last["fcf"], "basis": "FY", "revenue": last["revenue"]}
    equity, _ = latest_instant(f, ["StockholdersEquity"])
    ltd, _ = latest_instant(f, ["LongTermDebt", "LongTermDebtNoncurrent"])
    cash, _ = latest_instant(f, ["CashAndCashEquivalentsAtCarryingValue"])
    sti, _ = latest_instant(f, ["MarketableSecuritiesCurrent", "ShortTermInvestments"])
    shares, shares_asof = latest_instant(f, ["EntityCommonStockSharesOutstanding"], ns="dei", unit="shares")
    if shares is None:
        shares, shares_asof = latest_diluted_shares(f)
    capital = (equity or 0) + (ltd or 0)
    first = rows[-3]
    yrs = (date.fromisoformat(cur["end"]) - date.fromisoformat(first["fy_end"])).days / 365.25
    cagr = None
    if first["fcf"] > 0 and cur["fcf"] > 0 and yrs > 0.5:
        cagr = (cur["fcf"] / first["fcf"]) ** (1 / yrs) - 1
    rev_cagr = None
    if first.get("revenue") and cur.get("revenue") and first["revenue"] > 0 and yrs > 0.5:
        rev_cagr = (cur["revenue"] / first["revenue"]) ** (1 / yrs) - 1
    history = rows + ([{"fy_end": cur["end"], "fcf": cur["fcf"], "ttm": True}] if cur["basis"] == "TTM" else [])
    return {
        "history": history,
        "fy_end": last["fy_end"],
        "fcf_basis": cur["basis"],
        "fcf_period_end": cur["end"],
        "fy_fcf": last["fcf"],
        "revenue": cur["revenue"],
        "fcf": cur["fcf"],
        "fcf_margin": (cur["fcf"] / cur["revenue"]) if cur["revenue"] else None,
        "positive_fcf_years": sum(1 for r in rows[-3:] if r["fcf"] > 0) - (0 if cur["fcf"] > 0 else 1),
        "fcf_cagr": cagr,
        "rev_cagr": rev_cagr,
        "cash_roc": (cur["fcf"] / capital) if capital > 0 else None,
        "net_cash": (cash or 0) + (sti or 0) - (ltd or 0),
        "shares": shares,
        "shares_asof": shares_asof,
        "capex_missing": last["capex_missing"],
    }


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

def price_stats(closes: list[tuple[str, float]], lookback: int) -> dict | None:
    if len(closes) <= lookback:
        return None
    last = closes[-1][1]
    prior = closes[-1 - lookback][1]
    hi52 = max(c for _, c in closes)
    return {
        "last": last, "last_date": closes[-1][0],
        "chg": last / prior - 1,
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
