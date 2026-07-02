"""
strategy/retest.py

EMA Retest (pullback) Detector.

After a reversal is detected (EMA5 crosses EMA10 in the new direction),
price is expected to pull back toward the fast EMAs before continuation.
Per the spec, a valid pullback for a BUY setup means price touches into
the zone at-or-between EMA5 and EMA10, or slightly below EMA10 -- but
must NOT close below EMA20 (that would invalidate the setup, since
closing below the slow EMA suggests the reversal failed rather than
just pulling back).

SELL pullback is the exact mirror.

This module only determines *whether and where* a valid pullback
occurred after a reversal -- it does not evaluate the confirmation
candle itself (that's confirmation.py's job).
"""

from __future__ import annotations

from dataclasses import dataclass

from indicators.ema import EMASet
from models.candle import Candle
from models.signal import SignalDirection
from utils.logger import get_logger

logger = get_logger(__name__)

# How many candles after the reversal candle to search for a valid pullback
# before giving up on this setup.
MAX_PULLBACK_LOOKFORWARD = 15


@dataclass(frozen=True)
class RetestResult:
    """
    Outcome of a pullback/retest scan following a reversal.

    Attributes:
        detected:       True if a valid pullback was found (and not
                         invalidated by a close beyond EMA20).
        pullback_candle: The candle where the pullback into the EMA5-10
                          zone occurred.
        pullback_index:  Index of pullback_candle within the original
                          candle list (absolute, non-negative), so
                          confirmation.py knows where to start looking
                          for the confirmation candle.
        invalidated:     True if price closed beyond EMA20 before a
                          valid pullback could be confirmed -- the setup
                          should be discarded entirely if this is True.
    """
    detected: bool
    pullback_candle: Candle | None
    pullback_index: int | None
    invalidated: bool = False


def _is_valid_buy_pullback_touch(candle: Candle, ema_set: EMASet, idx: int) -> bool:
    """
    True if `candle` (at index idx) touches the valid BUY pullback zone:
    its low reaches down to or below EMA10 (covering "at/between EMA5 &
    EMA10" and "slightly below EMA10" in one check), while the candle
    has not closed below EMA20 (that invalidation is checked separately
    by the caller before this function runs).
    """
    ema10 = ema_set.mid_at(idx)
    return candle.low <= ema10


def _is_valid_sell_pullback_touch(candle: Candle, ema_set: EMASet, idx: int) -> bool:
    """
    Mirror of _is_valid_buy_pullback_touch for SELL setups: candle's
    high must rise up to or above EMA10 (covering "at/between EMA5 &
    EMA10" and "slightly above EMA10"), while not yet closing above
    EMA20 (checked separately by the caller).
    """
    ema10 = ema_set.mid_at(idx)
    return candle.high >= ema10


def detect_pullback(
    candles: list[Candle],
    ema_set: EMASet,
    direction: SignalDirection,
    reversal_index: int,
) -> RetestResult:
    """
    Search forward from `reversal_index` for a valid EMA5/EMA10 pullback
    in the given direction, without price closing beyond EMA20.

    Args:
        candles:        Full chronological candle history.
        ema_set:        Pre-computed EMA5/10/20 series aligned with candles.
        direction:      BUY or SELL -- which pullback rule to apply.
        reversal_index: Absolute index (non-negative) of the candle where
                         the reversal was confirmed. The search begins at
                         reversal_index + 1.

    Returns:
        A RetestResult. If price closes beyond EMA20 at any point during
        the search (before a valid pullback touch is found), the result
        is invalidated=True and detected=False -- the calling
        signal_generator should discard this setup entirely rather than
        keep waiting.
    """
    n = len(candles)
    start = reversal_index + 1
    end = min(n, reversal_index + 1 + MAX_PULLBACK_LOOKFORWARD)

    for idx in range(start, end):
        candle = candles[idx]
        ema20 = ema_set.slow_at(idx)

        if direction == SignalDirection.BUY:
            # Invalidate if price closes below EMA20 -- setup failed.
            if candle.close < ema20:
                logger.info(
                    "Pullback invalidated | %s | close %.5f broke below EMA20 %.5f @ %s",
                    candle.symbol, candle.close, ema20, candle.time,
                )
                return RetestResult(
                    detected=False, pullback_candle=None,
                    pullback_index=None, invalidated=True,
                )

            if _is_valid_buy_pullback_touch(candle, ema_set, idx):
                logger.info(
                    "Pullback detected | %s | BUY zone touch @ %s (low=%.5f)",
                    candle.symbol, candle.time, candle.low,
                )
                return RetestResult(
                    detected=True, pullback_candle=candle,
                    pullback_index=idx, invalidated=False,
                )

        else:  # SELL
            # Invalidate if price closes above EMA20 -- setup failed.
            if candle.close > ema20:
                logger.info(
                    "Pullback invalidated | %s | close %.5f broke above EMA20 %.5f @ %s",
                    candle.symbol, candle.close, ema20, candle.time,
                )
                return RetestResult(
                    detected=False, pullback_candle=None,
                    pullback_index=None, invalidated=True,
                )

            if _is_valid_sell_pullback_touch(candle, ema_set, idx):
                logger.info(
                    "Pullback detected | %s | SELL zone touch @ %s (high=%.5f)",
                    candle.symbol, candle.time, candle.high,
                )
                return RetestResult(
                    detected=True, pullback_candle=candle,
                    pullback_index=idx, invalidated=False,
                )

    # No pullback found within the lookforward window -- not yet
    # invalidated, just not (yet) confirmed. Caller can re-check on the
    # next scan as more candles close.
    return RetestResult(detected=False, pullback_candle=None, pullback_index=None, invalidated=False)
