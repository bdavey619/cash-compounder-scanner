"""The published site: Today (ranking), a page per company, Track record and Method.
Every page is static HTML with its data embedded, so GitHub Pages can serve it as-is."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as C

TEMPLATES = Path(__file__).parent / "templates"

CSS = """
:root{color-scheme:light;--bg:#F7F7F5;--card:#FFFFFF;--border:#E6E6E1;--line:#EEEEEA;--ink:#111214;--ink2:#3A3C42;
--muted:#6B6D73;--faint:#8A8C92;--accent:#1F4FD1;--accent-ink:#163B9E;--accent-bg:#E8EEFC;--accent-soft:#EEF1F8;
--down:#B42318;--up:#157347;--hover:#F4F6FB;--chip:#F1F1EE;--warn-bg:#FDF1DC;--warn-ink:#7A4A00}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#111214;--card:#1A1B1E;--border:#2C2D31;--line:#232428;
--ink:#F2F2F0;--ink2:#D0D1D4;--muted:#A0A2A8;--faint:#8A8C92;--accent:#7B9DF7;--accent-ink:#B3C7FF;--accent-bg:#1E2A47;
--accent-soft:#1C2233;--down:#F08A7E;--up:#6CC896;--hover:#1F2126;--chip:#24262B;--warn-bg:#3A2E17;--warn-ink:#F0C27A}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1280px;margin:0 auto;padding:28px 32px 56px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px}
.h1{margin:0 0 8px;font-size:30px;line-height:1.15;letter-spacing:-.02em;font-weight:700}
.lede{margin:0;font-size:15px;line-height:1.55;color:var(--muted);max-width:740px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.kpi{padding:16px 18px}.kpi .k{font-size:12px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:26px;font-weight:700;letter-spacing:-.02em;margin-top:4px}.kpi .d{font-size:13px;color:var(--muted);margin-top:2px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--accent-bg);color:var(--accent-ink);font-weight:600;font-size:12px}
.tag{font-size:11.5px;font-weight:600;padding:2px 7px;border-radius:999px;background:var(--chip);color:var(--ink2);white-space:nowrap}
.tag.new{background:var(--accent-bg);color:var(--accent-ink)}.tag.warn{background:var(--warn-bg);color:var(--warn-ink)}
.btn{height:36px;padding:0 14px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;font-size:14px;font-weight:600;cursor:pointer}
.chip{height:36px;padding:0 14px;border-radius:999px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;background:var(--card);color:var(--ink2);border:1px solid var(--border)}
.chip[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}.chip span{opacity:.65;font-weight:500;margin-left:4px}
select,input[type=search]{height:36px;padding:0 10px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;font-size:14px}
.note{padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:14px;line-height:1.5;color:var(--ink2)}
.muted{color:var(--muted)}.faint{color:var(--faint)}.down{color:var(--down)}.up{color:var(--up)}.acc{color:var(--accent)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
footer.foot{margin-top:28px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--faint)}
@media (max-width:820px){.wrap{padding:20px 16px 48px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.h1{font-size:24px}}
"""

NAV_CSS = """
.cc-nav{background:var(--card,#fff);border-bottom:1px solid var(--border,#E6E6E1);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.cc-nav .in{max-width:1280px;margin:0 auto;padding:0 32px;min-height:60px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.cc-nav .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;letter-spacing:-.01em;color:var(--ink,#111214);text-decoration:none}
.cc-nav .mark{width:26px;height:26px;border-radius:6px;background:#1F4FD1;display:flex;align-items:center;justify-content:center}
.cc-nav nav{display:flex;gap:4px;font-size:14px;flex-wrap:wrap}
.cc-nav nav a{padding:8px 12px;border-radius:8px;color:var(--muted,#55575E);text-decoration:none}
.cc-nav nav a[aria-current=page]{background:var(--accent-soft,#F0F2F7);color:var(--ink,#111214);font-weight:600}
.cc-nav .asof{font-size:13px;color:var(--muted,#6B6D73)}
@media (max-width:820px){.cc-nav .in{padding:8px 16px}.cc-nav .asof{display:none}}
"""

PAGES = [("index.html", "Today"), ("backtest/", "Backtest"), ("track.html", "Track record"), ("method.html", "Method")]


def nav(active: str, asof_text: str, prefix: str = "") -> str:
    links = "".join(
        f'<a href="{prefix}{href}"' + (' aria-current="page"' if label == active else "") + f">{label}</a>"
        for href, label in PAGES)
    return (f'<header class="cc-nav"><div class="in"><a class="brand" href="{prefix}index.html">'
            '<span class="mark"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 12 L6 7 L9 9.5 L14 3" '
            'fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>Cash Compounders</a>'
            f'<nav aria-label="Main">{links}</nav><div class="asof">{asof_text}</div></div></header>')


_KEEP_UPPER = {"LP", "L.P.", "PLC", "NV", "N.V.", "SA", "AG", "SE", "II", "III", "USA", "US", "REIT", "ETF", "LLC", "ADR", "AT&T", "H&R"}


def nice_name(name: str) -> str:
    """SEC names are often all caps ('SKYWEST INC'); make them readable."""
    name = re.sub(r"\s*/[A-Z]{2}/?$", "", name.strip())
    if name != name.upper():
        return name
    out = []
    for w in name.split():
        out.append(w if w in _KEEP_UPPER else w.lower() if w in ("OF", "AND", "THE") and out else w.capitalize())
    s = " ".join(out)
    return re.sub(r"\bInc\b\.?", "Inc.", s).replace("Inc..", "Inc.")


def company_payload(r: dict, ec: dict, rank: int) -> dict:
    """Everything the ranking and the company page need for one company."""
    fl = r
    tags = []
    if r.get("flag_drop") or r.get("flag_drawdown"):
        tags.append(["Fell sharply", ""])
    if r.get("flag_cluster"):
        tags.append(["Insider buying", ""])
    if r.get("volatile"):
        tags.append(["Cyclical", "warn"])
    if r.get("peak_margin"):
        tags.append(["Peak margins", "warn"])
    if "leveraged" in (r.get("risk_why") or []):
        tags.append(["Leveraged", "warn"])
    return {
        "t": r["ticker"], "name": nice_name(r["name"]), "legal": r["name"], "cik": r["cik"], "rank": rank,
        "sector": r.get("sector") or "Other", "sic": (r.get("sic_desc") or "").title(),
        "price": r["price"], "date": r["price_date"], "mcap": r["mcap_bn"], "yld": r.get("fcf_yield"),
        "pfcf": r["p_fcf"], "margin": r["fcf_margin"], "roc": r.get("cash_roc"), "rec": r.get("ref_g"),
        "impl": r.get("implied_g"), "gap": r.get("gap"), "chg5": r.get("chg_5d"), "m1": r.get("off_1m_high"),
        "shares": r.get("share_cagr"), "fps": r.get("fcf_ps_cagr"), "rps": r.get("rev_ps_cagr"),
        "net": r["net_cash_bn"], "sbc": r.get("sbc_pct_rev"), "fcf": r["fcf_bn"], "basis": r["fcf_basis"],
        "end": r["fcf_period_end"], "spark": r.get("spark") or [],
        "lists": {"leaders": False, "drop": bool(fl.get("flag_drop") or fl.get("flag_drawdown")),
                  "insiders": bool(fl.get("flag_cluster")), "gap": bool(fl.get("flag_gap"))},
        "isNew": "yield" in (r.get("new_in") or []), "tags": tags,
        "bases": ec["bases"], "defaultBase": ec["defaultBase"], "hist": ec["hist"],
        "premium": ec["riskPremium"], "premiumWhy": ec["riskWhy"],
        "notes": [b["t"] for b in ec["badges"] if b["k"] != "link"],
    }


def build(data: dict, explorer_company) -> tuple[list, dict]:
    rows = sorted(data["all_quality"], key=lambda r: -(r.get("fcf_yield") or 0))
    leaders = {r["ticker"] for r in data.get("yield_leaders", [])}
    cos = []
    for i, r in enumerate(rows, 1):
        p = company_payload(r, explorer_company(r, "all"), i)
        p["lists"]["leaders"] = r["ticker"] in leaders
        cos.append(p)
    m = data["meta"]
    meta = {"asof": m["asof"], "funnel": m["funnel"], "rates": m.get("rates", {}), "regime": m.get("regime"),
            "track": m.get("track", {}), "changes": m.get("changes", {}),
            "rules": {k: m["config"][k] for k in ("MIN_FCF_MARGIN", "MIN_MARKET_CAP_USD", "MIN_REVENUE_USD", "MIN_CASH_RETURN_ON_CAPITAL",
                                                  "MIN_POSITIVE_FCF_YEARS", "DROP_THRESHOLD", "DRAWDOWN_THRESHOLD", "MIN_CLUSTER_BUYERS",
                                                  "MAX_IMPLIED_GROWTH", "GAP_THRESHOLD", "REQUIRED_RETURN", "EQUITY_PREMIUM",
                                                  "TERMINAL_GROWTH", "FORECAST_YEARS", "TRACK_CAP", "PEAK_MARGIN_RATIO")}}
    return cos, meta


def _fill(name: str, active: str, cos, meta, prefix: str = "") -> str:
    from datetime import date
    d = date.fromisoformat(meta["asof"])
    asof_text = f"Data as of {d.strftime('%b')} {d.day}, {d.year} close"
    tpl = (TEMPLATES / name).read_text()
    js = lambda x: json.dumps(x, separators=(",", ":")).replace("</", "<\\/")
    return (tpl.replace("__CSS__", CSS + NAV_CSS).replace("__NAV__", nav(active, asof_text, prefix))
               .replace("__COMPANIES__", js(cos)).replace("__META__", js(meta)))


def write_site(out_dir: Path, data: dict, explorer_company) -> None:
    cos, meta = build(data, explorer_company)
    (out_dir / "index.html").write_text(_fill("home.html", "Today", cos, meta))
    (out_dir / "company.html").write_text(_fill("company.html", "Today", cos, meta))
    (out_dir / "track.html").write_text(_fill("track.html", "Track record", [], meta))
    (out_dir / "method.html").write_text(_fill("method.html", "Method", [], meta))
