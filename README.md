# Daily cash-compounder scan

Every weekday after the close, this scans US-listed companies for one pattern: a **quality cash
generator** that just hit a **trigger** (a sharp drop or several insiders buying), where the
**price now implies slower cash-flow growth than the company's own track record**. The output is a
research shortlist, not buy signals.

```
All SEC filers  ->  Quality filter  ->  Trigger today  ->  Reverse-DCF gap  ->  Shortlist
 (~6-10k)            FCF margin,        5-day drop, or      implied growth
                     consistency,       2+ insiders         well below track
                     return on capital  buying (Form 4)     record
```

Each run writes:

- `report.html`: the visual daily report (light and dark mode)
- `explorer.html`: the interactive reverse-DCF explorer ("What is the price assuming?"), loaded with
  that day's shortlist, watchlist and triggered names. Each shortlist card links straight to its company.
- `results.csv` and `results.json`

## Run it daily on GitHub (free)

1. Create a new **private** repo and push this folder to it.
2. **Settings → Secrets and variables → Actions → New repository secret**
   `SEC_USER_AGENT` = `Your Name you@example.com`. SEC requires a contact in the User-Agent
   and blocks requests without one.
3. **Settings → Pages → Source: GitHub Actions.**
4. **Actions → Daily cash-compounder scan → Run workflow** for the first run. It downloads SEC's
   bulk company-facts archive (~1 GB) and every run refreshes it so new 10-Qs appear the next day.
   Expect each run to take roughly 10-30 minutes.

The latest report is then at your repo's Pages URL, with a dated copy under `/archive/`. GitHub
Pages on a private repo needs a paid GitHub plan; on a free plan, download the report from the
run's **Artifacts** instead, or make the repo public.

## Run it on your own machine

```bash
export SEC_USER_AGENT="Your Name you@example.com"
python -m scanner.scan --out out                 # full scan (downloads ~1 GB of SEC data per day)
python -m scanner.scan --tickers V MA ADBE --out out   # quick test on a few names
open out/report.html
```

Python 3.10+, standard library only. Offline self-test with fictional sample companies:

```bash
python tests/make_fixtures.py fixtures && python -m scanner.scan --fixtures fixtures --asof 2026-11-06 --out out
python -m unittest discover tests
```

## Tuning

All thresholds live in `scanner/config.py`: revenue floor, FCF margin, return on capital, drop
size and window, insider cluster size, required return, terminal growth, and how big a gap
counts.

## How each piece works

| Step | Source | Notes |
|---|---|---|
| Universe | `sec.gov/files/company_tickers.json` | One primary ticker per company |
| Fundamentals | SEC XBRL company facts (bulk `companyfacts.zip`, refreshed daily) | Last 3-4 fiscal years from 10-Ks, plus **trailing twelve months from the latest 10-Q** (last fiscal year + this year's year-to-date − last year's matching year-to-date). Falls back to the 10-K when no newer quarter exists |
| Prices | Stooq daily CSV, Yahoo chart API as fallback | Only fetched for companies that pass the quality filter |
| Insider buying | EDGAR daily form index → each Form 4's XML | Counts open-market purchases (code P) only; sales and option exercises are ignored |
| Reverse DCF | `scanner/analysis.py` | Same model as the interactive explorer: 10 years of constant growth, then terminal growth, discounted at the required return, plus net cash |

## Known blind spots

- SEC publishes its bulk company-facts archive nightly, so a 10-Q filed today shows up in tomorrow's
  run. Raise `FUNDAMENTALS_CACHE_DAYS` in `config.py` to download less often.
- **Reference growth** is the lower of FCF and revenue growth (three fiscal years ago → trailing twelve months), capped at
  20%. Raw FCF growth is too noisy on its own: a rebound from a depressed base (memory chips, energy) made almost every
  price look cheap in the first live run. The shortlist also requires the price to imply 10%/yr or less.
- **Sharp drops** are measured against the S&P 500 (SPY), so a market-wide sell-off doesn't flag everything.
- **Banks and insurers** don't fit a free-cash-flow screen and will mostly fail or mislead.
- Companies with **several share classes** may be missing a share count and drop out.
- **Stock-based compensation** isn't subtracted from free cash flow.
- Stooq and Yahoo are unofficial price sources and can rate-limit or change format.
