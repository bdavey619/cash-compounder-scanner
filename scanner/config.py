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
DRAWDOWN_DAYS = 21               # trading days (~1 month) for the pullback trigger
DRAWDOWN_THRESHOLD = -0.15       # down this much from its 1-month high, beyond the S&P 500's own drawdown
INSIDER_LOOKBACK_DAYS = 7        # calendar days of Form 4 filings to scan
MIN_CLUSTER_BUYERS = 2           # distinct insiders making open-market purchases (code "P")
MIN_BUY_VALUE_USD = 25_000       # ignore token purchases (dividend reinvestment, small plan buys)

# --- Reverse DCF ---
# Base required return = 10-year Treasury yield (looked up each run) + EQUITY_PREMIUM, kept within a sane band.
# When rates rise, every stock has to clear a higher bar automatically.
EQUITY_PREMIUM = 0.04            # extra return demanded for owning a steady stock instead of a Treasury
RISK_FREE_FALLBACK = 0.045       # used only if the 10-year yield can't be fetched
REQUIRED_RETURN_MIN = 0.07
REQUIRED_RETURN_MAX = 0.13
REQUIRED_RETURN = RISK_FREE_FALLBACK + EQUITY_PREMIUM  # replaced at run time by the live figure
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

# Banks, lenders, brokers and insurers: "free cash flow" doesn't mean the same thing for them (a lender's
# loans are investing outflows, an insurer's float is operating inflow), so they're excluded, as most
# FCF screens do. SIC code ranges from the SEC.
EXCLUDED_SIC = [(6000, 6799)]   # the whole finance, insurance & real estate division, as ex-financials screens do
MAX_FCF_MARGIN = 0.9             # FCF above 90% of revenue isn't a normal operating business; treat as bad data

# --- Peak margins ---
# Today's FCF margin far above the company's own multi-year average often means the market expects it
# to fall back, so a "cheap" price may be fair. Those names are judged on normalized free cash flow
# (current revenue x their own average margin), the same way cyclicals are judged on a 3-year average.
PEAK_MARGIN_YEARS = 6            # fiscal years used for the average (needs at least 4)
PEAK_MARGIN_RATIO = 1.3          # current margin at least 1.3x the average...
PEAK_MARGIN_MIN_PTS = 0.05       # ...and at least 5 points above it

# --- Track record ---
# Every flagged name is logged with its price and the S&P 500's that day, then measured against SPY.
TRACK_HORIZONS = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}   # calendar days
TRACK_REPEAT_DAYS = 30           # don't log the same ticker in the same group again within this window

# --- Housekeeping ---
FUNDAMENTALS_CACHE_DAYS = 0.9    # refresh SEC company facts daily so new 10-Qs show up fast (~1 GB download)
SEC_RATE_LIMIT_PER_SEC = 8       # SEC asks for no more than 10 requests per second

# --- Labels ---
# A company is marked "volatile cash flow" (cyclical) when its last 3 fiscal years of FCF swing by
# 60%+ of their average. It stays on the lists, labeled, with a 3-year-average view in the explorer.
