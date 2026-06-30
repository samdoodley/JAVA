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

# ─── Capital Configuration ────────────────────────────────────────────────────
STARTING_CAPITAL = 100000.0    # Virtual capital for paper mode (₹1 lakh)
CAPITAL_PER_TRADE = 5000.0     # Max capital per trade
RISK_PER_TRADE_PCT = 1.0       # % of capital to risk per trade
MAX_POSITIONS = 5              # Max concurrent positions

# ─── Strategy Configuration ───────────────────────────────────────────────────
EMA_LENGTH = 200    # EMA trend filter period (candlesticks)
MIN_WICK_PCT = 40   # Min wick size (% of candle range) to form a zone
RISK_REWARD = 2.0   # Risk:Reward ratio for TP calculation

# ─── Scan Configuration ───────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 60  # Scan loop interval in seconds
RISK_REWARD   = 2.0    # TP = entry ± risk × 2  →  1:2 RR
MAX_POSITIONS = 5      # Max simultaneous open positions

# ─── Timing ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 300   # Scan every 5 minutes (aligns with 5-min candle)

# ─── Dashboard ────────────────────────────────────────────────────────────────
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5050

# ─── Mock Mode ─────────────────────────────────────────────────────────────────
USE_MOCK_DATA = True  # Use synthetic candles when Kite historical fetch fails or lacks permission
