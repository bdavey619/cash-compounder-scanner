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
.cc-nav .right{display:flex;align-items:center;gap:14px}
.cc-mode{display:inline-flex;border:1px solid var(--border,#E6E6E1);border-radius:999px;padding:2px;background:var(--bg,#F7F7F5)}
.cc-mode button{all:unset;cursor:pointer;font-size:12.5px;font-weight:600;padding:5px 11px;border-radius:999px;color:var(--muted,#6B6D73)}
.cc-mode button[aria-pressed=true]{background:var(--card,#fff);color:var(--ink,#111214);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.cc-mode button:focus-visible{outline:2px solid #1F4FD1}
.info{all:unset;cursor:help;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;
border:1px solid currentColor;font-size:10px;font-weight:700;font-style:normal;line-height:1;color:var(--faint,#8A8C92);margin-left:5px;vertical-align:1px;text-transform:none;letter-spacing:0}
.info:focus-visible{outline:2px solid #1F4FD1;outline-offset:2px}
#cc-tip{position:fixed;z-index:50;max-width:280px;background:var(--ink,#111214);color:var(--bg,#fff);font-size:13px;line-height:1.45;
font-weight:400;text-transform:none;letter-spacing:0;padding:9px 11px;border-radius:8px;pointer-events:none;display:none;box-shadow:0 6px 18px rgba(0,0,0,.18)}
@media (max-width:820px){.cc-nav .in{padding:8px 16px}.cc-nav .asof{display:none}}
"""

PAGES = [("learn.html", "Start here"), ("index.html", "Today"), ("backtest/", "Backtest"), ("track.html", "Track record"), ("method.html", "Method")]


def nav(active: str, asof_text: str, prefix: str = "", toggle: bool = False) -> str:
    links = "".join(
        f'<a href="{prefix}{href}"' + (' aria-current="page"' if label == active else "") + f">{label}</a>"
        for href, label in PAGES)
    return (f'<header class="cc-nav"><div class="in"><a class="brand" href="{prefix}index.html">'
            '<span class="mark"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 12 L6 7 L9 9.5 L14 3" '
            'fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>Cash Compounders</a>'
            f'<nav aria-label="Main">{links}</nav><div class="right">'
            + ('<div class="cc-mode" role="group" aria-label="Wording"><button type="button" data-mode="plain">Plain English</button>'
               '<button type="button" data-mode="pro">Pro</button></div>' if toggle else '')
            + f'<div class="asof">{asof_text}</div></div></div></header>')


GLOSSARY = {
    # key: (pro label, plain label, definition shown in the info bubble)
    "yld": ("FCF yield", "Cash back per $1",
            "Free cash flow divided by the company's market value: how many cents of cash the business produces each year for every $1 you'd pay for the whole company."),
    "pfcf": ("P/FCF", "Years to earn back",
             "Price ÷ free cash flow: how many years of today's cash it would take to earn back what you paid. Lower means cheaper."),
    "margin": ("FCF margin", "Cash kept per $1 of sales",
               "Free cash flow as a share of revenue: of every $1 the company sells, how much ends up as spare cash after paying for everything."),
    "rec": ("Growth record", "Past growth",
            "How fast the company's cash and sales per share grew over the last four years (we use the lower of the two, capped at 20%)."),
    "impl": ("Price implies", "Price's bet",
             "The yearly cash growth over the next 10 years that today's price only makes sense with. Negative means the price is betting the cash will shrink."),
    "mcap": ("Mkt cap", "Company value", "Market value: the share price times the number of shares, what it would cost to buy the whole company."),
    "m1": ("1 month", "Last month", "How far the share price is below its highest close of the past month."),
    "sector": ("Sector", "Industry", "The kind of business, based on the industry code the company reports to the SEC."),
    "roc": ("Cash return on capital", "Cash earned on money invested",
            "Free cash flow divided by the money owners and lenders have put in. High means the business doesn't need much money to make a lot of cash."),
    "shares": ("Share count", "Change in share count",
               "How fast the number of shares has changed per year. Falling (buybacks) means each share owns more of the company; rising (new shares) means less."),
    "net": ("Net debt", "Debt minus cash", "Long-term debt minus cash and short-term investments. Negative means the company has more cash than debt."),
    "rf": ("10-year Treasury", "Safe bond rate",
           "What the US government pays to borrow for 10 years. It's the 'no risk' option, so a stock has to promise more to be worth it."),
    "req": ("Required return", "Return you'd demand",
            "The yearly return you'd want for owning a stock instead of a safe bond: the bond rate plus 4 points for the extra risk."),
    "gt": ("Growth after year 10", "Long-run growth", "How fast cash grows forever after the first 10 years. Usually set near the economy's long-run growth, 2–4%."),
    "leaders": ("Cash-yield leaders", "Most cash per $1", "The top fifth of the strong cash businesses, ranked by cash back per $1."),
    "quality": ("Quality universe", "Strong cash businesses",
                "Companies that turn at least 15% of sales into spare cash, every year, after paying their people in stock too."),
    "drop": ("Fell sharply this week", "Dropped a lot lately", "The share price fell at least 10 points more than the market in a week, or 15 points more from its 1-month high."),
    "gap": ("Priced below its record", "Price bets on a slowdown", "The price only makes sense if growth slows a lot compared with the company's own past."),
    "insiders": ("Insider buying", "Bosses buying shares", "At least two of the company's own executives or directors bought shares with their own money this week."),
    "all": ("All quality", "All strong cash businesses", "Every company that passes the cash tests, cheap or not."),
    "Cyclical": ("Cyclical", "Boom-and-bust", "Its cash swings a lot with the economy, so it's judged on a 3-year average and has to clear a higher bar."),
    "Peak margins": ("Peak margins", "Unusually good year", "It's keeping far more cash per $1 of sales than usual, so it's judged on a normal year instead."),
    "Leveraged": ("Leveraged", "Lots of debt", "Its debt is more than 3 years of cash flow, so it has to clear a higher bar."),
    "Fell sharply": ("Fell sharply", "Dropped a lot", "The price fell much more than the market recently."),
    "Insider buying": ("Insider buying", "Bosses buying", "Two or more of its own executives or directors just bought shares."),
}

MODE_JS = r"""
(function(){
  const G = __GLOSS__;
  let mode = 'plain';
  try { mode = localStorage.getItem('cc-mode') || 'plain'; } catch (e) {}
  const subs = [];
  const tip = document.createElement('div'); tip.id = 'cc-tip'; tip.setAttribute('role', 'tooltip');
  window.CC = {
    get mode() { return mode; },
    L: k => G[k] ? (mode === 'pro' ? G[k][0] : G[k][1]) : k,
    def: k => G[k] ? G[k][2] : '',
    info: k => G[k] ? `<button type="button" class="info" data-tip="${G[k][2].replace(/"/g, '&quot;')}" aria-label="What is ${(mode === 'pro' ? G[k][0] : G[k][1]).replace(/"/g, '')}?">i</button>` : '',
    on: fn => subs.push(fn),
    set: m => { mode = m; try { localStorage.setItem('cc-mode', m); } catch (e) {} paint(); subs.forEach(f => f(m)); }
  };
  function paint() { document.querySelectorAll('.cc-mode button').forEach(b => b.setAttribute('aria-pressed', b.dataset.mode === mode)); }
  function place(el) {
    tip.textContent = el.dataset.tip; tip.style.display = 'block';
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    let x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8), y = r.bottom + 8;
    if (y + h > innerHeight - 8) y = r.top - h - 8;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  const hide = () => tip.style.display = 'none';
  document.addEventListener('DOMContentLoaded', () => {
    document.body.appendChild(tip); paint();
    document.querySelectorAll('.cc-mode button').forEach(b => b.onclick = () => CC.set(b.dataset.mode));
  });
  document.addEventListener('mouseover', e => { const el = e.target.closest && e.target.closest('[data-tip]'); el ? place(el) : hide(); });
  document.addEventListener('focusin', e => { const el = e.target.closest && e.target.closest('[data-tip]'); el ? place(el) : hide(); });
  document.addEventListener('click', e => { const el = e.target.closest && e.target.closest('.info'); if (el) { e.preventDefault(); e.stopPropagation(); place(el); } }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  addEventListener('scroll', hide, { passive: true });
})();
"""

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


def _fill(name: str, active: str, cos, meta, prefix: str = "", toggle: bool = False) -> str:
    from datetime import date
    d = date.fromisoformat(meta["asof"])
    asof_text = f"Data as of {d.strftime('%b')} {d.day}, {d.year} close"
    tpl = (TEMPLATES / name).read_text()
    js = lambda x: json.dumps(x, separators=(",", ":")).replace("</", "<\\/")
    mode_js = MODE_JS.replace("__GLOSS__", js({k: list(v) for k, v in GLOSSARY.items()}))
    return (tpl.replace("__CSS__", CSS + NAV_CSS).replace("__NAV__", nav(active, asof_text, prefix, toggle))
               .replace("__MODEJS__", mode_js)
               .replace("__COMPANIES__", js(cos)).replace("__META__", js(meta)))


def write_site(out_dir: Path, data: dict, explorer_company) -> None:
    cos, meta = build(data, explorer_company)
    (out_dir / "index.html").write_text(_fill("home.html", "Today", cos, meta, toggle=True))
    (out_dir / "company.html").write_text(_fill("company.html", "Today", cos, meta, toggle=True))
    (out_dir / "learn.html").write_text(_fill("learn.html", "Start here", [], meta))
    (out_dir / "track.html").write_text(_fill("track.html", "Track record", [], meta))
    (out_dir / "method.html").write_text(_fill("method.html", "Method", [], meta))
