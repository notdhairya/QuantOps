"""
execution/order_manager.py

Order Manager: the only module that directly calls the MetaTrader5
Python package to place, modify, or close orders.

Single responsibility: translate our domain objects (Signal, Trade) into
MT5 API calls and back. No strategy logic lives here -- by the time a
Signal reaches this module, all decision-making is already done.

Isolating all MetaTrader5 import/calls to this one module also means
the rest of the codebase (strategy/, risk/, indicators/) can be unit
tested without an MT5 terminal running at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from models.signal import Signal, SignalDirection
from models.trade import Trade, TradeStatus
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrderResult:
    """
    Outcome of an order placement attempt.

    Attributes:
        success:       True if MT5 confirmed the order filled.
        ticket:        Broker ticket number if successful, else None.
        fill_price:    Actual fill price if successful, else None.
        error_message: Human-readable error if success is False.
    """
    success: bool
    ticket: Optional[int]
    fill_price: Optional[float]
    error_message: str = ""


@dataclass(frozen=True)
class SymbolSpec:
    """
    Broker-reported constraints for a symbol, fetched fresh from MT5
    before every order so we never use stale or hardcoded values.

    Attributes:
        point:          Smallest price movement (e.g. 0.00001 for EURUSD,
                         1.0 for DJ30).
        pip_size:       One pip in price units (10 × point for most
                         5-digit brokers, equal to point for indices).
        pip_value:      Dollar value of 1 pip per 1.0 standard lot.
        lot_step:       Minimum lot increment (e.g. 0.01 for forex,
                         1.0 for some indices).
        min_lot:        Minimum allowed volume.
        max_lot:        Maximum allowed volume.
        stops_level:    Broker minimum stop distance in points.
    """
    point: float
    pip_size: float
    pip_value: float
    lot_step: float
    min_lot: float
    max_lot: float
    stops_level: int = 0


@dataclass(frozen=True)
class OpenPosition:
    """A live MT5 position opened by this bot."""
    ticket: int
    symbol: str
    timeframe: str
    direction: SignalDirection
    volume: float
    price_open: float
    stop_loss: float
    take_profit: float
    opened_at: datetime


def _get_mt5():
    """
    Lazily import and return the MetaTrader5 module.
    Imported lazily so that modules importing order_manager indirectly
    don't require the MetaTrader5 package / Windows MT5 terminal to be
    present just to load -- only code paths that actually place orders
    need it importable.
    """
    import MetaTrader5 as mt5
    return mt5


def connect(login: int, password: str, server: str) -> bool:
    """
    Initialize and log in to the MT5 terminal.

    Args:
        login:    MT5 account number.
        password: MT5 account password.
        server:   MT5 broker server name.

    Returns:
        True if connection and login succeeded.
    """
    mt5 = _get_mt5()
    if not mt5.initialize():
        logger.error("MT5 initialize() failed: %s", mt5.last_error())
        return False

    if not mt5.login(login, password=password, server=server):
        logger.error("MT5 login failed: %s", mt5.last_error())
        mt5.shutdown()
        return False

    account_info = mt5.account_info()
    logger.info(
        "Connected to MT5 | login=%s server=%s balance=%.2f %s",
        login, server, account_info.balance, account_info.currency,
    )
    return True


def shutdown() -> None:
    """Cleanly disconnect from the MT5 terminal."""
    mt5 = _get_mt5()
    mt5.shutdown()
    logger.info("MT5 connection closed.")


def get_account_balance() -> float:
    """Return the current account balance from MT5."""
    mt5 = _get_mt5()
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"Could not fetch account info: {mt5.last_error()}")
    return info.balance


def get_symbol_spec(symbol: str) -> SymbolSpec:
    """
    Query MT5 for all volume/pip constraints for `symbol`.

    This is the single correct source of lot_step, min_lot, pip_value etc.
    for ANY instrument type -- forex, indices, commodities, crypto.
    No hardcoded pip sizes or lot steps are used; everything comes
    from the broker's own symbol info, so DJ30 (point=1.0, lot_step=1.0)
    is handled exactly as correctly as EURUSD (point=0.00001, lot_step=0.01).

    Args:
        symbol: Instrument symbol as it appears in MT5 (e.g. "DJ30.r").

    Returns:
        A SymbolSpec with all constraints needed for lot sizing and
        order validation.

    Raises:
        RuntimeError: if MT5 cannot find the symbol.
    """
    mt5 = _get_mt5()

    # Ensure the symbol is active in MarketWatch before querying it.
    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol '{symbol}' not found on this broker.")

    point    = info.point if info.point > 0 else 0.00001
    digits   = info.digits

    # pip_size: for 5-digit forex brokers one pip = 10 points (0.00010).
    # For indices and other instruments quoted in whole or half units,
    # the "pip" concept doesn't apply cleanly -- we use 1 point = 1 pip
    # for those, which gives correct dollar-risk math when combined with
    # trade_tick_value below.
    if digits >= 4 and point <= 0.001:
        # Standard forex / precious metals (4-5 digit quotes)
        pip_size = point * 10
    else:
        # Indices, crypto, commodities -- 1 point = 1 "pip" for our math
        pip_size = point

    # pip_value: dollar value of 1 pip per 1.0 lot.
    # trade_tick_value = $ per tick (1 point); scale to $ per pip.
    tick_value = info.trade_tick_value if info.trade_tick_value > 0 else 1.0
    pip_value  = tick_value * (pip_size / point)

    spec = SymbolSpec(
        point     = point,
        pip_size  = pip_size,
        pip_value = pip_value,
        lot_step  = info.volume_step  if info.volume_step  > 0 else 0.01,
        min_lot   = info.volume_min   if info.volume_min   > 0 else 0.01,
        max_lot   = info.volume_max   if info.volume_max   > 0 else 100.0,
        stops_level = int(getattr(info, "trade_stops_level", 0) or 0),
    )

    logger.debug(
        "SymbolSpec | %s | point=%.5f pip_size=%.5f pip_value=%.4f "
        "lot_step=%.2f min=%.2f max=%.2f stops_level=%d",
        symbol, spec.point, spec.pip_size, spec.pip_value,
        spec.lot_step, spec.min_lot, spec.max_lot, spec.stops_level,
    )
    return spec


def get_current_spread_price(symbol: str) -> float:
    """Return the current ask-bid spread in price units."""
    mt5 = _get_mt5()
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"Could not get current tick for {symbol}.")
    return max(0.0, float(tick.ask - tick.bid))


def get_bot_open_positions() -> list[OpenPosition]:
    """Return live MT5 positions tagged with this bot's magic number."""
    mt5 = _get_mt5()
    positions = mt5.positions_get()
    if not positions:
        return []

    bot_positions: list[OpenPosition] = []
    for position in positions:
        if getattr(position, "magic", None) != config.MAGIC_NUMBER:
            continue

        symbol = str(position.symbol)
        comment = str(getattr(position, "comment", "") or "")
        timeframe = _timeframe_from_comment(comment)
        direction = (
            SignalDirection.BUY
            if position.type == mt5.POSITION_TYPE_BUY
            else SignalDirection.SELL
        )

        bot_positions.append(
            OpenPosition(
                ticket=int(position.ticket),
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                volume=float(position.volume),
                price_open=float(position.price_open),
                stop_loss=float(position.sl or 0.0),
                take_profit=float(position.tp or 0.0),
                opened_at=datetime.fromtimestamp(int(position.time), tz=timezone.utc),
            )
        )

    return bot_positions


def _timeframe_from_comment(comment: str) -> str:
    """Extract timeframe from comments like 'EMA-SR M5'."""
    parts = comment.strip().split()
    if parts:
        candidate = parts[-1].upper()
        if candidate in config.TIMEFRAMES:
            return candidate
    return config.TIMEFRAMES[0]


def get_pip_value(symbol: str) -> float:
    """
    Return the dollar value of 1 pip per 1.0 standard lot for `symbol`,
    fetched live from MT5 symbol info (no hardcoded pip sizes).
    """
    try:
        return get_symbol_spec(symbol).pip_value
    except RuntimeError:
        logger.warning(
            "Could not get pip value for %s, using default %.2f",
            symbol, config.DEFAULT_PIP_VALUE,
        )
        return config.DEFAULT_PIP_VALUE


def _candidate_filling_modes(mt5) -> list[int]:
    """Broker compatibility fallback order for market deal requests."""
    return [
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_RETURN,
    ]


def _validate_market_order(
    symbol: str,
    direction: SignalDirection,
    lot_size: float,
    price: float,
    sl: float,
    tp: float,
    spec: SymbolSpec,
) -> str:
    """
    Return an error message if the order is clearly invalid before MT5 send.
    An empty string means the request passed local validation.
    """
    if lot_size < spec.min_lot or lot_size > spec.max_lot:
        return (
            f"Invalid lot size for {symbol}: {lot_size} "
            f"outside broker range {spec.min_lot}-{spec.max_lot}"
        )

    steps = round(lot_size / spec.lot_step)
    if abs(lot_size - (steps * spec.lot_step)) > 1e-8:
        return f"Invalid lot size for {symbol}: {lot_size} does not match step {spec.lot_step}"

    if price <= 0 or sl <= 0 or tp <= 0:
        return f"Invalid order prices for {symbol}: price={price} sl={sl} tp={tp}"

    if direction == SignalDirection.BUY and not (sl < price < tp):
        return f"Invalid BUY levels for {symbol}: sl={sl} price={price} tp={tp}"

    if direction == SignalDirection.SELL and not (tp < price < sl):
        return f"Invalid SELL levels for {symbol}: tp={tp} price={price} sl={sl}"

    min_stop_distance = spec.stops_level * spec.point
    if min_stop_distance > 0:
        if abs(price - sl) < min_stop_distance:
            return (
                f"SL too close for {symbol}: distance={abs(price - sl):.5f} "
                f"minimum={min_stop_distance:.5f}"
            )
        if abs(price - tp) < min_stop_distance:
            return (
                f"TP too close for {symbol}: distance={abs(price - tp):.5f} "
                f"minimum={min_stop_distance:.5f}"
            )

    return ""


def place_order(signal: Signal, lot_size: float) -> OrderResult:
    """
    Place a market order on MT5 corresponding to `signal`.

    Fetches live symbol constraints (lot_step, min_lot, filling mode)
    from MT5 before placing so the order is always correctly formatted
    for the specific instrument -- forex, indices, commodities all work
    without special-casing.

    Args:
        signal:   The validated Signal to execute.
        lot_size: Position size in lots (from risk/lot_calculator.py,
                   already rounded to the correct lot_step for this
                   symbol via get_symbol_spec()).

    Returns:
        An OrderResult describing success/failure and fill details.
    """
    mt5 = _get_mt5()

    # Fetch (or refresh) symbol info; also makes it visible in MarketWatch.
    try:
        spec = get_symbol_spec(signal.symbol)
    except RuntimeError as e:
        return OrderResult(success=False, ticket=None, fill_price=None, error_message=str(e))

    tick = mt5.symbol_info_tick(signal.symbol)
    if tick is None:
        msg = f"Could not get current tick for {signal.symbol}."
        logger.error(msg)
        return OrderResult(success=False, ticket=None, fill_price=None, error_message=msg)

    order_type = mt5.ORDER_TYPE_BUY if signal.direction == SignalDirection.BUY else mt5.ORDER_TYPE_SELL
    price = tick.ask if signal.direction == SignalDirection.BUY else tick.bid

    validation_error = _validate_market_order(
        signal.symbol, signal.direction, lot_size, price,
        signal.stop_loss, signal.take_profit, spec,
    )
    if validation_error:
        logger.error(validation_error)
        return OrderResult(
            success=False, ticket=None, fill_price=None,
            error_message=validation_error,
        )

    # Try IOC first (most brokers), fall back to FOK and then RETURN
    # filling modes so the bot works across different broker configurations.
    filling_modes = _candidate_filling_modes(mt5)

    for filling in filling_modes:
        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      signal.symbol,
            "volume":      lot_size,
            "type":        order_type,
            "price":       price,
            "sl":          signal.stop_loss,
            "tp":          signal.take_profit,
            "deviation":   20,
            "magic":       config.MAGIC_NUMBER,
            "comment":     f"EMA-SR {signal.timeframe}",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)

        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.trade(
                "Trade executed | %s %s %s lots=%.2f price=%.5f "
                "sl=%.5f tp=%.5f ticket=%d",
                signal.direction.value.upper(), signal.symbol, signal.timeframe,
                lot_size, result.price, signal.stop_loss,
                signal.take_profit, result.order,
            )
            return OrderResult(success=True, ticket=result.order, fill_price=result.price)

        # retcode 10030 = unsupported filling mode -- try the next one
        if result is not None and result.retcode == 10030:
            continue

        # Any other error -- stop trying filling modes
        break

    error_code = result.retcode if result else mt5.last_error()
    msg = f"Order send failed for {signal.symbol}: retcode={error_code}"
    logger.error(msg)
    return OrderResult(success=False, ticket=None, fill_price=None, error_message=msg)


def modify_stop_loss(ticket: int, symbol: str, new_sl: float, take_profit: float) -> bool:
    """
    Modify the stop-loss of an existing open position (used by the
    trailing stop module). Take-profit is re-sent unchanged since MT5's
    TRADE_ACTION_SLTP request requires both fields.

    Args:
        ticket:      Position ticket to modify.
        symbol:      Symbol of the position (required by the request).
        new_sl:      New stop-loss price.
        take_profit: Current take-profit price (unchanged, just re-sent).

    Returns:
        True if the modification succeeded.
    """
    mt5 = _get_mt5()

    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   symbol,
        "position": ticket,
        "sl":       new_sl,
        "tp":       take_profit,
    }
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error_code = result.retcode if result else mt5.last_error()
        logger.error("Stop-loss modify failed for ticket %d: retcode=%s", ticket, error_code)
        return False

    logger.order("Stop Loss update | ticket=%d new_sl=%.5f", ticket, new_sl)
    return True


def close_position(ticket: int, symbol: str, direction: SignalDirection, lot_size: float) -> OrderResult:
    """
    Close an open position by sending an opposing market order against it.

    Args:
        ticket:    Position ticket to close.
        symbol:    Symbol of the position.
        direction: The position's original direction (close order is
                   the opposite type).
        lot_size:  Volume to close (the full position size).

    Returns:
        An OrderResult describing success/failure and the close price.
    """
    mt5 = _get_mt5()

    position = None
    positions = mt5.positions_get(ticket=ticket)
    if positions:
        position = positions[0]
    else:
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            for candidate in positions:
                if getattr(candidate, "magic", None) == config.MAGIC_NUMBER:
                    position = candidate
                    break

    if position is not None:
        ticket = int(position.ticket)
        lot_size = float(position.volume)
        direction = (
            SignalDirection.BUY
            if position.type == mt5.POSITION_TYPE_BUY
            else SignalDirection.SELL
        )

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        msg = f"Could not get current tick for {symbol} to close ticket {ticket}."
        logger.error(msg)
        return OrderResult(success=False, ticket=None, fill_price=None, error_message=msg)

    close_type = mt5.ORDER_TYPE_SELL if direction == SignalDirection.BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if direction == SignalDirection.BUY else tick.ask

    result = None
    for filling in _candidate_filling_modes(mt5):
        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      symbol,
            "volume":      lot_size,
            "type":        close_type,
            "position":    ticket,
            "price":       price,
            "deviation":   20,
            "magic":       config.MAGIC_NUMBER,
            "comment":     "EMA-SR close",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)

        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.trade("Trade closed | ticket=%d %s price=%.5f", ticket, symbol, result.price)
            return OrderResult(success=True, ticket=ticket, fill_price=result.price)

        continue

    error_code = result.retcode if result else mt5.last_error()
    msg = f"Close failed for ticket {ticket}: retcode={error_code}"
    logger.error(msg)
    return OrderResult(success=False, ticket=None, fill_price=None, error_message=msg)
