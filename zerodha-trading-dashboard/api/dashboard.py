"""Flask Blueprint for the trading dashboard endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template

from services.data_provider import TradeReportProvider

logger = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)

provider = TradeReportProvider()


@bp.route("/")
def index() -> str:
    """Render the dashboard page."""
    return render_template("dashboard.html")


@bp.route("/api/dashboard")
def api_dashboard() -> Any:
    """Return the full dashboard payload for the frontend."""
    report = provider.get_report()
    status = provider.get_status()
    return jsonify(_build_dashboard_payload(report, status))


@bp.route("/api/statistics")
def api_statistics() -> Any:
    """Return summary statistics for cards and charts."""
    report = provider.get_report()
    return jsonify(_build_statistics_payload(report))


@bp.route("/api/open_positions")
def api_open_positions() -> Any:
    """Return a table-friendly view of open positions."""
    report = provider.get_report()
    symbols = report.get("symbol_summary") or {}

    rows: List[Dict[str, Any]] = []
    for symbol, data in sorted(symbols.items()):
        if not isinstance(data, dict):
            continue
        rows.append(
            {
                "symbol": symbol,
                "open_qty": self_or_default(data.get("open_qty"), 0),
                "capital_deployed": self_or_default(data.get("capital_deployed"), 0.0),
                "unrealized_pnl": self_or_default(data.get("unrealized_pnl"), 0.0),
                "closed_qty": self_or_default(data.get("closed_qty"), 0),
                "closed_pnl": self_or_default(data.get("closed_pnl"), 0.0),
                "buy_trades": self_or_default(data.get("buy_trades"), 0),
                "sell_trades": self_or_default(data.get("sell_trades"), 0),
                "open_position_count": self_or_default(data.get("open_position_count"), 1 if self_or_default(data.get("open_qty"), 0) != 0 else 0),
            }
        )

    return jsonify({"positions": rows})


@bp.route("/api/symbol_summary")
def api_symbol_summary() -> Any:
    """Return the raw symbol summary for downstream consumers."""
    report = provider.get_report()
    return jsonify({"symbol_summary": report.get("symbol_summary") or {}})


@bp.route("/api/health")
def api_health() -> Any:
    """Return provider health information."""
    return jsonify(provider.get_status())


def _build_dashboard_payload(report: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "report": report,
        "status": status,
        "summary": _build_statistics_payload(report),
        "positions": _build_positions_payload(report),
    }


def _build_statistics_payload(report: Dict[str, Any]) -> Dict[str, Any]:
    symbol_summary = report.get("symbol_summary") or {}
    win_rate = 0.0
    if report.get("wins", 0) + report.get("losses", 0) > 0:
        win_rate = round((report.get("wins", 0) / (report.get("wins", 0) + report.get("losses", 0))) * 100, 2)

    profitable = []
    losing = []
    for symbol, data in symbol_summary.items():
        if isinstance(data, dict):
            pnl = self_or_default(data.get("unrealized_pnl"), 0.0)
            if pnl >= 0:
                profitable.append({"symbol": symbol, "value": pnl})
            else:
                losing.append({"symbol": symbol, "value": abs(pnl)})

    profitable.sort(key=lambda item: item["value"], reverse=True)
    losing.sort(key=lambda item: item["value"], reverse=True)

    return {
        "pnl_today": self_or_default(report.get("pnl_today"), 0.0),
        "realized_pnl": self_or_default(report.get("realized_pnl"), 0.0),
        "equity": self_or_default(report.get("equity"), 0.0),
        "deployed": self_or_default(report.get("deployed"), 0.0),
        "win_rate": win_rate,
        "wins": self_or_default(report.get("wins"), 0),
        "losses": self_or_default(report.get("losses"), 0),
        "total_trades": self_or_default(report.get("total_trades"), 0),
        "open_positions": self_or_default(report.get("open_positions"), 0),
        "unique_symbols": self_or_default(report.get("unique_symbols_traded"), 0),
        "top_profitable": profitable[:5],
        "top_losing": losing[:5],
        "status": report.get("status", "Waiting for valid trading data"),
    }


def _build_positions_payload(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    symbol_summary = report.get("symbol_summary") or {}
    positions: List[Dict[str, Any]] = []
    for symbol, data in sorted(symbol_summary.items()):
        if not isinstance(data, dict):
            continue
        positions.append(
            {
                "symbol": symbol,
                "open_qty": self_or_default(data.get("open_qty"), 0),
                "capital_deployed": self_or_default(data.get("capital_deployed"), 0.0),
                "unrealized_pnl": self_or_default(data.get("unrealized_pnl"), 0.0),
                "closed_qty": self_or_default(data.get("closed_qty"), 0),
                "closed_pnl": self_or_default(data.get("closed_pnl"), 0.0),
                "buy_trades": self_or_default(data.get("buy_trades"), 0),
                "sell_trades": self_or_default(data.get("sell_trades"), 0),
                "open_position_count": self_or_default(data.get("open_position_count"), 1 if self_or_default(data.get("open_qty"), 0) != 0 else 0),
            }
        )
    return positions


def self_or_default(value: Any, default: Any) -> Any:
    return value if value is not None else default
