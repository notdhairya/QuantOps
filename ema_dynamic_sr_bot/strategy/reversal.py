"""
strategy/reversal.py

Reversal Detector.

Per the spec, a BUY reversal requires, in order:
    1. Previous trend was bearish.
    2. A strong bullish reversal candle (Bullish Engulfing OR Strong
       Momentum Candle).
    3. EMA5 crosses above EMA10.

A SELL reversal is the exact mirror (previous trend bullish, strong
bearish reversal candle, EMA5 crosses below EMA10).

This module owns candlestick pattern recognition (engulfing / strong
momentum / hammer -- hammer is used later by confirmation.py, but lives
here since it's still "candlestick pattern" responsibility) and the
EMA5/EMA10 crossover check. It does NOT know about pullbacks or
confirmation candles -- that's retest.py and confirmation.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.ema import EMASet
from indicators.trend import Trend, detect_trend_at, find_previous_trend_before_index
from models.candle import Candle, CandleColor
from models.signal import SignalDirection
from utils.logger import get_logger

logger = get_logger(__name__)

_last_logged_reversal: dict[tuple[str, str, SignalDirection, object, object], bool] = {}


def _log_reversal_once(
    candle: Candle,
    direction: SignalDirection,
    prior_trend: Trend,
    pattern: str,
    cross_time: object,
) -> None:
    """Log each distinct reversal/crossover combination only once."""
    key = (candle.symbol, candle.timeframe, direction, candle.time, cross_time)
    if key in _last_logged_reversal:
        return
    _last_logged_reversal[key] = True

    direction_word = "bullish" if direction == SignalDirection.BUY else "bearish"
    logger.info(
        "Reversal detected | %s | prior_trend=%s pattern=%s "
        "(candle @ %s) EMA5/10 %s crossover @ %s",
        candle.symbol, prior_trend.name, pattern, candle.time,
        direction_word, cross_time,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Candlestick pattern recognition
# ─────────────────────────────────────────────────────────────────────────────

def is_bullish_engulfing(prev: Candle, curr: Candle) -> bool:
    """
    A bullish engulfing pattern: a bearish candle followed by a bullish
    candle whose body fully engulfs the previous candle's body.

    Args:
        prev: The earlier candle (expected bearish).
        curr: The later candle (expected bullish, engulfing prev).

    Returns:
        True if curr is a bullish engulfing of prev.
    """
    if not prev.is_bearish or not curr.is_bullish:
        return False
    return curr.open <= prev.close and curr.close >= prev.open


def is_bearish_engulfing(prev: Candle, curr: Candle) -> bool:
    """
    A bearish engulfing pattern: a bullish candle followed by a bearish
    candle whose body fully engulfs the previous candle's body.

    Args:
        prev: The earlier candle (expected bullish).
        curr: The later candle (expected bearish, engulfing prev).

    Returns:
        True if curr is a bearish engulfing of prev.
    """
    if not prev.is_bullish or not curr.is_bearish:
        return False
    return curr.open >= prev.close and curr.close <= prev.open


def is_strong_bullish_momentum_candle(candle: Candle) -> bool:
    """
    A "Strong Momentum Candle" (bullish variant): large bullish body
    relative to its full range, indicating decisive buying pressure.

    Uses config.STRONG_MOMENTUM_BODY_RATIO as the minimum body/range
    threshold.
    """
    return candle.is_bullish and candle.body_ratio() >= config.STRONG_MOMENTUM_BODY_RATIO


def is_strong_bearish_momentum_candle(candle: Candle) -> bool:
    """
    A "Strong Momentum Candle" (bearish variant): large bearish body
    relative to its full range, indicating decisive selling pressure.
    """
    return candle.is_bearish and candle.body_ratio() >= config.STRONG_MOMENTUM_BODY_RATIO


def is_hammer(candle: Candle) -> bool:
    """
    A Hammer pattern: small body near the top of the range, a long lower
    wick (>= config.HAMMER_WICK_TO_BODY_RATIO times the body), and little
    to no upper wick. Direction-agnostic shape; bullish/bearish context
    is applied by the caller (confirmation.py) based on which side of
    the trend it's being evaluated on.

    Returns:
        True if `candle` matches the Hammer shape.
    """
    body = candle.body_size
    if body == 0:
        return False  # a doji isn't a hammer; avoid div-by-zero below

    long_lower_wick = candle.lower_wick >= config.HAMMER_WICK_TO_BODY_RATIO * body
    small_upper_wick = (
        candle.upper_wick / candle.range_size <= config.HAMMER_MAX_OPPOSING_WICK_RATIO
        if candle.range_size > 0 else True
    )
    return long_lower_wick and small_upper_wick


def is_inverted_hammer(candle: Candle) -> bool:
    """
    An Inverted Hammer / Shooting-Star shape: small body near the bottom
    of the range, a long upper wick, and little to no lower wick. This is
    the mirror of is_hammer(), used for bearish confirmation contexts.
    """
    body = candle.body_size
    if body == 0:
        return False

    long_upper_wick = candle.upper_wick >= config.HAMMER_WICK_TO_BODY_RATIO * body
    small_lower_wick = (
        candle.lower_wick / candle.range_size <= config.HAMMER_MAX_OPPOSING_WICK_RATIO
        if candle.range_size > 0 else True
    )
    return long_upper_wick and small_lower_wick


# ─────────────────────────────────────────────────────────────────────────────
# EMA5/EMA10 crossover
# ─────────────────────────────────────────────────────────────────────────────

def ema_crossed_bullish(ema_set: EMASet, index: int = -1) -> bool:
    """
    True if EMA5 crossed above EMA10 between index-1 and index
    (i.e. EMA5 was <= EMA10 on the prior candle, and EMA5 > EMA10 now).
    """
    if len(ema_set) < 2:
        return False
    n = len(ema_set)
    pos = index if index >= 0 else n + index
    if pos < 1:
        return False

    prev_fast, prev_mid = ema_set.fast_at(pos - 1), ema_set.mid_at(pos - 1)
    curr_fast, curr_mid = ema_set.fast_at(pos), ema_set.mid_at(pos)

    return prev_fast <= prev_mid and curr_fast > curr_mid


def ema_crossed_bearish(ema_set: EMASet, index: int = -1) -> bool:
    """
    True if EMA5 crossed below EMA10 between index-1 and index
    (i.e. EMA5 was >= EMA10 on the prior candle, and EMA5 < EMA10 now).
    """
    if len(ema_set) < 2:
        return False
    n = len(ema_set)
    pos = index if index >= 0 else n + index
    if pos < 1:
        return False

    prev_fast, prev_mid = ema_set.fast_at(pos - 1), ema_set.mid_at(pos - 1)
    curr_fast, curr_mid = ema_set.fast_at(pos), ema_set.mid_at(pos)

    return prev_fast >= prev_mid and curr_fast < curr_mid


# ─────────────────────────────────────────────────────────────────────────────
# Reversal result + top-level detector
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReversalResult:
    """
    Outcome of a reversal scan at a specific candle index.

    Attributes:
        detected:        True if a valid reversal setup was found.
        direction:       SignalDirection if detected, else None.
        reversal_candle: The candle that formed the reversal pattern.
        pattern_name:    Human-readable pattern name for logging
                          ("Bullish Engulfing", "Strong Momentum Candle", etc).
    """
    detected: bool
    direction: SignalDirection | None
    reversal_candle: Candle | None
    pattern_name: str = ""


def detect_reversal(
    candles: list[Candle],
    ema_set: EMASet,
    index: int = -1,
    cross_search_window: int = 5,
) -> ReversalResult:
    """
    Run the full reversal detection chain ending at `index`:
        1. Previous trend (looking back from the pattern candle) must be
           the opposite of the prospective new direction.
        2. The candle at `index` must be a qualifying reversal pattern
           (Bullish/Bearish Engulfing OR Strong Momentum Candle).
        3. EMA5 must cross EMA10 in the new direction either on the same
           bar as the pattern candle, or within `cross_search_window`
           bars after it (in live markets the EMA cross frequently lags
           the pattern candle by a bar or two as the fast EMA catches
           up to the sharp price move) -- BUT the cross must occur at or
           before `index` itself, since `index` represents "now" (the
           latest closed candle) and we cannot look into the future.

           In other words: cross_index must satisfy
               pattern_index <= cross_index <= index
           This means if `index` is the latest candle, the pattern
           candle may be slightly older than `index` (up to
           cross_search_window bars back) as long as the cross has
           landed by `index`.

    Args:
        candles:              Chronologically-ordered candle history.
        ema_set:               Pre-computed EMA5/10/20 series aligned
                               with `candles`.
        index:                 Candle index marking "now" -- the latest
                               closed candle to evaluate up to (default
                               -1 = most recent).
        cross_search_window:   How many bars after the pattern candle to
                               allow the EMA cross to land.

    Returns:
        A ReversalResult describing whether a reversal was found by `index`.
    """
    n = len(candles)
    pos = index if index >= 0 else n + index

    if pos < 1 or pos >= n:
        return ReversalResult(detected=False, direction=None, reversal_candle=None)

    # Search pattern candles from (pos - cross_search_window) up to pos,
    # so the cross has room to land on or before `pos`.
    earliest_pattern_idx = max(1, pos - cross_search_window)

    for pattern_idx in range(earliest_pattern_idx, pos + 1):
        curr_candle = candles[pattern_idx]
        prev_candle = candles[pattern_idx - 1]
        prior_trend = find_previous_trend_before_index(ema_set, pattern_idx)

        # ── Try BUY reversal ─────────────────────────────────────────────
        if prior_trend == Trend.BEARISH:
            pattern = None
            if is_bullish_engulfing(prev_candle, curr_candle):
                pattern = "Bullish Engulfing"
            elif is_strong_bullish_momentum_candle(curr_candle):
                pattern = "Strong Momentum Candle"

            if pattern:
                # Look for the bullish cross anywhere from pattern_idx to pos.
                for cross_idx in range(pattern_idx, pos + 1):
                    if ema_crossed_bullish(ema_set, cross_idx):
                        _log_reversal_once(
                            curr_candle,
                            SignalDirection.BUY,
                            prior_trend,
                            pattern,
                            candles[cross_idx].time,
                        )
                        return ReversalResult(
                            detected=True,
                            direction=SignalDirection.BUY,
                            reversal_candle=curr_candle,
                            pattern_name=pattern,
                        )

        # ── Try SELL reversal ────────────────────────────────────────────
        if prior_trend == Trend.BULLISH:
            pattern = None
            if is_bearish_engulfing(prev_candle, curr_candle):
                pattern = "Bearish Engulfing"
            elif is_strong_bearish_momentum_candle(curr_candle):
                pattern = "Strong Momentum Candle"

            if pattern:
                for cross_idx in range(pattern_idx, pos + 1):
                    if ema_crossed_bearish(ema_set, cross_idx):
                        _log_reversal_once(
                            curr_candle,
                            SignalDirection.SELL,
                            prior_trend,
                            pattern,
                            candles[cross_idx].time,
                        )
                        return ReversalResult(
                            detected=True,
                            direction=SignalDirection.SELL,
                            reversal_candle=curr_candle,
                            pattern_name=pattern,
                        )

    return ReversalResult(detected=False, direction=None, reversal_candle=None)
