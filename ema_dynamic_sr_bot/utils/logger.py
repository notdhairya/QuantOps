"""
utils/logger.py

Centralized logging configuration with custom named levels.

Custom levels added on top of Python's standard INFO/WARNING/ERROR:

    Level name   Value   Used for
    ─────────────────────────────────────────────────────────────
    SIGNAL        25     A new BUY/SELL signal was generated
    ORDER         26     An order was sent to the broker
    TRADE         27     A trade was opened / closed / modified
    RISK          28     Risk / lot size calculation result
    TRAIL         29     Trailing stop activated or updated
    DAILY         35     Daily risk manager event (halt / reset)

These sit between INFO (20) and WARNING (30) in priority, so setting
LOG_LEVEL = "INFO" in config.py will show all of them, and setting it
to "WARNING" will hide them (useful in production when you only want
to see problems).

Usage in any module:
    from utils.logger import get_logger
    logger = get_logger(__name__)

    logger.signal("BUY signal generated | EURUSD M15 ...")
    logger.order("Order sent | EURUSD ticket=123456")
    logger.trade("Trade opened | EURUSD lots=0.10 sl=1.0950")
    logger.risk("Lot size | EURUSD balance=10000 risk=1% -> 0.10 lots")
    logger.trail("EMA trail activated | EURUSD profit=1.2R")
    logger.daily("Daily risk halt | 2 consecutive losses")
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Custom level definitions
# ─────────────────────────────────────────────────────────────────────────────

SIGNAL_LEVEL = 25
ORDER_LEVEL  = 26
TRADE_LEVEL  = 27
RISK_LEVEL   = 28
TRAIL_LEVEL  = 29
DAILY_LEVEL  = 35

logging.addLevelName(SIGNAL_LEVEL, "SIGNAL")
logging.addLevelName(ORDER_LEVEL,  "ORDER ")
logging.addLevelName(TRADE_LEVEL,  "TRADE ")
logging.addLevelName(RISK_LEVEL,   "RISK  ")
logging.addLevelName(TRAIL_LEVEL,  "TRAIL ")
logging.addLevelName(DAILY_LEVEL,  "DAILY ")


# ─────────────────────────────────────────────────────────────────────────────
# Extended Logger class
# ─────────────────────────────────────────────────────────────────────────────

class TradingLogger(logging.Logger):
    """
    Logger subclass that adds one convenience method per custom level,
    so callers can write logger.signal(...) / logger.order(...) etc.
    instead of the verbose logger.log(SIGNAL_LEVEL, ...) form.
    """

    def signal(self, message: str, *args, **kwargs) -> None:
        """Log a generated BUY/SELL signal (level SIGNAL = 25)."""
        if self.isEnabledFor(SIGNAL_LEVEL):
            self._log(SIGNAL_LEVEL, message, args, **kwargs)

    def order(self, message: str, *args, **kwargs) -> None:
        """Log an order being sent to the broker (level ORDER = 26)."""
        if self.isEnabledFor(ORDER_LEVEL):
            self._log(ORDER_LEVEL, message, args, **kwargs)

    def trade(self, message: str, *args, **kwargs) -> None:
        """Log a trade open / close / modify event (level TRADE = 27)."""
        if self.isEnabledFor(TRADE_LEVEL):
            self._log(TRADE_LEVEL, message, args, **kwargs)

    def risk(self, message: str, *args, **kwargs) -> None:
        """Log a risk / lot-size calculation (level RISK = 28)."""
        if self.isEnabledFor(RISK_LEVEL):
            self._log(RISK_LEVEL, message, args, **kwargs)

    def trail(self, message: str, *args, **kwargs) -> None:
        """Log a trailing-stop activation or update (level TRAIL = 29)."""
        if self.isEnabledFor(TRAIL_LEVEL):
            self._log(TRAIL_LEVEL, message, args, **kwargs)

    def daily(self, message: str, *args, **kwargs) -> None:
        """Log a daily risk manager event (level DAILY = 35)."""
        if self.isEnabledFor(DAILY_LEVEL):
            self._log(DAILY_LEVEL, message, args, **kwargs)


# Register the subclass so every logging.getLogger() call returns a
# TradingLogger instead of the base Logger.
logging.setLoggerClass(TradingLogger)


# ─────────────────────────────────────────────────────────────────────────────
# Format & setup
# ─────────────────────────────────────────────────────────────────────────────

_LOG_FORMAT  = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(
    level: int = logging.INFO,
    log_file: str | None = "trading_bot.log",
    console_filter: logging.Filter | None = None,
) -> None:
    """
    Configure the root logger once at startup (called from main.py).

    Args:
        level:          Minimum log level to emit. logging.INFO (20) will
                         show all custom levels (SIGNAL=25 through DAILY=35)
                         plus INFO, WARNING, ERROR, CRITICAL.
        log_file:       Path to also write logs to. None = console only.
        console_filter: Optional filter attached ONLY to the console handler.
                         The file handler always gets everything unfiltered
                         so the full scan history is always in the log file.
                         Pass main.py's _SmartConsoleFilter() here to suppress
                         routine INFO chatter while keeping instant logging
                         for important events (SIGNAL / ORDER / TRADE etc.).
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    if console_filter is not None:
        console_handler.addFilter(console_filter)
    root.addHandler(console_handler)

    if log_file:
        # File handler: NO filter -- every record goes to the file.
        file_handler = logging.FileHandler(Path(log_file), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> TradingLogger:
    """
    Get a module-scoped TradingLogger.

    Args:
        name: Pass __name__ so log lines show which module emitted them.

    Returns:
        A TradingLogger with .signal() .order() .trade() .risk()
        .trail() .daily() methods in addition to the standard ones.
    """
    return logging.getLogger(name)  # type: ignore[return-value]