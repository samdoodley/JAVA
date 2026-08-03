"""
WickFill Auto-Trader Bot v4 — Zerodha Kite
Strategy: EMA 200 Filter + Wick Zones + Zone Fills + Trend-Score Ranked Entry

CHANGES IN THIS VERSION
------------------------
1. Dynamic NSE-wide universe scanner REMOVED (unchanged from v3). The bot
   trades only the fixed config.py WATCHLIST (PRICE_BANDS).
2. The old 4-tier TRENDING/STRONG_NEUTRAL/WEAK_NEUTRAL/CHOPPY gate has been
   replaced with a single-threshold ranked-entry gate:

     PHASE 1 — SCAN & SCORE: every symbol in WATCHLIST gets its smoothed
     trend score computed (ADX(14) + 200-EMA slope + price-distance from
     EMA, EMA-smoothed across scans via SCORE_SMOOTHING_ALPHA). Symbols
     scoring below MIN_TREND_SCORE_FOR_ENTRY (30 — tuned to screen out only
     genuinely choppy/range-bound stocks, not neutral ones) are ignored
     outright — the existing WickFill strategy is never evaluated for them.

     PHASE 2 — STRATEGY FILTER: symbols at/above the threshold still have
     to pass every existing strategy condition completely unmodified (EMA
     bias, wick-zone detection, zone-fill entry, 3% max-risk cap). The
     score never substitutes for or relaxes any of these checks.

     PHASE 3 — RANK & ENTER: symbols that pass everything are grouped by
     price band and ranked by trend score (highest first) within that
     band. The bot then enters top-ranked candidates first, up to each
     band's max_positions slot count (4 in ₹500-1000, 3 in ₹1000-2000, 2
     in ₹2000-4000) and the overall MAX_POSITIONS cap — while still
     honouring cooldown, the daily per-symbol SL-hit circuit breaker, and
     duplicate-position protection exactly as before.
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
    # ── Trend-score ranked entry gate ────────────────────────────────────
    TREND_SLOPE_LOOKBACK, MIN_TREND_SCORE_FOR_ENTRY, SCORE_SMOOTHING_ALPHA,
)
from reporting import save_trade_report

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
engine = ExecutionEngine()

# ── Hard network timeout for every Kite HTTP call ────────────────────────────
KITE_REQUEST_TIMEOUT_SEC = 10

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "positions":        {},
    "trades":           [],
    "zones":            {},
    "scan_status":       "IDLE",
    "connected":         False,
    "last_scan":         None,
    "equity":            0.0,
    "deployed":          0.0,
    "pnl_today":         0.0,
    "wins":              0,
    "losses":            0,
    "kite":              None,
    "scan_count":        0,
    "cooldowns":         {},
    "available_margin":  0.0,
    "margin_last_fetch":  None,
    "margin_source":     "unknown",
    "equity_initialized": False,
    "eod_summary_date_sent": None,
    "sl_hit_counts":      {},
    # ── Trend-score gate telemetry (per-symbol smoothed score + tier) ────
    "trend_scores":       {},   # symbol -> smoothed score (float)
    "trend_tiers":         {},  # symbol -> last classified tier (str)
}

_state_lock = threading.Lock()


def safe_state_update(updates: dict):
    with _state_lock:
        state.update(updates)


# ── Centralized "trade skipped/rejected before entry" logger ─────────────────
def log_skip(symbol: str, reason: str):
    log.info(f"❌ Skipping {symbol}: {reason}")


def _fmt_remaining(minutes: float) -> str:
    total_seconds = max(0, int(round(minutes * 60)))
    m, s = divmod(total_seconds, 60)
    return f"{m}m {s}s"


# ── Telegram Notifications ────────────────────────────────────────────────────
def send_telegram(message: str, silent: bool = False):
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
        kite = KiteConnect(api_key=API_KEY, timeout=KITE_REQUEST_TIMEOUT_SEC)

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

        if MANUAL_REQUEST_TOKEN:
            log.info("Using MANUAL_REQUEST_TOKEN…")
            data = kite.generate_session(MANUAL_REQUEST_TOKEN, api_secret=API_SECRET)
            kite.set_access_token(data["access_token"])
            safe_state_update({"kite": kite, "connected": True})
            log.info("✅ Connected via manual request_token")
            return True

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


# ── Price Bands (value-based position caps) ───────────────────────────────────
# Static WATCHLIST / PRICE_BANDS only — a symbol's band is decided by fixed
# membership in config.py's PRICE_BANDS, exactly as it was before the
# dynamic scanner existed.
def band_for_symbol(symbol: str, price: float | None = None) -> dict | None:
    for band in PRICE_BANDS:
        if symbol in band["symbols"]:
            return band
    return None


def band_open_positions_count(band_name: str) -> int:
    with _state_lock:
        return sum(1 for p in state["positions"].values() if p.get("band") == band_name)


def band_capacity_available(symbol: str, price: float | None = None) -> bool:
    band = band_for_symbol(symbol, price)
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


def _current_watchlist() -> list[str]:
    """The list of symbols the scan loop evaluates every cycle — always the
    fixed static WATCHLIST from config.py."""
    return WATCHLIST


def start_execution_engine_if_needed():
    """Starts the KiteTicker WebSocket if not already running and subscribes to the
    full static WATCHLIST (small, fixed symbol set — no subscription-cap
    concerns)."""
    if not USE_KITETICKER or engine.is_connected():
        return
    kite = state["kite"]
    if kite is None or not _instrument_cache:
        return
    token_map = {s: _instrument_cache[s] for s in WATCHLIST if s in _instrument_cache}
    engine.set_symbol_token_map(dict(_instrument_cache))
    engine.on_ticks_callback = _handle_ticks
    engine.on_order_update_callback = _handle_order_update
    access_token = getattr(kite, "access_token", None)
    if not access_token:
        log.warning("⚠️ Cannot start KiteTicker — no access_token found on the Kite session.")
        return
    engine.start(API_KEY, access_token)
    time.sleep(1.0)
    if token_map:
        engine.subscribe_symbols(list(token_map.keys()))


# ── Live Account Margin (replaces all hardcoded capital/leverage) ────────────
def fetch_margins() -> float:
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
            virtual_capital = state["equity"]

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
    available = fetch_margins()
    return available * (MAX_MARGIN_PER_TRADE_PCT / 100)


def get_available_capital() -> float:
    total_margin = fetch_margins()
    with _state_lock:
        return total_margin - state["deployed"]


def is_within_investment_limit(
    symbol: str, direction: str, entry: float, qty: int,
    known_margin: float | None = None,
) -> tuple[bool, float]:
    total_capital = fetch_margins()
    if known_margin is not None and known_margin > 0:
        required = known_margin
    else:
        required = _order_margin_required(symbol, direction, qty)
        if required is None or required <= 0:
            required = entry * qty
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


# ── ADX (Wilder's, 14-period default) ─────────────────────────────────────────
def _calc_adx(candles: list[dict], period: int = 14) -> float:
    n = len(candles)
    if n < period + 1:
        return 0.0
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    tr_list: list[float] = [candles[0]["high"] - candles[0]["low"]]

    for i in range(1, n):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        prev_close = candles[i - 1]["close"]
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - prev_close),
            abs(candles[i]["low"] - prev_close),
        )
        tr_list.append(tr)

    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])
    smoothed_tr = sum(tr_list[1:period + 1])

    dx_list: list[float] = [0.0] * period
    for i in range(period, n):
        if smoothed_tr == 0:
            dx = 0.0
        else:
            plus_di = 100.0 * smoothed_plus / smoothed_tr
            minus_di = 100.0 * smoothed_minus / smoothed_tr
            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
        dx_list.append(dx)
        if i < n - 1:
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[i + 1]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i + 1]
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i + 1]

    adx_vals: list[float] = [sum(dx_list[:period]) / period]
    for dx in dx_list[period:]:
        adx_vals.append((adx_vals[-1] * (period - 1) + dx) / period)

    return adx_vals[-1] if adx_vals else 0.0


# ── 4-Tier Trend-Score Gate ───────────────────────────────────────────────────
def _calc_raw_trend_score(candles: list[dict], ema_vals: list[float | None]) -> float:
    """
    Combines three components into a single 0-100 trend-strength score:
      - ADX(14): how strongly the market is trending, regardless of direction
        (contributes up to 60 points)
      - 200-EMA slope over TREND_SLOPE_LOOKBACK candles: how fast the EMA
        itself is moving (contributes up to 25 points)
      - Price distance from the 200-EMA, as a %: how extended/committed the
        current move is (contributes up to 15 points)
    """
    closes = [c["close"] for c in candles]
    valid_ema = [v for v in ema_vals if v is not None]
    if len(valid_ema) < TREND_SLOPE_LOOKBACK + 1 or not closes:
        return 0.0

    adx = _calc_adx(candles, 14)
    adx_component = min(adx, 60.0)

    slope_window = valid_ema[-TREND_SLOPE_LOOKBACK:]
    base = slope_window[0]
    ema_slope_pct = ((slope_window[-1] - base) / base * 100) if base else 0.0
    slope_component = min(abs(ema_slope_pct) * 20.0, 25.0)

    ema_now = valid_ema[-1]
    price_dist_pct = (abs(closes[-1] - ema_now) / ema_now * 100) if ema_now else 0.0
    dist_component = min(price_dist_pct * 10.0, 15.0)

    return round(min(adx_component + slope_component + dist_component, 100.0), 2)


def _smooth_trend_score(symbol: str, raw_score: float) -> float:
    """EMA-smooths the raw trend score per symbol across scans using
    SCORE_SMOOTHING_ALPHA, so one noisy candle can't flip a symbol's tier
    back and forth every cycle."""
    with _state_lock:
        prev = state["trend_scores"].get(symbol)
        smoothed = raw_score if prev is None else (
            SCORE_SMOOTHING_ALPHA * raw_score + (1 - SCORE_SMOOTHING_ALPHA) * prev
        )
        state["trend_scores"][symbol] = smoothed
    return round(smoothed, 2)


def classify_trend_tier(score: float) -> str:
    return "TRADEABLE" if score >= MIN_TREND_SCORE_FOR_ENTRY else "CHOPPY"


def trend_gate_allows_entry(symbol: str, candles: list[dict], ema_vals: list[float | None]) -> tuple[bool, str, float]:
    """
    Returns (allowed, tier, smoothed_score).
      score >= MIN_TREND_SCORE_FOR_ENTRY -> "TRADEABLE": covers both neutral
        and trending stocks — the unmodified WickFill strategy checks below
        run as normal for either.
      score <  MIN_TREND_SCORE_FOR_ENTRY -> "CHOPPY": symbol is skipped
        before any zone-fill logic runs at all.

    Passing this gate does not guarantee a trade — the caller still has to
    pass every existing strategy filter (EMA bias, wick %, zone fill, 3%
    risk cap) unmodified. It only decides who's eligible to be evaluated,
    and later (in the scan loop) who gets ranked highest for a limited
    number of band slots.
    """
    raw_score = _calc_raw_trend_score(candles, ema_vals)
    score = _smooth_trend_score(symbol, raw_score)
    tier = classify_trend_tier(score)

    with _state_lock:
        state["trend_tiers"][symbol] = tier

    return tier == "TRADEABLE", tier, score


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
    """
    Core EMA200 + wick-zone strategy, now gated by a single trend-score
    threshold before any zone-fill logic runs. The threshold is set low on
    purpose — it filters out only genuinely choppy stocks, not neutral ones:

      1. Need enough candles for the 200-EMA.
      2. Compute the smoothed trend score for this symbol.
         - score < MIN_TREND_SCORE_FOR_ENTRY -> CHOPPY, no signal, skip the
           rest of the checks entirely (symbol is never zone-checked).
         - score >= MIN_TREND_SCORE_FOR_ENTRY -> TRADEABLE (covers both
           neutral and trending stocks) -> continue to the unmodified
           strategy checks below.
      3. From here on the strategy logic is unchanged: EMA bias, wick-zone
         detection, zone-fill entry, 3% max-risk cap, TP at RISK_REWARD.
    """
    needed = EMA_LENGTH + max(10, TREND_SLOPE_LOOKBACK + 1)
    if len(candles) < needed:
        log.debug(f"{symbol}: only {len(candles)} candles, need {needed}")
        return None

    closes   = [c["close"] for c in candles]
    ema_vals = calc_ema(closes, EMA_LENGTH)
    ema_now  = ema_vals[-1]

    if ema_now is None:
        return None

    allowed, tier, score = trend_gate_allows_entry(symbol, candles, ema_vals)
    if not allowed:
        log.debug(f"{symbol}: trend gate blocked entry — tier={tier} score={score}")
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
                    "trend_tier":  tier,
                    "trend_score": score,
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
                    "trend_tier":  tier,
                    "trend_score": score,
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
    if entry <= 0:
        return 0, 0.0

    if QTY_MODE == "capital":
        per_trade_capital = max_capital_per_trade()
        if per_trade_capital <= 0:
            return 0, 0.0

        naive_qty = max(1, int(per_trade_capital / entry))

        required = _order_margin_required(symbol, direction, naive_qty)
        if required is None or required <= 0:
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

        max_qty_by_exposure_cap = max(1, int((per_trade_capital * MAX_TRADE_LEVERAGE_MULTIPLIER) / entry))
        final_qty = min(scaled_qty, max_qty_by_exposure_cap)

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
        return qty, entry * qty

    qty = max(1, int(QTY_FIXED_SIZE))
    return qty, entry * qty


# ── Live Order-Margin Verification / Auto-Shrink ─────────────────────────────
def verify_and_shrink_order_qty(symbol: str, direction: str, qty: int, price: float) -> int:
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
    band = band_for_symbol(symbol, entry_price)

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
                    "margin_used": round(margin_used, 2),
                    "open_time":  signal["time"],
                    "trend_tier": signal.get("trend_tier"),
                    "trend_score": signal.get("trend_score"),
                    "pnl":        0.0,
                    "status":     "OPEN",
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
        f"1R={risk:.2f} | Initial SL {initial_sl} ({INITIAL_SL_R}R) | Ref target {signal['tp']} | Qty {qty} | "
        f"Trend {signal.get('trend_tier')} ({signal.get('trend_score')})"
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
            f"1R = ₹{risk:.2f} | Value ₹{trade_value:,.2f}\n"
            f"Trend {signal.get('trend_tier')} ({signal.get('trend_score')})"
        )
    _save_current_report()


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
        }

        if ENABLE_SLIPPAGE_MONITOR and reason in ("SL_HIT", "TRAIL_SL_HIT"):
            configured_sl = pos["sl"]
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
    band = band_for_symbol(symbol, entry_price)

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
        margin_used = filled_price * filled_qty

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
            "margin_used":        round(margin_used, 2),
            "order_id":           oid,
            "broker_sl_order_id": broker_sl_order_id,
            "open_time":          signal["time"],
            "trend_tier":         signal.get("trend_tier"),
            "trend_score":        signal.get("trend_score"),
            "pnl":                0.0,
            "status":             "OPEN",
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
    if not engine.try_begin_exit(symbol):
        return
    try:
        with _state_lock:
            pos = state["positions"].get(symbol)
        if not pos:
            return

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

    with _state_lock:
        if symbol in state["positions"]:
            pnl = (price - pos["entry"]) * pos["qty"] if direction == "BUY" else (pos["entry"] - price) * pos["qty"]
            state["positions"][symbol]["pnl"] = round(pnl, 2)
    _save_current_report()


def _handle_ticks(ticks: list):
    try:
        with _state_lock:
            open_symbols = set(state["positions"].keys())
        for symbol, price in ticks:
            if symbol in open_symbols:
                _process_tick_for_position(symbol, price)
    except Exception as e:
        log.warning(f"_handle_ticks error: {e}")


def _handle_order_update(data: dict):
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
            return

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
    for symbol in _current_watchlist():
        try:
            candles = get_candles(symbol, 100)
            if not candles:
                continue
            zones = detect_wick_zones(candles)
            bull  = [z for z in zones if z["type"] == "BULL"]
            bear  = [z for z in zones if z["type"] == "BEAR"]
            if bull or bear:
                with _state_lock:
                    state["zones"][symbol] = {
                        "bull": len(bull), "bear": len(bear),
                        "last_type": zones[-1]["type"] if zones else "NONE",
                    }
        except Exception as e:
            log.warning(f"Zone update error {symbol}: {e}")
        time.sleep(0.4)


# ── Fast Position Monitor Loop (independent of the signal scan cadence) ──────
def _maybe_send_eod_summary():
    if not TELEGRAM_NOTIFY_EOD_SUMMARY:
        return
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    with _state_lock:
        if state.get("eod_summary_date_sent") == today_str:
            return
        state["eod_summary_date_sent"] = today_str
        wins, losses, pnl_today = state["wins"], state["losses"], state["pnl_today"]
    total = wins + losses
    wr = round(wins / total * 100, 1) if total else 0
    mode_tag = "📝 PAPER" if PAPER_TRADING else "💰 LIVE"
    send_telegram(
        f"📊 EOD SUMMARY ({today_str}) — {mode_tag}\n"
        f"Trades: {total} | Wins: {wins} | Losses: {losses} | Win rate: {wr}%\n"
        f"PnL today: ₹{pnl_today:,.2f}"
    )


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
def _run_static_watchlist_scan(current_scan_num: int, is_warmup: bool):
    """
    Two-phase scan over the fixed config.py WATCHLIST:

      PHASE 1 — COLLECT: every symbol is evaluated through check_signal()
      (EMA200 + wick-zone + zone-fill), which itself first requires the
      symbol's smoothed trend score to be >= MIN_TREND_SCORE_FOR_ENTRY
      before running any of the existing strategy checks. Only symbols
      that pass EVERY existing strategy condition unmodified end up as
      candidates — the trend score never substitutes for or relaxes any
      of those checks, it only decides who gets checked at all.

      PHASE 2 — RANK & ENTER: candidates are grouped by price band and
      sorted by trend score (highest first) within each band. The bot
      then walks each band's ranked list and enters trades up to that
      band's max_positions slot count (existing open positions in the
      band count against that cap), still honouring cooldown, the daily
      per-symbol SL-hit circuit breaker, duplicate-position protection,
      and the overall MAX_POSITIONS cap — exactly as before.
    """
    candidates: list[dict] = []

    for symbol in WATCHLIST:
        try:
            scan_started = time.monotonic()
            candles = get_candles(symbol, EMA_LENGTH + 60)
            if not candles:
                log.debug(f"{symbol}: no candle data returned — skipping evaluation this cycle.")
                continue
            signal = check_signal(symbol, candles)
            if signal:
                log.info(
                    f"🎯 Signal: {signal['direction']} {symbol} | EMA={signal['ema']} | "
                    f"Trend score {signal['trend_score']} (>= {MIN_TREND_SCORE_FOR_ENTRY})"
                )
                band = band_for_symbol(symbol, signal["entry"])
                signal["band_name"] = band["name"] if band else None
                candidates.append(signal)
                elapsed = time.monotonic() - scan_started
                if elapsed > 5:
                    log.info(f"⏱️ {symbol}: candle fetch + signal check took {elapsed:.1f}s")
            else:
                log.debug(f"No signal: {symbol}")
        except Exception as e:
            log.warning(f"Scan error {symbol}: {e}")
        time.sleep(0.4)

    if not candidates:
        log.info(
            f"📭 No qualifying candidates this cycle (trend score < "
            f"{MIN_TREND_SCORE_FOR_ENTRY}, or existing strategy filters not met)."
        )
        return

    if is_warmup:
        for signal in candidates:
            log_skip(
                signal["symbol"],
                f"Warm-up mode active (scan {current_scan_num}/{WARMUP_SCANS}) — "
                f"signal noted, no trades placed this cycle"
            )
        return

    # Group candidates by band, then rank each band's list by trend score
    # (highest first) so the strongest-trending setups get first claim on
    # that band's limited slots.
    by_band: dict[str | None, list[dict]] = {}
    for signal in candidates:
        by_band.setdefault(signal["band_name"], []).append(signal)
    for band_candidates in by_band.values():
        band_candidates.sort(key=lambda s: s["trend_score"], reverse=True)

    band_lookup = {b["name"]: b for b in PRICE_BANDS}

    for band_name, band_candidates in by_band.items():
        band = band_lookup.get(band_name)
        band_cap = band["max_positions"] if band else None
        ranked_desc = ", ".join(f"{s['symbol']}({s['trend_score']})" for s in band_candidates)
        log.info(f"📊 Band ₹{band_name or 'n/a'}: {len(band_candidates)} candidate(s) ranked by trend score — {ranked_desc}")

        for signal in band_candidates:
            symbol = signal["symbol"]

            if len(state["positions"]) >= MAX_POSITIONS:
                log.info(
                    f"🛑 Max positions cap reached ({MAX_POSITIONS}/{MAX_POSITIONS}) — "
                    f"stopping entries for the rest of this cycle."
                )
                return

            remaining = cooldown_remaining_min(symbol)
            if remaining > 0:
                log_skip(symbol, f"Cooldown {_fmt_remaining(remaining)} remaining")
                continue

            if sl_hit_limit_reached(symbol):
                log_skip(
                    symbol,
                    f"Daily SL-hit limit reached ({sl_hit_count_today(symbol)}/"
                    f"{MAX_SL_HITS_PER_DAY} today) — blocked for the rest of today, "
                    f"resumes fresh tomorrow (or on bot restart)"
                )
                continue

            if not band_capacity_available(symbol, signal["entry"]):
                band_count = band_open_positions_count(band_name) if band_name else 0
                log_skip(
                    symbol,
                    f"Band ₹{band_name or 'n/a'} limit reached ({band_count}/{band_cap})"
                )
                continue

            # enter_trade() itself still enforces duplicate-position
            # protection and re-checks MAX_POSITIONS/band caps atomically
            # under the state lock right before recording a position.
            enter_trade(signal)


def scan_loop():
    log.info("🔍 Scan loop started (signal detection — 5-min candles)")
    log.info(f"   EMA_LENGTH={EMA_LENGTH} | MIN_WICK_PCT={MIN_WICK_PCT} | MAX_POSITIONS={MAX_POSITIONS}")
    log.info(f"   Trading window: {TRADING_START_TIME}–{NO_NEW_ENTRIES_AFTER} IST | Force square-off: {SQUARE_OFF_TIME} IST | Max hold: {MAX_HOLD_MINUTES} min")
    capital_source_desc = f"PAPER virtual pool ₹{PAPER_VIRTUAL_CAPITAL:,.0f}" if PAPER_TRADING else "LIVE from kite.margins()"
    log.info(f"   Capital sizing: {capital_source_desc} | Per-trade cap: {MAX_MARGIN_PER_TRADE_PCT}% of usable margin | Safety buffer: {MARGIN_SAFETY_BUFFER_PCT}%")
    log.info(f"   Cooldown after exit: {COOLDOWN_MINUTES} min | Daily SL-hit limit per symbol: {MAX_SL_HITS_PER_DAY} | Startup warm-up (analysis only): {WARMUP_SCANS} scan(s)")
    log.info(f"   Trailing SL: initial {INITIAL_SL_R}R → " + " → ".join(f"{s['trigger_r']}R⇒SL{s['sl_r']}R" for s in TRAIL_STAGES))
    log.info(
        f"   Trend-score gate: avoid CHOPPY only — tradeable (neutral + trending) >= "
        f"{MIN_TREND_SCORE_FOR_ENTRY} (smoothing alpha {SCORE_SMOOTHING_ALPHA}) — tradeable "
        f"candidates that pass the full unmodified strategy are ranked by score within each "
        f"band and entered highest-first, up to that band's max_positions"
    )
    log.info(f"   Universe mode: 📌 static WATCHLIST ({len(WATCHLIST)} symbols) — dynamic NSE-wide scanner removed")
    for band in PRICE_BANDS:
        log.info(f"   Band ₹{band['name']}: max {band['max_positions']} positions | {', '.join(band['symbols'])}")

    scan_interval = SCAN_INTERVAL_SEC

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
            fetch_margins()
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
            _run_static_watchlist_scan(current_scan_num, is_warmup)

        safe_state_update({"scan_status": "RUNNING"})
        log.info(f"Scan done | Positions: {len(state['positions'])}/{MAX_POSITIONS} | Watchlist: {len(WATCHLIST)} symbols | PnL: ₹{state['pnl_today']:.2f}")
        if state["positions"]:
            print_open_mtm()
        time.sleep(scan_interval)


# ── Dashboard State API ───────────────────────────────────────────────────────
def get_dashboard_state() -> dict:
    with _state_lock:
        total = state["wins"] + state["losses"]
        wr    = round(state["wins"] / total * 100, 1) if total else 0
        mtm   = _compute_open_mtm_breakdown_locked()
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
            "watchlist_size":       len(WATCHLIST),
            "trend_scores":         dict(state["trend_scores"]),
            "trend_tiers":          dict(state["trend_tiers"]),
            "min_trend_score_for_entry": MIN_TREND_SCORE_FOR_ENTRY,
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
    log.info(f"Trend gate     : avoid CHOPPY only, tradeable (neutral+trending) >= {MIN_TREND_SCORE_FOR_ENTRY}, ranked by score within each band")
    log.info(f"Universe mode  : 📌 static WATCHLIST ({len(WATCHLIST)} symbols) — dynamic NSE scanner removed")
    log.info(f"Max Positions  : {MAX_POSITIONS} (" + " + ".join(f"{b['max_positions']} in ₹{b['name']}" for b in PRICE_BANDS) + ")")
    log.info(f"Trading start  : {TRADING_START_TIME} IST (market-open delay)")
    log.info(f"Entries cutoff : {NO_NEW_ENTRIES_AFTER} IST")
    log.info(f"Square-off at  : {SQUARE_OFF_TIME} IST")
    log.info(f"Max hold time  : {MAX_HOLD_MINUTES} min")
    log.info(f"Position check : every {MONITOR_INTERVAL_SEC}s (independent of the signal scan)")
    log.info(f"Reconciliation : every {RECONCILE_INTERVAL_SEC}s (bot state vs real Kite positions, live mode only)")
    log.info(f"Cooldown       : {COOLDOWN_MINUTES} min after exit")
    log.info(f"Daily SL limit : {MAX_SL_HITS_PER_DAY} stop-loss exits per symbol per day (resets on new day / bot restart)")
    log.info(f"Warm-up scans  : {WARMUP_SCANS} (analysis only, no trades)")
    log.info(f"Trailing SL    : initial {INITIAL_SL_R}R, ladder " + " → ".join(f"{s['trigger_r']}R⇒{s['sl_r']}R" for s in TRAIL_STAGES))
    log.info(f"Kite HTTP timeout: {KITE_REQUEST_TIMEOUT_SEC}s (prevents hung network calls from stalling the scan)")

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
        send_telegram(
            f"🚀 WickFill Auto-Trader started\n"
            f"Mode: {mode_tag}\n"
            f"Universe: {len(WATCHLIST)} symbols (static watchlist) | Max positions: {MAX_POSITIONS}\n"
            f"Trend gate: avoid CHOPPY only, tradeable >= {MIN_TREND_SCORE_FOR_ENTRY}, ranked by score per band\n"
            f"Window: {TRADING_START_TIME}–{NO_NEW_ENTRIES_AFTER} IST | Square-off: {SQUARE_OFF_TIME} IST\n"
            f"Daily SL-hit limit per symbol: {MAX_SL_HITS_PER_DAY} (fresh count today)"
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