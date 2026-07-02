"""
risk/lot_calculator.py

Lot Calculator: converts a risk percentage + stop-loss distance into a
concrete lot size, using live broker constraints for the specific symbol.

Formula:
    dollar_risk  = account_balance × (risk_percent / 100)
    sl_pips      = stop_loss_distance / pip_size       (pip_size from MT5)
    lot_size     = dollar_risk / (sl_pips × pip_value) (pip_value from MT5)

All pip_size, pip_value, lot_step, min_lot, max_lot values come directly
from MT5 via SymbolSpec -- no hardcoded values, so forex, indices,
commodities, and crypto all size correctly with the same formula.
"""

from __future__ import annotations

import math

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def calculate_lot_size(
    account_balance: float,
    risk_percent: float,
    stop_loss_distance_price: float,
    pip_value_per_lot: float,
    symbol: str,
    pip_size: float | None = None,
    lot_step: float = config.LOT_STEP,
    min_lot: float = config.MIN_LOT,
    max_lot: float = config.MAX_LOT,
) -> float:
    """
    Calculate the lot size that risks exactly `risk_percent` of
    `account_balance`, given the stop-loss distance.

    Args:
        account_balance:           Current account balance.
        risk_percent:               Percentage of balance to risk (e.g. 1.0).
        stop_loss_distance_price:  Absolute price distance from entry to
                                    stop-loss (i.e. Signal.risk_distance).
        pip_value_per_lot:          Dollar value of 1 pip per 1.0 standard
                                    lot. Pass the value from
                                    SymbolSpec.pip_value (from MT5) for
                                    correct results on all instrument types.
        symbol:                    Instrument symbol (used only for logging).
        pip_size:                  Size of 1 pip in price units, from
                                    SymbolSpec.pip_size. If None, falls back
                                    to a simple heuristic (JPY=0.01, else
                                    0.0001) -- only used if SymbolSpec is
                                    unavailable (e.g. offline testing).
        lot_step:                  Broker minimum lot increment.
        min_lot:                    Broker minimum lot size.
        max_lot:                    Broker maximum lot size.

    Returns:
        Lot size rounded down to the nearest lot_step and clamped to
        [min_lot, max_lot].

    Raises:
        ValueError: if any of the key numeric inputs are non-positive.
    """
    if account_balance <= 0:
        raise ValueError(f"account_balance must be positive, got {account_balance}.")
    if risk_percent <= 0:
        raise ValueError(f"risk_percent must be positive, got {risk_percent}.")
    if stop_loss_distance_price <= 0:
        raise ValueError(
            f"stop_loss_distance_price must be positive, got {stop_loss_distance_price}."
        )
    if pip_value_per_lot <= 0:
        raise ValueError(f"pip_value_per_lot must be positive, got {pip_value_per_lot}.")

    # Resolve pip_size: use the live broker value if provided, else heuristic.
    if pip_size is None or pip_size <= 0:
        pip_size = 0.01 if "JPY" in symbol.upper() else 0.0001

    sl_pips     = stop_loss_distance_price / pip_size
    dollar_risk = account_balance * (risk_percent / 100.0)
    raw_lot     = dollar_risk / (sl_pips * pip_value_per_lot)
    lot_size    = _round_lot(raw_lot, lot_step, min_lot, max_lot)

    logger.risk(
        "Lot size calculated | %s | balance=%.2f risk=%.1f%% "
        "sl_price=%.5f sl_pips=%.1f pip_value=%.4f -> lot=%.2f",
        symbol, account_balance, risk_percent,
        stop_loss_distance_price, sl_pips, pip_value_per_lot, lot_size,
    )
    return lot_size


def _round_lot(raw: float, lot_step: float, min_lot: float, max_lot: float) -> float:
    """Round a raw lot value down to the nearest lot_step, then clamp."""
    if lot_step <= 0:
        lot_step = 0.01
    steps  = math.floor(raw / lot_step)
    result = round(steps * lot_step, 10)  # avoid floating-point drift
    result = round(result, 8)
    return max(min_lot, min(max_lot, result))