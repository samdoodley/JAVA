"""Data provider layer for reading the trading bot report file.

This module abstracts the JSON source so the application can later be
replaced with SQLite, REST, or WebSocket providers without changing the
frontend or API contracts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from config import BOT_REPORT_PATH

logger = logging.getLogger(__name__)


class TradeReportProvider:
    """Read and validate the trading bot report JSON file.

    The provider watches the configured file path and reloads automatically
    when the file content changes. It also tolerates missing files and
    partially written JSON by returning a safe fallback payload.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or BOT_REPORT_PATH)
        self._last_mtime: Optional[float] = None
        self._last_size: Optional[int] = None
        self._last_content: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._lock = threading.RLock()
        self._load_once()

    def _load_once(self) -> None:
        with self._lock:
            self._read_report(force=True)

    def get_report(self) -> Dict[str, Any]:
        """Return the latest parsed report payload.

        The implementation reloads automatically if the file appears to have
        changed on disk.
        """
        with self._lock:
            self._read_report(force=False)
            return self._snapshot_payload()

    def get_status(self) -> Dict[str, Any]:
        """Return provider status for UI display."""
        with self._lock:
            self._read_report(force=False)
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "last_error": self._last_error,
                "last_updated": self._last_content.get("timestamp") if self._last_content else None,
                "mtime": self._last_mtime,
                "size": self._last_size,
            }

    def _read_report(self, force: bool) -> None:
        if not self.path.exists():
            self._last_error = "Trading Bot Offline"
            if force or self._last_content is None:
                self._last_content = self._fallback_payload()
            self._last_mtime = None
            self._last_size = None
            return

        try:
            stat = self.path.stat()
        except OSError as exc:
            self._last_error = f"Unable to read report: {exc}"
            if force or self._last_content is None:
                self._last_content = self._fallback_payload()
            self._last_mtime = None
            self._last_size = None
            return

        mtime = stat.st_mtime
        size = stat.st_size
        should_reload = force or self._last_mtime != mtime or self._last_size != size

        if not should_reload:
            return

        self._last_mtime = mtime
        self._last_size = size

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw_text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            self._last_error = f"Unable to read report: {exc}"
            self._last_content = self._fallback_payload()
            return

        if not raw_text.strip():
            self._last_error = "Waiting for valid trading data"
            self._last_content = self._fallback_payload()
            return

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            self._last_error = f"Invalid JSON: {exc}"
            self._last_content = self._fallback_payload()
            return

        if not isinstance(parsed, dict):
            self._last_error = "Waiting for valid trading data"
            self._last_content = self._fallback_payload()
            return

        self._last_error = None
        self._last_content = self._normalise_payload(parsed)

    def _snapshot_payload(self) -> Dict[str, Any]:
        payload = self._last_content or self._fallback_payload()
        return dict(payload)

    def _fallback_payload(self) -> Dict[str, Any]:
        return {
            "timestamp": None,
            "equity": 0.0,
            "deployed": 0.0,
            "pnl_today": 0.0,
            "wins": 0,
            "losses": 0,
            "total_trades": 0,
            "open_positions": 0,
            "unique_symbols_traded": 0,
            "symbol_summary": {},
            "status": self._last_error or "Waiting for valid trading data",
        }

    def _normalise_payload(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        symbol_summary = raw.get("symbol_summary") or {}
        if not isinstance(symbol_summary, dict):
            symbol_summary = {}

        normalised = {
            "timestamp": raw.get("timestamp"),
            "equity": self._to_float(raw.get("equity"), 0.0),
            "deployed": self._to_float(raw.get("deployed"), 0.0),
            "pnl_today": self._to_float(raw.get("pnl_today"), 0.0),
            "wins": self._to_int(raw.get("wins"), 0),
            "losses": self._to_int(raw.get("losses"), 0),
            "total_trades": self._to_int(raw.get("total_trades"), 0),
            "open_positions": self._to_int(raw.get("open_positions"), 0),
            "unique_symbols_traded": self._to_int(raw.get("unique_symbols_traded"), 0),
            "symbol_summary": symbol_summary,
        }
        normalised["status"] = self._derive_status(normalised)
        return normalised

    def _derive_status(self, payload: Dict[str, Any]) -> str:
        if payload.get("timestamp") is None:
            return "Waiting for valid trading data"
        return "Live"

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
