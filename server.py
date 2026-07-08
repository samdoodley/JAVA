import json
import os
from flask import Flask, jsonify

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


@app.route("/dashboard")
def dashboard():
    if _dashboard_state_fn is not None:
        return jsonify(_dashboard_state_fn())
    report = _load_report_from_disk()
    if report is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(report)


@app.route("/report")
def report():
    if _dashboard_state_fn is not None:
        return jsonify(_dashboard_state_fn())
    report = _load_report_from_disk()
    if report is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(report)


def register_dashboard_state(fn):
    global _dashboard_state_fn
    _dashboard_state_fn = fn


def run_server(state_getter=None):
    if state_getter is not None:
        register_dashboard_state(state_getter)
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
