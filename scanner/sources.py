"""Data access: SEC EDGAR (fundamentals, Form 4 insider filings) and daily prices.

Two interchangeable fetchers share one interface (`get(url) -> bytes`):
  * LiveFetcher    - real HTTP, rate-limited, with the User-Agent SEC requires.
  * FixtureFetcher - maps the same URLs onto files in a local folder, for offline tests.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_FACTS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/{path}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
FRED_DGS10_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd={start}"


class FetchError(Exception):
    pass


class LiveFetcher:
    def __init__(self, user_agent: str, sec_rate_per_sec: float = 8):
        if not user_agent or "@" not in user_agent:
            raise SystemExit(
                "Set SEC_USER_AGENT to 'Your Name your.email@example.com'. "
                "SEC blocks requests without a contact in the User-Agent."
            )
        self.ua = user_agent
        self.min_gap = 1.0 / sec_rate_per_sec
        self._last_sec = 0.0

    def get(self, url: str, retries: int = 3, timeout: float = 30) -> bytes:
        is_sec = "sec.gov" in url
        for attempt in range(retries):
            if is_sec:
                wait = self.min_gap - (time.monotonic() - self._last_sec)
                if wait > 0:
                    time.sleep(wait)
                self._last_sec = time.monotonic()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.ua if is_sec else "Mozilla/5.0 (daily-cash-scanner)",
                "Accept-Encoding": "gzip",
            })
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise FetchError(f"404 {url}") from e
                if e.code in (403, 429) and attempt < retries - 1:
                    time.sleep(2 ** (attempt + 2))
                    continue
                raise FetchError(f"HTTP {e.code} {url}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise FetchError(f"{e} {url}") from e
        raise FetchError(url)

    def download_to(self, url: str, dest: Path) -> None:
        """Stream a large file (the ~1 GB SEC bulk archive) to disk."""
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)


class FixtureFetcher:
    """Serves URLs from a fixtures folder laid out like:
    company_tickers.json, companyfacts/CIK##########.json, prices/<ticker>.csv,
    daily-index/form.YYYYMMDD.idx, archives/<edgar path>
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def get(self, url: str, retries: int = 1, timeout: float = 30) -> bytes:
        path = self._map(url)
        if path is None or not path.exists():
            raise FetchError(f"no fixture for {url}")
        return path.read_bytes()

    def _map(self, url: str):
        if url == SEC_TICKERS_URL:
            return self.root / "company_tickers.json"
        m = re.search(r"companyfacts/(CIK\d{10})\.json", url)
        if m:
            return self.root / "companyfacts" / f"{m.group(1)}.json"
        m = re.search(r"submissions/(CIK\d{10})\.json", url)
        if m:
            return self.root / "submissions" / f"{m.group(1)}.json"
        m = re.search(r"daily-index/\d{4}/QTR\d/(form\.\d{8}\.idx)", url)
        if m:
            return self.root / "daily-index" / m.group(1)
        m = re.search(r"/Archives/(edgar/data/.+)$", url)
        if m:
            return self.root / "archives" / m.group(1)
        if "fred.stlouisfed.org" in url and "DGS10" in url:
            return self.root / "rates" / "DGS10.csv"
        m = re.search(r"stooq\.com/q/d/l/\?s=([a-z0-9\-]+)\.us", url)
        if m:
            return self.root / "prices" / f"{m.group(1)}.csv"
        return None


# ---------------------------------------------------------------- helpers

def load_tickers(fetcher) -> list[dict]:
    raw = json.loads(fetcher.get(SEC_TICKERS_URL))
    out, seen = [], set()
    for row in raw.values():
        cik = int(row["cik_str"])
        if cik in seen:  # keep the first (primary) ticker per company
            continue
        seen.add(cik)
        out.append({"cik": cik, "ticker": row["ticker"], "name": row["title"]})
    return out


def slim_facts(cik: int, data: dict) -> dict:
    slim = {"cik": cik, "facts": {}}
    for ns in ("us-gaap", "dei"):
        src = data.get("facts", {}).get(ns, {})
        slim["facts"][ns] = {k: v for k, v in src.items() if k in WANTED_CONCEPTS}
    return slim


def refresh_facts_bulk(fetcher, cache_dir: Path, max_age_days: float, log=print, only_ciks=None) -> bool:
    """Weekly: pull SEC's nightly companyfacts.zip once instead of ~10,000 API calls,
    and keep only the concepts we use (a few KB per company)."""
    import tempfile
    import zipfile
    marker = cache_dir / f".bulk_refreshed_{CACHE_VERSION}"
    if marker.exists() and (time.time() - marker.stat().st_mtime) < max_age_days * 86400:
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "companyfacts.zip"
        log("downloading SEC companyfacts.zip (about 1 GB)...")
        fetcher.download_to(SEC_FACTS_BULK_URL, zpath)
        n = 0
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                m = re.match(r"CIK(\d{10})\.json$", name)
                if not m:
                    continue
                cik = int(m.group(1))
                if only_ciks is not None and cik not in only_ciks:
                    continue
                try:
                    data = json.loads(zf.read(name))
                except ValueError:
                    continue
                (cache_dir / name).write_text(json.dumps(slim_facts(cik, data)))
                n += 1
                if n % 2000 == 0:
                    log(f"  unpacked {n:,} companies")
        log(f"cached slim facts for {n:,} companies")
    marker.touch()
    return True


def load_company_facts(fetcher, cik: int, cache_dir: Path | None, max_age_days: float,
                       allow_network: bool = True) -> dict | None:
    if cache_dir is not None:
        p = cache_dir / f"CIK{cik:010d}.json"
        if p.exists() and (time.time() - p.stat().st_mtime) < max_age_days * 86400:
            return json.loads(p.read_text())
        if p.exists() and not allow_network:
            return json.loads(p.read_text())
    if not allow_network:
        return None
    try:
        data = json.loads(fetcher.get(SEC_FACTS_URL.format(cik=cik)))
    except FetchError:
        return None
    slim = slim_facts(cik, data)  # keep only what we use, so the cache stays small
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"CIK{cik:010d}.json").write_text(json.dumps(slim))
    return slim


CACHE_VERSION = "v2"  # bump when WANTED_CONCEPTS changes so cached files get rebuilt

WANTED_CONCEPTS = {
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "StockholdersEquity",
    "LongTermDebt",
    "LongTermDebtNoncurrent",
    "LongTermDebtCurrent",
    "CashAndCashEquivalentsAtCarryingValue",
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "EntityCommonStockSharesOutstanding",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "ShareBasedCompensation",
    "AllocatedShareBasedCompensationExpense",
}


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-")


class SourceBreaker:
    """Stop calling a price source after it fails many times in a row (blocked or rate-limited),
    so one bad source can't stall the run."""

    def __init__(self, limit: int = 15):
        import threading
        self.limit, self.fails, self.lock = limit, {}, threading.Lock()

    def open(self, name):
        return self.fails.get(name, 0) >= self.limit

    def record(self, name, ok):
        with self.lock:
            self.fails[name] = 0 if ok else self.fails.get(name, 0) + 1


def load_prices(fetcher, ticker: str, breaker: "SourceBreaker | None" = None) -> list[tuple[str, float]]:
    """Daily closes, oldest first. Stooq first, Yahoo as fallback. One quick try per source."""
    breaker = breaker or SourceBreaker()
    if not breaker.open("stooq"):
        try:
            txt = fetcher.get(STOOQ_URL.format(sym=stooq_symbol(ticker)), retries=1, timeout=12).decode("utf-8", "replace")
            rows = []
            for line in txt.strip().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 5 and parts[4] not in ("", "null"):
                    rows.append((parts[0], float(parts[4])))
            if len(rows) > 5:
                breaker.record("stooq", True)
                return rows[-260:]
            breaker.record("stooq", False)
        except (FetchError, ValueError):
            breaker.record("stooq", False)
    if not breaker.open("yahoo"):
        try:
            js = json.loads(fetcher.get(YAHOO_URL.format(sym=ticker.replace(".", "-")), retries=1, timeout=12))
            res = js["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            rows = [(time.strftime("%Y-%m-%d", time.gmtime(t)), c) for t, c in zip(ts, closes) if c]
            breaker.record("yahoo", bool(rows))
            return rows[-260:]
        except Exception:
            breaker.record("yahoo", False)
    return []


def load_risk_free(fetcher, asof) -> dict:
    """Latest 10-year Treasury yield: FRED (Federal Reserve H.15) first, Yahoo's ^TNX as a fallback.
    Returns {"rate": 0.0487, "date": "2026-11-06", "source": "FRED DGS10"} or rate None."""
    from datetime import timedelta
    try:
        txt = fetcher.get(FRED_DGS10_URL.format(start=(asof - timedelta(days=30)).isoformat()), retries=2, timeout=15).decode()
        rows = [ln.split(",") for ln in txt.strip().splitlines()[1:]]
        rows = [(d, float(v)) for d, v in (r[:2] for r in rows if len(r) >= 2) if v not in ("", ".")]
        if rows:
            return {"rate": rows[-1][1] / 100, "date": rows[-1][0], "source": "FRED DGS10"}
    except (FetchError, ValueError, UnicodeDecodeError):
        pass
    try:
        js = json.loads(fetcher.get(YAHOO_URL.format(sym="%5ETNX"), retries=1, timeout=12))
        res = js["chart"]["result"][0]
        pts = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
        if pts:
            v = pts[-1][1]
            v = v / 10 if v > 20 else v  # ^TNX was once quoted at 10x the yield
            return {"rate": v / 100, "date": time.strftime("%Y-%m-%d", time.gmtime(pts[-1][0])), "source": "Yahoo ^TNX"}
    except Exception:
        pass
    return {"rate": None, "date": None, "source": None}


def load_submissions(fetcher, cik: int) -> dict:
    """Industry code plus the dates the company reported earnings (8-K with Item 2.02,
    'Results of Operations and Financial Condition') from SEC's submissions API."""
    out = {"sic": None, "sic_desc": "", "earnings_dates": []}
    try:
        js = json.loads(fetcher.get(SEC_SUBMISSIONS_URL.format(cik=cik)))
    except (FetchError, ValueError):
        return out
    out["sic"] = int(js["sic"]) if js.get("sic") else None
    out["sic_desc"] = js.get("sicDescription") or ""
    rec = js.get("filings", {}).get("recent", {})
    for form, filed, items in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", [])):
        if form in ("8-K", "8-K/A") and "2.02" in (items or ""):
            out["earnings_dates"].append(filed)
    return out


def load_sic(fetcher, cik: int) -> tuple[int | None, str]:
    s = load_submissions(fetcher, cik)
    return s["sic"], s["sic_desc"]


SECTORS = [  # (label, SIC ranges)
    ("Software & internet", [(7370, 7379)]),
    ("Semiconductors & hardware", [(3570, 3579), (3660, 3699), (3810, 3829)]),
    ("Energy", [(1300, 1399), (2900, 2999), (4920, 4925), (4610, 4619)]),
    ("Industrials", [(1500, 1799), (3400, 3569), (3580, 3659), (3700, 3809), (4000, 4599), (8700, 8748)]),
    ("Healthcare", [(2830, 2836), (3840, 3851), (8000, 8099)]),
    ("Financials & insurance", [(6000, 6799)]),
    ("Consumer", [(2000, 2399), (5000, 5999), (7000, 7299), (7800, 7999)]),
    ("Media & telecom", [(2700, 2799), (4800, 4899)]),
    ("Education & services", [(8200, 8299), (7300, 7369), (7380, 7399), (8100, 8199), (8300, 8699), (8900, 8999)]),
    ("Materials & chemicals", [(1000, 1499), (2400, 2829), (2840, 2899), (3000, 3399)]),
    ("Utilities", [(4900, 4919), (4926, 4999)]),
]


def sector_for(sic: int | None) -> str:
    if sic is None:
        return "Other"
    for label, ranges in SECTORS:
        if any(lo <= sic <= hi for lo, hi in ranges):
            return label
    return "Other"


def load_form4_index(fetcher, ymd: str) -> list[tuple[int, str]]:
    """(issuer-or-owner CIK, archive path) for every Form 4 filed that day."""
    y, m = int(ymd[:4]), int(ymd[4:6])
    url = SEC_DAILY_INDEX_URL.format(y=y, q=(m - 1) // 3 + 1, ymd=ymd)
    try:
        txt = fetcher.get(url).decode("latin-1")
    except FetchError:
        return []  # weekends and holidays have no index
    out = []
    for line in txt.splitlines():
        if not line.startswith("4 ") and not line.startswith("4/A "):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        try:
            out.append((int(parts[-3]), parts[-1]))
        except ValueError:
            continue
    return out


def load_filing_text(fetcher, path: str) -> str | None:
    try:
        return fetcher.get(SEC_ARCHIVE_URL.format(path=path)).decode("utf-8", "replace")
    except FetchError:
        return None


def env_user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "").strip()
