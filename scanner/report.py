"""Render the daily scan as one self-contained HTML page (no external assets)."""
from __future__ import annotations

import html
import math

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--card:#fff;--border:#e6e4df;--grid:#ecebe7;
--t1:#0b0b0b;--t2:#52514e;--t3:#86857f;--blue:#2a78d6;--blue-soft:#cde2fb;--grey:#8a8984;--grey-soft:#d9d8d3;
--good:#1c7c4a;--good-bg:#e3f2e8;--crit:#b3261e;--crit-bg:#fbe5e3;--warn-bg:#fdf0d8;--warn:#8a5300}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--surface:#1a1a19;--card:#222220;--border:#3a3936;--grid:#2e2d2b;
--t1:#f0efec;--t2:#c3c2b7;--t3:#8f8e86;--blue:#3987e5;--blue-soft:#1c3c63;--grey:#9b9a93;--grey-soft:#4a4945;
--good:#5cc08a;--good-bg:#1d3527;--crit:#f08a82;--crit-bg:#3d2220;--warn-bg:#3a2e17;--warn:#e0a24a}}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--t1);font:15px/1.5 -apple-system,BlinkMacSystemFont,Inter,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:36px 22px 70px}h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:19px;margin:30px 0 4px}.sub{color:var(--t2);margin:0 0 14px;font-size:14px;max-width:760px}
.meta{color:var(--t3);font-size:13px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:14px}
.co{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}
.co h3{margin:0;font-size:19px}.co .nm{color:var(--t2);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.chip{font-size:12.5px;padding:3px 9px;border-radius:999px;font-weight:600}
.chip.drop{background:var(--crit-bg);color:var(--crit)}.chip.ins{background:var(--good-bg);color:var(--good)}.chip.gap{background:var(--blue-soft);color:var(--blue)}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}
.kv div{font-size:12px;color:var(--t2)}.kv b{display:block;font-size:16px;color:var(--t1);font-variant-numeric:tabular-nums}
.small{font-size:12.5px;color:var(--t2)}.small a{color:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th{font-weight:500;color:var(--t2);text-align:right;padding:6px 8px;border-bottom:1px solid var(--border)}
td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--grid)}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
.empty{padding:22px;text-align:center;color:var(--t2);border:1px dashed var(--border);border-radius:12px}
details{margin-top:24px;font-size:13px;color:var(--t2)}summary{cursor:pointer;color:var(--t1);font-weight:600}
svg text{font-family:inherit}
@media (max-width:760px){.co{grid-template-columns:1fr}.kv{grid-template-columns:repeat(2,1fr)}}
"""


def esc(s):
    return html.escape(str(s))


def pct(x, d=0, sign=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    s = f"{x*100:+.{d}f}%" if sign else f"{x*100:.{d}f}%"
    return s.replace("-", "−")


def money_bn(x):
    if x is None:
        return "n/a"
    return f"${x/1000:.2f}T" if x >= 1000 else (f"${x:.0f}B" if x >= 100 else f"${x:.1f}B")


def funnel_svg(f):
    steps = [("All companies with SEC filings", f.get("universe", 0)),
             ("Pass quality filter", f.get("quality", 0)),
             ("Hit a trigger today", f.get("triggered", 0)),
             ("Shortlist: trigger + cheap vs. track record", f.get("shortlist", 0))]
    mx = max(1, steps[0][1])
    out = ['<svg viewBox="0 0 1000 196" width="100%" role="img" aria-label="Screening funnel">']
    for i, (lab, n) in enumerate(steps):
        y = 8 + i * 45
        w = max(6, 620 * (math.log10(n + 1) / math.log10(mx + 1)))
        fill = "var(--blue)" if i == 3 else "var(--blue-soft)"
        op = 1 if i == 3 else 0.55 + i * 0.15
        out.append(f'<text x="330" y="{y+24}" text-anchor="end" font-size="14" fill="var(--t2)">{esc(lab)}</text>')
        out.append(f'<rect x="345" y="{y+6}" width="{w:.0f}" height="28" rx="5" fill="{fill}" opacity="{op}"><title>{esc(lab)}: {n:,}</title></rect>')
        out.append(f'<text x="{345+w+10:.0f}" y="{y+25}" font-size="15" font-weight="650" fill="var(--t1)">{n:,}</text>')
    out.append('<text x="345" y="192" font-size="11" fill="var(--t3)">Bar length on a log scale so small counts stay visible</text></svg>')
    return "".join(out)


def spark_svg(vals, w=320, h=70):
    if not vals or len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    xs = [i * (w - 8) / (len(vals) - 1) + 4 for i in range(len(vals))]
    ys = [h - 14 - (v - lo) / rng * (h - 26) for v in vals]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    lastx, lasty = xs[-1], ys[-1]
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;display:block" role="img" aria-label="Price, last 90 trading days">'
            f'<path d="{d}" fill="none" stroke="var(--grey)" stroke-width="1.8"/>'
            f'<circle cx="{lastx:.1f}" cy="{lasty:.1f}" r="3.5" fill="var(--t1)"/>'
            f'<text x="4" y="{h-1}" font-size="10.5" fill="var(--t3)">90 trading days</text>'
            f'<text x="{w-4}" y="{h-1}" text-anchor="end" font-size="10.5" fill="var(--t3)">low {lo:,.2f} · high {hi:,.2f}</text></svg>')


def gap_svg(implied, track, w=320, h=66):
    """Implied vs track-record growth on one axis, -5% .. 40%."""
    if implied is None or track is None:
        return ""
    lo, hi = -0.05, 0.40
    X = lambda v: 12 + (max(lo, min(hi, v)) - lo) / (hi - lo) * (w - 24)
    xi, xt = X(implied), X(track)
    ticks = "".join(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="20" y2="40" stroke="var(--grid)"/>'
                    f'<text x="{X(t):.1f}" y="{h-2}" text-anchor="middle" font-size="10" fill="var(--t3)">{int(t*100)}%</text>'
                    for t in (0, 0.1, 0.2, 0.3, 0.4))
    off = lambda v: " \u25b8" if v > hi else ""
    close = abs(xi - xt) < 70
    ty = 52 if close else 12
    anchor = lambda x: "start" if x < 40 else ("end" if x > w - 40 else "middle")
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;display:block" role="img" aria-label="Implied versus track record growth">{ticks}'
            f'<line x1="{min(xi,xt):.1f}" x2="{max(xi,xt):.1f}" y1="30" y2="30" stroke="var(--grey-soft)" stroke-width="3"/>'
            f'<circle cx="{xt:.1f}" cy="30" r="6" fill="var(--grey)"><title>Track record {pct(track,1)}/yr</title></circle>'
            f'<circle cx="{xi:.1f}" cy="30" r="7" fill="var(--blue)"><title>Price implies {pct(implied,1)}/yr</title></circle>'
            f'<text x="{xi:.1f}" y="12" text-anchor="{anchor(xi)}" font-size="11" font-weight="600" fill="var(--blue)">implied {pct(implied)}{off(implied)}</text>'
            f'<text x="{xt:.1f}" y="{ty}" text-anchor="{anchor(xt)}" font-size="11" fill="var(--t2)">track {pct(track)}{off(track)}</text></svg>')


def fcf_bars_svg(hist, w=320, h=84):
    if not hist:
        return ""
    vals = [x["fcf_bn"] for x in hist]
    mx = max(max(vals), 0.001)
    mn = min(0, min(vals))
    rng = mx - mn
    bw = (w - 10) / len(vals)
    base = 18 + (mx / rng) * (h - 36)
    parts = []
    for i, x in enumerate(hist):
        v = x["fcf_bn"]
        bh = abs(v) / rng * (h - 36)
        y = base - bh if v >= 0 else base
        cx = 5 + i * bw + bw / 2
        parts.append(f'<rect x="{5+i*bw+bw*0.2:.1f}" y="{y:.1f}" width="{bw*0.6:.1f}" height="{max(bh,1):.1f}" rx="3" fill="{'var(--blue)' if x.get('ttm') else 'var(--grey)'}" opacity="{0.75 if x.get('ttm') else 1}"><title>{"12 months to" if x.get("ttm") else "FY ending"} {x["fy_end"]}: {money_bn(v)}</title></rect>')
        parts.append(f'<text x="{cx:.1f}" y="{h-2}" text-anchor="middle" font-size="10" fill="var(--t3)">{"TTM" if x.get("ttm") else "FY" + x["fy_end"][2:4]}</text>')
    parts.append(f'<text x="{5+(len(vals)-1)*bw+bw/2:.1f}" y="{base-(vals[-1]/rng*(h-36) if vals[-1]>=0 else 0)-5:.1f}" text-anchor="middle" font-size="10.5" font-weight="600" fill="var(--t1)">{money_bn(vals[-1])}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;display:block" role="img" aria-label="Annual free cash flow">{"".join(parts)}</svg>'


def company_card(r, explorer_url):
    chips = []
    if r["flag_drop"]:
        chips.append(f'<span class="chip drop">Down {pct(-r["chg_5d"])} in 5 days</span>')
    if r["flag_cluster"]:
        chips.append(f'<span class="chip ins">{len(r["insider_buyers"])} insiders bought ${r["insider_value"]/1e6:,.1f}M</span>')
    if r["flag_gap"]:
        chips.append(f'<span class="chip gap">Price implies {pct(r["implied_g"])}/yr vs {pct(r["fcf_cagr"])} track record</span>')
    buyers = ""
    if r["insider_buyers"]:
        buyers = '<div class="small" style="margin-top:8px">Insider buyers: ' + esc("; ".join(r["insider_buyers"])) + "</div>"
    edgar = f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r["cik"]}&type=4&dateb=&owner=include&count=40'
    filings = f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r["cik"]}&type=10-&dateb=&owner=include&count=40'
    return f"""<div class="card co">
<div>
  <h3>{esc(r['ticker'])}</h3><div class="nm">{esc(r['name'])}</div>
  <div class="chips">{''.join(chips)}</div>
  <div class="kv">
    <div>Price<b>${r['price']:,.2f}</b></div>
    <div>Market cap<b>{money_bn(r['mcap_bn'])}</b></div>
    <div>Price ÷ FCF ({esc(r['fcf_basis'])})<b>{r['p_fcf']:.1f}x</b></div>
    <div>FCF margin<b>{pct(r['fcf_margin'])}</b></div>
  </div>
  {buyers}
  <div class="small" style="margin-top:10px"><a href="{edgar}">Insider filings</a> · <a href="{filings}">Annual & quarterly reports</a>{f' · <a href="{explorer_url}">Open the reverse-DCF explorer</a>' if explorer_url else ''}</div>
</div>
<div>
  <div class="small">Growth the price requires vs. track record</div>{gap_svg(r['implied_g'], r['fcf_cagr'])}
  <div class="small" style="margin-top:4px">Price, last 90 trading days</div>{spark_svg(r['spark'])}
  <div class="small" style="margin-top:4px">Free cash flow: fiscal years + trailing 12 months</div>{fcf_bars_svg(r['fcf_history'])}
</div></div>"""


def table(rows, cols):
    if not rows:
        return '<div class="empty">None today.</div>'
    head = "".join(f"<th>{esc(c[0])}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c[1](r)}</td>" for c in cols) + "</tr>" for r in rows)
    return f'<div class="card" style="padding:6px 10px"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


COLS = [
    ("Ticker", lambda r: f"<b>{esc(r['ticker'])}</b>"),
    ("Company", lambda r: esc(r["name"][:34])),
    ("5-day", lambda r: pct(r["chg_5d"], 1, True)),
    ("Insiders", lambda r: str(len(r["insider_buyers"])) if r["insider_buyers"] else "–"),
    ("P/FCF", lambda r: f"{r['p_fcf']:.0f}x"),
    ("FCF margin", lambda r: pct(r["fcf_margin"])),
    ("Implied", lambda r: pct(r["implied_g"], 1)),
    ("Track", lambda r: pct(r["fcf_cagr"], 1)),
    ("Gap", lambda r: pct(r["gap"], 1, True)),
]


def render_report(data):
    m = data["meta"]
    cfg = m["config"]
    sl = data["shortlist"]
    cards = "".join(company_card(r, f"explorer.html#{r['ticker'].lower()}") for r in sl) if sl else (
        '<div class="empty"><b>No company cleared all three filters today.</b><br>That is the normal case: '
        "most days the market isn't mispricing a quality business. Check the two lists below.</div>")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Cash-Compounder Scan · {esc(m['asof'])}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Daily cash-compounder scan</h1>
<div class="meta">As of {esc(m['asof'])} · generated {esc(m['generated'])} · research shortlist, not buy signals · <a href="explorer.html" style="color:var(--blue)">Open today's names in the reverse-DCF explorer</a></div>
<div class="card">{funnel_svg(m['funnel'])}</div>

<h2>Shortlist</h2>
<p class="sub">Quality businesses that hit a trigger today <i>and</i> whose price implies cash flow growth at least {abs(cfg['GAP_THRESHOLD'])*100:.0f} points a year below their own track record. Each needs your judgment: is the problem temporary or permanent?</p>
{cards}

<h2>Triggered, but not cheap vs. track record</h2>
<p class="sub">Quality names that dropped sharply or saw insider buying, but the price still asks for about as much growth as they've delivered (or more).</p>
{table(data['triggered_only'], COLS)}

<h2>Watchlist: cheap vs. track record, no trigger yet</h2>
<p class="sub">The price already assumes a slowdown, but nothing happened today. Worth knowing before a trigger arrives.</p>
{table(data['gap_only'], COLS)}

<details><summary>How this screen works and its thresholds</summary>
<p><b>Quality:</b> latest fiscal-year revenue ≥ ${cfg['MIN_REVENUE_USD']/1e6:,.0f}M, free cash flow margin ≥ {cfg['MIN_FCF_MARGIN']*100:.0f}%, positive free cash flow in each of the last {cfg['MIN_POSITIVE_FCF_YEARS']} fiscal years, free cash flow ÷ (equity + long-term debt) ≥ {cfg['MIN_CASH_RETURN_ON_CAPITAL']*100:.0f}%, market cap ≥ ${cfg['MIN_MARKET_CAP_USD']/1e9:.0f}B.</p>
<p><b>Triggers:</b> price down {abs(cfg['DROP_THRESHOLD'])*100:.0f}%+ over {cfg['DROP_LOOKBACK_DAYS']} trading days, or {cfg['MIN_CLUSTER_BUYERS']}+ distinct insiders making open-market purchases (Form 4, code P) in the last {cfg['INSIDER_LOOKBACK_DAYS']} days.</p>
<p><b>Cash flow basis:</b> trailing twelve months, built from the latest 10-Q (last fiscal year + this year's year-to-date − last year's matching year-to-date). Falls back to the latest 10-K when no newer quarter is available.</p>
<p><b>Reverse DCF:</b> {cfg['REQUIRED_RETURN']*100:.0f}% required return, {cfg['TERMINAL_GROWTH']*100:.0f}% growth after year {cfg['FORECAST_YEARS']}, trailing-twelve-month free cash flow, net cash added. Track record = free cash flow growth per year from three fiscal years ago to the trailing twelve months.</p>
<p><b>Known blind spots:</b> banks and insurers don't fit a free cash flow screen; companies with several share classes may be missing a share count; stock-based compensation is not subtracted. Data: SEC EDGAR (company facts, Form 4), daily closes from Stooq/Yahoo.</p>
</details>
</div></body></html>"""
