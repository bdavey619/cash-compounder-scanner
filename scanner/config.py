"""Thresholds for the daily scan. Edit these to tune the screen."""

# --- Quality filter (applied to the last 3 fiscal years from 10-K filings) ---
MIN_REVENUE_USD = 500e6          # latest fiscal-year revenue
MIN_FCF_MARGIN = 0.15            # latest FCF / revenue
MIN_POSITIVE_FCF_YEARS = 3       # of the last 3 fiscal years
MIN_CASH_RETURN_ON_CAPITAL = 0.10  # latest FCF / (equity + long-term debt); a simple ROIC proxy
MIN_MARKET_CAP_USD = 2e9         # skip micro/small caps with thin data

# --- Event triggers ---
DROP_LOOKBACK_DAYS = 5           # trading days
DROP_THRESHOLD = -0.10           # price change over the lookback that counts as a sharp drop
INSIDER_LOOKBACK_DAYS = 7        # calendar days of Form 4 filings to scan
MIN_CLUSTER_BUYERS = 2           # distinct insiders making open-market purchases (code "P")
MIN_BUY_VALUE_USD = 25_000       # ignore token purchases (dividend reinvestment, small plan buys)

# --- Reverse DCF ---
REQUIRED_RETURN = 0.09
TERMINAL_GROWTH = 0.03
FORECAST_YEARS = 10
GAP_THRESHOLD = -0.03            # implied growth at least 3 pts/yr below track record

# --- Housekeeping ---
FUNDAMENTALS_CACHE_DAYS = 0.9    # refresh SEC company facts daily so new 10-Qs show up fast (~1 GB download)
SEC_RATE_LIMIT_PER_SEC = 8       # SEC asks for no more than 10 requests per second
