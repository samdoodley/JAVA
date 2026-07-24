"""
execution.py — WickFill Execution Engine
==========================================
Exchange-native stop-loss orders + KiteTicker WebSocket tick stream +
order-update callbacks + thread-safe exit guarding + latency/slippage
tracking.

WHY THIS MODULE EXISTS
-----------------------
The old execution path worked like this:

    every MONITOR_INTERVAL_SEC (5s):
        ltp = kite.ltp(symbol)      # REST poll
        if ltp crossed configured SL:
            place a MARKET order right now

Two things make that dangerous:

1.  It is a POLL, not a subscription. Between one check and the next, price
    can move a long way — and your own bot.log showed Kite's REST endpoint
    itself sometimes taking 15-30+ seconds to respond under load. During
    that whole window nothing is watching the price at all.
2.  Once the poll finally notices the SL has been crossed, it reacts with a
    MARKET order — which fills at whatever the price has become BY THEN,
    not at the SL price. Polling delay + market-order slippage compound.

That is exactly the oversized-loss pattern in your POLYCAB (SL 9116, exit
9091) and ASIANPAINT examples: the "stop" was never actually resting
anywhere with authority — it was a periodic Python guess.

This module fixes that at the root:

  - LIVE mode: the stop-loss is a real SL-M order resting AT THE EXCHANGE
    from the instant the entry fills. The exchange's own matching engine
    triggers it continuously, tick by tick — Python is not in that loop at
    all. Trailing MODIFIES that same resting order (modify_order), it is
    never cancelled and recreated. Python only finds out the stop fired
    via the order-update WebSocket push (on_order_update) — it does not
    poll order_history() to discover this.
  - PAPER mode: there is no real broker order to rest (Zerodha does not
    let you place real stop orders against a paper position). The closest
    honest simulation is to evaluate the stop against the live WebSocket
    tick stream — the same tick-by-tick feed the exchange itself is
    printing — instead of a 5-second REST poll. This closes almost all of
    the gap; it can never be quite as good as a real resting exchange
    order because there is fundamentally no real order sitting in the
    exchange's order book in paper mode.
  - Emergency fail-safe: if the resting exchange SL order is ever REJECTED
    or CANCELLED unexpectedly (margin issue, RMS, connectivity blip on
    Zerodha's side, etc.), the position is NEVER left unprotected — an
    immediate MARKET exit is fired the moment that rejection is observed.
  - If the WebSocket itself disconnects, reconnects, or the whole bot
    process crashes, the position is STILL protected, because the
    stop-loss order already lives at the exchange, independent of this
    process being alive. That is the entire point of "the exchange must
    own the stop" — Python's own uptime stops being a single point of
    failure for stop-loss protection.
"""

import time
import threading
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from kiteconnect import KiteTicker

log = logging.getLogger("execution")
IST = ZoneInfo("Asia/Kolkata")

# Order statuses that mean "this order is done, one way or another."
_TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}


class ExecutionEngine:
    """
    Owns the KiteTicker WebSocket connection, the live tick cache, order-update
    routing, per-symbol locks / exit guards, and latency + slippage bookkeeping.

    bot.py still owns all actual kite.* REST calls (place_order, modify_order,
    margins, etc.) — this class deliberately does NOT wrap the whole
    KiteConnect surface. Its only jobs are: (1) real-time ticks, (2) real-time
    order-status push, (3) making concurrent access to those two streams safe,
    and (4) recording execution-quality metrics (latency, slippage).
    """

    def __init__(self):
        self.kws: KiteTicker | None = None
        self._connected = False

        # symbol <-> instrument_token maps, built by bot.py from its own
        # instrument cache once instruments are loaded.
        self._symbol_to_token: dict[str, int] = {}
        self._token_to_symbol: dict[int, str] = {}

        # Live tick cache — symbol -> last traded price (updated on every tick).
        self._ltp_cache: dict[str, float] = {}
        self._ltp_lock = threading.Lock()

        # Per-symbol lock — serializes trailing-update / exit-check logic for
        # ONE symbol so a tick callback and an order-update callback can never
        # both be mutating that symbol's position at the same instant.
        self._symbol_locks: dict[str, threading.Lock] = {}
        self._symbol_locks_guard = threading.Lock()

        # Exit-in-progress guard — prevents duplicate exit orders / double
        # position-closing if two triggers (e.g. a tick-based check and an
        # order-update push) fire for the same symbol at nearly the same time.
        self._exit_in_progress: set[str] = set()
        self._exit_guard_lock = threading.Lock()

        # Order-update waiters — order_id -> threading.Event, plus the last
        # update payload received for that order_id. Lets bot.py "await" an
        # order's terminal status via the WebSocket push instead of polling
        # order_history() in a loop. A REST poll is still kept as a fallback
        # ONLY if the push never arrives within a short timeout (safety net,
        # not the primary mechanism).
        self._order_waiters: dict[str, threading.Event] = {}
        self._order_updates: dict[str, dict] = {}
        self._order_waiters_lock = threading.Lock()

        # Latency bookkeeping — key (usually the symbol) -> {stage: epoch_ts}
        self._latency: dict[str, dict[str, float]] = {}
        self._latency_lock = threading.Lock()

        # External callbacks bot.py registers.
        self.on_ticks_callback = None          # fn(ticks: list[tuple[str, float]]) — ORDERED, not deduplicated
        self.on_order_update_callback = None   # fn(update: dict)

    # ── Symbol <-> Token Map ──────────────────────────────────────────────
    def set_symbol_token_map(self, mapping: dict[str, int]):
        self._symbol_to_token = dict(mapping)
        self._token_to_symbol = {v: k for k, v in mapping.items()}

    # ── Per-Symbol Locking ────────────────────────────────────────────────
    def get_symbol_lock(self, symbol: str) -> threading.Lock:
        with self._symbol_locks_guard:
            if symbol not in self._symbol_locks:
                self._symbol_locks[symbol] = threading.Lock()
            return self._symbol_locks[symbol]

    # ── Exit Guard (prevents duplicate exits / double position-closing) ──
    def try_begin_exit(self, symbol: str) -> bool:
        """Atomically claim the right to exit this symbol. Returns False if
        an exit for this symbol is already underway on another thread —
        callers MUST skip their exit attempt entirely if this returns False."""
        with self._exit_guard_lock:
            if symbol in self._exit_in_progress:
                return False
            self._exit_in_progress.add(symbol)
            return True

    def end_exit(self, symbol: str):
        with self._exit_guard_lock:
            self._exit_in_progress.discard(symbol)

    # ── Latency Tracking ──────────────────────────────────────────────────
    def mark(self, key: str, stage: str):
        """Record 'stage happened now' for this trade key (symbol)."""
        with self._latency_lock:
            self._latency.setdefault(key, {})[stage] = time.time()

    def set_stage_time(self, key: str, stage: str, epoch_ts: float):
        """Record a stage timestamp explicitly (e.g. from an order-update
        payload's own exchange timestamp) rather than 'now'."""
        with self._latency_lock:
            self._latency.setdefault(key, {})[stage] = epoch_ts

    def get_latency_summary(self, key: str) -> dict:
        """
        Returns a dict with every recorded stage's ISO timestamp plus the
        derived delay figures (milliseconds), skipping any pair where one
        side wasn't recorded (e.g. paper trades have no sl_order_sent).
        """
        with self._latency_lock:
            stages = dict(self._latency.get(key, {}))

        def iso(ts):
            return datetime.fromtimestamp(ts, IST).isoformat() if ts else None

        def delta_ms(a, b):
            if a is None or b is None:
                return None
            return round((b - a) * 1000, 1)

        out = {f"{k}_time": iso(v) for k, v in stages.items()}
        out["execution_delay_ms"] = delta_ms(stages.get("entry_order_sent"), stages.get("entry_complete"))
        out["sl_fill_delay_ms"] = delta_ms(stages.get("sl_order_sent"), stages.get("sl_accepted"))
        out["total_exit_delay_ms"] = delta_ms(stages.get("sl_trigger_time"), stages.get("exit_filled"))
        return out

    def clear_latency(self, key: str):
        with self._latency_lock:
            self._latency.pop(key, None)

    # ── Slippage Calculation ──────────────────────────────────────────────
    @staticmethod
    def compute_slippage(
        entry: float, configured_sl: float, actual_exit: float, qty: int,
        direction: str,
    ) -> dict:
        """
        Matches the exact fields requested:
            configured SL, actual exit, slippage (points), expected loss,
            actual loss, R-multiple.
        Works for BUY or SELL, and for both a straight SL_HIT and a
        TRAIL_SL_HIT (where "expected" P&L at the configured stop might
        actually be a small locked-in profit, not a loss — the math still
        holds, it's just signed).
        """
        if direction == "BUY":
            expected_pnl = (configured_sl - entry) * qty
            actual_pnl = (actual_exit - entry) * qty
        else:
            expected_pnl = (entry - configured_sl) * qty
            actual_pnl = (entry - actual_exit) * qty

        slippage_points = round(abs(actual_exit - configured_sl), 4)
        slippage_percent = round((slippage_points / configured_sl) * 100, 4) if configured_sl else 0.0
        expected_loss = round(abs(expected_pnl), 2)
        actual_loss = round(abs(actual_pnl), 2)
        risk_multiple = round(actual_loss / expected_loss, 2) if expected_loss > 0 else None

        return {
            "configured_sl": configured_sl,
            "actual_exit": actual_exit,
            "slippage_points": slippage_points,
            "slippage_percent": slippage_percent,
            "expected_loss": expected_loss,
            "actual_loss": actual_loss,
            "risk_multiple": risk_multiple,
        }

    # ── Live Tick Cache ───────────────────────────────────────────────────
    def get_ltp(self, symbol: str) -> float | None:
        with self._ltp_lock:
            return self._ltp_cache.get(symbol)

    def is_connected(self) -> bool:
        return self._connected

    # ── KiteTicker Lifecycle ──────────────────────────────────────────────
    def start(self, api_key: str, access_token: str):
        if self.kws is not None:
            return  # already started
        self.kws = KiteTicker(api_key, access_token)
        self.kws.on_ticks = self._on_ticks
        self.kws.on_connect = self._on_connect
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect
        self.kws.on_noreconnect = self._on_noreconnect
        self.kws.on_order_update = self._on_order_update
        # threaded=True: runs the socket on a background thread so it never
        # blocks the scan/monitor loops. reconnect is handled internally by
        # pykiteconnect (exponential backoff) — the exchange-resting SL order
        # keeps protecting the position even while this socket is down/
        # reconnecting, which is exactly the fail-safe property required.
        self.kws.connect(threaded=True)
        log.info("🔌 KiteTicker starting (threaded WebSocket connection)…")

    def stop(self):
        if self.kws is not None:
            try:
                self.kws.close()
            except Exception:
                pass
        self._connected = False

    def subscribe_symbols(self, symbols: list[str]):
        if self.kws is None:
            return
        tokens = [self._symbol_to_token[s] for s in symbols if s in self._symbol_to_token]
        missing = [s for s in symbols if s not in self._symbol_to_token]
        if missing:
            log.warning(f"⚠️ KiteTicker: no instrument token found for {missing} — not subscribed.")
        if tokens:
            self.kws.subscribe(tokens)
            self.kws.set_mode(self.kws.MODE_FULL, tokens)
            log.info(f"📡 KiteTicker subscribed to {len(tokens)} symbols (FULL mode)")

    # ── WebSocket Callbacks ───────────────────────────────────────────────
    def _on_connect(self, ws, response):
        self._connected = True
        log.info("✅ KiteTicker connected")

    def _on_close(self, ws, code, reason):
        self._connected = False
        log.warning(f"⚠️ KiteTicker connection closed (code={code}, reason={reason})")

    def _on_error(self, ws, code, reason):
        log.warning(f"⚠️ KiteTicker error (code={code}, reason={reason})")

    def _on_reconnect(self, ws, attempts_count):
        log.warning(f"🔄 KiteTicker reconnecting… (attempt {attempts_count})")

    def _on_noreconnect(self, ws):
        self._connected = False
        log.error(
            "🚫 KiteTicker gave up reconnecting. Tick-driven trailing/paper-exit "
            "checks are now stale. LIVE positions remain protected by their "
            "resting exchange SL-M orders regardless — this only affects "
            "paper-mode simulation accuracy and live trailing responsiveness "
            "until the socket recovers."
        )

    def _on_ticks(self, ws, ticks):
        """
        FIXED: previously built a dict keyed by symbol (tick_map[symbol] =
        price), which SILENTLY OVERWRITES earlier ticks for the same symbol
        if more than one arrives in a single WebSocket batch — common during
        a fast price move. That meant a trailing-stop crossing that
        happened on an EARLIER tick in the batch could be skipped entirely,
        with only the LAST (and by then, worse) price in the batch ever
        checked against the stop — exactly the root cause of exit prices
        landing well past the actual trailing-stop level.

        Now every tick is forwarded in order as (symbol, price) tuples, so
        whoever processes them (bot.py's _handle_ticks) evaluates each one
        against the stop in sequence and reacts to the FIRST crossing, not
        whatever happened to be last in the batch. The ltp cache below still
        only needs the latest value (fine for dashboard/general lookups),
        but the callback now gets the full, ordered picture.
        """
        ordered_ticks = []
        with self._ltp_lock:
            for t in ticks:
                token = t.get("instrument_token")
                symbol = self._token_to_symbol.get(token)
                price = t.get("last_price")
                if symbol is None or price is None:
                    continue
                price = float(price)
                self._ltp_cache[symbol] = price
                ordered_ticks.append((symbol, price))
        if ordered_ticks and self.on_ticks_callback:
            try:
                self.on_ticks_callback(ordered_ticks)
            except Exception as e:
                log.warning(f"⚠️ on_ticks_callback error: {e}")

    def _on_order_update(self, ws, data):
        """
        Fires on every order status change pushed by Kite over the same
        WebSocket connection — this is what replaces repeatedly calling
        kite.order_history() in a loop. Two things happen with every update:
          1. Anyone awaiting this exact order_id (await_order_update) is
             woken up immediately.
          2. bot.py's registered handler is called so it can react to SL
             fills / rejections / cancellations in real time.
        """
        order_id = data.get("order_id")
        if order_id:
            with self._order_waiters_lock:
                self._order_updates[order_id] = data
                ev = self._order_waiters.get(order_id)
            if ev:
                ev.set()
        if self.on_order_update_callback:
            try:
                self.on_order_update_callback(data)
            except Exception as e:
                log.warning(f"⚠️ on_order_update_callback error: {e}")

    # ── Awaiting An Order's Terminal Status ───────────────────────────────
    def await_order_update(self, order_id: str, timeout: float = 6.0) -> dict | None:
        """
        Block (briefly) until a terminal status update (COMPLETE/REJECTED/
        CANCELLED) for this order_id arrives over the WebSocket, or until
        timeout. Returns the update dict, or None if nothing arrived in time
        (caller should then fall back to a one-shot REST poll as a safety
        net — NOT a repeating poll loop).
        """
        with self._order_waiters_lock:
            existing = self._order_updates.get(order_id)
            if existing and existing.get("status") in _TERMINAL_STATUSES:
                return existing
            ev = self._order_waiters.setdefault(order_id, threading.Event())

        fired = ev.wait(timeout=timeout)
        with self._order_waiters_lock:
            self._order_waiters.pop(order_id, None)
            update = self._order_updates.get(order_id)

        if fired and update and update.get("status") in _TERMINAL_STATUSES:
            return update
        return None