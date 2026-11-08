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
SEC_DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/{path}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"


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

    def get(self, url: str, retries: int = 3) -> bytes:
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
                with urllib.request.urlopen(req, timeout=30) as resp:
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

    def get(self, url: str, retries: int = 1) -> bytes:
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
        m = re.search(r"daily-index/\d{4}/QTR\d/(form\.\d{8}\.idx)", url)
        if m:
            return self.root / "daily-index" / m.group(1)
        m = re.search(r"/Archives/(edgar/data/.+)$", url)
        if m:
            return self.root / "archives" / m.group(1)
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


def refresh_facts_bulk(fetcher, cache_dir: Path, max_age_days: float, log=print) -> bool:
    """Weekly: pull SEC's nightly companyfacts.zip once instead of ~10,000 API calls,
    and keep only the concepts we use (a few KB per company)."""
    import tempfile
    import zipfile
    marker = cache_dir / ".bulk_refreshed"
    if marker.exists() and (time.time() - marker.stat().st_mtime) < max_age_days * 86400:
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "companyfacts.zip"
        log("downloading SEC companyfacts.zip (about 1 GB; weekly)...")
        fetcher.download_to(SEC_FACTS_BULK_URL, zpath)
        n = 0
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                m = re.match(r"CIK(\d{10})\.json$", name)
                if not m:
                    continue
                try:
                    data = json.loads(zf.read(name))
                except ValueError:
                    continue
                cik = int(m.group(1))
                (cache_dir / name).write_text(json.dumps(slim_facts(cik, data)))
                n += 1
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
}


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-")


def load_prices(fetcher, ticker: str) -> list[tuple[str, float]]:
    """Daily closes, oldest first. Stooq first, Yahoo as fallback."""
    try:
        txt = fetcher.get(STOOQ_URL.format(sym=stooq_symbol(ticker))).decode("utf-8", "replace")
        rows = []
        for line in txt.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 5 and parts[4] not in ("", "null"):
                rows.append((parts[0], float(parts[4])))
        if len(rows) > 5:
            return rows[-260:]
    except (FetchError, ValueError):
        pass
    try:
        js = json.loads(fetcher.get(YAHOO_URL.format(sym=ticker.replace(".", "-"))))
        res = js["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        rows = [(time.strftime("%Y-%m-%d", time.gmtime(t)), c) for t, c in zip(ts, closes) if c]
        return rows[-260:]
    except Exception:
        return []


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
