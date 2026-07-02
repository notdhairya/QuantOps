"""
Thin MetaTrader 5 helper layer: connect, resolve symbols, open and close market
positions. Used by correlation_trade.py.

All functions raise RuntimeError with the MT5 error text on failure so the caller
can decide what to do (e.g. abort before placing the second leg of a hedge).
"""
from __future__ import annotations

import MetaTrader5 as mt5

import mt5_config as cfg


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect() -> None:
    """Initialise the terminal and (optionally) log in to the configured account."""
    kwargs = {}
    if cfg.TERMINAL_PATH:
        kwargs["path"] = cfg.TERMINAL_PATH
    if cfg.LOGIN:
        kwargs.update(login=int(cfg.LOGIN), password=cfg.PASSWORD, server=cfg.SERVER)

    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise RuntimeError(f"MT5 account_info() failed (not logged in?): {mt5.last_error()}")
    return info


def shutdown() -> None:
    mt5.shutdown()


def account_info_safe():
    """Return account_info() or None (never raises)."""
    try:
        return mt5.account_info()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #
def resolve_symbol(base: str, suffix: str = "") -> str | None:
    """Map a plain pair name (e.g. 'EURUSD') to the broker's actual symbol name.

    Tries base+suffix and base directly, then searches all terminal symbols for one
    whose name starts with the base (shortest match wins). Returns None if nothing fits.
    """
    for cand in (base + suffix, base):
        if cand and mt5.symbol_info(cand) is not None:
            return cand

    base_u = base.upper()
    matches = [s.name for s in (mt5.symbols_get() or []) if s.name.upper().startswith(base_u)]
    matches.sort(key=len)  # prefer the cleanest/shortest match
    return matches[0] if matches else None


def ensure_visible(symbol: str) -> None:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol '{symbol}' not found")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"could not select symbol '{symbol}': {mt5.last_error()}")


def normalize_volume(symbol: str, lots: float) -> float:
    info = mt5.symbol_info(symbol)
    step = info.volume_step or 0.01
    vol = max(info.volume_min, min(info.volume_max, lots))
    vol = round(round(vol / step) * step, 8)
    return vol


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def _filling_candidates(symbol: str):
    """Return filling modes to try, best guess first (brokers differ)."""
    info = mt5.symbol_info(symbol)
    order = []
    mask = getattr(info, "filling_mode", 0)
    if mask & 1:  # SYMBOL_FILLING_FOK
        order.append(mt5.ORDER_FILLING_FOK)
    if mask & 2:  # SYMBOL_FILLING_IOC
        order.append(mt5.ORDER_FILLING_IOC)
    # fallbacks
    for f in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        if f not in order:
            order.append(f)
    return order


def open_position(symbol: str, direction: str, lots: float):
    """Open a market position. direction is 'long' or 'short'. Returns the deal result."""
    ensure_visible(symbol)
    volume = normalize_volume(symbol, lots)
    is_long = direction == "long"
    order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL

    last_result = None
    for filling in _filling_candidates(symbol):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"no tick for '{symbol}': {mt5.last_error()}")
        price = tick.ask if is_long else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": cfg.DEVIATION,
            "magic": cfg.MAGIC,
            "comment": cfg.COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        last_result = result
        if result is None:
            raise RuntimeError(f"order_send returned None for '{symbol}': {mt5.last_error()}")
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        # 10030 = unsupported filling mode -> try the next one
        if result.retcode != 10030:
            break
    raise RuntimeError(
        f"open {direction} {volume} {symbol} failed: retcode={last_result.retcode} "
        f"({last_result.comment})"
    )


def open_positions_by_magic():
    """All positions tagged with our magic number."""
    positions = mt5.positions_get()
    return [p for p in (positions or []) if p.magic == cfg.MAGIC]


def close_position(position):
    """Close a single position with an opposite market order."""
    symbol = position.symbol
    is_long = position.type == mt5.POSITION_TYPE_BUY
    close_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY

    last_result = None
    for filling in _filling_candidates(symbol):
        tick = mt5.symbol_info_tick(symbol)
        price = tick.bid if is_long else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": position.volume,
            "type": close_type,
            "position": position.ticket,
            "price": price,
            "deviation": cfg.DEVIATION,
            "magic": cfg.MAGIC,
            "comment": cfg.COMMENT + "_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        last_result = result
        if result is None:
            raise RuntimeError(f"close order_send None for '{symbol}': {mt5.last_error()}")
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        if result.retcode != 10030:
            break
    raise RuntimeError(
        f"close {symbol} #{position.ticket} failed: retcode={last_result.retcode} "
        f"({last_result.comment})"
    )
