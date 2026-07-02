"""
mt5_config.py

MT5 account credentials.

Set these environment variables before running main.py:
    MT5_LOGIN
    MT5_PASSWORD
    MT5_SERVER

Keeping credentials outside source files avoids leaking live/demo account
details when the bot folder is shared or backed up.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


LOGIN = 25687228
PASSWORD = "Dhairya7@"
SERVER = "VantageMarkets-Demo"
