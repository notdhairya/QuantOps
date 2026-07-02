"""
utils/helpers.py

General-purpose helper functions that don't belong to any single domain
module -- primarily MT5 data fetching and conversion into our Candle
model. Kept separate from execution/order_manager.py since that module
is specifically about order placement, while this is about market data
retrieval.
"""

from __future__ import annotations

from datetime import datetime, timezone

from models.candle import Candle
from utils.logger import get_logger

logger = get_logger(__name__)


_TIMEFRAME_MAP = None  # lazily populated on first use (requires MT5 import)


def _get_timeframe_map() -> dict[str, int]:
    """
    Lazily build the string-label -> MT5 timeframe-constant map.
    Lazy so this module can be imported without MetaTrader5 installed
    (e.g. in test environments) as long as fetch functions aren't called.
    """
    global _TIMEFRAME_MAP
    if _TIMEFRAME_MAP is None:
        import MetaTrader5 as mt5
        _TIMEFRAME_MAP = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
    return _TIMEFRAME_MAP


def timeframe_to_mt5(label: str) -> int:
    """
    Convert a string timeframe label (e.g. "M15") to its MT5 constant.

    Raises:
        ValueError: if `label` is not a recognized timeframe.
    """
    mapping = _get_timeframe_map()
    if label not in mapping:
        raise ValueError(f"Unknown timeframe label '{label}'. Valid: {list(mapping)}")
    return mapping[label]


def fetch_candles(symbol: str, timeframe: str, count: int) -> list[Candle]:
    """
    Fetch the most recent `count` closed candles for `symbol` on
    `timeframe` from MT5, converted into our Candle model.

    Args:
        symbol:    Instrument symbol (e.g. "EURUSD").
        timeframe: Timeframe label (e.g. "M15").
        count:     Number of bars to fetch.

    Returns:
        A chronologically-ordered list[Candle] (oldest first), with the
        most recent CLOSED candle as the last element (MT5's
        copy_rates_from_pos index 0 is the currently-forming candle, so
        we drop it to ensure only fully-closed bars are used for
        signal generation).

    Raises:
        RuntimeError: if MT5 returns no data or fewer bars than requested.
    """
    import MetaTrader5 as mt5

    mt5_tf = timeframe_to_mt5(timeframe)
    # Fetch one extra bar (count + 1) so that after dropping the
    # still-forming current candle, we still return `count` closed bars.
    rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count + 1)

    if rates is None or len(rates) < 2:
        raise RuntimeError(
            f"Could not fetch candles for {symbol} {timeframe}: "
            f"got {0 if rates is None else len(rates)} bars."
        )

    # Drop the first row (index 0 = currently forming, unclosed candle).
    closed_rates = rates[1:]

    candles = [
        Candle(
            time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=int(r["tick_volume"]),
            symbol=symbol,
            timeframe=timeframe,
        )
        for r in closed_rates
    ]
    return candles


def get_current_price(symbol: str, direction_is_buy: bool) -> float:
    """
    Get the current tradable price for `symbol`: ask price for BUY-side
    checks, bid price for SELL-side checks (matching how a real fill/
    close would occur).

    Args:
        symbol:           Instrument symbol.
        direction_is_buy: True to get the ask price, False for the bid.

    Returns:
        The current price.

    Raises:
        RuntimeError: if MT5 has no current tick for the symbol.
    """
    import MetaTrader5 as mt5

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"No current tick available for {symbol}.")
    return tick.ask if direction_is_buy else tick.bid
