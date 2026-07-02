"""
indicators/ema.py

Exponential Moving Average calculations.

Single responsibility: given a price series, compute EMA values. This
module knows nothing about trading strategy, candlesticks, or signals --
it is a pure numerical transform, which makes it trivially testable and
reusable (the same EMA function backs the trend detector, the reversal
detector's crossover check, and the retest detector's pullback zone).

Uses pandas internally for the exponential weighting (`ewm`), wrapped in
a small typed API so callers work with plain Python lists / numpy arrays
rather than needing to know pandas internals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from models.candle import Candle


def calculate_ema(values: list[float], period: int) -> np.ndarray:
    """
    Calculate the Exponential Moving Average of a price series.

    Args:
        values: Sequence of prices in chronological order (oldest first).
        period: EMA lookback period (e.g. 5, 10, 20).

    Returns:
        A numpy array the same length as `values`, where index i holds
        the EMA value using all values[0..i]. Early values (before the
        series has `period` data points) are still computed -- pandas'
        ewm uses all available data with adjust=False, which matches
        the standard trading-platform EMA seeding behaviour (MT5, etc.)
        closely enough for live trading once the warm-up period (a few
        x `period` bars) has passed. Callers should ignore EMA values
        from the first ~3x period bars of any series as unreliable warm-up.

    Raises:
        ValueError: if `values` is empty or `period` is not positive.
    """
    if not values:
        raise ValueError("Cannot calculate EMA on an empty price series.")
    if period <= 0:
        raise ValueError(f"EMA period must be positive, got {period}.")

    series = pd.Series(values, dtype=float)
    ema = series.ewm(span=period, adjust=False).mean()
    return ema.to_numpy()


def calculate_ema_from_candles(candles: list[Candle], period: int) -> np.ndarray:
    """
    Convenience wrapper: calculate EMA directly from a list of Candle
    objects, using each candle's closing price.

    Args:
        candles: Chronologically-ordered candles (oldest first).
        period:  EMA lookback period.

    Returns:
        Numpy array of EMA values aligned 1:1 with `candles`.
    """
    closes = [c.close for c in candles]
    return calculate_ema(closes, period)


class EMASet:
    """
    Holds the three EMA series (fast/mid/slow) for one symbol+timeframe,
    pre-aligned with the candle list they were computed from.

    This avoids every downstream detector recomputing EMA5/10/20
    independently -- they all share one EMASet instance per scan.
    """

    def __init__(self, candles: list[Candle], fast_period: int, mid_period: int, slow_period: int):
        """
        Args:
            candles:     Chronologically-ordered candles (oldest first).
            fast_period: Fast EMA period (e.g. 5).
            mid_period:  Mid EMA period (e.g. 10).
            slow_period: Slow EMA period (e.g. 20).

        Raises:
            ValueError: if candles is empty.
        """
        if not candles:
            raise ValueError("Cannot build EMASet from an empty candle list.")

        self.candles = candles
        self.fast_period = fast_period
        self.mid_period = mid_period
        self.slow_period = slow_period

        self.fast = calculate_ema_from_candles(candles, fast_period)
        self.mid = calculate_ema_from_candles(candles, mid_period)
        self.slow = calculate_ema_from_candles(candles, slow_period)

    def fast_at(self, index: int) -> float:
        """Fast EMA value at a given candle index (supports negative indexing)."""
        return float(self.fast[index])

    def mid_at(self, index: int) -> float:
        """Mid EMA value at a given candle index (supports negative indexing)."""
        return float(self.mid[index])

    def slow_at(self, index: int) -> float:
        """Slow EMA value at a given candle index (supports negative indexing)."""
        return float(self.slow[index])

    @property
    def latest_fast(self) -> float:
        return self.fast_at(-1)

    @property
    def latest_mid(self) -> float:
        return self.mid_at(-1)

    @property
    def latest_slow(self) -> float:
        return self.slow_at(-1)

    def __len__(self) -> int:
        return len(self.candles)
