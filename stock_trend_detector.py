"""
StockTrendDetector — Per-stock trend classification for the WickFill bot.

Evaluates 10 technical factors on every new candle and produces a 0–100
Trend Score, then classifies the stock as TRENDING / NEUTRAL / CHOPPY.

Designed to be called once per symbol per scan cycle — lightweight, no
network calls, no mutable global state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("trend_detector")


# ── Public result container ───────────────────────────────────────────────────
@dataclass
class TrendResult:
    score: int = 0
    state: str = "CHOPPY"          # TRENDING | NEUTRAL | CHOPPY
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# ── Indicator helpers ─────────────────────────────────────────────────────────
def _ema(closes: list[float], period: int) -> list[float | None]:
    if len(closes) < period:
        return [None] * len(closes)
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema


def _atr(candles: list[dict], period: int = 14) -> list[float | None]:
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        prev_close = candles[i - 1]["close"]
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - prev_close),
            abs(candles[i]["low"] - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return [None] * len(trs)
    atr_vals: list[float] = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr_vals.append((atr_vals[-1] * (period - 1) + tr) / period)
    return [None] * (period - 1) + atr_vals


def _adx(candles: list[dict], period: int = 14) -> list[float | None]:
    n = len(candles)
    if n < period + 1:
        return [None] * n

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

    return [None] * period + adx_vals


def _sma(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1: i + 1]) / period
    return out


def _std(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean = sum(window) / period
        out[i] = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    return out


def _vwap(candles: list[dict]) -> float | None:
    tp_vol = 0.0
    vol_sum = 0.0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        tp_vol += tp * c["volume"]
        vol_sum += c["volume"]
    if vol_sum == 0:
        return None
    return tp_vol / vol_sum


# ── Main detector ─────────────────────────────────────────────────────────────
class StockTrendDetector:
    """
    Lightweight, stateless trend classifier.

    Call compute() with a list of OHLCV candles (newest last) to get a
    TrendResult.  No network calls, no global state.
    """

    def __init__(self) -> None:
        self._cache: dict[str, TrendResult] = {}
        self._last_cache_key: str = ""

    @staticmethod
    def _make_cache_key(symbol: str, candles: list[dict]) -> str:
        last = candles[-1]
        return f"{symbol}:{last['time']}:{last['close']}"

    def compute(self, symbol: str, candles: list[dict]) -> TrendResult:
        if not candles:
            return TrendResult(score=0, state="CHOPPY", reasons=["Insufficient data"])
        key = self._make_cache_key(symbol, candles)
        if symbol in self._cache and self._last_cache_key == key:
            return self._cache[symbol]
        result = self._evaluate(candles)
        self._cache[symbol] = result
        self._last_cache_key = key
        return result

    def get_trend_score(self, symbol: str, candles: list[dict]) -> int:
        return self.compute(symbol, candles).score

    def get_market_state(self, symbol: str, candles: list[dict]) -> str:
        return self.compute(symbol, candles).state

    def get_reasons(self, symbol: str, candles: list[dict]) -> list[str]:
        return self.compute(symbol, candles).reasons

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()

    # ── Internal evaluation ─────────────────────────────────────────────────
    def _evaluate(self, candles: list[dict]) -> TrendResult:
        reasons: list[str] = []
        score = 0
        details: dict[str, Any] = {}
        n = len(candles)

        if n < 30:
            return TrendResult(score=0, state="CHOPPY", reasons=["Insufficient data"])

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]

        # 1. ADX(14)
        adx_vals = _adx(candles, 14)
        adx_now = adx_vals[-1] if adx_vals and adx_vals[-1] is not None else 0.0
        details["adx"] = round(adx_now, 2)
        if adx_now > 25:
            score += 20
            reasons.append(f"Strong ADX ({adx_now:.1f} > 25)")
        else:
            reasons.append(f"Weak ADX ({adx_now:.1f} ≤ 25)")

        # 2. ATR(14) expanding
        atr_vals = _atr(candles, 14)
        atr_now = atr_vals[-1] if atr_vals and atr_vals[-1] is not None else 0.0
        recent_atr = [v for v in atr_vals[-10:] if v is not None]
        atr_expanding = (
            len(recent_atr) >= 5
            and atr_now > sum(recent_atr[-5:]) / 5
            if recent_atr
            else False
        )
        details["atr"] = round(atr_now, 2)
        details["atr_expanding"] = atr_expanding
        if atr_expanding:
            score += 10
            reasons.append("ATR expanding")
        else:
            reasons.append("ATR contracting")

        # 3. EMA200 slope
        ema200 = _ema(closes, 200)
        ema_now = ema200[-1] if ema200 and ema200[-1] is not None else closes[-1]
        if n >= 205 and ema200[-5] is not None:
            slope_pct = (ema_now - ema200[-5]) / ema200[-5] * 100
            details["ema200_slope_pct"] = round(slope_pct, 3)
            if abs(slope_pct) > 0.15:
                score += 15
                reasons.append(f"Strong EMA200 slope ({slope_pct:+.2f}%)")
            else:
                reasons.append(f"Flat EMA200 slope ({slope_pct:+.2f}%)")
        else:
            reasons.append("Insufficient history for EMA200 slope")

        # 4. EMA200 cross count (last 30 candles)
        cross_count = 0
        if ema200[-1] is not None:
            lookback = min(30, n)
            start = n - lookback
            for i in range(start + 1, n):
                if ema200[i] is None or ema200[i - 1] is None:
                    continue
                prev_above = closes[i - 1] > ema200[i - 1]
                curr_above = closes[i] > ema200[i]
                if prev_above != curr_above:
                    cross_count += 1
        details["ema_cross_count"] = cross_count
        if cross_count <= 2:
            score += 15
            reasons.append(f"Low EMA cross count ({cross_count} ≤ 2)")
        else:
            reasons.append(f"High EMA cross count ({cross_count} > 2)")

        # 5. VWAP distance
        vwap = _vwap(candles)
        vwap_dist_pct = abs(closes[-1] - vwap) / vwap * 100 if vwap else 0.0
        details["vwap_distance_pct"] = round(vwap_dist_pct, 2)
        if vwap_dist_pct > 0.5:
            score += 10
            reasons.append(f"Price distant from VWAP ({vwap_dist_pct:.2f}%)")
        else:
            reasons.append(f"Price near VWAP ({vwap_dist_pct:.2f}%)")

        # 6. Bollinger Band Width expanding
        bb_period = 20
        if n >= bb_period:
            sma_vals = _sma(closes, bb_period)
            std_vals = _std(closes, bb_period)
            widths: list[float] = []
            for i in range(bb_period - 1, n):
                if sma_vals[i] is not None and std_vals[i] is not None and sma_vals[i] != 0:
                    widths.append(2 * std_vals[i] / sma_vals[i])
            if len(widths) >= 5:
                bb_now = widths[-1]
                bb_avg = sum(widths[-5:]) / 5
                bb_expanding = bb_now > bb_avg
                details["bollinger_width"] = round(bb_now, 4)
                details["bollinger_expanding"] = bb_expanding
                if bb_expanding:
                    score += 10
                    reasons.append("Bollinger width expanding")
                else:
                    reasons.append("Bollinger width contracting")
            else:
                reasons.append("Insufficient data for Bollinger width")
        else:
            reasons.append("Insufficient data for Bollinger width")

        # 7. Relative Volume
        vol_period = 20
        if n >= vol_period:
            avg_vol = sum(volumes[-vol_period - 1: -1]) / vol_period
            rel_vol = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
            details["relative_volume"] = round(rel_vol, 2)
            if rel_vol > 1.5:
                score += 10
                reasons.append(f"High relative volume ({rel_vol:.2f}x)")
            else:
                reasons.append(f"Normal relative volume ({rel_vol:.2f}x)")
        else:
            reasons.append("Insufficient data for relative volume")

        # 8. Higher High Higher Low structure
        hh_hl_period = min(10, n)
        start = n - hh_hl_period
        hh_count = 0
        hl_count = 0
        for i in range(start + 1, n):
            if highs[i] > highs[i - 1]:
                hh_count += 1
            if lows[i] > lows[i - 1]:
                hl_count += 1
        hh_hl_ok = hh_count >= hh_hl_period // 2 and hl_count >= hh_hl_period // 2
        details["hh_count"] = hh_count
        details["hl_count"] = hl_count
        details["hh_hl_structure"] = hh_hl_ok
        if hh_hl_ok:
            score += 5
            reasons.append("HH/HL structure intact")
        else:
            reasons.append("No clear HH/HL structure")

        # 9. Candle body strength
        body_period = min(10, n)
        body_sizes = []
        range_sizes = []
        for i in range(n - body_period, n):
            body = abs(candles[i]["close"] - candles[i]["open"])
            rng = candles[i]["high"] - candles[i]["low"]
            body_sizes.append(body)
            range_sizes.append(rng if rng > 0 else 1e-9)
        avg_body_ratio = sum(body_sizes) / sum(range_sizes) if sum(range_sizes) else 0.0
        details["avg_body_ratio"] = round(avg_body_ratio, 3)
        if avg_body_ratio > 0.6:
            score += 3
            reasons.append("Strong candle bodies")
        else:
            reasons.append("Weak candle bodies")

        # 10. Momentum over last 10 candles
        mom_period = min(10, n - 1)
        if mom_period > 0:
            momentum = (closes[-1] - closes[-1 - mom_period]) / closes[-1 - mom_period] * 100
        else:
            momentum = 0.0
        details["momentum_pct"] = round(momentum, 2)
        if momentum > 0:
            score += 2
            reasons.append(f"Positive momentum ({momentum:+.2f}%)")
        else:
            reasons.append(f"Negative momentum ({momentum:+.2f}%)")

        # Clamp score
        score = max(0, min(100, score))

        if score >= 75:
            state = "TRENDING"
        elif score >= 50:
            state = "NEUTRAL"
        else:
            state = "CHOPPY"

        return TrendResult(score=score, state=state, reasons=reasons, details=details)
