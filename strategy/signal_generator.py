"""
strategy/signal_generator.py

Signal Generator: orchestrates the full detection chain into a Signal.

Pipeline:
    1. Trend Detector confirms a trend exists on the higher timeframe.
    2. Reversal Detector finds a valid reversal on the trading timeframe.
    3. Retest Detector finds a valid pullback after the reversal.
    4. Confirmation Detector finds a valid confirmation candle after
       the pullback.
    5. If all four stages pass, compute SL/TP and emit a Signal.

This module owns NO detection logic itself -- it only sequences calls
to indicators/trend.py and strategy/{reversal,retest,confirmation}.py,
and translates a fully-confirmed setup into a Signal with concrete
entry/SL/TP prices. Single Responsibility: orchestration only.
"""

from __future__ import annotations

from datetime import datetime, timezone

import config
from indicators.ema import EMASet
from indicators.trend import Trend, detect_current_trend
from models.candle import Candle
from models.signal import Signal, SignalDirection
from strategy.confirmation import detect_confirmation
from strategy.retest import detect_pullback
from strategy.reversal import detect_reversal
from utils.logger import get_logger

logger = get_logger(__name__)

def _calculate_stop_loss(
    direction: SignalDirection,
    candles: list[Candle],
    ema_set: EMASet,
    pullback_index: int,
    confirmation_index: int,
) -> float:
    """
    Stop-loss placement per spec:
        BUY:  lowest pullback swing, OR below EMA20 (whichever is the
              more conservative / further-away level is NOT specified,
              so we use the lower of the two for BUY -- i.e. whichever
              gives the wider, safer stop).
        SELL: highest pullback swing, OR above EMA20 (mirror: the
              higher of the two).

    The "pullback swing" is the lowest low (for BUY) / highest high
    (for SELL) among all candles from the pullback index through the
    confirmation index inclusive.
    """
    swing_candles = candles[pullback_index:confirmation_index + 1]
    ema20_at_confirmation = ema_set.slow_at(confirmation_index)

    if direction == SignalDirection.BUY:
        swing_low = min(c.low for c in swing_candles)
        return min(swing_low, ema20_at_confirmation)
    else:
        swing_high = max(c.high for c in swing_candles)
        return max(swing_high, ema20_at_confirmation)


def _calculate_take_profit(entry_price: float, stop_loss: float, direction: SignalDirection) -> float:
    """
    Take-profit at a fixed Risk:Reward ratio (config.RISK_REWARD_RATIO,
    default 1:2) from the entry/SL distance.
    """
    risk_distance = abs(entry_price - stop_loss)
    reward_distance = risk_distance * config.RISK_REWARD_RATIO

    if direction == SignalDirection.BUY:
        return entry_price + reward_distance
    return entry_price - reward_distance


def generate_signal(
    candles: list[Candle],
    ema_set: EMASet,
    higher_tf_ema_set: EMASet,
    symbol: str,
    timeframe: str,
) -> Signal | None:
    """
    Run the full detection pipeline and produce a Signal if every stage
    confirms, else return None.

    Args:
        candles:           Chronological candle history for the trading
                            timeframe (e.g. M15).
        ema_set:            Pre-computed EMA5/10/20 for the trading timeframe.
        higher_tf_ema_set:  Pre-computed EMA5/10/20 for the higher timeframe
                            (e.g. H1), used as a trend filter so entries on
                            the trading timeframe only proceed in agreement
                            with the larger trend context.
        symbol:            Instrument being scanned.
        timeframe:         Trading timeframe label (e.g. "M15").

    Returns:
        A Signal if the entire chain confirms, otherwise None.
    """
    # ── Stage 0: Higher-timeframe trend filter ──────────────────────────
    higher_trend = detect_current_trend(higher_tf_ema_set, symbol, f"{timeframe}->HTF")
    if higher_trend == Trend.NEUTRAL:
        return None  # no clean higher-TF trend context; skip this scan

    # ── Stage 1: Reversal ────────────────────────────────────────────────
    reversal = detect_reversal(candles, ema_set, index=-1)
    if not reversal.detected:
        return None

    # Only take reversals that agree with the higher-timeframe trend
    # direction (a BUY reversal is only valid if HTF trend is bullish,
    # and vice versa) -- this keeps the multi-timeframe filter meaningful.
    if reversal.direction == SignalDirection.BUY and higher_trend != Trend.BULLISH:
        return None
    if reversal.direction == SignalDirection.SELL and higher_trend != Trend.BEARISH:
        return None

    reversal_index = candles.index(reversal.reversal_candle)

    # ── Stage 2: Retest / Pullback ───────────────────────────────────────
    retest = detect_pullback(candles, ema_set, reversal.direction, reversal_index)
    if retest.invalidated:
        logger.info("Setup discarded | %s %s | pullback invalidated by EMA20 break", symbol, timeframe)
        return None
    if not retest.detected:
        return None  # pullback hasn't formed yet; try again next scan

    # ── Stage 3: Confirmation ────────────────────────────────────────────
    confirmation = detect_confirmation(candles, ema_set, reversal.direction, retest.pullback_index)
    if not confirmation.detected:
        return None  # no confirmation candle yet; try again next scan

    # ── All stages passed -- build the Signal ────────────────────────────
    entry_price = confirmation.confirmation_candle.close
    stop_loss = _calculate_stop_loss(
        reversal.direction, candles, ema_set,
        retest.pullback_index, confirmation.confirmation_index,
    )
    take_profit = _calculate_take_profit(entry_price, stop_loss, reversal.direction)

    reason = (
        f"{reversal.pattern_name} reversal + EMA5/10 retest + "
        f"{confirmation.pattern_name} confirmation (RSI={confirmation.rsi_value:.1f})"
    )

    signal = Signal(
        symbol=symbol,
        timeframe=timeframe,
        direction=reversal.direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        generated_at=datetime.now(timezone.utc),
        reason=reason,
    )

    logger.signal("%s signal generated | %s", signal.direction.value.upper(), signal)
    return signal
