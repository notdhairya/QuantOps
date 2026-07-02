"""
config.py

Central configuration for the EMA Dynamic Support & Resistance Retest bot.

All tunable parameters live here as module-level constants, grouped by
concern. No other module should hard-code a magic number that belongs
here -- e.g. strategy/confirmation.py imports RSI_CONFIRMATION_MIN/MAX
from this file rather than embedding "40" and "60" inline.

This file is intentionally framework-free: it contains only data, no
logic, so it can be imported by any module without circular-import risk.
"""

from __future__ import annotations

import os

# ─────────────────────────────────────────────────────────────────────────────
# SYMBOLS & TIMEFRAMES
# ─────────────────────────────────────────────────────────────────────────────

# Symbols the bot scans. Each symbol is scanned independently across
# every timeframe in TIMEFRAMES below.
SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "DJ30.r",
    "NAS100.r",
]

# Every timeframe scans independently and can hold its own simultaneous
# trade (e.g. H1 Buy + M30 Buy + M15 Buy on the same symbol at once).
TIMEFRAMES: list[str] = ["M5", "M15", "M30", "H1"]

# Higher-timeframe trend filter used by the multi-timeframe scanner.
# Maps each tradable timeframe to the higher timeframe that confirms its
# trend bias before entries are considered on the tradable timeframe.
HIGHER_TIMEFRAME_MAP = {
    "M5": "M15",
    "M15": "M30",
    "M30": "H1",
    "H1": "H4",
}

MAX_TRADES_PER_SYMBOL_TIMEFRAME = 1

ALLOW_HEDGING = False

MIN_SIGNAL_SCORE = 9

# ─────────────────────────────────────────────────────────────────────────────
# EMA SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

EMA_FAST_PERIOD: int = 5
EMA_MID_PERIOD: int = 10
EMA_SLOW_PERIOD: int = 20

# ─────────────────────────────────────────────────────────────────────────────
# REVERSAL / CANDLESTICK PATTERN SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

# Minimum body-to-range ratio for a candle to qualify as a "Strong
# Momentum Candle" (large body, small wicks).
STRONG_MOMENTUM_BODY_RATIO: float = 0.65

# Minimum ratio of (opposing wick length / body length) for a candle to
# qualify as a "Hammer" (long tail, small body, little/no opposite wick).
HAMMER_WICK_TO_BODY_RATIO: float = 2.0

# Maximum body-to-range ratio allowed for the small wick on the opposite
# side of a Hammer, ensuring the candle isn't just a big momentum candle.
HAMMER_MAX_OPPOSING_WICK_RATIO: float = 0.25

# ─────────────────────────────────────────────────────────────────────────────
# CONFIRMATION SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

# RSI confirmation zone: a pullback must show RSI inside this band to be
# considered "cooled off but not broken" before confirmation is accepted.
RSI_PERIOD: int = 14
RSI_CONFIRMATION_MIN: float = 40.0
RSI_CONFIRMATION_MAX: float = 60.0

# ─────────────────────────────────────────────────────────────────────────────
# RISK / REWARD SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

RISK_REWARD_RATIO: float = 2.0       # Take-profit = 1 : 2 risk:reward
RISK_PERCENT_PER_TRADE: float = 1.0  # Risk exactly 1% of balance per trade

# Stop-loss execution buffer.
#
# MT5 charts often show bid candles. BUY positions close at bid, while SELL
# positions close at ask, so a SELL SL can be hit by spread even when the chart
# high appears slightly below the SL line. The bot widens initial SL by the
# live spread times this multiplier, plus the fixed pip buffer below.
STOP_SPREAD_BUFFER_MULTIPLIER: float = 1.5
EXTRA_STOP_BUFFER_PIPS: float = 1.0

# Trailing stop activates once unrealized profit reaches this multiple
# of the initial risk distance (1R).
TRAILING_ACTIVATION_R: float = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# DAILY PROTECTION SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

MAX_CONSECUTIVE_LOSSES: int = 2      # Stop new trades after 2 losses in a row
MAX_DAILY_LOSS_PERCENT: float = 3.0  # Stop new trades at -3% realized for the day

# ─────────────────────────────────────────────────────────────────────────────
# LOT SIZING SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

LOT_STEP: float = 0.01
MIN_LOT: float = 0.01
MAX_LOT: float = 100.0

# Fallback pip value (USD per pip per 1.0 standard lot) used only if
# MT5's symbol_info cannot be queried for a given symbol. Most USD-quoted
# majors are close to 10.0.
DEFAULT_PIP_VALUE: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCH SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

# Number of historical bars fetched per scan. Must be large enough to
# cover EMA20 warm-up plus enough lookback for swing/pullback detection.
BARS_TO_FETCH: int = 200

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE: str = "trading_bot.log"
LOG_LEVEL: str = "INFO"  # one of: DEBUG, INFO, WARNING, ERROR

# Telegram notifications.
#
# Create a Telegram bot with BotFather, then set these environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#
# The bot sends only startup/shutdown and trade open/close events. It also
# responds to /status when status commands are enabled.
TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = "8637356735:AAH7pl07SSwdKb-inKzremxm1IzL_S_tAT4"
TELEGRAM_CHAT_ID = "5813046432"
TELEGRAM_STATUS_COMMANDS: bool = True
TELEGRAM_POLL_INTERVAL_SECONDS: int = 3
TELEGRAM_TIMEOUT_SECONDS: int = 8

# ─────────────────────────────────────────────────────────────────────────────
# SCANNER LOOP SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

# Seconds between each full scan cycle across all symbols/timeframes.
SCAN_INTERVAL_SECONDS: int = 15

# Always print a small console heartbeat every scan, even while routine
# INFO logs are quiet. This makes it obvious the bot is alive.
PRINT_WORKING_HEARTBEAT: bool = True

# MT5 magic number used to tag all orders placed by this bot, so the
# bot can distinguish its own trades from manually-placed ones.
MAGIC_NUMBER: int = 778899
