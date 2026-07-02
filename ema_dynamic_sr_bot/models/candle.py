"""
models/candle.py

Defines the Candle data model: a single OHLC price bar.

This is the foundational data structure of the entire trading system.
Every module that touches price data (indicators, strategy detectors,
execution, risk) consumes Candle objects rather than raw arrays or
DataFrame rows, giving the codebase a single typed contract for price
data and eliminating index-based bugs (e.g. "was close at index 3 or 4?").

Design notes:
    - Immutable (frozen=True) — a candle that has already closed should
      never be mutated. This prevents subtle bugs where a strategy
      module accidentally modifies historical data shared across scans.
    - No indicator values live here. EMA/RSI values are computed
      separately by the indicators package and attached alongside
      candles (e.g. in a parallel list or merged DataFrame), keeping
      this class focused on one responsibility: representing raw price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CandleColor(Enum):
    """Classifies a candle by its body direction."""
    BULLISH = "bullish"   # close > open
    BEARISH = "bearish"   # close < open
    DOJI = "doji"         # close == open (or negligible body)


@dataclass(frozen=True)
class Candle:
    """
    Represents a single OHLC price bar for one symbol/timeframe.

    Attributes:
        time:    The bar's open time (UTC, as returned by MT5).
        open:    Opening price.
        high:    Highest price reached during the bar.
        low:     Lowest price reached during the bar.
        close:   Closing price (or current price if the bar is still forming).
        volume:  Tick volume for the bar (MT5 does not provide true volume
                 for most forex symbols, so this is tick_volume).
        symbol:  The instrument this candle belongs to (e.g. "EURUSD").
        timeframe: String label of the timeframe (e.g. "M5", "H1").
    """
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str
    timeframe: str

    @property
    def body_size(self) -> float:
        """Absolute size of the candle body (open-to-close distance)."""
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        """Absolute size of the full candle range (high-to-low distance)."""
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        """Length of the upper wick (above the body)."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Length of the lower wick (below the body)."""
        return min(self.open, self.close) - self.low

    @property
    def color(self) -> CandleColor:
        """Classify the candle as bullish, bearish, or doji."""
        if self.close > self.open:
            return CandleColor.BULLISH
        if self.close < self.open:
            return CandleColor.BEARISH
        return CandleColor.DOJI

    @property
    def is_bullish(self) -> bool:
        """True if the candle closed above where it opened."""
        return self.color == CandleColor.BULLISH

    @property
    def is_bearish(self) -> bool:
        """True if the candle closed below where it opened."""
        return self.color == CandleColor.BEARISH

    def body_ratio(self) -> float:
        """
        Ratio of body size to full range size, in [0.0, 1.0].

        Used by candlestick pattern detectors (e.g. "strong momentum
        candle" requires a body_ratio above some threshold, while a
        hammer requires a small body relative to a long wick).

        Returns:
            0.0 if the candle has zero range (open == high == low == close).
        """
        if self.range_size == 0:
            return 0.0
        return self.body_size / self.range_size

    def __repr__(self) -> str:
        return (
            f"Candle({self.symbol} {self.timeframe} {self.time:%Y-%m-%d %H:%M} "
            f"O={self.open:.5f} H={self.high:.5f} L={self.low:.5f} "
            f"C={self.close:.5f} {self.color.value})"
        )
