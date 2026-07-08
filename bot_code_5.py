"""
WickFill Auto-Trader Bot v2 — Zerodha Kite
Strategy: EMA 200 Filter + Wick Zones + Zone Fills
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

# ── Import every config value explicitly (no wildcard) ────────────────────────
from config import (
    API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET,
    MANUAL_REQUEST_TOKEN, ACCESS_TOKEN,
    PAPER_TRADING,
    STARTING_CAPITAL, CAPITAL_PER_TRADE, RISK_PER_TRADE_PCT,
    POSITION_QTY_MULTIPLIER,
    EMA_LENGTH, MIN_WICK_PCT, RISK_REWARD, MAX_POSITIONS,
    SCAN_INTERVAL_SEC,
    DASHBOARD_HOST, DASHBOARD_PORT,
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

# ── Nifty 50 scan universe ─────────────────────────────────────────────────
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL", "BRITANNIA",
    "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HCLTECH", "HDFC", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA",
    "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "UPL", "ULTRACEMCO", "VEDL", "WIPRO",
]

# Keep a backward-compatible alias for older code paths
WATCHLIST = NIFTY50

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "positions":   {},
    "trades":      [],
    "zones":       {},
    "scan_status": "IDLE",
    "connected":   False,
    "last_scan":   None,
    "equity":      STARTING_CAPITAL,
    "deployed":    0.0,
    "pnl_today":   0.0,
    "wins":        0,
    "losses":      0,
    "kite":        None,
}

_state_lock = threading.Lock()


def safe_state_update(updates: dict):
    with _state_lock:
        state.update(updates)


# ── Zerodha Kite Login ────────────────────────────────────────────────────────
def connect_kite() -> bool:
    if not API_KEY or not API_SECRET:
        log.error(
            "Missing Kite credentials: API_KEY and API_SECRET are required. "
            "Set them in .env before running the bot."
        )
        return False

    try:
        kite = KiteConnect(api_key=API_KEY)

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

        r = session.post("https://kite.zerodha.com/api/login", data={
            "user_id": USER_ID,
            "password": PASSWORD,
        })
        log.debug("Login response status: %s", r.status_code)
        log.debug("Login response body: %s", r.text[:400])
        r.raise_for_status()
        login_body = r.json()
        if not login_body or "data" not in login_body or "request_id" not in login_body["data"]:
            log.error("Kite login response missing request_id: %s", login_body)
            return False
        request_id = login_body["data"]["request_id"]

        r = session.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": totp_val,
            "twofa_type": "totp",
        })
        log.debug("2FA response status: %s", r.status_code)
        log.debug("2FA response body: %s", r.text[:400])
        r.raise_for_status()

        login_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
        r = session.get(login_url, allow_redirects=True)
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
        return False


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


# ── Instrument Token Cache ────────────────────────────────────────────────────
_instrument_cache: dict[str, int] = {}


def load_instruments():
    global _instrument_cache
    kite = state["kite"]
    if not kite or _instrument_cache:
        return
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            _instrument_cache[inst["tradingsymbol"]] = inst["instrument_token"]
        log.info(f"✅ Loaded {len(_instrument_cache)} NSE instruments")
    except Exception as e:
        log.error(f"Instrument load error: {e}")


def get_instrument_token(symbol: str) -> int | None:
    if not _instrument_cache:
        load_instruments()
    return _instrument_cache.get(symbol)


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
        from_dt = to_dt - timedelta(days=10)   # extra days to guarantee 200+ candles
        raw = kite.historical_data(
            instrument_token=token,
            from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            interval="5minute",
            continuous=False,
            oi=False,
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
    """
    EMA 200 filter → wick zone detection → zone fill entry
    SL = zone boundary | TP = 2× risk (1:2 RR) | 5-min candles
    """
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

    # Build zones from established history (not last 5 candles)
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


# ── Position Sizing ───────────────────────────────────────────────────────────
def calc_qty(entry: float, sl: float) -> int:
    risk_amount    = CAPITAL_PER_TRADE * RISK_PER_TRADE_PCT / 100
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0:
        return 0
    base_qty = max(1, int(risk_amount / risk_per_share))
    return max(1, base_qty * POSITION_QTY_MULTIPLIER)


# ── Paper Trading ─────────────────────────────────────────────────────────────
def _save_current_report():
    with _state_lock:
        save_trade_report(state)


def paper_enter(signal: dict):
    symbol = signal["symbol"]
    qty    = calc_qty(signal["entry"], signal["sl"])
    with _state_lock:
        if len(state["positions"]) >= MAX_POSITIONS:
            return
        if symbol in state["positions"]:
            return
        state["positions"][symbol] = {
            "symbol":    symbol,
            "direction": signal["direction"],
            "entry":     signal["entry"],
            "sl":        signal["sl"],
            "tp":        signal["tp"],
            "qty":       qty,
            "open_time": signal["time"],
            "pnl":       0.0,
            "status":    "OPEN",
        }
        state["deployed"] += signal["entry"] * qty
    log.info(f"📈 PAPER ENTER {signal['direction']} {symbol} @ {signal['entry']} | SL {signal['sl']} | TP {signal['tp']} | Qty {qty}")
    _save_current_report()


def paper_exit(symbol: str, price: float, reason: str):
    with _state_lock:
        pos = state["positions"].get(symbol)
        if not pos:
            return
        pnl    = (price - pos["entry"]) * pos["qty"] if pos["direction"] == "BUY" else (pos["entry"] - price) * pos["qty"]
        result = "WIN" if pnl > 0 else "LOSS"
        state["wins" if result == "WIN" else "losses"] += 1
        state["pnl_today"] += pnl
        state["equity"]    += pnl
        state["deployed"]  -= pos["entry"] * pos["qty"]
        state["trades"].insert(0, {**pos, "exit": round(price, 2), "pnl": round(pnl, 2),
                                   "result": result, "reason": reason,
                                   "close_time": datetime.now(IST).isoformat()})
        del state["positions"][symbol]
    log.info(f"📉 PAPER EXIT {symbol} @ {price} | PnL ₹{pnl:.2f} | {reason}")
    _save_current_report()


# ── Live Trading ──────────────────────────────────────────────────────────────
def live_enter(signal: dict):
    kite   = state["kite"]
    symbol = signal["symbol"]
    qty    = calc_qty(signal["entry"], signal["sl"])
    try:
        txn = kite.TRANSACTION_TYPE_BUY if signal["direction"] == "BUY" else kite.TRANSACTION_TYPE_SELL
        oid = kite.place_order(
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
            transaction_type=txn, quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS,
            validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
        )
        log.info(f"✅ LIVE ORDER {signal['direction']} {symbol} x{qty} | order_id={oid}")
        with _state_lock:
            state["positions"][symbol] = {
                "symbol": symbol, "direction": signal["direction"],
                "entry":  signal["entry"], "sl": signal["sl"], "tp": signal["tp"],
                "qty":    qty, "order_id": oid,
                "open_time": signal["time"], "pnl": 0.0, "status": "OPEN",
            }
            state["deployed"] += signal["entry"] * qty
    except Exception as e:
        log.error(f"Live order error {symbol}: {e}")


def live_exit(symbol: str, reason: str):
    kite = state["kite"]
    pos  = state["positions"].get(symbol)
    if not pos:
        return
    try:
        txn = kite.TRANSACTION_TYPE_SELL if pos["direction"] == "BUY" else kite.TRANSACTION_TYPE_BUY
        kite.place_order(
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NSE,
            transaction_type=txn, quantity=pos["qty"],
            order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_MIS,
            validity=kite.VALIDITY_DAY, variety=kite.VARIETY_REGULAR,
        )
        log.info(f"✅ LIVE EXIT {symbol} | {reason}")
    except Exception as e:
        log.error(f"Live exit error {symbol}: {e}")


def enter_trade(signal):
    paper_enter(signal) if PAPER_TRADING else live_enter(signal)


def exit_trade(symbol, price, reason):
    paper_exit(symbol, price, reason) if PAPER_TRADING else live_exit(symbol, reason)


# ── Position Monitor ──────────────────────────────────────────────────────────
def monitor_positions():
    for symbol, pos in list(state["positions"].items()):
        try:
            candles = get_candles(symbol, 3)
            if not candles:
                continue
            price = candles[-1]["close"]
            if pos["direction"] == "BUY":
                if price <= pos["sl"]:
                    exit_trade(symbol, pos["sl"], "SL_HIT")
                elif price >= pos["tp"]:
                    exit_trade(symbol, pos["tp"], "TP_HIT")
                else:
                    with _state_lock:
                        if symbol in state["positions"]:
                            state["positions"][symbol]["pnl"] = round((price - pos["entry"]) * pos["qty"], 2)
                    _save_current_report()
            else:
                if price >= pos["sl"]:
                    exit_trade(symbol, pos["sl"], "SL_HIT")
                elif price <= pos["tp"]:
                    exit_trade(symbol, pos["tp"], "TP_HIT")
                else:
                    with _state_lock:
                        if symbol in state["positions"]:
                            state["positions"][symbol]["pnl"] = round((pos["entry"] - price) * pos["qty"], 2)
                    _save_current_report()
        except Exception as e:
            log.warning(f"Monitor error {symbol}: {e}")


# ── Zone Tracker ──────────────────────────────────────────────────────────────
def update_zones():
    zone_summary = {}
    for symbol in NIFTY50:
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


# ── Main Scan Loop ────────────────────────────────────────────────────────────
def scan_loop():
    log.info("🔍 Scan loop started")
    log.info(f"   EMA_LENGTH={EMA_LENGTH} | MIN_WICK_PCT={MIN_WICK_PCT} | RISK_REWARD={RISK_REWARD} | MAX_POSITIONS={MAX_POSITIONS}")

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
            load_instruments()   # load tokens right after connect

        monitor_positions()

        for symbol in WATCHLIST:
            if len(state["positions"]) >= MAX_POSITIONS:
                break
            try:
                candles = get_candles(symbol, EMA_LENGTH + 60)
                if not candles:
                    continue
                signal = check_signal(symbol, candles)
                if signal:
                    log.info(f"🎯 Signal: {signal['direction']} {symbol} | EMA={signal['ema']}")
                    enter_trade(signal)
                else:
                    log.debug(f"No signal: {symbol}")
            except Exception as e:
                log.warning(f"Scan error {symbol}: {e}")
            time.sleep(0.4)

        safe_state_update({"scan_status": "RUNNING"})
        log.info(f"Scan done | Positions: {len(state['positions'])}/{MAX_POSITIONS} | Watchlist: {len(WATCHLIST)} symbols | PnL: ₹{state['pnl_today']:.2f}")
        time.sleep(SCAN_INTERVAL_SEC)


# ── Dashboard State API ───────────────────────────────────────────────────────
def get_dashboard_state() -> dict:
    with _state_lock:
        total = state["wins"] + state["losses"]
        wr    = round(state["wins"] / total * 100, 1) if total else 0
        return {
            "connected":    state["connected"],
            "scan_status":  state["scan_status"],
            "last_scan":    state["last_scan"],
            "equity":       round(state["equity"], 2),
            "deployed":     round(state["deployed"], 2),
            "pnl_today":    round(state["pnl_today"], 2),
            "win_rate":     wr,
            "total_trades": total,
            "wins":         state["wins"],
            "losses":       state["losses"],
            "positions":    list(state["positions"].values()),
            "trades":       state["trades"][:50],
            "zones":        state["zones"],
            "time":         datetime.now(IST).strftime("%H:%M:%S IST"),
            "market_open":  is_market_open(),
            "paper_mode":   PAPER_TRADING,
        }


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 WickFill Auto-Trader v2 — Zerodha Kite")
    log.info(f"Mode           : {'📝 PAPER TRADING' if PAPER_TRADING else '💰 LIVE TRADING'}")
    log.info(f"Capital/trade  : ₹{CAPITAL_PER_TRADE:,}")
    log.info(f"EMA Length     : {EMA_LENGTH}")
    log.info(f"Min Wick %     : {MIN_WICK_PCT}")
    log.info(f"Risk:Reward    : 1:{RISK_REWARD}")
    log.info(f"Max Positions  : {MAX_POSITIONS}")

    if not connect_kite():
        log.warning("Initial connection failed — will retry in scan loop")
    else:
        load_instruments()

    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()

    _save_current_report()
    from server import register_dashboard_state
    register_dashboard_state(get_dashboard_state)

    # The Flask app is served by Gunicorn/Wsgi in production.
    # Do not start another development server from the bot process.
    while True:
        time.sleep(60)