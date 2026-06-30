import json
import os
from datetime import datetime


def _round(value: float) -> float:
    return round(value, 2)


def generate_trade_report(state: dict) -> dict:
    trades = state.get("trades", [])
    positions = list(state.get("positions", {}).values())

    symbol_summary: dict[str, dict] = {}

    for trade in trades:
        symbol = trade["symbol"]
        if symbol not in symbol_summary:
            symbol_summary[symbol] = {
                "symbol": symbol,
                "closed_trades": 0,
                "closed_qty": 0,
                "realized_pnl": 0.0,
                "buy_trades": 0,
                "sell_trades": 0,
                "open_qty": 0,
                "open_position_count": 0,
                "capital_deployed": 0.0,
                "unrealized_pnl": 0.0,
            }
        summary = symbol_summary[symbol]
        summary["closed_trades"] += 1
        summary["closed_qty"] += trade.get("qty", 0)
        summary["realized_pnl"] += trade.get("pnl", 0.0)
        if trade.get("direction") == "BUY":
            summary["buy_trades"] += 1
        else:
            summary["sell_trades"] += 1

    for pos in positions:
        symbol = pos["symbol"]
        if symbol not in symbol_summary:
            symbol_summary[symbol] = {
                "symbol": symbol,
                "closed_trades": 0,
                "closed_qty": 0,
                "realized_pnl": 0.0,
                "buy_trades": 0,
                "sell_trades": 0,
                "open_qty": 0,
                "open_position_count": 0,
                "capital_deployed": 0.0,
                "unrealized_pnl": 0.0,
            }
        summary = symbol_summary[symbol]
        summary["open_qty"] += pos.get("qty", 0)
        summary["open_position_count"] += 1
        summary["capital_deployed"] += pos.get("entry", 0.0) * pos.get("qty", 0)
        summary["unrealized_pnl"] += pos.get("pnl", 0.0)

    for summary in symbol_summary.values():
        summary["realized_pnl"] = _round(summary["realized_pnl"])
        summary["capital_deployed"] = _round(summary["capital_deployed"])
        summary["unrealized_pnl"] = _round(summary["unrealized_pnl"])

    report = {
        "timestamp": datetime.now().isoformat(),
        "equity": _round(state.get("equity", 0.0)),
        "deployed": _round(state.get("deployed", 0.0)),
        "pnl_today": _round(state.get("pnl_today", 0.0)),
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "total_trades": len(trades),
        "open_positions": len(positions),
        "unique_symbols_traded": len(symbol_summary),
        "symbol_summary": sorted(symbol_summary.values(), key=lambda x: x["symbol"]),
        "positions": positions,
        "trades": trades[:50],
    }
    return report


def save_trade_report(state: dict, path: str | None = None) -> str:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_report.json")
    report = generate_trade_report(state)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return path
