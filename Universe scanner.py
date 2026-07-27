"""
universe_scanner.py — Dynamic NSE Universe Scanner for WickFill
================================================================
Replaces the static WATCHLIST / PRICE_BANDS symbol source with a
continuously-rebuilt universe:

    Entire NSE Equity Universe
        -> Price Band Filter   (LTP-based, dynamic membership)
        -> Liquidity Filter    (avg volume / avg traded value / optional
                                 ASM-GSM exclude list)
        -> Trend Filter        (NEW classifier, see note below)
        -> Select Best Candidates (per-band top-N, TRENDING first,
                                    NEUTRAL fills remaining slots,
                                    CHOPPY only as last resort)
        -> feeds bot.py's existing check_signal()/calc_qty()/entry/exit
           pipeline UNCHANGED.

WHAT THIS FILE DOES NOT TOUCH
-----------------------------
It never imports or calls check_signal, detect_wick_zones, calc_ema,
calc_qty, is_within_investment_limit, compute_trail_sl_r,
_process_tick_for_position, paper_enter/exit, live_enter/exit, or anything
in execution.py. Its ONLY output is:

    {
        "500-1000":  [symbol, symbol, ...],   # <= candidate_count
        "1000-2000": [symbol, symbol, ...],
        "2000-4000": [symbol, symbol, ...],
    }

— the exact shape your PRICE_BAND_500_1000 / _1000_2000 / _2000_4000 lists
already have. bot.py's scan_loop reads this instead of the static config
lists; everything downstream of "here is a list of symbols to scan" is
100% unchanged.

ABOUT "YOUR EXISTING TREND FILTER" (please read)
-------------------------------------------------
There is no existing TRENDING/NEUTRAL/CHOPPY classifier or trend-score
ranking in your current codebase — check_signal()'s EMA comparison is a
binary bias used only as one ingredient of the wick-zone signal, not a
standalone, liftable filter. Rather than silently invent something and
call it "your existing logic," this file contains a clearly-separate
TrendClassifier that:
  - reuses your EMA_LENGTH (200) so the trend definition is at least
    philosophically consistent with your existing bias check,
  - adds an EMA-slope term (trend must actually be moving, not flat) and
    a normalized price-distance-from-EMA term (trend must have some
    conviction, not just barely above/below the line),
  - produces a 0-100 score and a TRENDING / NEUTRAL / CHOPPY label using
    configurable thresholds.
This logic lives ONLY in this file and is never imported into bot.py's
signal path — it only decides which symbols bot.py is even allowed to look
at, never whether/when a trade fires.

ABOUT ASM/GSM EXCLUSION
------------------------
Kite's REST API does not expose NSE's surveillance-stage (ASM/GSM) flags.
There is no reliable, official Kite endpoint for this. This module exposes
an `exclude_symbols: set[str]` hook (config.ASM_GSM_EXCLUDE_SYMBOLS) so you
can plug in a list you maintain yourself (e.g. scraped from NSE's published
CSVs on a schedule outside this bot). Left empty, no ASM/GSM filtering
happens — liquidity filters (volume/value) still apply and catch most of
the same illiquid-stock risk in practice.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("universe_scanner")
IST = ZoneInfo("Asia/Kolkata")

# Heuristic ETF/non-equity name markers. Kite's NSE instrument dump tags
# ETFs with instrument_type == "EQ" too (they trade like equities), so
# there's no clean API flag distinguishing them from ordinary stocks.
# This is a best-effort filter — for production accuracy, maintain your
# own equity-only symbol whitelist/blacklist and pass it via
# extra_exclude_symbols to DynamicUniverseScanner.
_ETF_NAME_MARKERS = ("ETF", "FUND", "GOLD", "LIQUID", "NIFTYBEES", "BEES")


@dataclass
class TrendResult:
    symbol: str
    label: str          # "TRENDING" | "NEUTRAL" | "CHOPPY"
    score: float         # 0-100
    ema: float
    ltp: float


@dataclass
class ScanBandResult:
    name: str
    scanned: int = 0
    trending: int = 0
    neutral: int = 0
    choppy: int = 0
    selected: list[str] = field(default_factory=list)
    selected_scores: dict[str, float] = field(default_factory=dict)


class TrendClassifier:
    """
    NEW logic (see module docstring) — reuses EMA_LENGTH but is not the
    same thing as check_signal()'s binary bias, and is never called from
    inside check_signal(). Pure function of a candle list; no side effects,
    no state.
    """

    def __init__(self, ema_length: int, slope_lookback: int,
                 trending_min_score: float, choppy_max_score: float):
        self.ema_length = ema_length
        self.slope_lookback = slope_lookback
        self.trending_min_score = trending_min_score
        self.choppy_max_score = choppy_max_score

    @staticmethod
    def _ema_series(closes: list[float], period: int) -> list[float | None]:
        if len(closes) < period:
            return [None] * len(closes)
        k = 2.0 / (period + 1)
        ema = [sum(closes[:period]) / period]
        for price in closes[period:]:
            ema.append(price * k + ema[-1] * (1 - k))
        return [None] * (period - 1) + ema

    def classify(self, symbol: str, candles: list[dict]) -> TrendResult | None:
        needed = self.ema_length + self.slope_lookback + 5
        if len(candles) < needed:
            return None

        closes = [c["close"] for c in candles]
        ema_vals = self._ema_series(closes, self.ema_length)
        ema_now = ema_vals[-1]
        ema_then = ema_vals[-1 - self.slope_lookback]
        if ema_now is None or ema_then is None or ema_now == 0:
            return None

        ltp = closes[-1]

        # Slope term: % change in EMA over the lookback window, normalized
        # so ~1% move over the window maps to a full-strength contribution.
        slope_pct = (ema_now - ema_then) / ema_then * 100
        slope_score = max(-1.0, min(1.0, slope_pct / 1.0))  # clamp to [-1, 1]

        # Distance term: how far price sits from its own EMA, as a % of
        # price, similarly clamped so ~1.5% distance is full strength.
        distance_pct = (ltp - ema_now) / ema_now * 100
        distance_score = max(-1.0, min(1.0, distance_pct / 1.5))

        # Agreement bonus: slope and distance pointing the same direction
        # is a stronger trend signal than either alone.
        agree = 1.0 if (slope_score > 0) == (distance_score > 0) else 0.4

        raw = (abs(slope_score) * 0.5 + abs(distance_score) * 0.5) * agree
        score = round(max(0.0, min(1.0, raw)) * 100, 1)

        if score >= self.trending_min_score:
            label = "TRENDING"
        elif score <= self.choppy_max_score:
            label = "CHOPPY"
        else:
            label = "NEUTRAL"

        return TrendResult(symbol=symbol, label=label, score=score,
                            ema=round(ema_now, 2), ltp=round(ltp, 2))


class DynamicUniverseScanner:
    """
    Owns: the full NSE-EQ instrument universe (refreshed once/day), the
    per-symbol liquidity cache, the trend classifier, and the current
    candidate pools per price band. Thread-safe. Runs on its own background
    loop, independent of bot.py's scan_loop/monitor loop cadence.

    bot.py integration is exactly two calls:
        scanner.start()                         # once, after connect_kite()
        scanner.get_candidate_pools()            # inside scan_loop, each cycle,
                                                  # in place of reading the
                                                  # static WATCHLIST/PRICE_BANDS
    """

    def __init__(
        self,
        kite,
        price_bands: list[dict],           # config.UNIVERSE_PRICE_BANDS
        liquidity_lookback_days: int,
        min_avg_volume: float,
        min_avg_value: float,
        scan_interval_sec: int,
        instrument_refresh_time: str,      # "HH:MM" IST
        trend_ema_length: int,
        trend_slope_lookback: int,
        trending_min_score: float,
        choppy_max_score: float,
        exclude_symbols: set[str] | None = None,
        kite_call_with_retry=None,         # optional: reuse bot.py's retry wrapper
    ):
        self.kite = kite
        self.price_bands = price_bands
        self.liquidity_lookback_days = liquidity_lookback_days
        self.min_avg_volume = min_avg_volume
        self.min_avg_value = min_avg_value
        self.scan_interval_sec = scan_interval_sec
        self.instrument_refresh_time = instrument_refresh_time
        self.exclude_symbols = set(exclude_symbols or ())
        self._call = kite_call_with_retry or (lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k != "what"}))

        self.classifier = TrendClassifier(
            ema_length=trend_ema_length,
            slope_lookback=trend_slope_lookback,
            trending_min_score=trending_min_score,
            choppy_max_score=choppy_max_score,
        )

        self._lock = threading.Lock()
        self._instruments: dict[str, int] = {}          # tradingsymbol -> instrument_token
        self._instruments_date: str | None = None        # "YYYY-MM-DD" of last full refresh
        self._liquidity_cache: dict[str, dict] = {}       # symbol -> {"avg_volume", "avg_value", "date"}
        self._candidate_pools: dict[str, list[str]] = {b["name"]: [] for b in price_bands}
        self._band_of_symbol: dict[str, str] = {}         # symbol -> band name, for currently-selected candidates only
        self._last_scan_summary: list[ScanBandResult] = []
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Public accessors (what bot.py reads) ──────────────────────────────
    def get_candidate_pools(self) -> dict[str, list[str]]:
        with self._lock:
            return {name: list(syms) for name, syms in self._candidate_pools.items()}

    def get_watchlist(self) -> list[str]:
        """Flat list — direct drop-in replacement for config.WATCHLIST."""
        pools = self.get_candidate_pools()
        out: list[str] = []
        for syms in pools.values():
            out.extend(syms)
        return out

    def get_band_for_symbol(self, symbol: str) -> str | None:
        """
        Drop-in replacement for bot.py's band_for_symbol() name lookup —
        returns which band this symbol is CURRENTLY selected under, or None
        if it isn't in any current candidate pool (e.g. it fell out of
        trend between scans; an already-open position keeps its
        originally-recorded band regardless, since bot.py stores "band" on
        the position dict at entry time and never re-reads this after).
        """
        with self._lock:
            return self._band_of_symbol.get(symbol)

    def get_last_scan_summary(self) -> list[ScanBandResult]:
        with self._lock:
            return list(self._last_scan_summary)

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self.refresh_instrument_universe(force=True)
        self.run_scan_once()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("🌐 DynamicUniverseScanner started")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._maybe_refresh_instruments()
                self.run_scan_once()
            except Exception as e:
                log.warning(f"⚠️ Universe scan error: {e}")
            time.sleep(self.scan_interval_sec)

    # ── Step 1: Full NSE-EQ instrument universe (refreshed once/day) ──────
    def _maybe_refresh_instruments(self):
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        h, m = map(int, self.instrument_refresh_time.split(":"))
        refresh_after = now.replace(hour=h, minute=m, second=0, microsecond=0)

        with self._lock:
            already_done_today = self._instruments_date == today_str

        if already_done_today:
            return
        if now < refresh_after:
            return  # today's refresh window hasn't arrived yet
        self.refresh_instrument_universe(force=True)

    def refresh_instrument_universe(self, force: bool = False):
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        with self._lock:
            if not force and self._instruments_date == today_str:
                return

        try:
            raw = self._call(self.kite.instruments, "NSE", what="instruments (universe)")
        except Exception as e:
            log.warning(f"⚠️ Universe instrument refresh failed: {e} — keeping previous list.")
            return

        filtered: dict[str, int] = {}
        for inst in raw or []:
            if inst.get("segment") != "NSE" or inst.get("instrument_type") != "EQ":
                continue  # drops indices/futures/options/bonds/most non-EQ segments outright
            symbol = inst.get("tradingsymbol", "")
            name = (inst.get("name") or "").upper()
            if any(marker in symbol.upper() or marker in name for marker in _ETF_NAME_MARKERS):
                continue  # heuristic ETF/fund exclusion — see module docstring
            if symbol in self.exclude_symbols:
                continue
            filtered[symbol] = inst["instrument_token"]

        with self._lock:
            self._instruments = filtered
            self._instruments_date = today_str
        log.info(f"🗂️ Universe instrument refresh: {len(filtered)} NSE-EQ symbols loaded for {today_str}")

    # ── Step 3: Liquidity filter ───────────────────────────────────────────
    def _passes_liquidity(self, symbol: str, token: int) -> bool:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        cached = self._liquidity_cache.get(symbol)
        if cached and cached.get("date") == today_str:
            return cached["avg_volume"] >= self.min_avg_volume and cached["avg_value"] >= self.min_avg_value

        try:
            to_dt = datetime.now(IST)
            from_dt = to_dt - timedelta(days=self.liquidity_lookback_days * 2)  # buffer for weekends/holidays
            raw = self._call(
                self.kite.historical_data,
                instrument_token=token,
                from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
                interval="day",
                continuous=False,
                oi=False,
                what=f"liquidity {symbol}",
            )
        except Exception as e:
            log.debug(f"{symbol}: liquidity history fetch failed — {e}")
            return False

        if not raw:
            return False
        days = raw[-self.liquidity_lookback_days:]
        if not days:
            return False
        avg_volume = sum(d["volume"] for d in days) / len(days)
        avg_value = sum(d["volume"] * d["close"] for d in days) / len(days)

        self._liquidity_cache[symbol] = {
            "avg_volume": avg_volume, "avg_value": avg_value, "date": today_str,
        }
        return avg_volume >= self.min_avg_volume and avg_value >= self.min_avg_value

    # ── Steps 2, 4, 5, 6, 7: bands, trend, ranking, candidate selection ───
    def run_scan_once(self):
        with self._lock:
            instruments = dict(self._instruments)
        if not instruments:
            log.warning("⚠️ Universe scan skipped — instrument universe is empty (refresh may have failed).")
            return

        symbols = list(instruments.keys())
        ltp_map = self._batch_ltp(symbols)
        if not ltp_map:
            log.warning("⚠️ Universe scan skipped — batch LTP fetch returned nothing.")
            return

        band_results: list[ScanBandResult] = []
        new_pools: dict[str, list[str]] = {}
        new_band_of_symbol: dict[str, str] = {}

        for band_cfg in self.price_bands:
            name = band_cfg["name"]
            min_p, max_p = band_cfg["min_price"], band_cfg["max_price"]
            candidate_count = band_cfg["candidate_count"]

            in_band = [s for s in symbols if min_p <= ltp_map.get(s, -1) < max_p]

            trending: list[TrendResult] = []
            neutral: list[TrendResult] = []
            choppy: list[TrendResult] = []

            for symbol in in_band:
                token = instruments[symbol]
                if not self._passes_liquidity(symbol, token):
                    continue
                candles = self._get_candles(token, symbol)
                if not candles:
                    continue
                result = self.classifier.classify(symbol, candles)
                if result is None:
                    continue
                if result.label == "TRENDING":
                    trending.append(result)
                elif result.label == "NEUTRAL":
                    neutral.append(result)
                else:
                    choppy.append(result)

            trending.sort(key=lambda r: r.score, reverse=True)
            neutral.sort(key=lambda r: r.score, reverse=True)
            choppy.sort(key=lambda r: r.score, reverse=True)

            selected: list[TrendResult] = trending[:candidate_count]
            if len(selected) < candidate_count:
                selected += neutral[: candidate_count - len(selected)]
            if len(selected) < candidate_count:
                selected += choppy[: candidate_count - len(selected)]

            pool_symbols = [r.symbol for r in selected]
            new_pools[name] = pool_symbols
            for r in selected:
                new_band_of_symbol[r.symbol] = name

            br = ScanBandResult(
                name=name,
                scanned=len(in_band),
                trending=len(trending),
                neutral=len(neutral),
                choppy=len(choppy),
                selected=pool_symbols,
                selected_scores={r.symbol: r.score for r in selected},
            )
            band_results.append(br)

        with self._lock:
            self._candidate_pools = new_pools
            self._band_of_symbol = new_band_of_symbol
            self._last_scan_summary = band_results

        self._log_scan_summary(band_results)

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _batch_ltp(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        chunk_size = 200  # Kite's ltp()/quote() endpoints cap batch size
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            keys = [f"NSE:{s}" for s in chunk]
            try:
                quotes = self._call(self.kite.ltp, keys, what="universe LTP batch")
            except Exception as e:
                log.debug(f"Universe LTP batch fetch failed for chunk starting {chunk[0]}: {e}")
                continue
            for s in chunk:
                key = f"NSE:{s}"
                if quotes and key in quotes:
                    out[s] = float(quotes[key]["last_price"])
        return out

    def _get_candles(self, token: int, symbol: str) -> list[dict] | None:
        needed = self.classifier.ema_length + self.classifier.slope_lookback + 60
        try:
            to_dt = datetime.now(IST)
            from_dt = to_dt - timedelta(days=10)
            raw = self._call(
                self.kite.historical_data,
                instrument_token=token,
                from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
                interval="5minute",
                continuous=False,
                oi=False,
                what=f"universe candles {symbol}",
            )
        except Exception as e:
            log.debug(f"{symbol}: candle fetch failed — {e}")
            return None
        if not raw:
            return None
        return [{"close": float(r["close"]), "volume": float(r["volume"])} for r in raw[-needed:]]

    def _log_scan_summary(self, band_results: list[ScanBandResult]):
        for br in band_results:
            log.info(f"Band {br.name}")
            log.info(f"  Scanned: {br.scanned}")
            log.info(f"  Trending: {br.trending}")
            log.info(f"  Neutral: {br.neutral}")
            log.info(f"  Choppy: {br.choppy}")
            log.info("  Selected:")
            for sym in br.selected:
                score = br.selected_scores.get(sym)
                log.info(f"    {sym} ({score})")