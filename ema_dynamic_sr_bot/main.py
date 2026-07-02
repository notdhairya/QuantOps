"""
main.py

Entry point: the multi-timeframe scanner loop.

Logging behaviour:
  - SIGNAL / ORDER / TRADE / RISK / TRAIL / DAILY / WARNING / ERROR
    → printed to console INSTANTLY whenever they occur.
  - INFO (routine scan chatter: trend detected, reversal detected,
    pullback detected, etc.)
    → suppressed on the console; only flushed every
      STATUS_PRINT_INTERVAL_SECONDS (default 15 min) as a heartbeat.
  - ALL levels always written to trading_bot.log in real time so the
    full history is available for review.

Scanning never stops -- the 15-minute print interval only controls
console verbosity, not the scan frequency.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

import config
import mt5_config
from execution import order_manager
from execution.trade_manager import TradeManager
from indicators.ema import EMASet
from risk.daily_risk_manager import DailyRiskManager
from strategy.signal_generator import generate_signal
from utils.helpers import fetch_candles, get_current_price
from utils.logger import (
    SIGNAL_LEVEL, ORDER_LEVEL, TRADE_LEVEL,
    RISK_LEVEL, TRAIL_LEVEL, DAILY_LEVEL,
    get_logger, setup_logging,
)
from utils.telegram_notifier import telegram_notifier

logger = get_logger(__name__)
STARTED_AT: datetime | None = None
LAST_SCAN_AT: datetime | None = None
LAST_BALANCE: float = 0.0
LAST_CYCLE: int = 0

# Print a routine status heartbeat to the console every N seconds.
# Important events (SIGNAL/ORDER/TRADE/RISK/TRAIL/DAILY/WARNING/ERROR)
# always bypass this and print immediately.
STATUS_PRINT_INTERVAL_SECONDS = 15 * 60   # 15 minutes


def print_working_heartbeat(cycle: int, balance: float, open_trades: int) -> None:
    """Always-visible console heartbeat, independent of logging filters."""
    if not config.PRINT_WORKING_HEARTBEAT:
        return

    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(
        f"🟢 Bot working | scan #{cycle} | {timestamp} UTC | "
        f"balance={balance:.2f} | open_trades={open_trades}",
        flush=True,
    )


def build_status_message(trade_manager: TradeManager, daily_risk_manager: DailyRiskManager) -> str:
    """Build the Telegram /status response."""
    now = datetime.now(timezone.utc)
    open_trades = trade_manager.get_all_open_trades()
    uptime = "unknown"
    if STARTED_AT is not None:
        uptime_seconds = int((now - STARTED_AT).total_seconds())
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m {seconds}s"

    last_scan = LAST_SCAN_AT.strftime("%Y-%m-%d %H:%M:%S UTC") if LAST_SCAN_AT else "not yet"
    halt_text = daily_risk_manager.halt_reason or "no"

    lines = [
    "══════════════════════",
    "🤖 QUANTOPS STATUS",
    "══════════════════════",
    "",
    "🟢 Status        : RUNNING",
    f"⏱ Uptime        : {uptime}",
    f"🕒 Last Scan     : {last_scan}",
    f"🔄 Scan Cycle    : #{LAST_CYCLE}",
    "",
    f"💰 Balance       : ${LAST_BALANCE:.2f}",
    f"📂 Open Trades   : {len(open_trades)}",
    f"🛑 Daily Halt    : {halt_text}",
]

    for trade in open_trades[:10]:
        lines.append(
            f"- #{trade.ticket} {trade.symbol} {trade.timeframe} "
            f"{trade.direction.value.upper()} lots={trade.lot_size} "
            f"entry={trade.entry_price:.5f} sl={trade.stop_loss:.5f} tp={trade.take_profit:.5f}"
        )

    if len(open_trades) > 10:
        lines.append(f"...and {len(open_trades) - 10} more")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Smart console filter
# ─────────────────────────────────────────────────────────────────────────────

# Levels that always print to the console immediately, regardless of the
# 15-minute quiet period. Everything important is in this set.
_INSTANT_LEVELS = {
    SIGNAL_LEVEL,
    ORDER_LEVEL,
    TRADE_LEVEL,
    RISK_LEVEL,
    TRAIL_LEVEL,
    DAILY_LEVEL,
    logging.WARNING,
    logging.ERROR,
    logging.CRITICAL,
}


class _SmartConsoleFilter(logging.Filter):
    """
    Allows all records with an "important" level through immediately.
    Blocks INFO-level records unless the quiet period has expired,
    at which point it lets ONE INFO record through (the heartbeat) and
    resets the timer.

    This filter is attached only to the console handler; the file
    handler has no filter, so every record always goes to the log file.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_status_print: float = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        # Always show important events instantly
        if record.levelno in _INSTANT_LEVELS:
            return True

        # For INFO and below: only show when the interval has elapsed
        now = time.time()
        if now - self._last_status_print >= STATUS_PRINT_INTERVAL_SECONDS:
            self._last_status_print = now
            return True

        return False   # suppress routine chatter in the console


# ─────────────────────────────────────────────────────────────────────────────
# Scan helpers (unchanged from before)
# ─────────────────────────────────────────────────────────────────────────────

def build_ema_set(symbol: str, timeframe: str) -> tuple[list, EMASet]:
    candles = fetch_candles(symbol, timeframe, config.BARS_TO_FETCH)
    ema_set = EMASet(
        candles,
        fast_period=config.EMA_FAST_PERIOD,
        mid_period=config.EMA_MID_PERIOD,
        slow_period=config.EMA_SLOW_PERIOD,
    )
    return candles, ema_set


def scan_symbol_timeframe(
    symbol: str,
    timeframe: str,
    trade_manager: TradeManager,
    daily_risk_manager: DailyRiskManager,
) -> None:
    """
    Run one full scan cycle for a single (symbol, timeframe) pair:
    manage any existing trade, then look for a new signal if eligible.
    """
    try:
        candles, ema_set = build_ema_set(symbol, timeframe)
    except RuntimeError as e:
        logger.warning("Skipping %s %s this cycle: %s", symbol, timeframe, e)
        return

    # ── Manage existing open trade first (trailing, SL/TP) ──────────────
    open_trade = trade_manager.get_open_trade(symbol, timeframe)
    if open_trade is not None:
        try:
            current_price = get_current_price(
                symbol,
                direction_is_buy=(open_trade.direction.value == "sell"),
            )
        except RuntimeError as e:
            logger.warning("Could not fetch current price for %s: %s", symbol, e)
            return
        trade_manager.process_open_trade(symbol, timeframe, ema_set, current_price)
        return

    # ── No open trade -- check if new entries are allowed ───────────────
    if not daily_risk_manager.can_open_new_trade():
        return

    higher_tf = config.HIGHER_TIMEFRAME_MAP.get(timeframe)
    if higher_tf is None:
        logger.warning("No higher-TF mapping for %s; skipping.", timeframe)
        return

    try:
        _, higher_ema_set = build_ema_set(symbol, higher_tf)
    except RuntimeError as e:
        logger.warning(
            "Skipping %s %s (HTF %s unavailable): %s",
            symbol, timeframe, higher_tf, e,
        )
        return

    signal = generate_signal(candles, ema_set, higher_ema_set, symbol, timeframe)
    if signal is None:
        return

    try:
        balance = order_manager.get_account_balance()
    except RuntimeError as e:
        logger.error("Could not fetch account balance, skipping trade open: %s", e)
        return

    trade_manager.open_trade_from_signal(signal, balance)


def run_scan_cycle(
    trade_manager: TradeManager,
    daily_risk_manager: DailyRiskManager,
    cycle: int,
) -> None:
    """Run one full scan cycle across every symbol x timeframe combination."""
    global LAST_BALANCE, LAST_CYCLE, LAST_SCAN_AT
    try:
        balance = order_manager.get_account_balance()
    except RuntimeError as e:
        logger.error("Could not fetch account balance for daily reset check: %s", e)
        return

    LAST_BALANCE = balance
    LAST_CYCLE = cycle
    LAST_SCAN_AT = datetime.now(timezone.utc)

    daily_risk_manager.maybe_reset_for_new_day(datetime.now(timezone.utc), balance)
    trade_manager.sync_open_positions_from_broker()

    open_trades = trade_manager.get_all_open_trades()

    for symbol in config.SYMBOLS:
        for timeframe in config.TIMEFRAMES:
            scan_symbol_timeframe(symbol, timeframe, trade_manager, daily_risk_manager)

    # Heartbeat log (INFO level -- suppressed by the console filter for 15 min
    # but always written to the log file). Shows brief summary each cycle.
    logger.info(
        "Scan #%d | %s | balance=%.2f | open_trades=%d | symbols=%s",
        cycle,
        datetime.now(timezone.utc).strftime("%H:%M:%S"),
        balance,
        len(open_trades),
        ", ".join(config.SYMBOLS),
    )
    print_working_heartbeat(cycle, balance, len(open_trades))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Application entry point."""
    global STARTED_AT, LAST_BALANCE
    STARTED_AT = datetime.now(timezone.utc)

    setup_logging(
        level=getattr(logging, config.LOG_LEVEL),
        log_file=config.LOG_FILE,
        console_filter=_SmartConsoleFilter(),
    )

    logger.info("=" * 60)
    logger.info("EMA Dynamic Support & Resistance Retest bot starting...")
    logger.info("Symbols   : %s", config.SYMBOLS)
    logger.info("Timeframes: %s", config.TIMEFRAMES)
    logger.info("Scan every: %ds | Status print every: %ds",
                config.SCAN_INTERVAL_SECONDS, STATUS_PRINT_INTERVAL_SECONDS)
    logger.info("=" * 60)

    connected = order_manager.connect(
        login=mt5_config.LOGIN,
        password=mt5_config.PASSWORD,
        server=mt5_config.SERVER,
    )
    if not connected:
        logger.error("Failed to connect to MT5. Exiting.")
        sys.exit(1)

    try:
        balance = order_manager.get_account_balance()
    except RuntimeError as e:
        logger.error("Could not fetch initial account balance: %s", e)
        order_manager.shutdown()
        sys.exit(1)

    daily_risk_manager = DailyRiskManager(
        starting_balance=balance,
        current_date=datetime.now(timezone.utc).date(),
    )
    trade_manager = TradeManager(daily_risk_manager)
    LAST_BALANCE = balance
    telegram_notifier.start_command_listener(
        lambda: build_status_message(trade_manager, daily_risk_manager)
    )

    logger.info(
        "Connected | balance=%.2f | scanning %d symbol(s) x %d timeframe(s)",
        balance, len(config.SYMBOLS), len(config.TIMEFRAMES),
    )
    telegram_notifier.send_message(
f"""
══════════════════════
🤖 QUANTOPS STARTED
══════════════════════

🟢 Status      : ONLINE

💰 Balance     : ${balance:.2f}

📊 Symbols
• XAUUSD
• EURUSD
• GBPUSD
• USDJPY
• DJ30.r
• NAS100.r

⏰ Timeframes
• M5
• M15
• M30
• H1

⚙️ Risk per Trade : 1%
🎯 Risk Reward    : 1 : 2
📈 EMA Strategy   : EMA 5 / 10 / 20

📱 Send /status to view
live positions and bot status.

══════════════════════
"""
)
    if config.PRINT_WORKING_HEARTBEAT:
        print(
            "🟢 Bot started and connected to MT5 | "
            f"balance={balance:.2f} | scan_every={config.SCAN_INTERVAL_SECONDS}s",
            flush=True,
        )

    cycle = 0
    try:
        while True:
            cycle += 1
            run_scan_cycle(trade_manager, daily_risk_manager, cycle)
            time.sleep(config.SCAN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C).")
    finally:
        telegram_notifier.send_message(
            "Bot stopped\n"
            f"Last scan: {LAST_CYCLE}\n"
            f"Balance: {LAST_BALANCE:.2f}"
        )
        telegram_notifier.stop_command_listener()
        order_manager.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
