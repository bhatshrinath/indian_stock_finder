"""Central settings for the stock finder engine.

Everything tunable lives here so strategies, backtests and the app stay in sync.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "engine"
OUT_DIR = ROOT / "docs" / "data"          # published to GitHub Pages
CACHE_DIR = ROOT / "data" / "cache"        # local only (gitignored)

# ---------------------------------------------------------------- universe
# Nifty 200 = large + mid caps: liquid enough that a retail order never moves
# the price, and broad enough to find setups every week.
UNIVERSE_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://niftyindices.com/IndexConstituent/ind_nifty200list.csv",
]
UNIVERSE_FALLBACK = ENGINE_DIR / "nifty200_fallback.csv"
INDEX_SYMBOL = "^NSEI"                     # Nifty 50 for regime / relative strength

# ---------------------------------------------------------------- history
HISTORY_PERIOD = "5y"                      # yfinance period for full runs
BHAV_HISTORY_DAYS = int(os.getenv("BHAV_HISTORY_DAYS", "420"))  # NSE fallback depth

# ---------------------------------------------------------------- trading costs
# Delivery trades in India: STT 0.1% each side + exchange/SEBI/stamp/GST +
# a discount-broker fee. ~0.30% round trip is a realistic, slightly
# conservative figure. Every backtest number is AFTER these costs.
BASE_COST = 0.003
# Fixed depository (DP) charge on every sell day (e.g. Zerodha Rs 13.5 + GST).
# Small trades feel this: on Rs 10,000 it is another ~0.16%.
DP_CHARGE_RS = float(os.getenv("DP_CHARGE_RS") or 15.93)
ROUND_TRIP_COST = BASE_COST                # final value set below, once TRADE_AMOUNT is known

# ---------------------------------------------------------------- risk defaults
# The app lets the user change these; they're only defaults.
DEFAULT_CAPITAL = 500_000
DEFAULT_RISK_PER_TRADE = 0.01              # risk 1% of swing capital per trade
MAX_POSITION_PCT = 0.20                    # never more than 20% of swing capital in one stock

# ---------------------------------------------------------------- long-term model
LT_PORTFOLIO_SIZE = 15                     # number of stocks in the core portfolio
LT_MIN_HISTORY = 260                       # trading days needed to score a stock

# ---------------------------------------------------------------- swing filters
MIN_PRICE = 50                             # avoid very low-priced stocks
MIN_TURNOVER_CR = 10                       # min avg daily turnover (₹ crore, 20d)

# ---------------------------------------------------------------- SAFE MODE (very low risk)
# Only the 50 largest companies, only the most reliable setup, only when its own
# 5-year backtest clears a high bar, and at most a couple of trades at a time.
SAFE_MODE = os.getenv("SAFE_MODE", "on").strip().lower() != "off"
NIFTY50_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv",
]
NIFTY50_FALLBACK = ENGINE_DIR / "nifty50_fallback.csv"
SAFE_STRATEGIES = ("safe_dip",)
SAFE_MIN_WIN_RATE = 60.0          # % of past trades that made money (after costs)
SAFE_MIN_PROFIT_FACTOR = 1.2      # total gains / total losses in the backtest
SAFE_MIN_TRADES = 40              # enough history to trust the numbers
# One trade at a time with the whole amount: on 5 years of real data, 1 x Rs 20,000 earned
# ~7x more than 2 x Rs 10,000 (fixed charges hit small trades) with a smaller worst drop.
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES") or 1)   # empty GitHub variable -> default
MAX_NEW_PER_DAY = 1
# rupees per trade used in phone alerts and simulations (the app uses the amount set on the phone)
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT") or 20000)
# realistic total cost for a trade of TRADE_AMOUNT (percent costs + the fixed DP charge)
ROUND_TRIP_COST = BASE_COST + DP_CHARGE_RS / TRADE_AMOUNT

# ---------------------------------------------------------------- long-term holding rules
# Safety price that rises with the price (fraction below the highest close; None = no such rule),
# and the monthly "is it still among the strongest?" check. Chosen from the study in engine/research.py.
HOLD_STOCK_TRAIL = 0.12
HOLD_FUND_TRAIL = 0.10
HOLD_MONTHLY_CHECK = False

# ---------------------------------------------------------------- alerts/AI (optional)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SMTP_USER = os.getenv("GMAIL_USER", "").strip()
SMTP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "").strip()
# Paid feature: needs BOTH the API key secret AND repository variable AI_NOTES=on.
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY", "").strip()
                     if os.getenv("AI_NOTES", "").strip().lower() == "on" else "")
AI_MODEL = os.getenv("AI_MODEL", "claude-opus-5-5")
APP_URL = os.getenv("APP_URL", "").strip()

USER_AGENT = "indian-stock-finder/1.0 (+https://github.com/)"
