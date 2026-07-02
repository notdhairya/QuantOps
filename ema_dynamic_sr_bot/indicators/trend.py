"""
indicators/trend.py

Trend Detector: classifies the current trend using the EMA5/10/20 stack.

Rule (as specified):
    Bullish: EMA5 > EMA10 > EMA20  (fully stacked upward)
    Bearish: EMA5 < EMA10 < EMA20  (fully stacked downward)
    Neutral: anything else (tangled / crossed EMAs -- no clean trend)

This module is intentionally narrow: it answers exactly one question
("what is the trend, right now, on this EMASet?") and nothing else.
The reversal detector later asks a *different* question ("was the
*previous* trend the opposite of the current cross?"), which is why
trend classification needs to be queryable at an arbitrary historical
index, not just the latest bar.
"""

from __future__ import annotations

from enum import Enum

from indicators.ema import EMASet
from utils.logger import get_logger

logger = get_logger(__name__)


class Trend(Enum):
    """Trend classification result."""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def detect_trend_at(ema_set: EMASet, index: int) -> Trend:
    """
    Classify the trend at a specific candle index using the EMA stack.

    Args:
        ema_set: Pre-computed EMA5/10/20 series for the symbol+timeframe.
        index:   Candle index to evaluate (supports negative indexing,
                 e.g. -1 for the latest closed candle).

    Returns:
        Trend.BULLISH if EMA5 > EMA10 > EMA20 at that index.
        Trend.BEARISH if EMA5 < EMA10 < EMA20 at that index.
        Trend.NEUTRAL otherwise (EMAs tangled/crossed).
    """
    fast = ema_set.fast_at(index)
    mid = ema_set.mid_at(index)
    slow = ema_set.slow_at(index)

    if fast > mid > slow:
        return Trend.BULLISH
    if fast < mid < slow:
        return Trend.BEARISH
    return Trend.NEUTRAL


def detect_current_trend(ema_set: EMASet, symbol: str, timeframe: str) -> Trend:
    """
    Classify the trend on the most recently closed candle and log the
    result if it represents a change from the prior candle.

    Args:
        ema_set:   Pre-computed EMA5/10/20 series.
        symbol:    Instrument, used only for logging context.
        timeframe: Timeframe, used only for logging context.

    Returns:
        The current Trend classification.
    """
    current = detect_trend_at(ema_set, -1)

    # Log a trend change event if we have at least 2 candles to compare.
    if len(ema_set) >= 2:
        previous = detect_trend_at(ema_set, -2)
        if previous != current:
            logger.info(
                "Trend changed to %s | %s %s (EMA5=%.5f EMA10=%.5f EMA20=%.5f)",
                current.value.capitalize(), symbol, timeframe,
                ema_set.latest_fast, ema_set.latest_mid, ema_set.latest_slow,
            )

    return current


def find_previous_trend_before_index(ema_set: EMASet, index: int, max_lookback: int = 50) -> Trend:
    """
    Walk backward from `index` to find the most recent *non-neutral*
    trend classification before that point.

    Used by the reversal detector to answer "was the previous trend
    bearish?" -- it needs to look past any brief neutral/tangled period
    right at the crossover to find what the trend actually was before
    the reversal began forming.

    Args:
        ema_set:      Pre-computed EMA series.
        index:        Starting index to walk backward from (exclusive --
                       the search begins at index - 1).
        max_lookback:  Maximum number of candles to walk backward before
                       giving up and returning NEUTRAL.

    Returns:
        The most recent non-neutral Trend found, or Trend.NEUTRAL if
        none is found within max_lookback candles or if there isn't
        enough history.
    """
    # Normalize negative index to a positive one for safe arithmetic.
    n = len(ema_set)
    pos_index = index if index >= 0 else n + index

    steps = 0
    i = pos_index - 1
    while i >= 0 and steps < max_lookback:
        trend = detect_trend_at(ema_set, i)
        if trend != Trend.NEUTRAL:
            return trend
        i -= 1
        steps += 1

    return Trend.NEUTRAL
