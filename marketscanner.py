"""
market_scanner.py — Concurrent NSE-Wide Signal Scan
=====================================================
Runs the bot's EXISTING, UNCHANGED check_signal() against every symbol in
today's dynamic universe, in parallel, using a ThreadPoolExecutor. This
module contains ZERO strategy logic of its own — get_candles_fn and
check_signal_fn are passed in directly from bot.py, so there is exactly
one copy of the strategy anywhere in the codebase.

Only this module's job: fan a potentially large symbol list out across
worker threads, fetch each symbol's candles, run the unchanged strategy
function on them, and collect whatever qualifies — nothing about
indicators, entry/exit rules, or scoring math lives here beyond passing
through whatever check_signal_fn already computed.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("market_scanner")


def scan_universe_for_signals(
    symbols: list[str],
    get_candles_fn,
    check_signal_fn,
    ema_length: int,
    max_workers: int = 16,
) -> list[dict]:
    """
    Returns a list of qualifying signal dicts (exactly what check_signal_fn
    returns — unchanged fields, including the "score" field check_signal
    already attaches for ranking purposes). Symbols with insufficient
    candle history or any per-symbol error are simply skipped (logged at
    debug level) — one bad symbol never aborts the rest of the scan.
    """
    if not symbols:
        return []

    candles_needed = ema_length + 60

    def scan_one(symbol: str) -> dict | None:
        try:
            candles = get_candles_fn(symbol, candles_needed)
            if not candles:
                return None
            return check_signal_fn(symbol, candles)
        except Exception as e:
            log.debug(f"Scan error {symbol}: {e}")
            return None

    results = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scan_one, s): s for s in symbols}
        for fut in as_completed(futures):
            signal = fut.result()
            if signal:
                results.append(signal)
    elapsed = time.monotonic() - started
    log.debug(f"market_scanner: scanned {len(symbols)} symbols in {elapsed:.1f}s, {len(results)} qualified")
    return results