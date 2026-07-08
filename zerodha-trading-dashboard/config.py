"""Application configuration for the trading dashboard."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Configurable path to the trading bot report JSON.
# Override this in your environment or edit it directly.
BOT_REPORT_PATH = os.getenv(
    "BOT_REPORT_PATH",
    "/home/samkumarg/API ZERODHA/trade_report.json",
)

DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
