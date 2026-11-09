"""Track record: log every flagged company on the day it's flagged, then measure how it did
against the S&P 500 (SPY) 1, 3, 6 and 12 months later.

The ledger is a plain JSON file kept in the repo (track/ledger.json). Each daily run adds today's
flags and fills in any horizon that has now passed. Results at a horizon are frozen once measured.

Entry price is the closing price on the day of the flag (the scan runs after the close), so the
results assume you could buy at that close. Nothing is back-filled: the record starts the day the
ledger was created, so it can't flatter itself with hindsight.
"""
from __future__ import annotations

import bisect
import csv
import json
from datetime import date, timedelta
from pathlib import Path

from . import config as C

GROUP_ORDER = ["shortlist", "watchlist", "triggered"]


def load_ledger(path: Path | None) -> dict:
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            raise SystemExit(f"{path} is not valid JSON; refusing to overwrite the track record")
    return {"started": None, "entries": []}


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=0, separators=(",", ":")) + "\n")


def _close_on_or_after(closes: list[tuple[str, float]], day: str):
    dates = [d for d, _ in closes]
    i = bisect.bisect_left(dates, day)
    return closes[i] if i < len(closes) else None


def _close_on_or_before(closes: list[tuple[str, float]], day: str):
    dates = [d for d, _ in closes]
    i = bisect.bisect_right(dates, day) - 1
    return closes[i] if i >= 0 else None


def tickers_needing_prices(ledger: dict, asof: date) -> set[str]:
    """Tickers with an entry still inside its 12-month window."""
    oldest = (asof - timedelta(days=max(C.TRACK_HORIZONS.values()) + 10)).isoformat()
    return {e["ticker"] for e in ledger["entries"] if e["date"] >= oldest and e.get("r12m") is None}


def add_flags(ledger: dict, asof: date, groups: dict[str, list[dict]], spy_closes) -> int:
    """groups: {"shortlist": [company, ...], ...}; companies carry last/last_date from price_stats."""
    if ledger.get("started") is None:
        ledger["started"] = asof.isoformat()
    cutoff = (asof - timedelta(days=C.TRACK_REPEAT_DAYS)).isoformat()
    recent = {(e["ticker"], e["group"]) for e in ledger["entries"] if e["date"] > cutoff}
    today = {e["id"] for e in ledger["entries"]}
    added = 0
    for g in GROUP_ORDER:
        for c in groups.get(g, []):
            key = (c["ticker"], g)
            eid = f"{c['last_date']}-{c['ticker']}-{g}"
            if key in recent or eid in today:
                continue
            spy = _close_on_or_before(spy_closes, c["last_date"]) if spy_closes else None
            if not spy:
                continue  # can't measure against the market without a starting point
            flags = [k for k, f in (("drop", "flag_drop"), ("drawdown", "flag_drawdown"), ("insiders", "flag_cluster"),
                                     ("gap", "flag_gap")) if c.get(f)]
            ledger["entries"].append({
                "id": eid, "date": c["last_date"], "ticker": c["ticker"], "name": c["name"], "group": g,
                "price": round(c["last"], 4), "spy": round(spy[1], 4),
                "implied_g": _r(c.get("implied_g")), "ref_g": _r(c.get("ref_g")),
                "flags": flags, "earnings": bool(c.get("earnings_in_window")),
                "cyclical": bool(c.get("volatile")), "peak_margin": bool(c.get("peak_margin")),
            })
            recent.add(key)
            added += 1
    return added


def update_returns(ledger: dict, closes_by: dict[str, list], spy_closes) -> None:
    if not spy_closes:
        return
    for e in ledger["entries"]:
        closes = closes_by.get(e["ticker"])
        if not closes:
            continue
        last = closes[-1]
        spy_last = _close_on_or_before(spy_closes, last[0])
        if last[0] >= e["date"] and spy_last:
            e["last_date"], e["last_price"] = last[0], round(last[1], 4)
            e["ret"] = _r(last[1] / e["price"] - 1)
            e["excess"] = _r(e["ret"] - (spy_last[1] / e["spy"] - 1))
        for h, days in C.TRACK_HORIZONS.items():
            if e.get("r" + h) is not None:
                continue  # frozen once measured
            target = (date.fromisoformat(e["date"]) + timedelta(days=days)).isoformat()
            if last[0] < target:
                continue
            p, s = _close_on_or_after(closes, target), _close_on_or_after(spy_closes, target)
            if p and s:
                e["r" + h] = _r(p[1] / e["price"] - 1)
                e["x" + h] = _r(e["r" + h] - (s[1] / e["spy"] - 1))


def summarize(ledger: dict, asof: date) -> dict:
    """Per group and horizon: how many have been measured, average return beyond the S&P 500,
    and the share that beat it."""
    out = {"started": ledger.get("started"), "asof": asof.isoformat(), "n": len(ledger["entries"]), "groups": []}
    horizons = ["todate"] + list(C.TRACK_HORIZONS)
    for g in GROUP_ORDER:
        es = [e for e in ledger["entries"] if e["group"] == g]
        row = {"group": g, "n": len(es), "h": {}}
        for h in horizons:
            xs = [e["excess"] for e in es if e.get("excess") is not None and e.get("last_date", e["date"]) > e["date"]] if h == "todate" else \
                 [e["x" + h] for e in es if e.get("x" + h) is not None]
            row["h"][h] = {"n": len(xs), "avg": _r(sum(xs) / len(xs)) if xs else None,
                           "beat": _r(sum(1 for x in xs if x > 0) / len(xs)) if xs else None}
        out["groups"].append(row)
    nxt = None
    if ledger.get("started"):
        first = date.fromisoformat(ledger["started"])
        for h, days in C.TRACK_HORIZONS.items():
            if first + timedelta(days=days) > asof:
                nxt = {"h": h, "date": (first + timedelta(days=days)).isoformat()}
                break
    out["next"] = nxt
    return out


def write_csv(path: Path, ledger: dict) -> None:
    cols = ["date", "ticker", "name", "group", "flags", "earnings", "cyclical", "peak_margin", "price", "spy",
            "implied_g", "ref_g", "last_date", "last_price", "ret", "excess"] + \
           [p + h for h in C.TRACK_HORIZONS for p in ("r", "x")]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for e in sorted(ledger["entries"], key=lambda e: (e["date"], e["group"], e["ticker"]), reverse=True):
            w.writerow({**e, "flags": " ".join(e.get("flags", []))})


def _r(x):
    return None if x is None else round(float(x), 4)
