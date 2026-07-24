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
# There is no more fixed STARTING_CAPITAL / MAX_INVESTMENT_CAPITAL / MIS_LEVERAGE
# / BUYING_POWER here. Every rupee figure the bot sizes trades against is
# fetched at runtime from kite.margins() — for BOTH paper and live modes, so
# paper-mode sizing behaves exactly like it will once PAPER_TRADING is flipped
# to False (same real account, same real per-stock margin rules).
#
# This means: you must have a working Kite session (ACCESS_TOKEN or auto/manual
# login) even in paper mode now, since margins() is a live API call against
# your real Zerodha account (it is read-only — no orders are placed by it).

# Never deploy 100% of what Kite reports as "available" — this % is held back
# as a buffer against margin fluctuation, MTM swings on open positions, etc.
MARGIN_SAFETY_BUFFER_PCT = 10.0

# Cap on how much of the (buffered) available margin any SINGLE trade may use.
# e.g. 12 = no one trade uses more than 12% of what's currently usable.
#
# CHANGED 20.0 -> 11.0: with MAX_POSITIONS = 9, a 20% per-trade slice only
# allows 5 concurrent positions (5 x 20% = 100% of the pool), which is
# exactly the "5/9 filled, rest skipped on insufficient margin" behavior
# seen in the logs. 11% x 9 = 99% of the pool — all 9 positions fill
# reliably with a small buffer left over, rather than 12% (108% total)
# which could leave the 9th slot short on the odd day.
MAX_MARGIN_PER_TRADE_PCT = 11.0

# Only used if kite.margins() itself fails (network blip, no session yet,
# etc.) so the bot doesn't crash — real trading should basically never hit
# this path since a failed margin fetch should really just skip the trade.
FALLBACK_CAPITAL_IF_MARGIN_FETCH_FAILS = 100000.0

# How often (seconds) the available-margin figure is refreshed from Kite.
# Cached in between so every single trade-sizing call doesn't need its own
# live API hit.
MARGIN_REFRESH_INTERVAL_SEC = 30

# ─── Order Margin Verification / Auto-Shrink (LIVE only) ─────────────────────
# Zerodha's real MIS margin is NOT a flat multiplier — it varies per stock.
# Before placing a LIVE order, the bot calls kite.order_margins() (Kite's own
# margin calculator) to check the REAL margin this specific order needs. If
# it doesn't fit the available margin, quantity is shrunk and rechecked
# instead of the signal being rejected outright.
ORDER_MARGIN_SHRINK_STEP_PCT = 10.0     # shrink qty by 10% each retry
ORDER_MARGIN_MAX_SHRINK_ATTEMPTS = 8    # give up (skip trade) after this many shrinks

# Real per-stock MIS leverage (via kite.order_margins()) is now used to size
# UP toward the actual leveraged quantity your cash slice supports, instead
# of a naive slice÷price divide that assumes no leverage at all. This safety
# cap bounds that: no single trade's exposure (qty x entry) may exceed this
# multiple of the intended per-trade cash slice, even if Kite's real leverage
# for that stock would technically allow more.
MAX_TRADE_LEVERAGE_MULTIPLIER = 5.0

# ─── Position Sizing Mode ─────────────────────────────────────────────────────
# - "capital": qty = floor(per_trade_margin / entry_price), where
#   per_trade_margin = live available margin x MAX_MARGIN_PER_TRADE_PCT.
#   Recommended — every trade uses roughly the same slice of your REAL,
#   currently-available margin, not a fixed rupee number.
# - "risk": sizes off risk-per-share and RISK_PER_TRADE_PCT (of the same live
#   per-trade margin slice above), then multiplies by POSITION_QTY_MULTIPLIER.
# - "fixed": flat share count (QTY_FIXED_SIZE) for every stock, no margin
#   awareness at all.
QTY_MODE = "capital"
QTY_FIXED_SIZE = 80          # only used when QTY_MODE == "fixed"
RISK_PER_TRADE_PCT = 1.0     # % of the per-trade margin slice to risk ("risk" mode only)
POSITION_QTY_MULTIPLIER = 3  # "risk" mode only

# ─── Watchlist — Price Bands ───────────────────────────────────────────────────
# Only these symbols are scanned, grouped by share price. Each band has its
# own max concurrent position count so the bot doesn't overload one price
# bracket. A slot frees up the moment a position in that band exits (SL/trail/
# time/EOD) — cooldown still applies before that symbol specifically can be
# re-entered, but the band's *slot* is immediately available to any other
# signal in the same band.
PRICE_BAND_500_1000 = [
    "TATASTEEL", "HDFCBANK", "KOTAKBANK", "SBIN", "BEL", "CANBK","Union Bank of India",
    "POLYCAB", "Bank of India", "COALINDIA","MUTHOOTFIN"
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

# Overall hard cap — equals the sum of the three band caps (5 + 3 + 1 = 9).
# Kept as a safety net in addition to the per-band caps above and the live
# margin check.
MAX_POSITIONS = 9

# ─── Strategy Configuration ───────────────────────────────────────────────────
EMA_LENGTH = 200    # EMA trend filter period (candlesticks)
MIN_WICK_PCT = 50   # Min wick size (% of candle range) to form a zone
RISK_REWARD = 2.0   # Reference target only — actual exits are trailing-stop based (see below)

# ─── Trend Filter (per-stock classification) ──────────────────────────────────
# Computed every new candle. Only stocks classified as TRENDING are allowed
# to trade. CHOPPY stocks are skipped immediately (no entry evaluation).
# NEUTRAL stocks are skipped by default; set ALLOW_NEUTRAL_TRADES to True to
# let them through if you want optional exposure.
TREND_FILTER_ENABLED = True
TREND_MIN_SCORE_TRENDING = 75
TREND_MIN_SCORE_NEUTRAL = 50
ALLOW_NEUTRAL_TRADES = False

# ─── Timing ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 300   # Scan every 5 minutes (aligns with 5-min candle) — used for SIGNAL detection only

# How often (seconds) open positions are checked against their SL / trailing
# stop / time exit. Intentionally separate from SCAN_INTERVAL_SEC — signals
# only need a fresh look every 5 minutes (candle-based), but once you're in a
# trade, waiting 5 minutes between stop-loss checks lets price run well past
# your stop before the bot even looks again.
MONITOR_INTERVAL_SEC = 7

# No new trades are opened before this time (IST, 24-hr "HH:MM"), even though
# NSE opens at 09:15 — gives the first 5-min candles time to settle instead of
# reacting to the volatile open-auction swing.
TRADING_START_TIME = "09:30"

# No new trades are opened after this time (IST, 24-hr "HH:MM").
# Positions already open keep running normally (trailing SL still applies).
NO_NEW_ENTRIES_AFTER = "14:45"

# At this time (IST, 24-hr "HH:MM"), any still-open position is force-closed
# at the current market price so P&L is realized before the 3:30 PM market
# close, instead of being carried into an uncontrolled close-of-day print.
SQUARE_OFF_TIME = "15:20"

# Max time (in minutes) to hold ANY single trade. If a trade hasn't hit its
# TP or SL within this many minutes of being opened, it is force-closed at
# the current market price ("TIME_EXIT").
MAX_HOLD_MINUTES = 120

# ─── Cooldown After Exit ───────────────────────────────────────────────────────
# After a position on a symbol is closed for ANY reason (SL_HIT, TRAIL_SL_HIT,
# TIME_EXIT, EOD_SQUAREOFF, MANUAL_CLOSE_RECONCILED), the bot will NOT take a
# fresh entry on that same symbol again until this many minutes have passed.
# Set to 0 to disable.
COOLDOWN_MINUTES = 5

# ─── Daily Per-Symbol SL-Hit Circuit Breaker ──────────────────────────────────
# If a single symbol's stop-loss fires this many times in one calendar day —
# SL_HIT (original stop) OR TRAIL_SL_HIT (trailed stop), either counts as a
# "stop-loss exit" — the bot stops taking NEW entries on that ONE symbol for
# the rest of today. Every other symbol on the watchlist keeps trading
# normally per the usual strategy rules; this is a per-symbol block, not a
# bot-wide pause. Any position already open when the limit is hit is
# unaffected and continues to be managed normally (trail/SL/time/EOD).
#
# The counter resets automatically:
#   - at midnight IST (a new calendar date starts a fresh count for every
#     symbol), and
#   - on every bot restart (the count lives in memory only, nothing is
#     persisted to disk) — so a fresh start of the bot always begins with
#     every symbol's SL-hit count back at 0 for today, exactly as requested.
MAX_SL_HITS_PER_DAY = 5

# ─── Exchange-Native Execution Engine (LIVE mode only) ────────────────────────
# Controls how LIVE trading protects and exits positions. PAPER_TRADING is
# unaffected by this block — there is no real broker order for a paper
# position to attach an exchange stop to, so paper trades keep using the
# bot's own simulated fill logic (now sourced from live WebSocket ticks when
# available, for a closer approximation — it's still a simulation).
#
# USE_EXCHANGE_SL: place a REAL Kite SL-M stop-loss order at the exchange
# immediately after every LIVE entry fills, and let IT protect the position —
# not a Python price-polling loop. Trailing MODIFIES this same resting order
# (kite.modify_order) rather than cancelling and recreating it. This is the
# fix for the oversized-loss bug: the old code had a polling loop that could
# independently fire its own market exit racing against this same order.
USE_EXCHANGE_SL = True

# USE_KITETICKER: run a KiteTicker WebSocket for (a) tick-by-tick prices used
# for trailing-stop calculations and paper-mode simulation, and (b) real-time
# order-update push notifications — this is what replaces repeatedly polling
# kite.order_history() as the primary way LIVE fills/rejections are detected.
# A REST fallback (a single check, never a repeating loop) still fires
# automatically if a push doesn't arrive within a few seconds, or if this
# is False, so nothing is silently unprotected either way.
USE_KITETICKER = True

# ENABLE_SLIPPAGE_MONITOR: after every exchange SL fill, compute and log
# (configured SL vs actual exit, slippage in points/%, expected vs actual
# loss, R-multiple) and attach it to the trade record. Warns "High Slippage"
# whenever the result exceeds MAX_SLIPPAGE_POINTS or MAX_SLIPPAGE_PERCENT.
ENABLE_SLIPPAGE_MONITOR = True
MAX_SLIPPAGE_POINTS = 2.0      # absolute points beyond the configured SL before warning
MAX_SLIPPAGE_PERCENT = 0.5     # % of the configured SL price beyond which to warn

# ENABLE_LATENCY_LOG: record signal/entry/SL/exit timestamps per trade and
# log the derived millisecond delays (entry fill delay, SL acceptance delay,
# total exit delay), attaching the summary to the trade record.
ENABLE_LATENCY_LOG = True

# ─── Trailing Stop Ladder ───────────────────────────────────────────────────────
# Every trade's "R" (one risk unit) = the distance between entry and the
# wick-zone stop the signal was built from. The stop-loss is trailed up in
# stages as price moves in your favor instead of using a fixed take-profit.
INITIAL_SL_R = 1.0   # initial stop distance from entry, in R multiples

TRAIL_STAGES = [
    {"trigger_r": 1.5, "sl_r": 1.0},
    {"trigger_r": 2.0, "sl_r": 1.5},
    {"trigger_r": 2.5, "sl_r": 2.2},
    {"trigger_r": 3.0, "sl_r": 2.6},
]

# ─── Startup Warm-Up ───────────────────────────────────────────────────────────
# On startup, run this many FULL scan cycles in "analysis only" mode: every
# symbol is scanned and signals are still detected/logged, but NO trades are
# placed. Set to 0 to disable and trade from the very first scan.
WARMUP_SCANS = 1

# ─── Kite API Retry (timeouts / transient errors) ──────────────────────────────
KITE_RETRY_ATTEMPTS = 5     # total attempts per call, including the first
KITE_RETRY_DELAY_SEC = 3    # base pause between retries (grows a little each attempt)

# ─── Live Order Confirmation ───────────────────────────────────────────────────
# Polls order_history() more frequently with a shorter gap so a fill is
# recognized (and the position/SL placed) as fast as possible after Kite
# accepts the order — total worst-case wait is similar to before, but checks
# happen roughly 4x more often, so the common case (fills in 1-2 checks) is
# much quicker.
ORDER_CONFIRM_ATTEMPTS = 12     # how many times to poll order_history() for COMPLETE
ORDER_CONFIRM_DELAY_SEC = 0.4   # pause between polls
USE_BROKER_SL = True            # place a real SL-M order at the broker for every live entry
BROKER_SL_TICK_SIZE = 0.05      # NSE equity tick size, used to round SL trigger prices

# ─── Position Reconciliation (bot state <-> broker truth) ────────────────────
# LIVE trading NEVER relies solely on in-memory state. This loop periodically
# pulls the REAL positions from Zerodha (kite.positions()) and reconciles the
# bot's state to match: a manually-closed position is dropped from tracking,
# a quantity mismatch (partial manual exit/addition) is synced, and any
# broker position the bot isn't tracking at all is flagged (never auto-
# adopted — you close/manage that one yourself in Kite).
RECONCILE_INTERVAL_SEC = 45

# ─── Paper-Trading Virtual Capital ────────────────────────────────────────────
# Paper trading needs SOME capital pool to size trades against. Your real
# Zerodha account's actual cash balance (kite.margins()) can show ₹0 if the
# account isn't funded for this segment yet, funds are parked elsewhere, or
# you're just using the account for read-only market data while testing —
# that's a real fact about the account, not something the bot should treat
# as "every trade sizes to ₹0."
#
# So in PAPER MODE ONLY, the bot uses this notional starting capital instead
# of the real account balance, and tracks it moving up/down with paper P&L
# exactly like a real account would (wins add to it, losses subtract). Real
# per-stock leverage (via kite.order_margins()) is STILL used on top of this
# pool for realistic sizing — only the total capital figure is virtual here.
# LIVE mode is completely unaffected — it always uses your real
# kite.margins() balance, never this figure.
PAPER_VIRTUAL_CAPITAL = 500000.0

# ─── Dashboard ────────────────────────────────────────────────────────────────
# Set False to skip starting the Flask web dashboard entirely (e.g. once
# you're relying on Telegram for updates instead).
DASHBOARD_ENABLED = True
DASHBOARD_HOST = "0.0.0.0"   # 0.0.0.0 = reachable from other devices (needed for Tailscale/ngrok/LAN access)
DASHBOARD_PORT = 5050

# Basic-auth login for the mobile dashboard page. CHANGE THESE before exposing
# the dashboard publicly (e.g. via ngrok) — anyone with the URL who doesn't
# know this username/password will just see a login prompt instead of your
# trades/capital/P&L.
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "Vami")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "Vami2312")

# ─── Telegram Notifications ───────────────────────────────────────────────────
# Get trade/status updates on your phone via Telegram, independent of the
# web dashboard. Setup:
#   1. Message @BotFather on Telegram, /newbot, copy the token it gives you.
#   2. Send your new bot any message (so it can see your chat).
#   3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and
#      copy the "chat":{"id": ...} number — that's your TELEGRAM_CHAT_ID.
#   4. Put both in your .env as TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Which events push a message. Turn any of these off if a channel gets noisy.
TELEGRAM_NOTIFY_ENTRIES     = True   # every trade entry (paper + live)
TELEGRAM_NOTIFY_EXITS       = True   # every trade exit (SL/trail/time/EOD/manual)
TELEGRAM_NOTIFY_RECONCILE   = True   # manual close / qty mismatch / untracked broker position
TELEGRAM_NOTIFY_ERRORS      = True   # connection failures, rejected orders, broker-SL failures
TELEGRAM_NOTIFY_STARTUP     = True   # bot startup summary
TELEGRAM_NOTIFY_EOD_SUMMARY = True   # one end-of-day P&L summary after square-off

# Don't let a slow/unreachable Telegram API call ever delay scanning, order
# placement, or the monitor loop — sends run in a background thread and give
# up after this many seconds.
TELEGRAM_REQUEST_TIMEOUT_SEC = 5