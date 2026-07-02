"""
models/signal.py

Defines the Signal data model: the output of the strategy pipeline.

A Signal represents "the strategy says: open this trade, here." It is
produced by strategy/signal_generator.py after the full chain (trend ->
reversal -> retest -> confirmation) has validated, and consumed by
execution/order_manager.py to actually place the trade.

Keeping Signal as its own model (rather than passing loose tuples/dicts
between strategy and execution) means execution code never needs to know
*how* a signal was derived -- only what it needs to act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SignalDirection(Enum):
    """Trade direction implied by a signal."""
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Signal:
    """
    A fully-validated trade signal ready for execution.

    Attributes:
        symbol:        Instrument to trade (e.g. "EURUSD").
        timeframe:      Timeframe this signal was generated on (e.g. "M15").
        direction:      BUY or SELL.
        entry_price:    Suggested entry price (typically current market price
                         at confirmation candle close).
        stop_loss:      Initial stop-loss price level.
        take_profit:    Initial take-profit price level (1:2 R:R from entry/SL).
        generated_at:   Timestamp when the signal was generated (UTC).
        reason:         Human-readable explanation of why the signal fired
                         (useful for logs and post-trade review), e.g.
                         "Bullish reversal + EMA5/10 retest + engulfing confirm".
    """
    symbol: str
    timeframe: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    generated_at: datetime
    reason: str = ""

    @property
    def risk_distance(self) -> float:
        """Absolute price distance between entry and stop-loss (1R)."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        """Absolute price distance between entry and take-profit."""
        return abs(self.take_profit - self.entry_price)

    @property
    def risk_reward_ratio(self) -> float:
        """
        Reward:Risk ratio for this signal (e.g. 2.0 means 1:2).

        Returns 0.0 if risk_distance is zero to avoid division errors
        (this should never happen for a valid signal, but is guarded
        defensively since SL==entry would mean an invalid trade).
        """
        if self.risk_distance == 0:
            return 0.0
        return self.reward_distance / self.risk_distance

    def __repr__(self) -> str:
        return (
            f"Signal({self.symbol} {self.timeframe} {self.direction.value.upper()} "
            f"entry={self.entry_price:.5f} sl={self.stop_loss:.5f} "
            f"tp={self.take_profit:.5f} rr={self.risk_reward_ratio:.2f} "
            f"@ {self.generated_at:%Y-%m-%d %H:%M})"
        )
