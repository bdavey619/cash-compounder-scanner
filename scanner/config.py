"""Thresholds for the daily scan. Edit these to tune the screen."""

# --- Quality filter (applied to the last 3 fiscal years from 10-K filings) ---
MIN_REVENUE_USD = 500e6          # latest fiscal-year revenue
MIN_FCF_MARGIN = 0.15            # latest FCF / revenue
MIN_POSITIVE_FCF_YEARS = 3       # of the last 3 fiscal years
MIN_CASH_RETURN_ON_CAPITAL = 0.10  # latest FCF / (equity + long-term debt); a simple ROIC proxy
MIN_MARKET_CAP_USD = 2e9         # skip micro/small caps with thin data

# --- Event triggers ---
DROP_LOOKBACK_DAYS = 5           # trading days
DROP_THRESHOLD = -0.10           # price change over the lookback, relative to the S&P 500 (SPY), that counts as a sharp drop
DROP_RELATIVE_TO_MARKET = True   # so a market-wide sell-off doesn't flag every stock
INSIDER_LOOKBACK_DAYS = 7        # calendar days of Form 4 filings to scan
MIN_CLUSTER_BUYERS = 2           # distinct insiders making open-market purchases (code "P")
MIN_BUY_VALUE_USD = 25_000       # ignore token purchases (dividend reinvestment, small plan buys)

# --- Reverse DCF ---
REQUIRED_RETURN = 0.09           # base required return for a steady, lightly indebted business
RISK_PREMIUM_CYCLICAL = 0.015    # added when free cash flow swings with the cycle
RISK_PREMIUM_LEVERAGE = 0.01     # added when net debt exceeds LEVERAGE_NET_DEBT_TO_FCF x free cash flow
LEVERAGE_NET_DEBT_TO_FCF = 3.0
TERMINAL_GROWTH = 0.03
FORECAST_YEARS = 10
GAP_THRESHOLD = -0.05            # implied growth at least 5 pts/yr below the reference growth rate
MAX_IMPLIED_GROWTH = 0.10        # and the price assumes no more than 10%/yr: a genuinely modest bar
TRACK_CAP = 0.20                 # reference growth = lower of FCF and revenue growth PER SHARE, capped here,
                                 # so a rebound from a depressed base (e.g. a memory-chip upcycle) can't
                                 # make every price look cheap

# --- Housekeeping ---
FUNDAMENTALS_CACHE_DAYS = 0.9    # refresh SEC company facts daily so new 10-Qs show up fast (~1 GB download)
SEC_RATE_LIMIT_PER_SEC = 8       # SEC asks for no more than 10 requests per second

# --- Labels ---
# A company is marked "volatile cash flow" (cyclical) when its last 3 fiscal years of FCF swing by
# 60%+ of their average. It stays on the lists, labeled, with a 3-year-average view in the explorer.
