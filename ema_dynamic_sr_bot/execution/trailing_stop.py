"""
execution/trailing_stop.py

EMA Trailing Stop.

Per spec: once a trade's profit reaches 1R, activate EMA10 trailing --
the stop-loss follows EMA10 for both BUY and SELL, and the stop never
moves backward (Trade.update_stop_loss() already enforces that
invariant; this module just proposes new SL values).

Single responsibility: decide *whether* to activate/update trailing and
*what* the new SL should be. It does not call MT5 directly -- it
delegates the actual modify-order call to order_manager.py, keeping
this module testable without a live MT5 connection.
"""

from __future__ import annotations

import config
from execution import order_manager
from indicators.ema import EMASet
from models.signal import SignalDirection
from models.trade import Trade
from utils.logger import get_logger

logger = get_logger(__name__)


def maybe_activate_trailing(trade: Trade, current_price: float) -> bool:
    """
    Check if `trade` has reached the profit threshold to activate EMA10
    trailing, and flip trade.trailing_active if so.

    Args:
        trade:          The open Trade to check.
        current_price:  Current market price for the trade's symbol.

    Returns:
        True if trailing was just activated by this call (False if it
        was already active, or not yet eligible).
    """
    if trade.trailing_active:
        return False

    if trade.profit_in_r(current_price) >= config.TRAILING_ACTIVATION_R:
        trade.trailing_active = True
        logger.trail(
            "EMA trail activated | ticket=%s %s %s profit=%.2fR",
            trade.ticket, trade.symbol, trade.timeframe,
            trade.profit_in_r(current_price),
        )
        return True

    return False


def update_trailing_stop(trade: Trade, ema_set: EMASet) -> bool:
    """
    If trailing is active for `trade`, compute the latest EMA10 value
    and attempt to move the stop-loss to it (only ever forward, per
    Trade.update_stop_loss()'s built-in invariant).

    Args:
        trade:    The open Trade to update (must have trailing_active=True
                  to have any effect).
        ema_set:  Current EMA5/10/20 series for the trade's symbol+timeframe.

    Returns:
        True if the stop-loss was actually moved and the MT5 modify
        call succeeded. False if trailing isn't active, the proposed
        EMA10 level wouldn't move the stop forward, or the MT5 call failed.
    """
    if not trade.trailing_active:
        return False
    if trade.ticket is None:
        logger.warning("Cannot trail ticket=None for %s %s (trade not yet confirmed open).",
                       trade.symbol, trade.timeframe)
        return False

    new_sl_candidate = ema_set.latest_mid  # EMA10
    try:
        spec = order_manager.get_symbol_spec(trade.symbol)
        spread = order_manager.get_current_spread_price(trade.symbol)
        spread_buffer = spread * config.STOP_SPREAD_BUFFER_MULTIPLIER
        fixed_buffer = config.EXTRA_STOP_BUFFER_PIPS * spec.pip_size
        broker_buffer = spec.stops_level * spec.point
        stop_buffer = max(spread_buffer + fixed_buffer, broker_buffer)
    except RuntimeError as e:
        logger.warning("Could not apply trailing spread buffer for %s: %s", trade.symbol, e)
        stop_buffer = 0.0

    if trade.direction == SignalDirection.BUY:
        new_sl_candidate -= stop_buffer
    else:
        new_sl_candidate += stop_buffer

    moved = trade.update_stop_loss(new_sl_candidate)
    if not moved:
        return False  # proposed level would move the stop backward; ignored

    success = order_manager.modify_stop_loss(
        ticket=trade.ticket,
        symbol=trade.symbol,
        new_sl=trade.stop_loss,
        take_profit=trade.take_profit,
    )
    if not success:
        logger.warning(
            "EMA trail SL update rejected by broker | ticket=%s new_sl=%.5f",
            trade.ticket, trade.stop_loss,
        )
    return success


def process_trailing_for_trade(trade: Trade, ema_set: EMASet, current_price: float) -> None:
    """
    Convenience entry point combining activation-check and trail-update
    in one call, intended to be invoked once per open trade per scan
    cycle from trade_manager.py.

    Args:
        trade:          The open Trade to process.
        ema_set:        Current EMA5/10/20 series for the trade's
                        symbol+timeframe.
        current_price:  Current market price for profit_in_r calculation.
    """
    maybe_activate_trailing(trade, current_price)
    if trade.trailing_active:
        update_trailing_stop(trade, ema_set)
