"""
risk/daily_risk_manager.py

Daily Risk Manager: enforces the daily protection rules.

Per spec:
    - Stop opening NEW trades after 2 consecutive losing trades, OR
      daily realized loss reaches 3% of starting balance.
    - Existing open trades continue to be managed normally (this module
      only gates new entries, never forces closure of open trades).
    - State resets at the start of each new trading day.

Single responsibility: track daily trade outcomes and answer "can a new
trade be opened right now?" It does not execute trades or touch MT5 --
execution/order_manager.py asks this module for permission before
opening anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import config
from models.trade import Trade
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DailyRiskManager:
    """
    Tracks consecutive losses and realized daily P&L to gate new trade
    entries. One instance is shared across all symbols/timeframes since
    the daily loss limit is account-wide, not per-timeframe.

    Attributes:
        starting_balance:    Account balance at the start of the current
                              trading day -- used as the denominator for
                              the daily loss percentage check.
        current_date:        The trading day this instance's state applies to.
        consecutive_losses:  Count of consecutive losing trades since the
                              last win (or since day start).
        realized_pnl_today:  Sum of realized P&L (account currency) for
                              trades closed today.
        trading_halted:      True once either daily protection rule has
                              tripped; blocks new entries until reset.
    """
    starting_balance: float
    current_date: date
    consecutive_losses: int = 0
    realized_pnl_today: float = 0.0
    trading_halted: bool = False
    _halt_reason: str = ""

    def maybe_reset_for_new_day(self, now: datetime, current_balance: float) -> None:
        """
        Reset all daily counters if `now` falls on a different calendar
        day than the state currently tracks. Should be called at the
        start of every scan cycle, before any other daily-risk checks.

        Args:
            now:             Current timestamp (UTC).
            current_balance: Account balance to use as the new day's
                              starting_balance.
        """
        if now.date() != self.current_date:
            logger.daily(
                "Daily risk state reset | new trading day %s (prev day P&L=%.2f)",
                now.date(), self.realized_pnl_today,
            )
            self.current_date = now.date()
            self.starting_balance = current_balance
            self.consecutive_losses = 0
            self.realized_pnl_today = 0.0
            self.trading_halted = False
            self._halt_reason = ""

    def record_trade_close(self, trade: Trade) -> None:
        """
        Update daily counters after a trade closes. Call this from
        trade_manager.py immediately after Trade.close() is invoked.

        Args:
            trade: The just-closed Trade (must have realized_pnl set).
        """
        if trade.realized_pnl is None:
            return  # defensive guard; should not happen for a closed trade

        self.realized_pnl_today += trade.realized_pnl

        if trade.is_loss:
            self.consecutive_losses += 1
            logger.daily(
                "Loss recorded | consecutive_losses=%d realized_pnl_today=%.2f",
                self.consecutive_losses, self.realized_pnl_today,
            )
        elif trade.is_win:
            self.consecutive_losses = 0
            logger.daily(
                "Win recorded | consecutive_losses reset to 0 realized_pnl_today=%.2f",
                self.realized_pnl_today,
            )

        self._check_halt_conditions()

    def _check_halt_conditions(self) -> None:
        """Evaluate both halt conditions and set trading_halted if tripped."""
        if self.trading_halted:
            return  # already halted; no need to re-check or re-log

        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            self.trading_halted = True
            self._halt_reason = (
                f"{self.consecutive_losses} consecutive losing trades"
            )
            logger.daily("Daily risk limit reached | reason=%s", self._halt_reason)
            return

        if self.starting_balance > 0:
            loss_percent = (-self.realized_pnl_today / self.starting_balance) * 100
            if loss_percent >= config.MAX_DAILY_LOSS_PERCENT:
                self.trading_halted = True
                self._halt_reason = (
                    f"daily realized loss {loss_percent:.2f}% >= "
                    f"{config.MAX_DAILY_LOSS_PERCENT}% limit"
                )
                logger.daily("Daily risk limit reached | reason=%s", self._halt_reason)

    def can_open_new_trade(self) -> bool:
        """
        Returns:
            True if neither daily protection rule has tripped, meaning
            new trades may be opened. Existing trades are unaffected
            either way -- this only gates NEW entries.
        """
        return not self.trading_halted

    @property
    def halt_reason(self) -> str:
        """Human-readable reason trading is halted, or '' if not halted."""
        return self._halt_reason