"""Entry point for the Flask trading dashboard application."""

from __future__ import annotations

import logging
import os

from flask import Flask

from api.dashboard import bp as dashboard_bp
from config import BASE_DIR, DEBUG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))
    app.config["DEBUG"] = DEBUG
    app.register_blueprint(dashboard_bp)
    return app


app = create_app()


def main() -> None:
    """Run the Flask application with a resilient port fallback."""
    port = int(os.getenv("PORT", "5000"))
    try:
        app.run(host="0.0.0.0", port=port, debug=DEBUG)
    except OSError as exc:
        if "Address already in use" in str(exc):
            fallback_port = port + 1
            logger.warning("Port %s is busy; trying %s instead.", port, fallback_port)
            app.run(host="0.0.0.0", port=fallback_port, debug=DEBUG)
        else:
            raise


if __name__ == "__main__":
    main()
