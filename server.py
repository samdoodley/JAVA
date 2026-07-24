import json
import os
from functools import wraps
from flask import Flask, jsonify, request, Response, render_template_string

from config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_USER, DASHBOARD_PASSWORD

app = Flask(__name__)
_dashboard_state_fn = None


def _load_report_from_disk():
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_report.json")
    if not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_state_dict():
    if _dashboard_state_fn is not None:
        return _dashboard_state_fn()
    return _load_report_from_disk()


# ── Basic Auth ─────────────────────────────────────────────────────────────
def _check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASSWORD


def _auth_challenge():
    return Response(
        "Login required to view the WickFill dashboard.",
        401,
        {"WWW-Authenticate": 'Basic realm="WickFill Dashboard"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _auth_challenge()
        return f(*args, **kwargs)
    return decorated


@app.route("/dashboard")
@requires_auth
def dashboard():
    state = _get_state_dict()
    if state is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(state)


@app.route("/report")
@requires_auth
def report():
    state = _get_state_dict()
    if state is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(state)


MOBILE_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WickFill Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px 12px 40px;
    font-family: -apple-system, Roboto, Segoe UI, sans-serif;
    background: #0d1117; color: #e6edf3;
  }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .meta { color: #8b949e; font-size: 12px; margin-bottom: 16px; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 10px 12px;
  }
  .card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; }
  .card .value { font-size: 18px; font-weight: 600; margin-top: 2px; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  section { margin-bottom: 20px; }
  h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; margin: 0 0 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #21262d; }
  th { color: #8b949e; font-weight: 500; }
  .badge {
    display: inline-block; padding: 2px 6px; border-radius: 5px; font-size: 11px;
    background: #21262d;
  }
  .empty { color: #8b949e; font-size: 13px; padding: 8px 0; }
  #status-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: #f85149; margin-right: 6px; vertical-align: middle;
  }
  #status-dot.live { background: #3fb950; }
</style>
</head>
<body>
  <h1><span id="status-dot"></span>WickFill Dashboard</h1>
  <div class="meta" id="meta">Loading…</div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Open Positions (<span id="pos-count">0</span>)</h2>
    <div id="positions"></div>
  </section>

  <section>
    <h2>Recent Trades</h2>
    <div id="trades"></div>
  </section>

<script>
function money(v) {
  const n = Number(v) || 0;
  return (n < 0 ? "-₹" : "₹") + Math.abs(n).toLocaleString("en-IN", {maximumFractionDigits: 2});
}
function cls(v) { return Number(v) >= 0 ? "pos" : "neg"; }

async function refresh() {
  try {
    const res = await fetch("/dashboard", { cache: "no-store" });
    if (!res.ok) throw new Error("bad response");
    const d = await res.json();
    document.getElementById("status-dot").classList.add("live");

    document.getElementById("meta").textContent =
      `${d.paper_mode ? "PAPER" : "LIVE"} · ${d.scan_status || "-"} · ${d.market_open ? "Market open" : "Market closed"} · ${d.time || ""}`;

    document.getElementById("cards").innerHTML = `
      <div class="card"><div class="label">Equity</div><div class="value">${money(d.equity)}</div></div>
      <div class="card"><div class="label">P&L Today</div><div class="value ${cls(d.pnl_today)}">${money(d.pnl_today)}</div></div>
      <div class="card"><div class="label">Deployed</div><div class="value">${money(d.deployed)}</div></div>
      <div class="card"><div class="label">Available</div><div class="value">${money(d.available_margin)}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value">${d.win_rate ?? 0}%</div></div>
      <div class="card"><div class="label">Wins / Losses</div><div class="value">${d.wins ?? 0} / ${d.losses ?? 0}</div></div>
    `;

    const positions = d.positions || [];
    document.getElementById("pos-count").textContent = positions.length;
    document.getElementById("positions").innerHTML = positions.length ? `
      <table><thead><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>SL</th><th>TP</th><th>P&L</th></tr></thead>
      <tbody>${positions.map(p => `
        <tr>
          <td>${p.symbol}</td>
          <td><span class="badge">${p.direction}</span></td>
          <td>${p.qty}</td>
          <td>${money(p.entry)}</td>
          <td>${money(p.sl)}</td>
          <td>${money(p.tp)}</td>
          <td class="${cls(p.pnl)}">${money(p.pnl)}</td>
        </tr>`).join("")}
      </tbody></table>` : `<div class="empty">No open positions.</div>`;

    const trades = (d.trades || []).slice(0, 20);
    document.getElementById("trades").innerHTML = trades.length ? `
      <table><thead><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>Exit</th><th>P&L</th><th>Reason</th></tr></thead>
      <tbody>${trades.map(t => `
        <tr>
          <td>${t.symbol}</td>
          <td><span class="badge">${t.direction}</span></td>
          <td>${t.qty}</td>
          <td>${money(t.exit)}</td>
          <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
          <td>${t.reason || ""}</td>
        </tr>`).join("")}
      </tbody></table>` : `<div class="empty">No trades yet.</div>`;

  } catch (e) {
    document.getElementById("status-dot").classList.remove("live");
    document.getElementById("meta").textContent = "Connection lost — retrying…";
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.route("/")
@requires_auth
def index():
    return render_template_string(MOBILE_PAGE)


def register_dashboard_state(fn):
    global _dashboard_state_fn
    _dashboard_state_fn = fn


def run_server(state_getter=None):
    if state_getter is not None:
        register_dashboard_state(state_getter)
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, threaded=True)


if __name__ == "__main__":
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, threaded=True)