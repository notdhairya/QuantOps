"""
models/trade.py

Defines the Trade data model: a live or closed position managed by the bot.

Unlike Candle and Signal (which are immutable snapshots), a Trade is
intentionally mutable -- its stop_loss moves during trailing, its status
changes from OPEN to CLOSED, and its realized P&L is set on close. This
class is the single source of truth that execution/trade_manager.py and
execution/trailing_stop.py read and update throughout a trade's lifecycle.

Each timeframe manages its own Trade instances independently (per the
multi-timeframe rule: H1 Buy, M30 Buy, M15 Buy can all be open at once),
so a Trade always carries its symbol+timeframe identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from models.signal import SignalDirection


class TradeStatus(Enum):
    """Lifecycle state of a trade."""
    PENDING = "pending"   # signal generated, order not yet confirmed filled
    OPEN = "open"         # position is live in the market
    CLOSED = "closed"     # position has been closed (TP, SL, or manual)


class CloseReason(Enum):
    """Why a trade was closed, used for daily-risk tracking and logging."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MANUAL = "manual"
    NONE = "none"   # trade is still open


@dataclass
class Trade:
    """
    A trade managed by the bot, from open to close.

    Attributes:
        ticket:           Broker-assigned order/position ticket. None until
                           the order_manager confirms the fill.
        symbol:           Instrument (e.g. "EURUSD").
        timeframe:        Timeframe this trade belongs to (e.g. "M15"). Each
                           timeframe manages its own trades independently.
        direction:         BUY or SELL.
        entry_price:      Actual fill price.
        stop_loss:        Current stop-loss price (mutated by trailing_stop.py).
        take_profit:       Fixed take-profit price (1:2 R:R, set at entry,
                           does not move).
    lot_size:         Position size in lots, from risk/lot_calculator.py.
        pip_size:         Size of one pip in price units for this symbol.
        pip_value_per_lot:
                           Account-currency value of one pip at 1.0 lot.
        status:           PENDING / OPEN / CLOSED.
        opened_at:        Timestamp when the trade was opened.
        closed_at:        Timestamp when the trade was closed (None if open).
        close_reason:      Why the trade closed (NONE if still open).
        realized_pnl:      Profit/loss in account currency once closed
                           (None until closed).
        initial_risk_distance:
                           The original entry-to-SL distance (1R), captured
                           at open and never changed. Used to detect when
                           profit reaches 1R for trailing-stop activation,
                           even after stop_loss has since moved.
        trailing_active:   Whether EMA10 trailing has been activated for
                           this trade (profit reached >= 1R).
    """
    symbol: str
    timeframe: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    initial_risk_distance: float
    opened_at: datetime
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    ticket: Optional[int] = None
    status: TradeStatus = TradeStatus.PENDING
    closed_at: Optional[datetime] = None
    close_reason: CloseReason = CloseReason.NONE
    realized_pnl: Optional[float] = None
    trailing_active: bool = False

    def current_profit_distance(self, current_price: float) -> float:
        """
        Price distance currently in profit, signed by direction.

        Positive means the trade is in profit; negative means in loss.
        Used to check whether the trade has reached 1R for trailing
        stop activation.
        """
        if self.direction == SignalDirection.BUY:
            return current_price - self.entry_price
        return self.entry_price - current_price

    def profit_in_r(self, current_price: float) -> float:
        """
        Current profit expressed in multiples of initial risk (R).

        E.g. 1.0 means the trade has moved exactly 1R in profit,
        which is the trigger threshold for trailing stop activation.

        Returns 0.0 if initial_risk_distance is zero (defensive guard,
        should not occur for a validly-opened trade).
        """
        if self.initial_risk_distance == 0:
            return 0.0
        return self.current_profit_distance(current_price) / self.initial_risk_distance

    def update_stop_loss(self, new_sl: float) -> bool:
        """
        Update the stop-loss, enforcing the "never move backward" rule.

        For a BUY, the new SL must be higher than the current SL to be
        accepted. For a SELL, the new SL must be lower. This guarantees
        trailing_stop.py can call this freely every tick without ever
        accidentally widening risk.

        Returns:
            True if the stop-loss was updated, False if the proposed
            value would have moved it backward (and was therefore ignored).
        """
        if self.direction == SignalDirection.BUY:
            if new_sl > self.stop_loss:
                self.stop_loss = new_sl
                return True
            return False
        else:  # SELL
            if new_sl < self.stop_loss:
                self.stop_loss = new_sl
                return True
            return False

    def close(self, close_price: float, reason: CloseReason, closed_at: datetime) -> None:
        """
        Mark the trade as closed and compute realized P&L in account currency.
        """
        self.status = TradeStatus.CLOSED
        self.close_reason = reason
        self.closed_at = closed_at

        if self.direction == SignalDirection.BUY:
            price_distance = close_price - self.entry_price
        else:
            price_distance = self.entry_price - close_price

        if self.pip_size <= 0 or self.pip_value_per_lot <= 0:
            self.realized_pnl = price_distance
            return

        pips = price_distance / self.pip_size
        self.realized_pnl = pips * self.pip_value_per_lot * self.lot_size

    @property
    def is_open(self) -> bool:
        return self.status == TradeStatus.OPEN

    @property
    def is_loss(self) -> bool:
        """True only for a closed trade with negative realized P&L."""
        return self.status == TradeStatus.CLOSED and (self.realized_pnl or 0) < 0

    @property
    def is_win(self) -> bool:
        """True only for a closed trade with positive realized P&L."""
        return self.status == TradeStatus.CLOSED and (self.realized_pnl or 0) > 0

    def __repr__(self) -> str:
        return (
            f"Trade(#{self.ticket} {self.symbol} {self.timeframe} "
            f"{self.direction.value.upper()} entry={self.entry_price:.5f} "
            f"sl={self.stop_loss:.5f} tp={self.take_profit:.5f} "
            f"lots={self.lot_size} status={self.status.value} "
            f"trailing={self.trailing_active})"
        )
