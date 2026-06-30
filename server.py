from flask import Flask, jsonify

app = Flask(__name__)
_dashboard_state_fn = None

@app.route("/dashboard")
def dashboard():
    if _dashboard_state_fn is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(_dashboard_state_fn())

@app.route("/report")
def report():
    if _dashboard_state_fn is None:
        return jsonify({"error": "Dashboard state callback not registered"}), 500
    return jsonify(_dashboard_state_fn())

def register_dashboard_state(fn):
    global _dashboard_state_fn
    _dashboard_state_fn = fn

def run_server(state_getter=None):
    if state_getter is not None:
        register_dashboard_state(state_getter)
    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
