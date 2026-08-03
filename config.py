"""
WickFill Bot Configuration — Zerodha Kite
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    load_dotenv = lambda: None

# ─── Zerodha Kite Credentials ──────────────────────────────────────────────────
API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
USER_ID = os.getenv("KITE_USER_ID")
PASSWORD = os.getenv("KITE_PASSWORD")
TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
MANUAL_REQUEST_TOKEN = os.getenv("MANUAL_REQUEST_TOKEN") or None
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN") or None

# Fallback: if .env does not provide ACCESS_TOKEN, read access_token.txt
if not ACCESS_TOKEN:
    token_path = os.path.join(os.path.dirname(__file__), "access_token.txt")
    if os.path.isfile(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            ACCESS_TOKEN = f.read().strip() or None

# ─── Trading Mode ─────────────────────────────────────────────────────────────
PAPER_TRADING = True   # Set False only for real money trades

# ─── Capital — NOW 100% LIVE FROM KITE, NOTHING HARDCODED ─────────────────────
MARGIN_SAFETY_BUFFER_PCT = 10.0
MAX_MARGIN_PER_TRADE_PCT = 11.0
FALLBACK_CAPITAL_IF_MARGIN_FETCH_FAILS = 100000.0
MARGIN_REFRESH_INTERVAL_SEC = 30

# ─── Order Margin Verification / Auto-Shrink (LIVE only) ─────────────────────
ORDER_MARGIN_SHRINK_STEP_PCT = 10.0
ORDER_MARGIN_MAX_SHRINK_ATTEMPTS = 8
MAX_TRADE_LEVERAGE_MULTIPLIER = 5.0

# ─── Position Sizing Mode ─────────────────────────────────────────────────────
QTY_MODE = "capital"
QTY_FIXED_SIZE = 80
RISK_PER_TRADE_PCT = 1.0
POSITION_QTY_MULTIPLIER = 3

# ─── Watchlist — Price Bands (UNCHANGED — same symbols, same band caps) ───────
PRICE_BAND_500_1000 = [
    "TATASTEEL", "HDFCBANK", "KOTAKBANK", "SBIN", "BEL", "CANBK","UNIONBANK",
    "POLYCAB", "BANKINDIA", "COALINDIA","MUTHOOTFIN"
]
PRICE_BAND_1000_2000 = [
    "AXISBANK", "ICICIBANK", "BHARTIARTL", "RELIANCE", "INFY", "SUNPHARMA", "HCLTECH",
    "TRENT", "PERSISTENT",
]
PRICE_BAND_2000_4000 = [
    "TCS", "LT","TVSMOTOR","M&M",
]

PRICE_BANDS = [
    {"name": "500-1000",   "symbols": PRICE_BAND_500_1000,  "max_positions": 4},
    {"name": "1000-2000",  "symbols": PRICE_BAND_1000_2000, "max_positions": 3},
    {"name": "2000-4000",  "symbols": PRICE_BAND_2000_4000, "max_positions": 2},
]

WATCHLIST = PRICE_BAND_500_1000 + PRICE_BAND_1000_2000 + PRICE_BAND_2000_4000

MAX_POSITIONS = 9

# ─── Strategy Configuration ───────────────────────────────────────────────────
EMA_LENGTH = 200
MIN_WICK_PCT = 40
RISK_REWARD = 2.0

# ─── Trend-Score Ranked Entry Gate ─────────────────────────────────────────────
# Every symbol's smoothed trend score (0-100, from ADX + EMA slope +
# price-distance from the 200-EMA) is computed on every scan.
#
#   score <  MIN_TREND_SCORE_FOR_ENTRY  -> CHOPPY: symbol is ignored outright;
#                                           the existing WickFill strategy
#                                           (EMA bias, wick-zone, zone-fill,
#                                           3% risk cap) is never evaluated.
#   score >= MIN_TREND_SCORE_FOR_ENTRY  -> both NEUTRAL and TRENDING stocks
#                                           are treated the same and still
#                                           have to pass every one of those
#                                           existing filters unmodified — the
#                                           score does not relax or replace
#                                           any of them.
#
# MIN_TREND_SCORE_FOR_ENTRY is set low (30) on purpose — it's meant to screen
# out only genuinely choppy/range-bound stocks (low ADX, flat EMA, price
# hugging the EMA), not to require a strong trend. Raise it toward 50-60 if
# you want to trade only stronger trends again; lower it further (e.g. 20) if
# you want to filter even less.
#
# Among symbols that pass everything, the score is then used to RANK
# candidates within each price band (see PRICE_BANDS above), so that when a
# band has more qualifying setups than open slots, the strongest-trending
# ones are taken first, up to that band's max_positions / the overall
# MAX_POSITIONS cap.
TREND_SLOPE_LOOKBACK = 14
MIN_TREND_SCORE_FOR_ENTRY = 30
SCORE_SMOOTHING_ALPHA = 0.3

# ─── Timing ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 300
MONITOR_INTERVAL_SEC = 7
TRADING_START_TIME = "09:30"
NO_NEW_ENTRIES_AFTER = "14:45"
SQUARE_OFF_TIME = "15:20"
MAX_HOLD_MINUTES = 150

# ─── Cooldown After Exit ───────────────────────────────────────────────────────
COOLDOWN_MINUTES = 3

# ─── Daily Per-Symbol SL-Hit Circuit Breaker ──────────────────────────────────
MAX_SL_HITS_PER_DAY = 8

# ─── Exchange-Native Execution Engine (LIVE mode only) ────────────────────────
USE_EXCHANGE_SL = True
USE_KITETICKER = True
ENABLE_SLIPPAGE_MONITOR = True
MAX_SLIPPAGE_POINTS = 2.0
MAX_SLIPPAGE_PERCENT = 0.5
ENABLE_LATENCY_LOG = True

# ─── Trailing Stop Ladder ───────────────────────────────────────────────────────
INITIAL_SL_R = 1.0

TRAIL_STAGES = [
    {"trigger_r": 1.5, "sl_r": 1.0},
    {"trigger_r": 2.0, "sl_r": 1.5},
    {"trigger_r": 2.5, "sl_r": 2.2},
    {"trigger_r": 3.0, "sl_r": 2.6},
]

# ─── Startup Warm-Up ───────────────────────────────────────────────────────────
WARMUP_SCANS = 1

# ─── Kite API Retry (timeouts / transient errors) ──────────────────────────────
KITE_RETRY_ATTEMPTS = 5
KITE_RETRY_DELAY_SEC = 3

# ─── Live Order Confirmation ───────────────────────────────────────────────────
ORDER_CONFIRM_ATTEMPTS = 12
ORDER_CONFIRM_DELAY_SEC = 0.4
USE_BROKER_SL = True
BROKER_SL_TICK_SIZE = 0.05

# ─── Position Reconciliation (bot state <-> broker truth) ────────────────────
RECONCILE_INTERVAL_SEC = 45

# ─── Paper-Trading Virtual Capital ────────────────────────────────────────────
PAPER_VIRTUAL_CAPITAL = 500000.0

# ─── Dashboard ────────────────────────────────────────────────────────────────
DASHBOARD_ENABLED = True
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5050
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "Vami")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "Vami2312")

# ─── Telegram Notifications ───────────────────────────────────────────────────
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TELEGRAM_NOTIFY_ENTRIES     = True
TELEGRAM_NOTIFY_EXITS       = True
TELEGRAM_NOTIFY_RECONCILE   = True
TELEGRAM_NOTIFY_ERRORS      = True
TELEGRAM_NOTIFY_STARTUP     = True
TELEGRAM_NOTIFY_EOD_SUMMARY = True

TELEGRAM_REQUEST_TIMEOUT_SEC = 5