"""
WickFill Auto-Trader Bot v3 — Zerodha Kite
Strategy: EMA 200 Filter + Wick Zones + Zone Fills

*** PATCHED VERSION (round 3) ***
Changes vs your round-2 bot.py:
  1. NEW — Daily Per-Symbol SL-Hit Circuit Breaker: if any single symbol's
     stop-loss fires (SL_HIT or TRAIL_SL_HIT) MAX_SL_HITS_PER_DAY times in
     one calendar day, the bot stops taking NEW entries on that ONE symbol
     for the rest of today. Every other symbol keeps trading normally per
     the usual strategy rules — this is a per-symbol block, not a bot-wide
     pause. Any position already open when the limit is hit is unaffected
     and keeps being managed normally. The counter resets automatically at
     midnight IST AND on every bot restart (in-memory only, nothing
     persisted to disk), so a fresh bot start always begins today's count
     at 0 for every symbol.
        - New state key: "sl_hit_counts" (symbol -> {"date", "count"})
        - New helpers: record_sl_hit(), sl_hit_limit_reached()
        - Hooked into paper_exit() / live_exit() (counts the hit) and into
          scan_loop() (blocks new entries once the limit is reached this
          symbol/today), logged via log_skip() like every other rejection.
  2. Watchlist updated in config.py (POLYCAB, MUTHOOTFIN added to the
     500-1000 band; TRENT, PERSISTENT added to the 1000-2000 band) — no
     bot.py logic change needed for this, WATCHLIST/PRICE_BANDS are just
     read from config.py as before.

Everything else (round-2 fixes: request timeouts, deadlock fix, calc_qty
margin reuse, explicit skip logging, live_enter duplicate/band guard,
strategy, sizing %, trailing SL ladder, bands, timing windows, Telegram,
dashboard, reconciliation) is UNCHANGED from your round-2 file.
"""

import sys
import os
import time
import logging
import threading
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Make sure the bot's own folder is on sys.path so config.py is always found
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kiteconnect import KiteConnect
import pyotp
import requests

from execution import ExecutionEngine

# ── Import every config value explicitly (no wildcard) ────────────────────────
from config import (
    API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET,
    MANUAL_REQUEST_TOKEN, ACCESS_TOKEN,
    PAPER_TRADING,
    MARGIN_SAFETY_BUFFER_PCT, MAX_MARGIN_PER_TRADE_PCT,
    FALLBACK_CAPITAL_IF_MARGIN_FETCH_FAILS, MARGIN_REFRESH_INTERVAL_SEC,
    ORDER_MARGIN_SHRINK_STEP_PCT, ORDER_MARGIN_MAX_SHRINK_ATTEMPTS,
    MAX_TRADE_LEVERAGE_MULTIPLIER,
    RISK_PER_TRADE_PCT, POSITION_QTY_MULTIPLIER,
    QTY_MODE, QTY_FIXED_SIZE,
    EMA_LENGTH, MIN_WICK_PCT, RISK_REWARD, MAX_POSITIONS,
    WATCHLIST, PRICE_BANDS,
    SCAN_INTERVAL_SEC, MONITOR_INTERVAL_SEC,
    TRADING_START_TIME, NO_NEW_ENTRIES_AFTER, SQUARE_OFF_TIME, MAX_HOLD_MINUTES,
    COOLDOWN_MINUTES, WARMUP_SCANS,
    MAX_SL_HITS_PER_DAY,
    INITIAL_SL_R, TRAIL_STAGES,
    USE_EXCHANGE_SL, USE_KITETICKER, ENABLE_SLIPPAGE_MONITOR, ENABLE_LATENCY_LOG,
    MAX_SLIPPAGE_POINTS, MAX_SLIPPAGE_PERCENT,
    ORDER_CONFIRM_ATTEMPTS, ORDER_CONFIRM_DELAY_SEC, USE_BROKER_SL, BROKER_SL_TICK_SIZE,
    KITE_RETRY_ATTEMPTS, KITE_RETRY_DELAY_SEC,
    RECONCILE_INTERVAL_SEC,
    PAPER_VIRTUAL_CAPITAL, DASHBOARD_ENABLED,
    TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_NOTIFY_ENTRIES, TELEGRAM_NOTIFY_EXITS, TELEGRAM_NOTIFY_RECONCILE,
    TELEGRAM_NOTIFY_ERRORS, TELEGRAM_NOTIFY_STARTUP, TELEGRAM_NOTIFY_EOD_SUMMARY,
    TELEGRAM_REQUEST_TIMEOUT_SEC,
    DASHBOARD_HOST, DASHBOARD_PORT,
    TREND_FILTER_ENABLED, TREND_MIN_SCORE_TRENDING, TREND_MIN_SCORE_NEUTRAL,
    ALLOW_NEUTRAL_TRADES,
)
from reporting import save_trade_report
from stock_trend_detector import StockTrendDetector, TrendResult

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Execution Engine ──────────────────────────────────────────────────────────
# Owns the KiteTicker WebSocket (tick stream + order-update push), per-symbol
# locks, exit guards, and latency/slippage bookkeeping. See execution.py for
# the full explanation of why this replaces REST polling for stop-loss exits.
engine = ExecutionEngine()

trend_detector = StockTrendDetector()

# ── Hard network timeout for every Kite HTTP call ────────────────────────────
# pykiteconnect does NOT set a default request timeout. Without this, a
# stalled connection to Zerodha's servers just hangs the calling thread
# forever — kite_call_with_retry() only catches Timeout/ConnectionError/
# ReadTimeout, and none of those fire if the socket never times out in the
# first place.
KITE_REQUEST_TIMEOUT_SEC = 10

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "positions":        {},
    "trades":           [],
    "zones":            {},
    "scan_status":       "IDLE",
    "connected":         False,
    "last_scan":         None,
    "equity":            0.0,   # no more hardcoded STARTING_CAPITAL — set from live margin on first fetch
    "deployed":          0.0,
    "pnl_today":         0.0,
    "wins":              0,
    "losses":            0,
    "kite":              None,
    "scan_count":        0,     # incremented once per full scan cycle (used for warm-up)
    "cooldowns":         {},    # symbol -> datetime of last exit (used for cooldown gate)
    "available_margin":  0.0,  # live, from kite.margins() — refreshed every MARGIN_REFRESH_INTERVAL_SEC
    "margin_last_fetch":  None,
    "margin_source":     "unknown",   # "kite" or "fallback"
    "equity_initialized": False,
    "eod_summary_date_sent": None,    # date string ("YYYY-MM-DD") once today's EOD Telegram summary is sent
    # NEW: symbol -> {"date": "YYYY-MM-DD", "count": int} — per-symbol daily
    # SL/TRAIL_SL hit counter for the MAX_SL_HITS_PER_DAY circuit breaker.
    # In-memory only (not persisted), so a bot restart always starts fresh.
    "sl_hit_counts":      {},
    # NEW: trend filter tracking
    "trend_scores":       {},    # symbol -> {"score": int, "state": str, "reasons": list}
    "trend_signals_detected": {},  # symbol -> int (signals found by check_signal)
    "trend_skips":        {},    # symbol -> int (times skipped due to trend filter)
}

_state_lock = threading.Lock()


def safe_state_update(updates: dict):
    with _state_lock:
        state.update(updates)


# ── Centralized "trade skipped/rejected before entry" logger ─────────────────
# Every single place a detected signal fails to become a position — paper or
# live, cooldown, duplicate, band cap, qty=0, margin, order-margin failure,
# warm-up, trading window, daily SL-hit limit, whatever — must call this
# instead of a bare `return`/`continue`. No signal is allowed to disappear
# without one line explaining exactly why.
def log_skip(symbol: str, reason: str):
    log.info(f"❌ Skipping {symbol}: {reason}")


def _fmt_remaining(minutes: float) -> str:
    """Format a remaining-time float (in minutes) as 'Xm Ys' for skip logs."""
    total_seconds = max(0, int(round(minutes * 60)))
    m, s = divmod(total_seconds, 60)
    return f"{m}m {s}s"


# ── Telegram Notifications ────────────────────────────────────────────────────
def send_telegram(message: str, silent: bool = False):
    """
    Push a message to Telegram. Fire-and-forget on a background thread so a
    slow or unreachable Telegram API call NEVER delays scanning, order
    placement, or the position-monitor loop — trading logic never waits on
    this. No-ops quietly if TELEGRAM_ENABLED is False.
    """
    if not TELEGRAM_ENABLED:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ TELEGRAM_ENABLED is True but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing — skipping notification.")
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_notification": silent,
                },
                timeout=TELEGRAM_REQUEST_TIMEOUT_SEC,
            )
        except Exception as e:
            log.warning(f"⚠️ Telegram send failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ── Zerodha Kite Login ────────────────────────────────────────────────────────
def connect_kite() -> bool:
    if not API_KEY or not API_SECRET:
        log.error(
            "Missing Kite credentials: API_KEY and API_SECRET are required. "
            "Set them in .env before running the bot."
        )
        return False

    try:
        # timeout=KITE_REQUEST_TIMEOUT_SEC — every kite.* call this object
        # makes will now raise instead of hanging indefinitely.
        kite = KiteConnect(api_key=API_KEY, timeout=KITE_REQUEST_TIMEOUT_SEC)

        # Option 1: direct access token (fastest)
        if ACCESS_TOKEN:
            log.info("Using existing ACCESS_TOKEN from config")
            kite.set_access_token(ACCESS_TOKEN)
            safe_state_update({"kite": kite, "connected": True})
            log.info("✅ Connected via ACCESS_TOKEN")
            return True

        missing = []
        for name, value in (
            ("USER_ID", USER_ID),
            ("PASSWORD", PASSWORD),
            ("TOTP_SECRET", TOTP_SECRET),
        ):
            if not value:
                missing.append(name)

        if missing and not MANUAL_REQUEST_TOKEN:
            log.error(
                "Missing Kite credentials: %s. "
                "Set them in .env or use MANUAL_REQUEST_TOKEN for login.",
                ", ".join(missing),
            )
            return False

        # Option 2: manual request_token from browser redirect
        if MANUAL_REQUEST_TOKEN:
            log.info("Using MANUAL_REQUEST_TOKEN…")
            data = kite.generate_session(MANUAL_REQUEST_TOKEN, api_secret=API_SECRET)
            kite.set_access_token(data["access_token"])
            safe_state_update({"kite": kite, "connected": True})
            log.info("✅ Connected via manual request_token")
            return True

        # Option 3: auto-login via Kite web session
        session = requests.Session()
        totp_val = pyotp.TOTP(TOTP_SECRET).now()

        r = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": USER_ID, "password": PASSWORD},
            timeout=KITE_REQUEST_TIMEOUT_SEC,
        )
        log.debug("Login response status: %s", r.status_code)
        log.debug("Login response body: %s", r.text[:400])
        r.raise_for_status()
        login_body = r.json()
        if not login_body or "data" not in login_body or "request_id" not in login_body["data"]:
            log.error("Kite login response missing request_id: %s", login_body)
            return False
        request_id = login_body["data"]["request_id"]

        r = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": USER_ID,
                "request_id": request_id,
                "twofa_value": totp_val,
                "twofa_type": "totp",
            },
            timeout=KITE_REQUEST_TIMEOUT_SEC,
        )
        log.debug("2FA response status: %s", r.status_code)
        log.debug("2FA response body: %s", r.text[:400])
        r.raise_for_status()

        login_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
        r = session.get(login_url, allow_redirects=True, timeout=KITE_REQUEST_TIMEOUT_SEC)
        log.debug("Connect login redirect URL: %s", r.url)

        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(r.url).query)
        if "request_token" not in params:
            log.error("Auto-login failed — could not get request_token from redirect.")
            log.error("Redirect was: %s", r.url)
            log.error("👉 Paste request_token manually into MANUAL_REQUEST_TOKEN in config.py")
            return False

        request_token = params["request_token"][0]
        log.info("Got request_token: %s…", request_token[:8])

        data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(data["access_token"])

        safe_state_update({"kite": kite, "connected": True})
        log.info("✅ Connected to Zerodha Kite (auto-login)")
        return True

    except Exception as e:
        log.error("Kite connection error: %s", e)
        log.error(traceback.format_exc())
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram(f"🚫 Kite connection error: {e}")
        return False


# ── Kite API retry wrapper (handles timeouts / transient network errors) ──────
def kite_call_with_retry(fn, *args, what: str = "", attempts: int = None, delay: float = None, **kwargs):
    """
    Call a Kite API function, retrying a few times on timeout / transient
    connection errors instead of giving up on the first failure. Returns
    the function's result, or None if every attempt fails. Also logs how
    long each attempt took, so a slow-but-not-quite-timed-out Kite call is
    visible in the logs rather than silently eating several seconds per
    symbol.
    """
    attempts = attempts or KITE_RETRY_ATTEMPTS
    delay = delay if delay is not None else KITE_RETRY_DELAY_SEC

    last_err = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            elapsed = time.monotonic() - started
            if elapsed > 3:
                log.info(f"⏱️ Kite call {what or fn.__name__} took {elapsed:.1f}s")
            return result
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_err = e
            elapsed = time.monotonic() - started
            if attempt < attempts:
                wait = delay * attempt
                log.warning(
                    f"⏱️ Kite API timeout{' (' + what + ')' if what else ''} after {elapsed:.1f}s "
                    f"[attempt {attempt}/{attempts}] — retrying in {wait:.0f}s: {e}"
                )
                time.sleep(wait)
            else:
                log.warning(
                    f"⏱️ Kite API timeout{' (' + what + ')' if what else ''} — "
                    f"gave up after {attempts} attempts: {e}"
                )
        except Exception as e:
            last_err = e
            break
    if last_err:
        raise last_err
    return None


# ── Market Hours ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    o = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    c = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return o <= now <= c


def minutes_to_open() -> int:
    now = datetime.now(IST)
    o   = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return max(0, int((o - now).total_seconds() / 60))


# ── Trading Time Windows (new entries cutoff + forced square-off) ────────────
def _time_today(hhmm: str):
    now = datetime.now(IST)
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def can_take_new_entries() -> bool:
    now = datetime.now(IST)
    if now < _time_today(TRADING_START_TIME):
        return False
    return now < _time_today(NO_NEW_ENTRIES_AFTER)


def should_force_square_off() -> bool:
    return datetime.now(IST) >= _time_today(SQUARE_OFF_TIME)


def minutes_held(pos: dict) -> float:
    try:
        opened = datetime.fromisoformat(pos["open_time"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=IST)
        return (datetime.now(IST) - opened).total_seconds() / 60.0
    except Exception:
        return 0.0


# ── Cooldown After Exit ───────────────────────────────────────────────────────
def record_cooldown(symbol: str):
    if COOLDOWN_MINUTES <= 0:
        return
    with _state_lock:
        state["cooldowns"][symbol] = datetime.now(IST)


def cooldown_remaining_min(symbol: str) -> float:
    if COOLDOWN_MINUTES <= 0:
        return 0.0
    with _state_lock:
        last_exit = state["cooldowns"].get(symbol)
    if not last_exit:
        return 0.0
    elapsed = (datetime.now(IST) - last_exit).total_seconds() / 60.0
    remaining = COOLDOWN_MINUTES - elapsed
    return max(0.0, remaining)


def in_cooldown(symbol: str) -> bool:
    return cooldown_remaining_min(symbol) > 0.0


# ── Daily Per-Symbol SL-Hit Circuit Breaker ───────────────────────────────────
# If ANY symbol's stop-loss fires (SL_HIT or TRAIL_SL_HIT) MAX_SL_HITS_PER_DAY
# times in one calendar day, that ONE symbol is blocked from new entries for
# the rest of today. Every other symbol keeps trading normally. Resets
# automatically at midnight IST (a new date means a fresh count) and also on
# every bot restart, since this lives only in the in-memory `state` dict and
# is never written to disk — exactly the "fresh start tomorrow / on restart"
# behavior requested.
def record_sl_hit(symbol: str):
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        entry = state["sl_hit_counts"].get(symbol)
        if not entry or entry.get("date") != today_str:
            entry = {"date": today_str, "count": 0}
        entry["count"] += 1
        state["sl_hit_counts"][symbol] = entry
        count = entry["count"]

    log.info(f"🔢 {symbol}: SL-hit count today = {count}/{MAX_SL_HITS_PER_DAY}")
    if count >= MAX_SL_HITS_PER_DAY:
        log.warning(
            f"🚫 {symbol}: hit {count}/{MAX_SL_HITS_PER_DAY} stop-loss exits today — "
            f"blocked from new entries for the rest of today. Other symbols are unaffected."
        )
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram(
                f"🚫 {symbol}: hit {count} SL/trail-SL exits today — "
                f"no more new entries on this symbol until tomorrow's fresh session."
            )


def sl_hit_limit_reached(symbol: str) -> bool:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        entry = state["sl_hit_counts"].get(symbol)
        if not entry or entry.get("date") != today_str:
            return False
        return entry["count"] >= MAX_SL_HITS_PER_DAY


def sl_hit_count_today(symbol: str) -> int:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        entry = state["sl_hit_counts"].get(symbol)
        if not entry or entry.get("date") != today_str:
            return 0
        return entry["count"]


# ── Trend Filter Tracking ─────────────────────────────────────────────────────
def record_trend_score(symbol: str, result: TrendResult):
    with _state_lock:
        state["trend_scores"][symbol] = {
            "score": result.score,
            "state": result.state,
            "reasons": result.reasons,
            "details": result.details,
        }


def record_trend_signal(symbol: str):
    with _state_lock:
        state["trend_signals_detected"][symbol] = state["trend_signals_detected"].get(symbol, 0) + 1


def record_trend_skip(symbol: str):
    with _state_lock:
        state["trend_skips"][symbol] = state["trend_skips"].get(symbol, 0) + 1


# ── Price Bands (value-based position caps) ───────────────────────────────────
def band_for_symbol(symbol: str) -> dict | None:
    for band in PRICE_BANDS:
        if symbol in band["symbols"]:
            return band
    return None


def band_open_positions_count(band_name: str) -> int:
    with _state_lock:
        return sum(1 for p in state["positions"].values() if p.get("band") == band_name)


def band_capacity_available(symbol: str) -> bool:
    band = band_for_symbol(symbol)
    if not band:
        return True
    return band_open_positions_count(band["name"]) < band["max_positions"]


# ── Instrument Token Cache ────────────────────────────────────────────────────
_instrument_cache: dict[str, int] = {}


def load_instruments():
    global _instrument_cache
    kite = state["kite"]
    if not kite or _instrument_cache:
        return
    try:
        instruments = kite_call_with_retry(kite.instruments, "NSE", what="instruments")
        for inst in instruments:
            _instrument_cache[inst["tradingsymbol"]] = inst["instrument_token"]
        log.info(f"✅ Loaded {len(_instrument_cache)} NSE instruments")
    except Exception as e:
        log.error(f"Instrument load error: {e}")


def get_instrument_token(symbol: str) -> int | None:
    if not _instrument_cache:
        load_instruments()
    return _instrument_cache.get(symbol)


_engine_started = False


def start_execution_engine_if_needed():
    """
    Starts the KiteTicker WebSocket exactly once, right after instruments are
    loaded (so the symbol->token map is available), and subscribes to the
    full WATCHLIST — this covers every symbol the scan loop can ever open a
    position on, with no per-symbol special-casing required.
    """
    global _engine_started
    if not USE_KITETICKER or _engine_started:
        return
    kite = state["kite"]
    if kite is None or not _instrument_cache:
        return
    token_map = {s: _instrument_cache[s] for s in WATCHLIST if s in _instrument_cache}
    engine.set_symbol_token_map(token_map)
    engine.on_ticks_callback = _handle_ticks
    engine.on_order_update_callback = _handle_order_update
    access_token = getattr(kite, "access_token", None)
    if not access_token:
        log.warning("⚠️ Cannot start KiteTicker — no access_token found on the Kite session.")
        return
    engine.start(API_KEY, access_token)
    # Give the socket a moment to connect before subscribing.
    time.sleep(1.0)
    engine.subscribe_symbols(list(token_map.keys()))
    _engine_started = True


# ── Live Account Margin (replaces all hardcoded capital/leverage) ────────────
def fetch_margins() -> float:
    """
    Returns the "usable" capital to size trades against (after
    MARGIN_SAFETY_BUFFER_PCT is set aside as reserve). Cached for
    MARGIN_REFRESH_INTERVAL_SEC.

    - LIVE mode: always the REAL available margin from kite.margins()
      (equity segment) — a read-only call, never places an order.
    - PAPER mode: a notional capital pool (PAPER_VIRTUAL_CAPITAL) that moves
      up/down with paper P&L exactly like a real account would, since the
      connected account's real cash balance may legitimately be ₹0 (unfunded
      for this segment, funds parked elsewhere, used only for market data)
      — that shouldn't mean every paper trade sizes to zero. Real per-stock
      leverage (via order_margins()) is still applied on top of this pool
      for realistic sizing; only the total capital figure is virtual here.
    """
    now = datetime.now(IST)

    with _state_lock:
        last_fetch = state.get("margin_last_fetch")
        cached = state.get("available_margin", 0.0)

    if last_fetch and (now - last_fetch).total_seconds() < MARGIN_REFRESH_INTERVAL_SEC:
        return cached

    if PAPER_TRADING:
        with _state_lock:
            if not state.get("equity_initialized"):
                state["equity"] = PAPER_VIRTUAL_CAPITAL
                state["equity_initialized"] = True
            virtual_capital = state["equity"]  # grows/shrinks with paper wins/losses

        usable = virtual_capital * (1 - MARGIN_SAFETY_BUFFER_PCT / 100)
        safe_state_update({
            "available_margin": round(usable, 2),
            "margin_last_fetch": now,
            "margin_source": "paper_virtual",
        })
        return usable

    kite = state["kite"]
    if kite is None:
        log.warning("⚠️ Margin fetch skipped — not connected to Kite yet. Using cached/fallback value.")
        return cached or FALLBACK_CAPITAL_IF_MARGIN_FETCH_FAILS

    try:
        margins = kite_call_with_retry(kite.margins, "equity", what="margins")
        if not margins:
            raise ValueError("margins() returned empty response")
        available = margins.get("available", {})
        live_balance = available.get("live_balance")
        if live_balance is None:
            live_balance = available.get("cash", 0.0)
        live_balance = float(live_balance)

        usable = live_balance * (1 - MARGIN_SAFETY_BUFFER_PCT / 100)

        if not state.get("equity_initialized"):
            safe_state_update({"equity": live_balance, "equity_initialized": True})

        safe_state_update({
            "available_margin": round(usable, 2),
            "margin_last_fetch": now,
            "margin_source": "kite",
        })
        log.info(
            f"💰 Margin refreshed from Kite: live balance ₹{live_balance:,.2f} → "
            f"usable ₹{usable:,.2f} (after {MARGIN_SAFETY_BUFFER_PCT}% safety buffer)"
        )
        return usable
    except Exception as e:
        log.warning(f"⚠️ Margin fetch failed ({e}) — using last cached/fallback value.")
        safe_state_update({"margin_source": "fallback"})
        return cached or FALLBACK_CAPITAL_IF_MARGIN_FETCH_FAILS


def max_capital_per_trade() -> float:
    """The slice of the current live available margin any ONE trade may use."""
    available = fetch_margins()
    return available * (MAX_MARGIN_PER_TRADE_PCT / 100)


def get_available_capital() -> float:
    """How much of the LIVE available margin is still free (not yet deployed)."""
    total_margin = fetch_margins()
    with _state_lock:
        return total_margin - state["deployed"]


def is_within_investment_limit(
    symbol: str, direction: str, entry: float, qty: int,
    known_margin: float | None = None,
) -> tuple[bool, float]:
    """
    Check a prospective trade against available capital using its REAL
    required MARGIN (via order_margins()) — NOT the full notional trade
    value (entry x qty). With real MIS leverage, required margin is only a
    fraction of notional exposure; comparing notional exposure to available
    capital would reject almost every leveraged trade even when it easily
    fits. Falls back to the full notional value (conservative) only if the
    margin lookup itself fails. Returns (allowed, required_margin_used).

    Accepts known_margin — if calc_qty() already computed the real required
    margin for this exact symbol/qty, pass it in here instead of hitting
    order_margins() over the network a second time.
    """
    total_capital = fetch_margins()
    if known_margin is not None and known_margin > 0:
        required = known_margin
    else:
        required = _order_margin_required(symbol, direction, qty)
        if required is None or required <= 0:
            required = entry * qty  # couldn't verify — fall back to conservative no-leverage figure
    if required > total_capital:
        return False, required
    if required > get_available_capital():
        return False, required
    return True, required


# ── Live Market Price (LTP) ───────────────────────────────────────────────────
def get_ltp(symbol: str) -> float | None:
    kite = state["kite"]
    if kite is None:
        return None
    try:
        key = f"NSE:{symbol}"
        quote = kite_call_with_retry(kite.ltp, key, what=f"LTP {symbol}")
        return float(quote[key]["last_price"])
    except Exception as e:
        log.warning(f"LTP fetch error {symbol}: {e}")
        return None


def get_ltp_batch(symbols: list[str]) -> dict[str, float]:
    kite = state["kite"]
    if kite is None or not symbols:
        return {}
    try:
        keys = [f"NSE:{s}" for s in symbols]
        quotes = kite_call_with_retry(kite.ltp, keys, what="LTP batch")
        return {
            s: float(quotes[f"NSE:{s}"]["last_price"])
            for s in symbols
            if quotes and f"NSE:{s}" in quotes
        }
    except Exception as e:
        log.warning(f"Batch LTP fetch error: {e}")
        return {}


# ── Candle Fetcher ────────────────────────────────────────────────────────────
def get_candles(symbol: str, n: int = 260) -> list[dict] | None:
    kite = state["kite"]
    if kite is None:
        return None
    token = get_instrument_token(symbol)
    if not token:
        return None
    try:
        to_dt   = datetime.now(IST)
        from_dt = to_dt - timedelta(days=10)
        raw = kite_call_with_retry(
            kite.historical_data,
            instrument_token=token,
            from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            interval="5minute",
            continuous=False,
            oi=False,
            what=f"candles {symbol}",
        )
        candles = []
        for r in raw[-n:]:
            candles.append({
                "time":   r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": float(r["volume"]),
            })
        return candles
    except Exception as e:
        log.warning(f"Candle fetch error {symbol}: {e}")
        return None


# ── EMA ───────────────────────────────────────────────────────────────────────
def calc_ema(closes: list[float], period: int) -> list[float | None]:
    if len(closes) < period:
        return [None] * len(closes)
    k   = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema


# ── Wick Zone Detection ───────────────────────────────────────────────────────
def detect_wick_zones(candles: list[dict]) -> list[dict]:
    zones = []
    for i, c in enumerate(candles):
        body_top = max(c["open"], c["close"])
        body_bot = min(c["open"], c["close"])
        c_range  = c["high"] - c["low"]
        if c_range == 0:
            continue
        upper_pct = (c["high"] - body_top) / c_range * 100
        lower_pct = (body_bot  - c["low"])  / c_range * 100

        if upper_pct >= MIN_WICK_PCT:
            zones.append({
                "type": "BEAR", "top": c["high"], "bottom": body_top,
                "index": i, "time": c["time"], "filled": False,
            })
        if lower_pct >= MIN_WICK_PCT:
            zones.append({
                "type": "BULL", "top": body_bot, "bottom": c["low"],
                "index": i, "time": c["time"], "filled": False,
            })
    return zones


# ── Strategy Signal ───────────────────────────────────────────────────────────
def check_signal(symbol: str, candles: list[dict]) -> dict | None:
    needed = EMA_LENGTH + 10
    if len(candles) < needed:
        log.debug(f"{symbol}: only {len(candles)} candles, need {needed}")
        return None

    closes   = [c["close"] for c in candles]
    ema_vals = calc_ema(closes, EMA_LENGTH)
    ema_now  = ema_vals[-1]

    if ema_now is None:
        return None

    current_price = closes[-1]
    bias = "BUY" if current_price > ema_now else "SELL"

    zones       = detect_wick_zones(candles[:-5])
    last_candle = candles[-1]

    for zone in reversed(zones[-30:]):
        if zone["filled"]:
            continue

        if zone["type"] == "BULL" and bias == "BUY":
            if zone["bottom"] <= last_candle["low"] <= zone["top"]:
                entry = last_candle["close"]
                sl    = zone["bottom"]
                risk  = entry - sl
                if risk <= 0 or risk / entry > 0.03:
                    continue
                return {
                    "symbol": symbol, "direction": "BUY",
                    "entry":  round(entry, 2),
                    "sl":     round(sl, 2),
                    "tp":     round(entry + risk * RISK_REWARD, 2),
                    "risk":   round(risk, 2),
                    "ema":    round(ema_now, 2),
                    "zone":   zone,
                    "time":   datetime.now(IST).isoformat(),
                }

        elif zone["type"] == "BEAR" and bias == "SELL":
            if zone["bottom"] <= last_candle["high"] <= zone["top"]:
                entry = last_candle["close"]
                sl    = zone["top"]
                risk  = sl - entry
                if risk <= 0 or risk / entry > 0.03:
                    continue
                return {
                    "symbol": symbol, "direction": "SELL",
                    "entry":  round(entry, 2),
                    "sl":     round(sl, 2),
                    "tp":     round(entry - risk * RISK_REWARD, 2),
                    "risk":   round(risk, 2),
                    "ema":    round(ema_now, 2),
                    "zone":   zone,
                    "time":   datetime.now(IST).isoformat(),
                }
    return None


# ── Trailing Stop Ladder ──────────────────────────────────────────────────────
def compute_trail_sl_r(current_r: float) -> float | None:
    if not TRAIL_STAGES or current_r < TRAIL_STAGES[0]["trigger_r"]:
        return None

    stages = list(TRAIL_STAGES)
    if len(stages) >= 2:
        trig_gap = stages[-1]["trigger_r"] - stages[-2]["trigger_r"]
        sl_gap   = stages[-1]["sl_r"]      - stages[-2]["sl_r"]
    else:
        trig_gap, sl_gap = 0.5, 0.4

    while trig_gap > 0 and current_r >= stages[-1]["trigger_r"] + trig_gap:
        stages.append({
            "trigger_r": round(stages[-1]["trigger_r"] + trig_gap, 4),
            "sl_r":      round(stages[-1]["sl_r"] + sl_gap, 4),
        })

    applicable = [s for s in stages if current_r >= s["trigger_r"]]
    return applicable[-1]["sl_r"] if applicable else None


# ── Position Sizing (LIVE margin based — no more fixed leverage) ─────────────
def _order_margin_required(symbol: str, direction: str, qty: int) -> float | None:
    """
    Ask Kite's REAL margin calculator (order_margins()) what a MIS order of
    this quantity would actually require. This is a READ-ONLY call — no order
    is placed — so it's safe to call in paper mode too, which is exactly what
    lets paper-mode sizing match live-mode sizing (same real per-stock
    leverage figures either way). Returns None if the lookup fails.
    """
    kite = state["kite"]
    if kite is None or qty <= 0:
        return None
    try:
        txn = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_param = [{
            "exchange":         kite.EXCHANGE_NSE,
            "tradingsymbol":    symbol,
            "transaction_type": txn,
            "variety":          kite.VARIETY_REGULAR,
            "product":          kite.PRODUCT_MIS,
            "order_type":       kite.ORDER_TYPE_MARKET,
            "quantity":         qty,
            "price":            0,
        }]
        result = kite_call_with_retry(kite.order_margins, order_param, what=f"order_margins {symbol}")
        return float(result[0]["total"]) if result else None
    except Exception as e:
        log.warning(f"order_margins lookup failed {symbol} qty={qty}: {e}")
        return None


def calc_qty(symbol: str, direction: str, entry: float, sl: float) -> tuple[int, float]:
    """
    Return (quantity, margin_used) based on the configured sizing mode. ALL
    modes (except "fixed") size off the LIVE available margin fetched from
    Kite via fetch_margins()/max_capital_per_trade() — there is no fixed
    CAPITAL_PER_TRADE / MIS_LEVERAGE constant anywhere in this calculation.

    "capital" mode goes a step further: it doesn't just divide the cash slice
    by the raw entry price (which implicitly assumes NO leverage) — it asks
    Kite's real order_margins() what per-stock MIS leverage actually applies,
    then scales the quantity UP to what that cash slice can really support.
    A MAX_TRADE_LEVERAGE_MULTIPLIER safety cap still bounds the result so an
    unusually high per-stock leverage figure can't balloon position size.

    Returns margin_used alongside qty, so callers (paper_enter/live_enter)
    reuse the already-computed real margin instead of calling
    order_margins() a second/third time over the network for the same
    symbol+qty.
    """
    if entry <= 0:
        return 0, 0.0

    if QTY_MODE == "capital":
        per_trade_capital = max_capital_per_trade()
        if per_trade_capital <= 0:
            return 0, 0.0

        naive_qty = max(1, int(per_trade_capital / entry))

        required = _order_margin_required(symbol, direction, naive_qty)
        if required is None or required <= 0:
            # Couldn't verify real leverage (no Kite session, API hiccup) —
            # fall back to the conservative no-leverage figure. This is a
            # FALLBACK, not a rejection, but it must still be visible in the
            # log since it silently changes sizing behavior for this trade.
            log.warning(
                f"⚠️ {symbol}: order margin verification failed (order_margins() "
                f"returned no usable figure) — proceeding with conservative "
                f"no-leverage qty {naive_qty} instead of real-leverage sizing."
            )
            return naive_qty, entry * naive_qty

        margin_per_share = required / naive_qty
        if margin_per_share <= 0:
            return naive_qty, required

        scaled_qty = max(1, int(per_trade_capital / margin_per_share))

        # Safety cap: exposure (qty x entry) may never exceed
        # MAX_TRADE_LEVERAGE_MULTIPLIER x the intended cash slice.
        max_qty_by_exposure_cap = max(1, int((per_trade_capital * MAX_TRADE_LEVERAGE_MULTIPLIER) / entry))
        final_qty = min(scaled_qty, max_qty_by_exposure_cap)

        # Margin isn't always perfectly linear across quantity brackets —
        # re-verify the scaled figure actually fits the cash slice and trim
        # if it doesn't.
        final_margin = required
        recheck = _order_margin_required(symbol, direction, final_qty)
        if recheck is not None:
            final_margin = recheck
            if recheck > per_trade_capital:
                ratio = per_trade_capital / recheck
                final_qty = max(1, int(final_qty * ratio))
                reverify = _order_margin_required(symbol, direction, final_qty)
                if reverify is not None:
                    final_margin = reverify

        if final_qty != naive_qty:
            log.info(
                f"📐 {symbol}: real-leverage sizing — naive (no-leverage) qty {naive_qty} "
                f"(₹{required:,.2f} margin) → scaled qty {final_qty} using real per-share "
                f"margin ₹{margin_per_share:,.2f} (cash slice ₹{per_trade_capital:,.2f}, "
                f"exposure cap {MAX_TRADE_LEVERAGE_MULTIPLIER}x)"
            )
        return final_qty, final_margin

    risk_per_share = abs(entry - sl)
    if risk_per_share == 0:
        return 0, 0.0

    if QTY_MODE == "risk":
        per_trade_capital = max_capital_per_trade()
        risk_amount = per_trade_capital * RISK_PER_TRADE_PCT / 100
        base_qty = max(1, int(risk_amount / risk_per_share))
        qty = max(1, base_qty * POSITION_QTY_MULTIPLIER)
        return qty, entry * qty  # estimate; real check still happens in is_within_investment_limit

    qty = max(1, int(QTY_FIXED_SIZE))
    return qty, entry * qty


# ── Live Order-Margin Verification / Auto-Shrink ─────────────────────────────
def verify_and_shrink_order_qty(symbol: str, direction: str, qty: int, price: float) -> int:
    """
    Before placing a LIVE order, ask Kite's OWN margin calculator
    (kite.order_margins()) what this specific order will really require —
    Zerodha's MIS margin is NOT a flat multiplier, it varies per stock. If
    the required margin exceeds what's currently available, shrink the
    quantity by ORDER_MARGIN_SHRINK_STEP_PCT and recheck, instead of
    rejecting the signal outright. Returns the quantity that fits (possibly
    reduced), or 0 if even 1 share doesn't fit.
    """
    kite = state["kite"]
    if kite is None or qty <= 0:
        return qty

    txn = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
    available = get_available_capital()
    current_qty = qty

    for attempt in range(1, ORDER_MARGIN_MAX_SHRINK_ATTEMPTS + 1):
        if current_qty <= 0:
            return 0
        try:
            order_param = [{
                "exchange":         kite.EXCHANGE_NSE,
                "tradingsymbol":    symbol,
                "transaction_type": txn,
                "variety":          kite.VARIETY_REGULAR,
                "product":          kite.PRODUCT_MIS,
                "order_type":       kite.ORDER_TYPE_MARKET,
                "quantity":         current_qty,
                "price":            0,
            }]
            result = kite_call_with_retry(kite.order_margins, order_param, what=f"order_margins {symbol}")
            required = float(result[0]["total"]) if result else None
            if required is None:
                log.warning(f"⚠️ {symbol}: order_margins returned no figure — proceeding with unverified qty {current_qty}")
                return current_qty

            if required <= available:
                if current_qty != qty:
                    log.info(f"📉 {symbol}: qty shrunk {qty} → {current_qty} to fit real Kite margin (₹{required:,.2f} required, ₹{available:,.2f} available)")
                return current_qty

            shrunk = max(1, int(current_qty * (1 - ORDER_MARGIN_SHRINK_STEP_PCT / 100)))
            log.info(
                f"📉 {symbol}: order_margins needs ₹{required:,.2f} > available ₹{available:,.2f} "
                f"— shrinking qty {current_qty} → {shrunk} (attempt {attempt}/{ORDER_MARGIN_MAX_SHRINK_ATTEMPTS})"
            )
            if shrunk == current_qty:
                # already at the floor (1 share) and it still doesn't fit
                log_skip(
                    symbol,
                    f"Order margin verification failure — even 1 share requires more margin "
                    f"than available (₹{available:,.2f})"
                )
                return 0
            current_qty = shrunk
        except Exception as e:
            log.warning(f"order_margins check failed for {symbol}: {e} — proceeding with unverified qty {current_qty}")
            return current_qty

    log_skip(
        symbol,
        f"Order margin verification failure — margin still doesn't fit after "
        f"{ORDER_MARGIN_MAX_SHRINK_ATTEMPTS} shrink attempts"
    )
    return 0


# ── Paper Trading ─────────────────────────────────────────────────────────────
def _save_current_report():
    with _state_lock:
        save_trade_report(state)


def paper_enter(signal: dict):
    symbol = signal["symbol"]

    ltp = get_ltp(symbol)
    entry_price = ltp if ltp is not None else signal["entry"]
    direction = signal["direction"]
    qty, computed_margin = calc_qty(symbol, direction, entry_price, signal["sl"])

    if qty <= 0:
        log_skip(
            symbol,
            f"Quantity calculated as zero (entry ₹{entry_price:,.2f}) — usable margin "
            f"too small for this entry price, or real per-share margin too high"
        )
        return

    allowed, margin_used = is_within_investment_limit(
        symbol, direction, entry_price, qty, known_margin=computed_margin
    )
    if not allowed:
        log_skip(
            symbol,
            f"Insufficient usable margin — required ₹{margin_used:,.2f}, "
            f"available ₹{get_available_capital():,.2f}"
        )
        return

    risk = signal["risk"]
    if direction == "BUY":
        initial_sl = round(entry_price - INITIAL_SL_R * risk, 2)
    else:
        initial_sl = round(entry_price + INITIAL_SL_R * risk, 2)
    band = band_for_symbol(symbol)

    # NOTE: deadlock fix retained — get_available_capital() (which re-
    # acquires _state_lock internally) is called AFTER releasing the lock
    # below, never while it's held.
    skip_reason = None
    with _state_lock:
        if len(state["positions"]) >= MAX_POSITIONS:
            skip_reason = (
                f"Max positions cap reached ({len(state['positions'])}/{MAX_POSITIONS}) "
                f"— filled by another symbol between signal and entry"
            )
        elif symbol in state["positions"]:
            skip_reason = "Duplicate trade — a position on this symbol is already open"
        else:
            band_count = (
                sum(1 for p in state["positions"].values() if p.get("band") == band["name"])
                if band else 0
            )
            if band and band_count >= band["max_positions"]:
                skip_reason = f"Band ₹{band['name']} limit reached ({band_count}/{band['max_positions']})"
            else:
                state["positions"][symbol] = {
                    "symbol":     symbol,
                    "direction":  direction,
                    "entry":      round(entry_price, 2),
                    "risk":       risk,
                    "initial_sl": initial_sl,
                    "sl":         initial_sl,
                    "tp":         signal["tp"],
                    "band":       band["name"] if band else None,
                    "qty":        qty,
                    "margin_used": round(margin_used, 2),   # real margin consumed, NOT notional trade value
                    "open_time":  signal["time"],
                    "pnl":        0.0,
                    "mfe":        0.0,
                    "mae":        0.0,
                    "status":     "OPEN",
                    "trend_score": trend_detector.get_trend_score(symbol, candles) if TREND_FILTER_ENABLED else None,
                    "trend_state": trend_detector.get_market_state(symbol, candles) if TREND_FILTER_ENABLED else None,
                }
                state["deployed"] += margin_used
                deployed_after = state["deployed"]

    if skip_reason:
        log_skip(symbol, skip_reason)
        return

    available_after = get_available_capital()
    trade_value = entry_price * qty
    log.info(
        f"📈 PAPER ENTER {direction} {symbol} @ {entry_price} (market) | "
        f"1R={risk:.2f} | Initial SL {initial_sl} ({INITIAL_SL_R}R) | Ref target {signal['tp']} | Qty {qty}"
    )
    log.info(
        f"   💰 Sizing: cash slice ₹{max_capital_per_trade():,.2f} "
        f"({MAX_MARGIN_PER_TRADE_PCT}% of usable capital) | Entry ₹{entry_price:,.2f} | "
        f"Qty = {qty} shares | Notional value ₹{trade_value:,.2f} | Real margin used ₹{margin_used:,.2f} | "
        f"Deployed ₹{deployed_after:,.2f} | Available ₹{available_after:,.2f} | "
        f"Band {band['name'] if band else 'n/a'}"
    )
    if TELEGRAM_NOTIFY_ENTRIES:
        send_telegram(
            f"📝 PAPER ENTER\n"
            f"<b>{direction} {symbol}</b>\n"
            f"Qty {qty} @ ₹{entry_price}\n"
            f"SL ₹{initial_sl} ({INITIAL_SL_R}R) | Ref target ₹{signal['tp']}\n"
            f"1R = ₹{risk:.2f} | Value ₹{trade_value:,.2f}"
        )
    _save_current_report()


# ── Shared Exit Bookkeeping ────────────────────────────────────────────────
# Every exit path — paper tick-triggered SL/trail exit, paper TIME_EXIT/EOD,
# live market exit (TIME_EXIT/EOD/manual), live exchange-SL-fill (via
# order-update push), and live emergency exit — funnels through this ONE
# function for pnl/state/cooldown/SL-hit-counter/slippage/latency/Telegram/
# report bookkeeping, so that logic is never duplicated (and never drifts
# out of sync) across the different trigger paths.
#
# Callers are responsible for claiming/releasing engine.try_begin_exit() /
# engine.end_exit() around their WHOLE exit sequence (order placement +
# this call) — this function itself does not touch the guard, since some
# callers need the guard held across an earlier order-placement step too.
def _book_exit(symbol: str, exit_price: float, filled_qty: int | None, reason: str,
                is_live: bool, latency_key: str | None = None):
    with _state_lock:
        pos = state["positions"].get(symbol)
        if not pos:
            return None
        qty = filled_qty if filled_qty else pos["qty"]
        pnl = (
            (exit_price - pos["entry"]) * qty if pos["direction"] == "BUY"
            else (pos["entry"] - exit_price) * qty
        )
        result = "WIN" if pnl > 0 else "LOSS"
        state["wins" if result == "WIN" else "losses"] += 1
        state["pnl_today"] += pnl
        state["equity"] += pnl
        freed_capital = pos.get("margin_used", pos["entry"] * qty)
        state["deployed"] -= freed_capital
        deployed_after = state["deployed"]

        trade_record = {
            **pos, "exit": round(exit_price, 2), "pnl": round(pnl, 2),
            "result": result, "reason": reason,
            "close_time": datetime.now(IST).isoformat(),
            "mfe": round(pos.get("mfe", 0.0), 2),
            "mae": round(pos.get("mae", 0.0), 2),
            "holding_minutes": round(minutes_held(pos), 1),
            "trend_score": pos.get("trend_score"),
            "trend_state": pos.get("trend_state"),
        }

        # ── Slippage Logging ─────────────────────────────────────────────
        # Only meaningful for a stop-loss exit (original or trailed) — the
        # whole point is comparing what the CONFIGURED stop was against
        # where the exit actually happened.
        if ENABLE_SLIPPAGE_MONITOR and reason in ("SL_HIT", "TRAIL_SL_HIT"):
            configured_sl = pos["sl"]  # pos["sl"] always holds the currently active stop
            slip = engine.compute_slippage(pos["entry"], configured_sl, exit_price, qty, pos["direction"])
            trade_record.update(slip)
            if slip["slippage_points"] > MAX_SLIPPAGE_POINTS or slip["slippage_percent"] > MAX_SLIPPAGE_PERCENT:
                log.warning(
                    f"⚠️ HIGH SLIPPAGE {symbol}: configured SL {slip['configured_sl']} vs actual exit "
                    f"{slip['actual_exit']} — slippage {slip['slippage_points']} pts "
                    f"({slip['slippage_percent']}%) | expected loss ₹{slip['expected_loss']:,.2f} vs "
                    f"actual ₹{slip['actual_loss']:,.2f} ({slip['risk_multiple']}R)"
                )
                if TELEGRAM_NOTIFY_ERRORS:
                    send_telegram(
                        f"⚠️ HIGH SLIPPAGE {symbol}: SL {slip['configured_sl']} → exit {slip['actual_exit']} "
                        f"({slip['slippage_points']} pts, {slip['risk_multiple']}R vs expected loss)"
                    )

        # ── Latency Logging ──────────────────────────────────────────────
        if ENABLE_LATENCY_LOG and latency_key:
            engine.mark(latency_key, "exit_filled")
            trade_record["latency"] = engine.get_latency_summary(latency_key)
            engine.clear_latency(latency_key)

        state["trades"].insert(0, trade_record)
        del state["positions"][symbol]

    available_after = get_available_capital()
    record_cooldown(symbol)
    if reason in ("SL_HIT", "TRAIL_SL_HIT"):
        record_sl_hit(symbol)

    tag = "LIVE" if is_live else "PAPER"
    icon = "✅" if is_live else "📉"
    log.info(f"{icon} {tag} EXIT {symbol} @ {exit_price} | PnL ₹{pnl:.2f} | {reason}")
    log.info(f"   💰 Freed ₹{freed_capital:,.2f} | Deployed ₹{deployed_after:,.2f} | Available ₹{available_after:,.2f}")
    if TELEGRAM_NOTIFY_EXITS:
        emoji = "✅" if pnl > 0 else "🔴"
        send_telegram(
            f"{'💰' if is_live else '📝'} {tag} EXIT {emoji}\n"
            f"<b>{trade_record['direction']} {symbol}</b>\n"
            f"Exit ₹{exit_price} | PnL ₹{pnl:,.2f}\n"
            f"Reason: {reason}"
        )
    if COOLDOWN_MINUTES > 0:
        log.info(f"   🧊 {symbol} on cooldown for {COOLDOWN_MINUTES} min — no re-entry until then.")
    _save_current_report()
    return trade_record


def paper_exit(symbol: str, price: float, reason: str):
    """
    PAPER-mode exit. There is no real broker order to rest, so for SL/
    TRAIL_SL exits this is called directly from the tick handler the
    instant a tick crosses the configured stop (see
    _process_tick_for_position) — not from a periodic REST poll — which is
    the closest honest simulation of exchange-level execution achievable
    without a real resting order. TIME_EXIT/EOD calls still come from the
    position monitor loop, which is time-based rather than price-based.
    """
    if not engine.try_begin_exit(symbol):
        return
    try:
        latency_key = symbol if ENABLE_LATENCY_LOG else None
        if latency_key and reason in ("SL_HIT", "TRAIL_SL_HIT"):
            engine.mark(latency_key, "sl_trigger_time")
        _book_exit(symbol, price, None, reason, is_live=False, latency_key=latency_key)
    finally:
        engine.end_exit(symbol)


# ── Live Order Safety ─────────────────────────────────────────────────────────
def _round_to_tick(price: float, tick: float = None) -> float:
    tick = tick if tick is not None else BROKER_SL_TICK_SIZE
    return round(round(price / tick) * tick, 2)


def confirm_order_filled(order_id: str, what: str = "") -> dict | None:
    """
    Poll Zerodha for an order's ACTUAL status instead of assuming
    place_order() succeeding means it filled. Returns
    {"average_price": float, "filled_quantity": int} once status is
    COMPLETE, or None if REJECTED/CANCELLED or never confirmed within
    ORDER_CONFIRM_ATTEMPTS tries (caller must NOT treat the trade as open).
    """
    kite = state["kite"]
    if kite is None or not order_id:
        return None
    for attempt in range(1, ORDER_CONFIRM_ATTEMPTS + 1):
        try:
            history = kite_call_with_retry(kite.order_history, order_id, what=f"order_history {what}")
            if history:
                latest = history[-1]
                status = latest.get("status", "")
                if status == "COMPLETE":
                    return {
                        "average_price":   float(latest.get("average_price") or 0.0),
                        "filled_quantity": int(latest.get("filled_quantity") or 0),
                    }
                if status in ("REJECTED", "CANCELLED"):
                    reason = latest.get("status_message") or status
                    log.error(f"🚫 Order {order_id} {what} was {status}: {reason}")
                    return None
        except Exception as e:
            log.warning(f"Order confirm error {what} (attempt {attempt}/{ORDER_CONFIRM_ATTEMPTS}): {e}")
        time.sleep(ORDER_CONFIRM_DELAY_SEC)
    log.error(f"🚫 Order {order_id} {what} did not confirm COMPLETE within {ORDER_CONFIRM_ATTEMPTS} attempts — treating as failed.")
    return None


def place_broker_sl(symbol: str, direction: str, qty: int, trigger_price: float) -> str | None:
    kite = state["kite"]
    if kite is None:
        return None
    try:
        exit_txn = kite.TRANSACTION_TYPE_SELL if direction == "BUY" else kite.TRANSACTION_TYPE_BUY
        trigger = _round_to_tick(trigger_price)
        oid = kite_call_with_retry(
            kite.place_order,
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
            transaction_type=exit_txn, quantity=qty,
            order_type=kite.ORDER_TYPE_SLM,
            trigger_price=trigger,
            product=kite.PRODUCT_MIS,
            validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
            what=f"broker_sl {symbol}",
        )
        log.info(f"🛡️ Broker SL-M placed {symbol} | trigger {trigger} | order_id={oid}")
        return oid
    except Exception as e:
        log.error(f"🚫 Broker SL placement FAILED for {symbol} — position has NO broker-side protection: {e}")
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram(f"🚫 Broker SL placement FAILED for {symbol} — position has NO broker-side protection: {e}")
        return None


def modify_broker_sl(order_id: str, symbol: str, new_trigger_price: float):
    kite = state["kite"]
    if kite is None or not order_id:
        return
    try:
        trigger = _round_to_tick(new_trigger_price)
        kite_call_with_retry(
            kite.modify_order,
            variety=kite.VARIETY_REGULAR,
            order_id=order_id,
            trigger_price=trigger,
            what=f"modify_sl {symbol}",
        )
        log.info(f"🛡️ Broker SL-M for {symbol} moved to {trigger}")
    except Exception as e:
        log.warning(f"Broker SL modify error {symbol}: {e}")


def cancel_broker_sl(order_id: str, symbol: str):
    kite = state["kite"]
    if kite is None or not order_id:
        return
    try:
        kite_call_with_retry(
            kite.cancel_order,
            variety=kite.VARIETY_REGULAR,
            order_id=order_id,
            what=f"cancel_sl {symbol}",
        )
        log.info(f"🛡️ Broker SL-M for {symbol} cancelled")
    except Exception as e:
        log.warning(f"Broker SL cancel error {symbol} (order may have already fired/expired): {e}")


# ── Live Trading ──────────────────────────────────────────────────────────────
def live_enter(signal: dict):
    kite   = state["kite"]
    symbol = signal["symbol"]

    ltp = get_ltp(symbol)
    entry_price = ltp if ltp is not None else signal["entry"]
    direction = signal["direction"]
    qty, computed_margin = calc_qty(symbol, direction, entry_price, signal["sl"])

    if qty <= 0:
        log_skip(
            symbol,
            f"Quantity calculated as zero (entry ₹{entry_price:,.2f}) — usable margin "
            f"too small for this entry price, or real per-share margin too high"
        )
        return

    # Final live re-check right before placing the real order (funds/margin
    # can shift between signal time and order time) — shrinks further if needed.
    qty = verify_and_shrink_order_qty(symbol, direction, qty, entry_price)
    if qty <= 0:
        return

    known_margin = computed_margin
    allowed, margin_used = is_within_investment_limit(
        symbol, direction, entry_price, qty, known_margin=known_margin
    )
    if not allowed:
        log_skip(
            symbol,
            f"Insufficient usable margin — required ₹{margin_used:,.2f}, "
            f"available ₹{get_available_capital():,.2f}"
        )
        return

    risk = signal["risk"]
    if direction == "BUY":
        initial_sl = round(entry_price - INITIAL_SL_R * risk, 2)
    else:
        initial_sl = round(entry_price + INITIAL_SL_R * risk, 2)
    band = band_for_symbol(symbol)

    with _state_lock:
        if len(state["positions"]) >= MAX_POSITIONS:
            skip_reason = (
                f"Max positions cap reached ({len(state['positions'])}/{MAX_POSITIONS}) "
                f"— filled by another symbol between signal and entry"
            )
        elif symbol in state["positions"]:
            skip_reason = "Duplicate trade — a position on this symbol is already open"
        else:
            band_count = (
                sum(1 for p in state["positions"].values() if p.get("band") == band["name"])
                if band else 0
            )
            skip_reason = (
                f"Band ₹{band['name']} limit reached ({band_count}/{band['max_positions']})"
                if band and band_count >= band["max_positions"] else None
            )
    if skip_reason:
        log_skip(symbol, skip_reason)
        return

    # ── Latency tracking starts here: signal → entry order → entry fill →
    # SL order → SL accepted. See execution.py / _book_exit for the rest of
    # the chain (sl_trigger_time / exit_filled), recorded when the position
    # eventually closes.
    latency_key = symbol if ENABLE_LATENCY_LOG else None
    if latency_key:
        try:
            engine.set_stage_time(latency_key, "signal_time", datetime.fromisoformat(signal["time"]).timestamp())
        except Exception:
            pass
        engine.mark(latency_key, "entry_order_sent")

    try:
        txn = kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL
        oid = kite_call_with_retry(
            kite.place_order,
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
            transaction_type=txn, quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_MIS,
            validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
            what=f"place_order {symbol}",
        )
    except Exception as e:
        log.error(f"Live order error {symbol}: {e}")
        return

    # Prefer the WebSocket order-update push (near-instant) over polling
    # order_history() in a loop. confirm_order_filled() (REST poll) is kept
    # ONLY as a one-shot fallback if the push doesn't arrive within the
    # timeout — a safety net, not the primary mechanism.
    update = engine.await_order_update(oid, timeout=6.0) if USE_KITETICKER else None
    if update and update.get("status") == "COMPLETE":
        fill = {
            "average_price":   float(update.get("average_price") or 0.0),
            "filled_quantity": int(update.get("filled_quantity") or 0),
        }
    elif update and update.get("status") in ("REJECTED", "CANCELLED"):
        fill = None
    else:
        fill = confirm_order_filled(oid, what=f"entry {symbol}")

    if fill is None:
        log.error(f"🚫 {symbol} entry order {oid} did not confirm as FILLED (rejected/cancelled/timed out) — NOT recording a position. Check Kite/order book manually.")
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram(f"🚫 {symbol} entry order {oid} did NOT confirm as filled (rejected/cancelled/timed out) — no position recorded. Check Kite manually.")
        return

    if latency_key:
        engine.mark(latency_key, "entry_complete")

    filled_price = fill["average_price"] or entry_price
    filled_qty = fill["filled_quantity"] or qty
    if filled_qty <= 0:
        log.error(f"🚫 {symbol} entry order {oid} confirmed but filled_quantity is 0 — NOT recording a position.")
        return
    if filled_qty != qty:
        log.warning(f"⚠️ {symbol} PARTIAL FILL: requested {qty}, filled {filled_qty}. Using the filled quantity.")

    if direction == "BUY":
        initial_sl = round(filled_price - INITIAL_SL_R * risk, 2)
    else:
        initial_sl = round(filled_price + INITIAL_SL_R * risk, 2)

    # ── Exchange-Native Stop-Loss ─────────────────────────────────────────
    # This SL-M order is placed IMMEDIATELY after the entry fill is
    # confirmed — it starts resting at the exchange right now, before
    # anything else happens. From this point on, THE EXCHANGE owns the
    # stop: its own matching engine watches every tick and fires the order
    # the instant price crosses the trigger, with no Python polling loop
    # anywhere in that path. Trailing later calls modify_order() on this
    # SAME order (see _process_tick_for_position) — it is never cancelled
    # and recreated.
    broker_sl_order_id = None
    if USE_BROKER_SL and USE_EXCHANGE_SL:
        if latency_key:
            engine.mark(latency_key, "sl_order_sent")
        broker_sl_order_id = place_broker_sl(symbol, direction, filled_qty, initial_sl)
        if broker_sl_order_id is None:
            log.error(
                f"⚠️ {symbol} entered WITHOUT exchange-side SL protection (order placement "
                f"failed) — the bot's own monitor loop is the ONLY thing protecting this "
                f"position right now. Consider closing it manually if this persists."
            )
        elif latency_key:
            engine.mark(latency_key, "sl_accepted")

    log.info(f"✅ LIVE ORDER {direction} {symbol} x{filled_qty} @ {filled_price} (confirmed fill, avg price from Kite) | order_id={oid}")

    margin_used = _order_margin_required(symbol, direction, filled_qty)
    if margin_used is None or margin_used <= 0:
        margin_used = filled_price * filled_qty  # fallback: conservative no-leverage figure

    with _state_lock:
        state["positions"][symbol] = {
            "symbol":             symbol,
            "direction":          direction,
            "entry":              round(filled_price, 2),
            "risk":               risk,
            "initial_sl":         initial_sl,
            "sl":                 initial_sl,
            "tp":                 signal["tp"],
            "band":               band["name"] if band else None,
            "qty":                filled_qty,
            "margin_used":        round(margin_used, 2),   # real margin consumed, NOT notional trade value
            "order_id":           oid,
            "broker_sl_order_id": broker_sl_order_id,
            "open_time":          signal["time"],
            "pnl":                0.0,
            "mfe":                0.0,
            "mae":                0.0,
            "status":             "OPEN",
            "trend_score":        trend_detector.get_trend_score(symbol, get_candles(symbol, 260) or []) if TREND_FILTER_ENABLED else None,
            "trend_state":        trend_detector.get_market_state(symbol, get_candles(symbol, 260) or []) if TREND_FILTER_ENABLED else None,
        }
        state["deployed"] += margin_used
        deployed_after  = state["deployed"]
    available_after = get_available_capital()
    log.info(
        f"   💰 Sizing (LIVE margin): per-trade slice ₹{max_capital_per_trade():,.2f} "
        f"({MAX_MARGIN_PER_TRADE_PCT}% of usable margin) | Entry ₹{filled_price:,.2f} | "
        f"Qty = {filled_qty} shares (confirmed fill) | Real margin used ₹{margin_used:,.2f} | "
        f"Deployed ₹{deployed_after:,.2f} | Available ₹{available_after:,.2f} | "
        f"Band {band['name'] if band else 'n/a'}"
    )
    if TELEGRAM_NOTIFY_ENTRIES:
        send_telegram(
            f"💰 LIVE ORDER FILLED\n"
            f"<b>{direction} {symbol}</b>\n"
            f"Qty {filled_qty} @ ₹{filled_price} (confirmed fill)\n"
            f"SL ₹{initial_sl} ({INITIAL_SL_R}R) | Ref target ₹{signal['tp']}\n"
            f"Order ID {oid}"
        )
        if broker_sl_order_id is None:
            send_telegram(f"⚠️ {symbol}: entered WITHOUT broker-side SL protection — bot monitor is the only safety net right now.")


def live_exit(symbol: str, reason: str):
    """
    LIVE exit via an explicit MARKET order — used ONLY for reasons that are
    not price-triggered: TIME_EXIT, EOD_SQUAREOFF, MANUAL_CLOSE_RECONCILED.
    A normal SL/TRAIL_SL exit does NOT go through here anymore — that is
    handled entirely by the resting exchange SL-M order + the order-update
    push (see live_exit_broker_sl_filled), with no Python market order
    involved at all. Guarded by engine.try_begin_exit so this can never run
    at the same moment as a broker-SL-fill or emergency-exit for the same
    symbol.
    """
    if not engine.try_begin_exit(symbol):
        return
    try:
        kite = state["kite"]
        with _state_lock:
            pos = state["positions"].get(symbol)
        if not pos:
            return

        latency_key = symbol if ENABLE_LATENCY_LOG else None
        if latency_key:
            engine.mark(latency_key, "exit_order_sent")

        try:
            txn = kite.TRANSACTION_TYPE_SELL if pos["direction"] == "BUY" else kite.TRANSACTION_TYPE_BUY
            oid = kite_call_with_retry(
                kite.place_order,
                tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
                transaction_type=txn, quantity=pos["qty"],
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_MIS,
                validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
                what=f"exit_order {symbol}",
            )
        except Exception as e:
            log.error(f"Live exit order error {symbol}: {e}")
            return

        update = engine.await_order_update(oid, timeout=6.0) if USE_KITETICKER else None
        if update and update.get("status") == "COMPLETE":
            fill = {
                "average_price":   float(update.get("average_price") or 0.0),
                "filled_quantity": int(update.get("filled_quantity") or 0),
            }
        else:
            fill = confirm_order_filled(oid, what=f"exit {symbol}")

        if fill is None:
            log.error(
                f"🚫 {symbol} exit order {oid} did not confirm as FILLED — position left OPEN in "
                f"tracking, will re-attempt the exit next monitor cycle. Check Kite/order book manually."
            )
            return

        exit_price = fill["average_price"] or pos["entry"]
        filled_qty = fill["filled_quantity"] or pos["qty"]

        if pos.get("broker_sl_order_id"):
            cancel_broker_sl(pos["broker_sl_order_id"], symbol)

        _book_exit(symbol, exit_price, filled_qty, reason, is_live=True, latency_key=latency_key)
    finally:
        engine.end_exit(symbol)


def live_exit_broker_sl_filled(symbol: str, order_update: dict):
    """
    Called from _handle_order_update the instant the resting exchange SL-M
    order for this symbol shows status COMPLETE. NO new order is placed
    here — the exchange already filled it; this only books the outcome
    (pnl/state/slippage/latency/cooldown/SL-hit-counter/Telegram/report).
    This is the primary way a stop-loss exit happens in LIVE mode now.
    """
    if not engine.try_begin_exit(symbol):
        return
    try:
        with _state_lock:
            pos = state["positions"].get(symbol)
        if not pos:
            return

        latency_key = symbol if ENABLE_LATENCY_LOG else None
        if latency_key:
            engine.mark(latency_key, "sl_trigger_time")

        exit_price = float(order_update.get("average_price") or pos["entry"])
        filled_qty = int(order_update.get("filled_quantity") or pos["qty"])
        reason = "SL_HIT" if pos["sl"] == pos.get("initial_sl") else "TRAIL_SL_HIT"

        log.info(
            f"🛡️ Exchange SL-M order FILLED for {symbol} @ {exit_price} — confirmed via the "
            f"order-update WebSocket push, no Python market order was placed for this exit."
        )
        _book_exit(symbol, exit_price, filled_qty, reason, is_live=True, latency_key=latency_key)
    finally:
        engine.end_exit(symbol)


def live_emergency_exit(symbol: str, trigger_reason: str):
    """
    Fail-safe (requirement: "never leave a position unprotected"). Fires
    ONLY when the resting exchange SL order is observed to be REJECTED or
    CANCELLED while a position on this symbol is STILL open in bot state.
    That combination can only mean the SL protection genuinely disappeared
    unexpectedly (margin issue, RMS action, a Zerodha-side problem, etc.) —
    the bot's own intentional cancels (inside live_exit / 
    live_exit_broker_sl_filled) always remove the position from state
    BEFORE cancelling the SL order, so this function finding an open
    position here means real, immediate danger. Places a MARKET exit right
    away rather than waiting for the next monitor tick.
    """
    if not engine.try_begin_exit(symbol):
        return
    try:
        with _state_lock:
            pos = state["positions"].get(symbol)
        if not pos:
            return  # already closed through a normal path — nothing left to protect

        kite = state["kite"]
        log.error(f"🚨 EMERGENCY EXIT {symbol}: {trigger_reason} — position is UNPROTECTED, firing immediate MARKET exit.")
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram(
                f"🚨 EMERGENCY EXIT {symbol}: {trigger_reason} — firing an immediate MARKET "
                f"exit to avoid leaving this position unprotected."
            )

        latency_key = symbol if ENABLE_LATENCY_LOG else None
        try:
            txn = kite.TRANSACTION_TYPE_SELL if pos["direction"] == "BUY" else kite.TRANSACTION_TYPE_BUY
            oid = kite_call_with_retry(
                kite.place_order,
                tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
                transaction_type=txn, quantity=pos["qty"],
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_MIS,
                validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
                what=f"emergency_exit {symbol}",
            )
        except Exception as e:
            log.error(f"🚨 EMERGENCY EXIT order placement FAILED for {symbol}: {e} — MANUAL INTERVENTION REQUIRED IMMEDIATELY.")
            if TELEGRAM_NOTIFY_ERRORS:
                send_telegram(f"🚨🚨 {symbol}: emergency exit order FAILED to even place: {e} — CHECK KITE MANUALLY RIGHT NOW.")
            return

        update = engine.await_order_update(oid, timeout=6.0) if USE_KITETICKER else None
        if update and update.get("status") == "COMPLETE":
            fill = {
                "average_price":   float(update.get("average_price") or 0.0),
                "filled_quantity": int(update.get("filled_quantity") or 0),
            }
        else:
            fill = confirm_order_filled(oid, what=f"emergency_exit {symbol}")

        if fill is None:
            log.error(
                f"🚨 {symbol} emergency exit order did NOT confirm as filled — CHECK KITE MANUALLY "
                f"IMMEDIATELY, this position may still be open and unprotected."
            )
            return

        exit_price = fill["average_price"] or pos["entry"]
        filled_qty = fill["filled_quantity"] or pos["qty"]
        _book_exit(symbol, exit_price, filled_qty, "EMERGENCY_MARKET_EXIT", is_live=True, latency_key=latency_key)
    finally:
        engine.end_exit(symbol)


# ── Tick-Driven Trailing / Paper-Exit Processing ─────────────────────────────
def _process_tick_for_position(symbol: str, price: float):
    """
    The ONE place trailing math and (paper-only) SL/TRAIL_SL exit decisions
    happen, called both from the tick stream (_handle_ticks, primary path)
    and from the REST-poll backup (monitor_positions, used when ticks are
    delayed/unavailable) — so the two paths can never disagree with each
    other or duplicate an exit.

      - PAPER mode: there is no real resting order, so this simulates the
        SL/TRAIL_SL exit itself the instant price crosses the stop — the
        closest honest approximation to exchange execution available
        without a real order in the book.
      - LIVE mode: this function ONLY computes/advances the trailing stop
        and calls modify_broker_sl() on the resting exchange SL-M order.
        It NEVER independently exits a live position on a price check —
        that decision belongs exclusively to the exchange's own SL-M order
        (booked via live_exit_broker_sl_filled) or an explicit TIME_EXIT/
        EOD_SQUAREOFF (via live_exit). This is exactly what prevents the
        old race between a Python price-poll exit and the resting SL order.
    """
    with _state_lock:
        pos = state["positions"].get(symbol)
    if not pos:
        return

    risk = pos.get("risk") or 0
    direction = pos["direction"]
    current_r = 0.0
    if risk > 0:
        current_r = (price - pos["entry"]) / risk if direction == "BUY" else (pos["entry"] - price) / risk

    new_sl_r = compute_trail_sl_r(current_r)
    if new_sl_r is not None and risk > 0:
        if direction == "BUY":
            candidate_sl = round(pos["entry"] + new_sl_r * risk, 2)
            should_move = candidate_sl > pos["sl"]
        else:
            candidate_sl = round(pos["entry"] - new_sl_r * risk, 2)
            should_move = candidate_sl < pos["sl"]
        if should_move:
            with _state_lock:
                if symbol in state["positions"]:
                    state["positions"][symbol]["sl"] = candidate_sl
            pos = dict(pos)
            pos["sl"] = candidate_sl
            log.info(
                f"🪜 TRAIL {symbol} | reached {current_r:.2f}R → SL moved to {candidate_sl} "
                f"({new_sl_r}R)"
            )
            if not PAPER_TRADING and USE_BROKER_SL and USE_EXCHANGE_SL and pos.get("broker_sl_order_id"):
                modify_broker_sl(pos["broker_sl_order_id"], symbol, candidate_sl)

    if PAPER_TRADING:
        is_initial_sl = pos["sl"] == pos.get("initial_sl")
        reason = "SL_HIT" if is_initial_sl else "TRAIL_SL_HIT"
        if direction == "BUY":
            if price <= pos["sl"]:
                paper_exit(symbol, price, reason)
                return
        else:
            if price >= pos["sl"]:
                paper_exit(symbol, price, reason)
                return

    # Mark-to-market for the dashboard only — never an exit decision on the
    # LIVE side (paper falls through to here too when no exit fired above).
    with _state_lock:
        if symbol in state["positions"]:
            pnl = (price - pos["entry"]) * pos["qty"] if direction == "BUY" else (pos["entry"] - price) * pos["qty"]
            state["positions"][symbol]["pnl"] = round(pnl, 2)
            if pnl > state["positions"][symbol].get("mfe", 0):
                state["positions"][symbol]["mfe"] = round(pnl, 2)
            if pnl < state["positions"][symbol].get("mae", 0):
                state["positions"][symbol]["mae"] = round(pnl, 2)
    _save_current_report()


def _handle_ticks(ticks: list):
    """
    Registered with engine.on_ticks_callback — fires on every batch of
    ticks from the KiteTicker WebSocket. `ticks` is an ORDERED list of
    (symbol, price) pairs exactly as received from the exchange feed — NOT
    deduplicated to a single last-value-per-symbol dict.

    FIXED: this used to receive a dict (tick_map[symbol] = price), which
    silently drops earlier ticks for the same symbol if more than one
    arrives in a single WebSocket batch (common during a fast reversal —
    exactly the situation that produced an exit at 1928.70 instead of the
    actual trailing-stop level of 1928.48). Processing every tick in order
    guarantees the position check reacts to the FIRST tick that reaches
    the stop, not whatever price happened to be last in that batch.
    _process_tick_for_position already re-reads state["positions"] fresh
    on every call and no-ops once a position is closed, so continuing to
    iterate remaining ticks for an already-exited symbol within the same
    batch is safe.
    """
    try:
        with _state_lock:
            open_symbols = set(state["positions"].keys())
        for symbol, price in ticks:
            if symbol in open_symbols:
                _process_tick_for_position(symbol, price)
    except Exception as e:
        log.warning(f"_handle_ticks error: {e}")


def _handle_order_update(data: dict):
    """
    Registered with engine.on_order_update_callback — fires on every order
    status push from Kite over the WebSocket. Routes:
      - The resting exchange SL-M order for an open position reports
        COMPLETE -> live_exit_broker_sl_filled (books the fill; places NO
        new order, the exchange already executed it).
      - That SAME SL-M order reports REJECTED/CANCELLED while the position
        is STILL open in bot state -> live_emergency_exit. The bot's own
        deliberate cancels (inside live_exit / live_exit_broker_sl_filled)
        always remove the position from state BEFORE cancelling the SL
        order, so finding the position still open here can only mean the
        SL protection genuinely vanished unexpectedly.
      - Anything else (entry orders, forced-exit market orders, etc.) is
        handled synchronously by whoever is already awaiting that exact
        order_id via engine.await_order_update() — nothing further to do
        for those here.
    """
    try:
        order_id = data.get("order_id")
        status = data.get("status")
        if not order_id or status not in ("COMPLETE", "REJECTED", "CANCELLED"):
            return

        with _state_lock:
            match_symbol = None
            for symbol, pos in state["positions"].items():
                if pos.get("broker_sl_order_id") == order_id:
                    match_symbol = symbol
                    break
        if not match_symbol:
            return  # not a resting SL order we're tracking (or already closed)

        if status == "COMPLETE":
            live_exit_broker_sl_filled(match_symbol, data)
        else:
            live_emergency_exit(match_symbol, f"exchange SL order {status}: {data.get('status_message')}")
    except Exception as e:
        log.error(f"_handle_order_update error: {e}")


def enter_trade(signal):
    paper_enter(signal) if PAPER_TRADING else live_enter(signal)


def exit_trade(symbol, price, reason):
    paper_exit(symbol, price, reason) if PAPER_TRADING else live_exit(symbol, reason)


def generate_market_trend_report() -> str:
    """
    Generate a clean, structured market trend report for ALL watchlist symbols,
    grouped by TRENDING / NEUTRAL / CHOPPY, with tradable/skipped lists.
    """
    lines = [
        "",
        "=" * 50,
        "MARKET TREND REPORT",
        "=" * 50,
        "",
    ]

    trending = []
    neutral = []
    choppy = []

    for symbol in WATCHLIST:
        try:
            candles = get_candles(symbol, 260)
            if not candles:
                choppy.append((symbol, 0, "No candle data"))
                continue
            result = trend_detector.compute(symbol, candles)
            if result.state == "TRENDING":
                trending.append((symbol, result.score, result.reasons))
            elif result.state == "NEUTRAL":
                neutral.append((symbol, result.score, result.reasons))
            else:
                choppy.append((symbol, result.score, result.reasons))
        except Exception as e:
            choppy.append((symbol, 0, f"Error: {e}"))

    lines.append("TRENDING")
    lines.append("")
    for sym, score, _ in trending:
        lines.append(f"{sym:<16} Score {score}")

    lines.append("")
    lines.append("-" * 50)
    lines.append("")
    lines.append("NEUTRAL")
    lines.append("")
    for sym, score, _ in neutral:
        lines.append(f"{sym:<16} Score {score}")

    lines.append("")
    lines.append("-" * 50)
    lines.append("")
    lines.append("CHOPPY")
    lines.append("")
    for sym, score, _ in choppy:
        lines.append(f"{sym:<16} Score {score}")

    lines.append("")
    lines.append("=" * 50)
    lines.append("")

    tradable = [sym for sym, _, _ in trending]
    if ALLOW_NEUTRAL_TRADES:
        tradable.extend([sym for sym, _, _ in neutral])

    skipped = [sym for sym, _, _ in choppy]
    if not ALLOW_NEUTRAL_TRADES:
        skipped.extend([sym for sym, _, _ in neutral])

    lines.append("TRADABLE TODAY")
    lines.append("")
    if tradable:
        for sym in tradable:
            lines.append(f"  ✓ {sym}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Skipped Today")
    lines.append("")
    if skipped:
        for sym in skipped:
            lines.append(f"  ✗ {sym}")
        lines.append("")
        lines.append("Reason:")
        lines.append("Trend Score below 75")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("=" * 50)
    lines.append("")

    return "\n".join(lines)


def generate_trend_eod_report() -> str:
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        trades = list(state["trades"])
        trend_scores = dict(state.get("trend_scores", {}))
        trend_signals = dict(state.get("trend_signals_detected", {}))
        trend_skips = dict(state.get("trend_skips", {}))
        sl_hit_counts = dict(state.get("sl_hit_counts", {}))
        wins, losses = state["wins"], state["losses"]

    # Per-symbol stats from closed trades
    symbol_stats: dict[str, dict] = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in symbol_stats:
            symbol_stats[sym] = {
                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                "sl_hits": 0, "trail_sl_hits": 0, "holding_minutes_sum": 0.0,
            }
        s = symbol_stats[sym]
        s["trades"] += 1
        if t["result"] == "WIN":
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] += t.get("pnl", 0.0)
        s["holding_minutes_sum"] += t.get("holding_minutes", 0.0)
        if t.get("reason") == "SL_HIT":
            s["sl_hits"] += 1
        elif t.get("reason") == "TRAIL_SL_HIT":
            s["trail_sl_hits"] += 1

    all_symbols = sorted(set(list(trend_scores.keys()) + list(symbol_stats.keys())))

    trending_stocks = []
    neutral_stocks = []
    choppy_stocks = []
    for sym in all_symbols:
        ts = trend_scores.get(sym, {})
        cls = ts.get("state", "CHOPPY")
        if cls == "TRENDING":
            trending_stocks.append(sym)
        elif cls == "NEUTRAL":
            neutral_stocks.append(sym)
        else:
            choppy_stocks.append(sym)

    lines = [
        "",
        "=" * 40,
        "MARKET ANALYSIS REPORT",
        "=" * 40,
        "",
        "Trending Stocks",
    ]
    for sym in trending_stocks:
        lines.append(f"  {sym}")
    lines.append("")
    lines.append("Neutral Stocks")
    for sym in neutral_stocks:
        lines.append(f"  {sym}")
    lines.append("")
    lines.append("Skipped Choppy Stocks")
    for sym in choppy_stocks:
        lines.append(f"  {sym}")

    # Per-stock detail
    lines.append("")
    lines.append("-" * 40)
    for sym in all_symbols:
        ts = trend_scores.get(sym, {})
        score = ts.get("score", 0)
        cls = ts.get("state", "CHOPPY")
        reasons = ", ".join(ts.get("reasons", [])[:3])
        st = symbol_stats.get(sym, {})
        trades = st.get("trades", 0)
        wins = st.get("wins", 0)
        losses = st.get("losses", 0)
        pnl = st.get("pnl", 0.0)
        avg_hold = (st["holding_minutes_sum"] / trades) if trades else 0.0
        sl_hits = st.get("sl_hits", 0)
        trail_sl_hits = st.get("trail_sl_hits", 0)
        signals = trend_signals.get(sym, 0)
        skips = trend_skips.get(sym, 0)

        lines.append("")
        lines.append(f"{sym}")
        lines.append(f"  Trend Score  : {score}")
        lines.append(f"  Classification: {cls}")
        if cls == "CHOPPY":
            lines.append(f"  Skipped      : {skips}")
            lines.append(f"  Reason       : {reasons}")
        else:
            lines.append(f"  Signals      : {signals}")
            lines.append(f"  Trades       : {trades}")
            lines.append(f"  Wins         : {wins}")
            lines.append(f"  Losses       : {losses}")
            lines.append(f"  PnL          : {pnl:+,.2f}")
            if trades:
                lines.append(f"  Avg Hold     : {avg_hold:.1f} min")
                lines.append(f"  Win Rate     : {wins/trades*100:.1f}%")
            lines.append(f"  SL Hits      : {sl_hits}")
            lines.append(f"  Trail SL Hits: {trail_sl_hits}")

    # Analytics
    def _bucket(syms, stats_map):
        pnl_list, wr_list, dur_list, sl_list, trail_list = [], [], [], [], []
        rr_list, mae_list, mfe_list = [], [], []
        for sym in syms:
            st = stats_map.get(sym, {})
            t = st.get("trades", 0)
            if t == 0:
                continue
            pnl_list.append(st.get("pnl", 0.0))
            wr_list.append(st.get("wins", 0) / t)
            dur_list.append(st.get("holding_minutes_sum", 0.0) / t)
            sl_list.append(st.get("sl_hits", 0) / t)
            trail_list.append(st.get("trail_sl_hits", 0) / t)
        return pnl_list, wr_list, dur_list, sl_list, trail_list

    # Calculate RR achieved, MAE, MFE from trades
    def _trade_bucket(syms, trades_list):
        pnl_list, rr_list, mae_list, mfe_list = [], [], [], []
        for t in trades_list:
            if t["symbol"] not in syms:
                continue
            pnl_list.append(t.get("pnl", 0.0))
            risk = t.get("risk", 0)
            if risk:
                rr_list.append(t.get("pnl", 0.0) / risk)
            mae_list.append(abs(t.get("mae", 0.0)))
            mfe_list.append(abs(t.get("mfe", 0.0)))
        return pnl_list, rr_list, mae_list, mfe_list

    trend_pnl, trend_wr, trend_dur, trend_sl, trend_trail = _bucket(trending_stocks, symbol_stats)
    choppy_pnl, choppy_wr, choppy_dur, choppy_sl, choppy_trail = _bucket(choppy_stocks, symbol_stats)

    trend_trade_pnl, trend_rr, trend_mae, trend_mfe = _trade_bucket(trending_stocks, trades)
    choppy_trade_pnl, choppy_rr, choppy_mae, choppy_mfe = _trade_bucket(choppy_stocks, trades)

    def _avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    lines.append("")
    lines.append("-" * 40)
    lines.append("ANALYTICS")
    lines.append("")
    lines.append(f"Average PnL (TRENDING)     : {_avg(trend_pnl):+,.2f}")
    lines.append(f"Average PnL (CHOPPY)       : {_avg(choppy_pnl):+,.2f}")
    lines.append(f"Win Rate (TRENDING)        : {_avg(trend_wr)*100:.1f}%")
    lines.append(f"Win Rate (CHOPPY)          : {_avg(choppy_wr)*100:.1f}%")
    lines.append(f"Avg SL Hits/trade (TREND)  : {_avg(trend_sl):.2f}")
    lines.append(f"Avg SL Hits/trade (CHOP)   : {_avg(choppy_sl):.2f}")
    lines.append(f"Avg Trail SL Hits/trade    : {_avg(trend_trail):.2f}")
    lines.append(f"Avg Duration (TREND)       : {_avg(trend_dur):.1f} min")
    lines.append(f"Avg Duration (CHOP)        : {_avg(choppy_dur):.1f} min")
    lines.append(f"Avg RR Achieved (TREND)    : {_avg(trend_rr):.2f}")
    lines.append(f"Avg RR Achieved (CHOP)     : {_avg(choppy_rr):.2f}")
    lines.append(f"Avg MAE (TREND)            : {_avg(trend_mae):,.2f}")
    lines.append(f"Avg MAE (CHOP)             : {_avg(choppy_mae):,.2f}")
    lines.append(f"Avg MFE (TREND)            : {_avg(trend_mfe):,.2f}")
    lines.append(f"Avg MFE (CHOP)             : {_avg(choppy_mfe):,.2f}")

    # PnL comparison
    total_pnl_with_filter = sum(t.get("pnl", 0.0) for t in trades)
    total_signals = sum(trend_signals.get(s, 0) for s in trend_signals)
    total_skips = sum(trend_skips.get(s, 0) for s in trend_skips)

    lines.append("")
    lines.append("-" * 40)
    lines.append("SUMMARY")
    lines.append(f"Average Trend Score        : {_avg([ts.get('score', 0) for ts in trend_scores.values()]):.1f}")
    lines.append(f"Total Trades Allowed       : {len(trades)}")
    lines.append(f"Total Trades Skipped       : {total_skips}")
    lines.append(f"PnL With Filter           : {total_pnl_with_filter:+,.2f}")
    lines.append(f"PnL Without Filter        : N/A (no baseline data)")
    lines.append(f"Improvement %             : N/A (no baseline)")
    lines.append(f"Win Rate                   : {wins / (wins + losses) * 100:.1f}%" if (wins + losses) else "Win Rate: 0.0%")
    lines.append(f"Drawdown Reduction %      : N/A (no baseline)")
    lines.append("")
    if _avg(trend_pnl) > _avg(choppy_pnl):
        lines.append("Recommendation: Trend filter BENEFICIAL")
    else:
        lines.append("Recommendation: Trend filter NOT BENEFICIAL")
    lines.append("=" * 40)
    lines.append("")

    return "\n".join(lines)


# ── Force Square-Off (EOD) ────────────────────────────────────────────────────
def force_square_off_all(reason: str = "EOD_SQUAREOFF"):
    for symbol, pos in list(state["positions"].items()):
        try:
            price = get_ltp(symbol)
            if price is None:
                candles = get_candles(symbol, 3)
                price = candles[-1]["close"] if candles else pos.get("entry")
            log.info(f"⏰ EOD SQUARE-OFF {symbol} @ {price} (market)")
            exit_trade(symbol, price, reason)
        except Exception as e:
            log.warning(f"Square-off error {symbol}: {e}")


# ── Position Reconciliation (bot state <-> broker truth, LIVE mode only) ────
def reconcile_positions_with_broker():
    """
    Pull REAL positions from Zerodha (kite.positions()) and reconcile the
    bot's in-memory state against them. Never rely solely on in-memory state
    for live trading:
      - If the bot thinks a position is open but the broker shows it closed
        (e.g. manually closed from the Kite app/web), remove it from the
        bot's tracking and record it as closed using the broker's own last
        traded price — no duplicate exit order is placed.
      - If quantities differ (partial manual exit/addition), sync the bot's
        qty to match the broker's real qty.
      - If the broker has an OPEN position the bot isn't tracking at all,
        flag it loudly — it is NEVER auto-adopted; manage/close it manually.
    """
    kite = state["kite"]
    if kite is None:
        return
    try:
        broker_positions = kite_call_with_retry(kite.positions, what="positions")
        day_positions = {
            p["tradingsymbol"]: p
            for p in (broker_positions.get("day") or [])
            if p.get("product") == "MIS"
        }
    except Exception as e:
        log.warning(f"⚠️ Position reconcile: fetch failed — {e}")
        return

    with _state_lock:
        bot_symbols = list(state["positions"].keys())

    for symbol in bot_symbols:
        broker_pos = day_positions.get(symbol)
        broker_qty = abs(broker_pos["quantity"]) if broker_pos else 0

        with _state_lock:
            pos = dict(state["positions"].get(symbol) or {})
        if not pos:
            continue

        if broker_qty == 0:
            price = (broker_pos or {}).get("last_price") or get_ltp(symbol) or pos["entry"]
            log.warning(
                f"🔄 RECONCILE {symbol}: broker shows this position CLOSED (closed manually at "
                f"Zerodha, or hit a broker-side SL the bot didn't see yet) — removing from bot "
                f"tracking at ₹{price} without placing a duplicate exit order."
            )
            if TELEGRAM_NOTIFY_RECONCILE:
                send_telegram(
                    f"🔄 RECONCILE: {symbol} was closed at Zerodha (manually, or broker-side SL) "
                    f"— removed from bot tracking at ₹{price}, no duplicate exit placed."
                )
            with _state_lock:
                pos = state["positions"].pop(symbol, None)
            if pos:
                if pos.get("broker_sl_order_id"):
                    cancel_broker_sl(pos["broker_sl_order_id"], symbol)
                pnl = (price - pos["entry"]) * pos["qty"] if pos["direction"] == "BUY" else (pos["entry"] - price) * pos["qty"]
                with _state_lock:
                    result = "WIN" if pnl > 0 else "LOSS"
                    state["wins" if result == "WIN" else "losses"] += 1
                    state["pnl_today"] += pnl
                    state["equity"] += pnl
                    state["deployed"] -= pos.get("margin_used", pos["entry"] * pos["qty"])
                    state["trades"].insert(0, {**pos, "exit": round(price, 2), "pnl": round(pnl, 2),
                                               "result": result, "reason": "MANUAL_CLOSE_RECONCILED",
                                               "close_time": datetime.now(IST).isoformat()})
                record_cooldown(symbol)
                _save_current_report()

        elif broker_qty != pos["qty"]:
            log.warning(
                f"🔄 RECONCILE {symbol}: bot qty {pos['qty']} != broker qty {broker_qty} — "
                f"syncing bot state to the broker's real quantity (partial manual exit/addition detected)."
            )
            if TELEGRAM_NOTIFY_RECONCILE:
                send_telegram(
                    f"🔄 RECONCILE: {symbol} qty mismatch — bot had {pos['qty']}, broker shows "
                    f"{broker_qty}. Bot synced to the broker's real quantity."
                )
            with _state_lock:
                if symbol in state["positions"]:
                    old_qty = state["positions"][symbol]["qty"]
                    old_margin = state["positions"][symbol].get("margin_used", pos["entry"] * old_qty)
                    margin_per_share = (old_margin / old_qty) if old_qty else 0
                    new_margin = margin_per_share * broker_qty
                    state["positions"][symbol]["qty"] = broker_qty
                    state["positions"][symbol]["margin_used"] = round(new_margin, 2)
                    state["deployed"] -= (old_margin - new_margin)
            _save_current_report()

    with _state_lock:
        bot_symbols_now = set(state["positions"].keys())
    for symbol, broker_pos in day_positions.items():
        if symbol not in bot_symbols_now and broker_pos.get("quantity", 0) != 0:
            log.warning(
                f"⚠️ RECONCILE: broker has an OPEN {symbol} position (qty {broker_pos['quantity']}) "
                f"that the bot is NOT tracking — likely placed manually or from before a bot restart. "
                f"NOT auto-adopting it. Close/manage it directly in Kite if unintended."
            )
            if TELEGRAM_NOTIFY_RECONCILE:
                send_telegram(
                    f"⚠️ RECONCILE: broker has an OPEN {symbol} position (qty {broker_pos['quantity']}) "
                    f"the bot isn't tracking — not auto-adopted. Check/manage it in Kite if unintended."
                )


def reconcile_loop():
    """
    Independent background loop (separate from scan/monitor cadence) that
    periodically checks broker truth against bot state. Only meaningful in
    LIVE mode (paper positions don't exist at the broker), so it's a no-op
    while PAPER_TRADING is True.
    """
    log.info(f"🔄 Position reconciliation loop started — checking broker truth every {RECONCILE_INTERVAL_SEC}s")
    while True:
        try:
            if state["connected"] and not PAPER_TRADING:
                reconcile_positions_with_broker()
        except Exception as e:
            log.warning(f"Reconcile loop error: {e}")
        time.sleep(RECONCILE_INTERVAL_SEC)


# ── PnL Verification Helper ───────────────────────────────────────────────────
def _compute_open_mtm_breakdown_locked() -> dict:
    gross_open_profit = 0.0
    gross_open_loss = 0.0
    for p in state["positions"].values():
        pnl = p.get("pnl", 0.0)
        if pnl > 0:
            gross_open_profit += pnl
        elif pnl < 0:
            gross_open_loss += pnl
    return {
        "gross_open_profit": round(gross_open_profit, 2),
        "gross_open_loss":   round(gross_open_loss, 2),
        "net_open_pnl":      round(gross_open_profit + gross_open_loss, 2),
    }


def compute_open_mtm_breakdown() -> dict:
    with _state_lock:
        return _compute_open_mtm_breakdown_locked()


def print_open_mtm() -> str:
    with _state_lock:
        positions = dict(state["positions"])
        closed_pnl = state["pnl_today"]

    symbols = list(positions.keys())
    ltp_map = get_ltp_batch(symbols) if symbols else {}

    def fmt(n: float) -> str:
        return f"{'+' if n >= 0 else ''}{round(n, 2)}"

    lines = ["=" * 8 + " OPEN MTM " + "=" * 8, ""]
    total_open_mtm = 0.0
    for symbol, pos in positions.items():
        price = ltp_map.get(symbol)
        if price is None:
            price = get_ltp(symbol)
        if price is None:
            price = pos["entry"]
        mtm = (
            (price - pos["entry"]) * pos["qty"] if pos["direction"] == "BUY"
            else (pos["entry"] - price) * pos["qty"]
        )
        total_open_mtm += mtm
        lines += [
            symbol,
            pos["direction"],
            f"Entry : {pos['entry']}",
            f"LTP   : {round(price, 2)}",
            f"Qty   : {pos['qty']}",
            f"MTM   : {fmt(mtm)}",
            "",
        ]

    dashboard_total = total_open_mtm + closed_pnl
    lines += [
        "Total Open MTM",
        fmt(total_open_mtm),
        "",
        "Closed PnL",
        fmt(closed_pnl),
        "",
        "Dashboard",
        fmt(dashboard_total),
    ]

    block = "\n".join(lines)
    log.info("\n" + block)
    return block


# ── Position Monitor ──────────────────────────────────────────────────────────
def monitor_positions():
    """
    Runs every MONITOR_INTERVAL_SEC as a REST-poll BACKUP to the tick-driven
    path (_handle_ticks) — it exists for when ticks are delayed/unavailable
    (USE_KITETICKER off, socket briefly reconnecting, etc.), not as the
    primary exit mechanism. TIME_EXIT is the only exit decision made here
    directly; everything else (trailing + paper SL/TRAIL_SL exits) is
    delegated to _process_tick_for_position so the poll-driven and
    tick-driven paths can never disagree or double-exit a position. LIVE
    stop-loss exits are NEVER decided here — only by the resting exchange
    SL-M order (see live_exit_broker_sl_filled).
    """
    symbols = list(state["positions"].keys())
    if not symbols:
        return

    ltp_map = get_ltp_batch(symbols)

    for symbol, pos in list(state["positions"].items()):
        try:
            price = ltp_map.get(symbol)
            if price is None:
                price = get_ltp(symbol)
            if price is None:
                candles = get_candles(symbol, 3)
                if not candles:
                    continue
                price = candles[-1]["close"]

            held = minutes_held(pos)
            if held >= MAX_HOLD_MINUTES:
                log.info(f"⏳ TIME EXIT {symbol} @ {price} (market) | held {held:.1f} min (limit {MAX_HOLD_MINUTES})")
                exit_trade(symbol, price, "TIME_EXIT")
                continue

            _process_tick_for_position(symbol, price)
        except Exception as e:
            log.warning(f"Monitor error {symbol}: {e}")


# ── Zone Tracker ──────────────────────────────────────────────────────────────
def update_zones():
    zone_summary = {}
    for symbol in WATCHLIST:
        try:
            candles = get_candles(symbol, 100)
            if not candles:
                continue
            zones = detect_wick_zones(candles)
            bull  = [z for z in zones if z["type"] == "BULL"]
            bear  = [z for z in zones if z["type"] == "BEAR"]
            if bull or bear:
                zone_summary[symbol] = {
                    "bull": len(bull), "bear": len(bear),
                    "last_type": zones[-1]["type"] if zones else "NONE",
                }
        except Exception as e:
            log.warning(f"Zone update error {symbol}: {e}")
        time.sleep(0.4)
    with _state_lock:
        state["zones"] = zone_summary


# ── Fast Position Monitor Loop (independent of the signal scan cadence) ──────
def _maybe_send_eod_summary():
    """Send exactly one Telegram end-of-day P&L summary per calendar day,
    once SQUARE_OFF_TIME has passed. Safe to call every monitor tick."""
    if not TELEGRAM_NOTIFY_EOD_SUMMARY:
        return
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        if state.get("eod_summary_date_sent") == today_str:
            return
        state["eod_summary_date_sent"] = today_str
        wins, losses, pnl_today = state["wins"], state["losses"], state["pnl_today"]
        trend_scores = dict(state.get("trend_scores", {}))
        trades = list(state.get("trades", []))
    total = wins + losses
    wr = round(wins / total * 100, 1) if total else 0
    mode_tag = "📝 PAPER" if PAPER_TRADING else "💰 LIVE"

    trend_report = generate_trend_eod_report()
    market_trend_report = generate_market_trend_report()

    has_data = total > 0 or len(trend_scores) > 0 or len(trades) > 0
    if not has_data:
        send_telegram(
            f"📊 EOD SUMMARY ({today_str}) — {mode_tag}\n"
            f"Trades: {total} | Wins: {wins} | Losses: {losses} | Win rate: {wr}%\n"
            f"PnL today: ₹{pnl_today:,.2f}\n"
            f"No trading activity today."
        )
        log.info(f"EOD summary sent (no data) | Trades: {total} | PnL: ₹{pnl_today:,.2f}")
        return

    send_telegram(
        f"📊 EOD SUMMARY ({today_str}) — {mode_tag}\n"
        f"Trades: {total} | Wins: {wins} | Losses: {losses} | Win rate: {wr}%\n"
        f"PnL today: ₹{pnl_today:,.2f}\n\n"
        f"{market_trend_report}\n\n"
        f"{trend_report}"
    )
    log.info(market_trend_report)
    log.info(trend_report)


def position_monitor_loop():
    log.info(f"🩺 Position monitor loop started — checking open positions every {MONITOR_INTERVAL_SEC}s")
    while True:
        try:
            if state["connected"]:
                if should_force_square_off():
                    if state["positions"]:
                        force_square_off_all("EOD_SQUAREOFF")
                    _maybe_send_eod_summary()
                elif state["positions"]:
                    monitor_positions()
        except Exception as e:
            log.warning(f"Position monitor loop error: {e}")
        time.sleep(MONITOR_INTERVAL_SEC)


# ── Main Scan Loop ────────────────────────────────────────────────────────────
def scan_loop():
    log.info("🔍 Scan loop started (signal detection — 5-min candles)")
    log.info(f"   EMA_LENGTH={EMA_LENGTH} | MIN_WICK_PCT={MIN_WICK_PCT} | MAX_POSITIONS={MAX_POSITIONS}")
    log.info(f"   Trading window: {TRADING_START_TIME}–{NO_NEW_ENTRIES_AFTER} IST | Force square-off: {SQUARE_OFF_TIME} IST | Max hold: {MAX_HOLD_MINUTES} min")
    capital_source_desc = f"PAPER virtual pool ₹{PAPER_VIRTUAL_CAPITAL:,.0f}" if PAPER_TRADING else "LIVE from kite.margins()"
    log.info(f"   Capital sizing: {capital_source_desc} | Per-trade cap: {MAX_MARGIN_PER_TRADE_PCT}% of usable margin | Safety buffer: {MARGIN_SAFETY_BUFFER_PCT}%")
    log.info(f"   Cooldown after exit: {COOLDOWN_MINUTES} min | Daily SL-hit limit per symbol: {MAX_SL_HITS_PER_DAY} | Startup warm-up (analysis only): {WARMUP_SCANS} scan(s)")
    log.info(f"   Trailing SL: initial {INITIAL_SL_R}R → " + " → ".join(f"{s['trigger_r']}R⇒SL{s['sl_r']}R" for s in TRAIL_STAGES))
    for band in PRICE_BANDS:
        log.info(f"   Band ₹{band['name']}: max {band['max_positions']} positions | {', '.join(band['symbols'])}")

    while True:
        if not is_market_open():
            mins = minutes_to_open()
            log.info(f"Market closed. Opens in ~{mins} min. Sleeping 60 s…")
            safe_state_update({"scan_status": "CLOSED"})
            time.sleep(60)
            continue

        safe_state_update({"scan_status": "SCANNING", "last_scan": datetime.now(IST).isoformat()})

        if not state["connected"]:
            if not connect_kite():
                time.sleep(30)
                continue
            load_instruments()
            fetch_margins()   # prime the live margin cache right after connecting
            start_execution_engine_if_needed()

        with _state_lock:
            state["scan_count"] += 1
            current_scan_num = state["scan_count"]
        is_warmup = current_scan_num <= WARMUP_SCANS
        if is_warmup:
            log.info(
                f"🧪 Warm-up scan {current_scan_num}/{WARMUP_SCANS} — analyzing the watchlist only, "
                f"no trades will be placed this cycle."
            )

        now = datetime.now(IST)
        if now < _time_today(TRADING_START_TIME):
            log.info(
                f"⏱ Before {TRADING_START_TIME} IST — letting the market settle, no new entries yet. "
                f"Open positions ({len(state['positions'])}) continue to run normally."
            )
        elif not can_take_new_entries():
            log.info(
                f"⏱ Past {NO_NEW_ENTRIES_AFTER} IST — no new entries this cycle. "
                f"Open positions ({len(state['positions'])}) continue to run to SL/trail/square-off."
            )
        else:
            cap_hit_logged = False
            for symbol in WATCHLIST:
                if len(state["positions"]) >= MAX_POSITIONS:
                    if not cap_hit_logged:
                        log.info(
                            f"🛑 Max positions cap reached ({MAX_POSITIONS}/{MAX_POSITIONS}) — "
                            f"stopping scan for remaining symbols this cycle."
                        )
                        cap_hit_logged = True
                    break
                try:
                    scan_started = time.monotonic()
                    candles = get_candles(symbol, EMA_LENGTH + 60)
                    if not candles:
                        log.debug(f"{symbol}: no candle data returned — skipping evaluation this cycle.")
                        continue

                    trend_result = trend_detector.compute(symbol, candles)
                    record_trend_score(symbol, trend_result)

                    if TREND_FILTER_ENABLED:
                        if trend_result.state == "CHOPPY":
                            log_skip(
                                symbol,
                                f"Trend filter: CHOPPY (score {trend_result.score}/100) — "
                                f"reasons: {', '.join(trend_result.reasons[:3])}"
                            )
                            record_trend_skip(symbol)
                            continue
                        if trend_result.state == "NEUTRAL" and not ALLOW_NEUTRAL_TRADES:
                            log_skip(
                                symbol,
                                f"Trend filter: NEUTRAL (score {trend_result.score}/100) — "
                                f"skipping neutral unless ALLOW_NEUTRAL_TRADES=True"
                            )
                            record_trend_skip(symbol)
                            continue

                    signal = check_signal(symbol, candles)
                    if signal:
                        log.info(f"🎯 Signal: {signal['direction']} {symbol} | EMA={signal['ema']}")
                        record_trend_signal(symbol)

                        if is_warmup:
                            log_skip(
                                symbol,
                                f"Warm-up mode active (scan {current_scan_num}/{WARMUP_SCANS}) — "
                                f"signal noted, no trades placed this cycle"
                            )
                            continue

                        remaining = cooldown_remaining_min(symbol)
                        if remaining > 0:
                            log_skip(symbol, f"Cooldown {_fmt_remaining(remaining)} remaining")
                            continue

                        # NEW: daily per-symbol SL-hit circuit breaker — checked
                        # right alongside cooldown/band checks, before any entry
                        # attempt is made. Other symbols are completely unaffected.
                        if sl_hit_limit_reached(symbol):
                            log_skip(
                                symbol,
                                f"Daily SL-hit limit reached ({sl_hit_count_today(symbol)}/"
                                f"{MAX_SL_HITS_PER_DAY} today) — blocked for the rest of today, "
                                f"resumes fresh tomorrow (or on bot restart)"
                            )
                            continue

                        if not band_capacity_available(symbol):
                            band = band_for_symbol(symbol)
                            band_name = band["name"] if band else "n/a"
                            band_count = band_open_positions_count(band_name)
                            log_skip(
                                symbol,
                                f"Band ₹{band_name} limit reached ({band_count}/{band['max_positions']})"
                            )
                            continue

                        enter_trade(signal)
                        elapsed = time.monotonic() - scan_started
                        if elapsed > 5:
                            log.info(f"⏱️ {symbol}: full signal→entry pipeline took {elapsed:.1f}s")
                    else:
                        log.debug(f"No signal: {symbol}")
                except Exception as e:
                    log.warning(f"Scan error {symbol}: {e}")
                time.sleep(0.4)

        safe_state_update({"scan_status": "RUNNING"})
        log.info(f"Scan done | Positions: {len(state['positions'])}/{MAX_POSITIONS} | Watchlist: {len(WATCHLIST)} symbols | PnL: ₹{state['pnl_today']:.2f}")
        if state["positions"]:
            print_open_mtm()
        time.sleep(SCAN_INTERVAL_SEC)


# ── Dashboard State API ───────────────────────────────────────────────────────
def get_dashboard_state() -> dict:
    with _state_lock:
        total = state["wins"] + state["losses"]
        wr    = round(state["wins"] / total * 100, 1) if total else 0
        mtm   = _compute_open_mtm_breakdown_locked()
        trend_scores = dict(state.get("trend_scores", {}))
        trend_signals = dict(state.get("trend_signals_detected", {}))
        trend_skips = dict(state.get("trend_skips", {}))
        return {
            "connected":         state["connected"],
            "scan_status":       state["scan_status"],
            "last_scan":         state["last_scan"],
            "equity":            round(state["equity"], 2),
            "deployed":          round(state["deployed"], 2),
            "available_margin":  round(state["available_margin"], 2),
            "margin_source":     state["margin_source"],
            "max_margin_per_trade_pct": MAX_MARGIN_PER_TRADE_PCT,
            "pnl_today":         round(state["pnl_today"], 2),
            "gross_open_profit": mtm["gross_open_profit"],
            "gross_open_loss":   mtm["gross_open_loss"],
            "net_open_pnl":      mtm["net_open_pnl"],
            "win_rate":          wr,
            "total_trades":      total,
            "wins":              state["wins"],
            "losses":            state["losses"],
            "positions":         list(state["positions"].values()),
            "trades":            state["trades"][:50],
            "zones":             state["zones"],
            "sl_hit_counts":     dict(state["sl_hit_counts"]),
            "max_sl_hits_per_day": MAX_SL_HITS_PER_DAY,
            "time":              datetime.now(IST).strftime("%H:%M:%S IST"),
            "market_open":       is_market_open(),
            "paper_mode":        PAPER_TRADING,
            "no_new_entries_after": NO_NEW_ENTRIES_AFTER,
            "square_off_time":      SQUARE_OFF_TIME,
            "trend_filter_enabled": TREND_FILTER_ENABLED,
            "trend_scores":      trend_scores,
            "trend_signals_detected": trend_signals,
            "trend_skips":       trend_skips,
            "market_trend_report": generate_market_trend_report(),
        }


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 WickFill Auto-Trader v3 — Zerodha Kite")
    log.info(f"Mode           : {'📝 PAPER TRADING' if PAPER_TRADING else '💰 LIVE TRADING'}")
    log.info(f"Product        : MIS (intraday)")
    log.info(f"Order type     : MARKET")
    capital_source_desc = f"PAPER virtual pool ₹{PAPER_VIRTUAL_CAPITAL:,.0f} (real per-stock leverage still applied via order_margins)" if PAPER_TRADING else "LIVE from kite.margins() — no hardcoded capital/leverage"
    log.info(f"Capital sizing : {capital_source_desc}")
    log.info(f"Per-trade cap  : {MAX_MARGIN_PER_TRADE_PCT}% of usable margin (safety buffer {MARGIN_SAFETY_BUFFER_PCT}%)")
    log.info(f"EMA Length     : {EMA_LENGTH}")
    log.info(f"Min Wick %     : {MIN_WICK_PCT}")
    log.info(f"Risk:Reward    : 1:{RISK_REWARD}")
    log.info(f"Max Positions  : {MAX_POSITIONS} (" + " + ".join(f"{b['max_positions']} in ₹{b['name']}" for b in PRICE_BANDS) + ")")
    log.info(f"Trading start  : {TRADING_START_TIME} IST (market-open delay)")
    log.info(f"Entries cutoff : {NO_NEW_ENTRIES_AFTER} IST")
    log.info(f"Square-off at  : {SQUARE_OFF_TIME} IST")
    log.info(f"Max hold time  : {MAX_HOLD_MINUTES} min")
    log.info(f"Position check : every {MONITOR_INTERVAL_SEC}s (independent of the {SCAN_INTERVAL_SEC}s signal scan)")
    log.info(f"Reconciliation : every {RECONCILE_INTERVAL_SEC}s (bot state vs real Kite positions, live mode only)")
    log.info(f"Cooldown       : {COOLDOWN_MINUTES} min after exit")
    log.info(f"Daily SL limit : {MAX_SL_HITS_PER_DAY} stop-loss exits per symbol per day (resets on new day / bot restart)")
    log.info(f"Warm-up scans  : {WARMUP_SCANS} (analysis only, no trades)")
    log.info(f"Trailing SL    : initial {INITIAL_SL_R}R, ladder " + " → ".join(f"{s['trigger_r']}R⇒{s['sl_r']}R" for s in TRAIL_STAGES))
    log.info(f"Kite HTTP timeout: {KITE_REQUEST_TIMEOUT_SEC}s (prevents hung network calls from stalling the scan)")
    log.info(f"Trend filter   : {'ENABLED' if TREND_FILTER_ENABLED else 'DISABLED'} | Min trending: {TREND_MIN_SCORE_TRENDING} | Min neutral: {TREND_MIN_SCORE_NEUTRAL} | Allow neutral: {ALLOW_NEUTRAL_TRADES}")

    if not connect_kite():
        log.warning("Initial connection failed — will retry in scan loop")
        if TELEGRAM_NOTIFY_ERRORS:
            send_telegram("🚫 WickFill bot started but initial Kite connection FAILED — will keep retrying in the scan loop.")
    else:
        load_instruments()
        fetch_margins()
        start_execution_engine_if_needed()

    if TELEGRAM_NOTIFY_STARTUP:
        mode_tag = "📝 PAPER TRADING" if PAPER_TRADING else "💰 LIVE TRADING"
        trend_tag = f"Trend filter: {'ON' if TREND_FILTER_ENABLED else 'OFF'} (min trending {TREND_MIN_SCORE_TRENDING})"
        send_telegram(
            f"🚀 WickFill Auto-Trader started\n"
            f"Mode: {mode_tag}\n"
            f"Watchlist: {len(WATCHLIST)} symbols | Max positions: {MAX_POSITIONS}\n"
            f"Window: {TRADING_START_TIME}–{NO_NEW_ENTRIES_AFTER} IST | Square-off: {SQUARE_OFF_TIME} IST\n"
            f"Daily SL-hit limit per symbol: {MAX_SL_HITS_PER_DAY} (fresh count today)\n"
            f"{trend_tag}"
        )

    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()

    monitor_thread = threading.Thread(target=position_monitor_loop, daemon=True)
    monitor_thread.start()

    reconcile_thread = threading.Thread(target=reconcile_loop, daemon=True)
    reconcile_thread.start()

    _save_current_report()

    if DASHBOARD_ENABLED:
        from server import register_dashboard_state, run_server
        register_dashboard_state(get_dashboard_state)

        dashboard_thread = threading.Thread(target=run_server, daemon=True)
        dashboard_thread.start()
        log.info(f"🌐 Dashboard live at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/dashboard  (login: {os.getenv('DASHBOARD_USER', 'admin')})")
    else:
        log.info("🌐 Dashboard disabled (DASHBOARD_ENABLED=False) — relying on Telegram/logs for updates.")

    while True:
        time.sleep(60)