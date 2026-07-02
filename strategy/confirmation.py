"""
strategy/confirmation.py

Confirmation Detector.

After a valid pullback is found, entry requires a confirmation candle.
Per the spec, accepted confirmation patterns for BUY are: Bullish
Engulfing, Hammer, or Strong Bullish Candle (mirror for SELL).

This implementation adds the evidence-based refinement from industry
research (confirmed with the user): the confirmation candle must CLOSE
back in the trend direction (not just touch/wick), AND RSI must sit in
the 40-60 "cooled off but not broken" zone at that candle. Both
conditions must hold together -- pattern alone is not sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from indicators.ema import EMASet
from models.candle import Candle
from models.signal import SignalDirection
from strategy.reversal import (
    is_bullish_engulfing,
    is_bearish_engulfing,
    is_strong_bullish_momentum_candle,
    is_strong_bearish_momentum_candle,
    is_hammer,
    is_inverted_hammer,
)
from utils.logger import get_logger

logger = get_logger(__name__)

MAX_CONFIRMATION_LOOKFORWARD = 10


@dataclass(frozen=True)
class ConfirmationResult:
    """
    Outcome of a confirmation scan following a valid pullback.

    Attributes:
        detected:            True if a valid confirmation candle was found.
        confirmation_candle: The candle that confirmed entry.
        confirmation_index:  Absolute index of confirmation_candle.
        pattern_name:        Human-readable pattern name for logging.
        rsi_value:           RSI value at the confirmation candle.
    """
    detected: bool
    confirmation_candle: Candle | None
    confirmation_index: int | None
    pattern_name: str = ""
    rsi_value: float = 0.0


def calculate_rsi(closes: list[float], period: int) -> list[float]:
    """
    Calculate RSI (Relative Strength Index) over a closing-price series.

    Args:
        closes: Chronologically-ordered closing prices.
        period: RSI lookback period (e.g. 14).

    Returns:
        A list the same length as `closes`. The first `period` entries
        are not reliable (insufficient warm-up data) and are filled with
        50.0 (neutral) as a safe placeholder -- callers should avoid
        triggering confirmation logic on those early indices in practice,
        which is naturally the case here since confirmation only runs
        well after BARS_TO_FETCH worth of warm-up history.
    """
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)

    rsi_values = [50.0] * len(closes)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    rsi_values[period] = _rsi_from_avgs(avg_gain, avg_loss)

    # Wilder's smoothing for the remainder of the series.
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi_values[i] = _rsi_from_avgs(avg_gain, avg_loss)

    return rsi_values


def _rsi_confirms(rsi_value: float) -> bool:
    """True if RSI sits within the configured confirmation zone."""
    return config.RSI_CONFIRMATION_MIN <= rsi_value <= config.RSI_CONFIRMATION_MAX


def detect_confirmation(
    candles: list[Candle],
    ema_set: EMASet,
    direction: SignalDirection,
    pullback_index: int,
) -> ConfirmationResult:
    """
    Search forward from `pullback_index` for a valid confirmation candle:
    a qualifying candlestick pattern in the trend direction, with the
    candle closing back in that direction, AND RSI in the 40-60 zone.

    Args:
        candles:        Full chronological candle history.
        ema_set:        Pre-computed EMA5/10/20 series (used to re-check
                         EMA20 has not been violated since the pullback).
        direction:      BUY or SELL.
        pullback_index: Absolute index of the confirmed pullback candle.
                         Search begins at pullback_index + 1 (a candle
                         can also self-confirm if pattern + close + RSI
                         all align on the same bar as the pullback touch,
                         so we also check pullback_index itself first).

    Returns:
        A ConfirmationResult.
    """
    n = len(candles)
    closes = [c.close for c in candles]
    rsi_series = calculate_rsi(closes, config.RSI_PERIOD)

    start = pullback_index  # include the pullback candle itself
    end = min(n, pullback_index + 1 + MAX_CONFIRMATION_LOOKFORWARD)

    for idx in range(start, end):
        if idx < 1:
            continue
        curr = candles[idx]
        prev = candles[idx - 1]
        rsi_value = rsi_series[idx]
        ema20 = ema_set.slow_at(idx)

        if direction == SignalDirection.BUY:
            # Re-validate EMA20 hasn't been violated.
            if curr.close < ema20:
                return ConfirmationResult(
                    detected=False, confirmation_candle=None,
                    confirmation_index=None,
                )

            pattern = None
            if is_bullish_engulfing(prev, curr):
                pattern = "Bullish Engulfing"
            elif is_hammer(curr) and curr.is_bullish:
                pattern = "Hammer"
            elif is_strong_bullish_momentum_candle(curr):
                pattern = "Strong Bullish Candle"

            closed_in_direction = curr.close > curr.open

            if pattern and closed_in_direction and _rsi_confirms(rsi_value):
                logger.info(
                    "Confirmation candle found | %s | pattern=%s RSI=%.1f "
                    "close=%.5f @ %s",
                    curr.symbol, pattern, rsi_value, curr.close, curr.time,
                )
                return ConfirmationResult(
                    detected=True, confirmation_candle=curr,
                    confirmation_index=idx, pattern_name=pattern,
                    rsi_value=rsi_value,
                )

        else:  # SELL
            if curr.close > ema20:
                return ConfirmationResult(
                    detected=False, confirmation_candle=None,
                    confirmation_index=None,
                )

            pattern = None
            if is_bearish_engulfing(prev, curr):
                pattern = "Bearish Engulfing"
            elif is_inverted_hammer(curr) and curr.is_bearish:
                pattern = "Hammer"
            elif is_strong_bearish_momentum_candle(curr):
                pattern = "Strong Bearish Candle"

            closed_in_direction = curr.close < curr.open

            if pattern and closed_in_direction and _rsi_confirms(rsi_value):
                logger.info(
                    "Confirmation candle found | %s | pattern=%s RSI=%.1f "
                    "close=%.5f @ %s",
                    curr.symbol, pattern, rsi_value, curr.close, curr.time,
                )
                return ConfirmationResult(
                    detected=True, confirmation_candle=curr,
                    confirmation_index=idx, pattern_name=pattern,
                    rsi_value=rsi_value,
                )

    return ConfirmationResult(detected=False, confirmation_candle=None, confirmation_index=None)
