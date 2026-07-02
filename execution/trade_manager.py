"""
execution/trade_manager.py
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import config
from execution import order_manager
from execution.trailing_stop import process_trailing_for_trade
from indicators.ema import EMASet
from models.signal import Signal, SignalDirection
from models.trade import CloseReason, Trade, TradeStatus
from risk.daily_risk_manager import DailyRiskManager
from risk.lot_calculator import calculate_lot_size
from utils.logger import get_logger
from utils.telegram_notifier import telegram_notifier

logger = get_logger(__name__)


class TradeManager:
    """
    Tracks and manages all trades opened by the bot, one slot per
    (symbol, timeframe) key.
    """

    def __init__(self, daily_risk_manager: DailyRiskManager):
        self._trades: dict[int, Trade] = {}
        self._daily_risk_manager = daily_risk_manager

    def has_open_trade(self, symbol: str, timeframe: str) -> bool:
        """True if this (symbol, timeframe) pair already has an open trade."""
        return any(
            trade.symbol == symbol
            and trade.timeframe == timeframe
            and trade.status in (TradeStatus.PENDING, TradeStatus.OPEN)
            for trade in self._trades.values()
        )

    def get_open_trade(self, symbol: str, timeframe: str) -> Trade | None:
        """Return the open trade for this pair, if one exists."""
        for trade in self._trades.values():
            if (
                trade.symbol == symbol
                and trade.timeframe == timeframe
                and trade.status in (TradeStatus.PENDING, TradeStatus.OPEN)
            ):
                return trade
        return None

    def sync_open_positions_from_broker(self) -> None:
        """
        Refresh in-memory trade tracking from MT5 open positions.
        Handles positions closed by broker (TP/SL) and syncs new ones.
        """
        broker_positions = order_manager.get_bot_open_positions()
        broker_tickets = {position.ticket for position in broker_positions}

        for ticket, trade in list(self._trades.items()):
            if trade.status == TradeStatus.OPEN and ticket not in broker_tickets:
                self._resolve_broker_close(trade)

        for position in broker_positions:
            try:
                spec = order_manager.get_symbol_spec(position.symbol)
            except RuntimeError as e:
                logger.warning("Cannot sync position %s %s: %s", position.ticket, position.symbol, e)
                continue

            existing = self._trades.get(position.ticket)
            if existing is not None and existing.status == TradeStatus.OPEN:
                existing.stop_loss = position.stop_loss
                existing.take_profit = position.take_profit
                existing.lot_size = position.volume
                continue

            initial_risk_distance = abs(position.price_open - position.stop_loss)
            if initial_risk_distance <= 0:
                initial_risk_distance = spec.pip_size

            trade = Trade(
                symbol=position.symbol,
                timeframe=position.timeframe,
                direction=position.direction,
                entry_price=position.price_open,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                lot_size=position.volume,
                initial_risk_distance=initial_risk_distance,
                opened_at=position.opened_at,
                pip_size=spec.pip_size,
                pip_value_per_lot=spec.pip_value,
                ticket=position.ticket,
                status=TradeStatus.OPEN,
            )
            self._trades[position.ticket] = trade
            logger.trade(
                "Synced open broker position | ticket=%s %s %s %s lots=%.2f entry=%.5f sl=%.5f tp=%.5f",
                trade.ticket, trade.symbol, trade.timeframe, trade.direction.value.upper(),
                trade.lot_size, trade.entry_price, trade.stop_loss, trade.take_profit,
            )

    def open_trade_from_signal(self, signal: Signal, account_balance: float) -> Trade | None:
        """
        Open a new trade from a validated Signal.
        """
        if self.has_open_trade(signal.symbol, signal.timeframe):
            logger.warning(
                "Skipped signal | %s %s already has an open trade.",
                signal.symbol, signal.timeframe,
            )
            return None

        try:
            spec = order_manager.get_symbol_spec(signal.symbol)
        except RuntimeError as e:
            logger.error("Cannot get symbol spec for %s: %s", signal.symbol, e)
            return None

        signal = self._apply_execution_stop_buffer(signal, spec)

        lot_size = calculate_lot_size(
            account_balance=account_balance,
            risk_percent=config.RISK_PERCENT_PER_TRADE,
            stop_loss_distance_price=signal.risk_distance,
            pip_value_per_lot=spec.pip_value,
            symbol=signal.symbol,
            pip_size=spec.pip_size,
            lot_step=spec.lot_step,
            min_lot=spec.min_lot,
            max_lot=spec.max_lot,
        )

        result = order_manager.place_order(signal, lot_size)
        if not result.success:
            logger.error(
                "Failed to open trade | %s %s | %s",
                signal.symbol, signal.timeframe, result.error_message,
            )
            return None

        trade = Trade(
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            direction=signal.direction,
            entry_price=result.fill_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            lot_size=lot_size,
            initial_risk_distance=abs(result.fill_price - signal.stop_loss),
            opened_at=datetime.now(timezone.utc),
            pip_size=spec.pip_size,
            pip_value_per_lot=spec.pip_value,
            ticket=result.ticket,
            status=TradeStatus.OPEN,
        )

        self._trades[trade.ticket] = trade

        telegram_notifier.send_message(
            f"══════════════════════\n"
            f"🟢 {trade.direction.value.upper()} EXECUTED\n"
            f"══════════════════════\n\n"
            f"📊 Symbol      : {trade.symbol}\n"
            f"⏰ Timeframe   : {trade.timeframe}\n\n"
            f"📈 Setup\n"
            f"✅ Reversal Detected\n"
            f"✅ EMA5 / EMA10 Cross\n"
            f"✅ EMA Retest\n"
            f"✅ Confirmation Candle\n\n"
            f"💰 Entry        : {trade.entry_price:.5f}\n"
            f"🛑 Stop Loss    : {trade.stop_loss:.5f}\n"
            f"🎯 Take Profit  : {trade.take_profit:.5f}\n\n"
            f"📦 Lot Size     : {trade.lot_size:.2f}\n"
            f"⚠️ Risk         : {config.RISK_PERCENT_PER_TRADE}%\n\n"
            f"🎫 Ticket       : {trade.ticket}\n"
            f"══════════════════════"
        )

        return trade

    def _apply_execution_stop_buffer(self, signal: Signal, spec: order_manager.SymbolSpec) -> Signal:
        """
        Widen the strategy SL for real execution spread and broker stop rules.
        """
        try:
            spread = order_manager.get_current_spread_price(signal.symbol)
        except RuntimeError as e:
            logger.warning("Could not fetch spread for %s; using fixed SL buffer only: %s", signal.symbol, e)
            spread = 0.0

        spread_buffer = spread * config.STOP_SPREAD_BUFFER_MULTIPLIER
        fixed_buffer = config.EXTRA_STOP_BUFFER_PIPS * spec.pip_size
        broker_buffer = spec.stops_level * spec.point
        stop_buffer = max(spread_buffer + fixed_buffer, broker_buffer)

        if stop_buffer <= 0:
            return signal

        if signal.direction == SignalDirection.BUY:
            stop_loss = signal.stop_loss - stop_buffer
            risk_distance = signal.entry_price - stop_loss
            take_profit = signal.entry_price + (risk_distance * config.RISK_REWARD_RATIO)
        else:
            stop_loss = signal.stop_loss + stop_buffer
            risk_distance = stop_loss - signal.entry_price
            take_profit = signal.entry_price - (risk_distance * config.RISK_REWARD_RATIO)

        adjusted = replace(signal, stop_loss=stop_loss, take_profit=take_profit)
        logger.risk(
            "Execution SL buffer applied | %s %s %s spread=%.5f buffer=%.5f sl %.5f->%.5f tp %.5f->%.5f",
            signal.symbol, signal.timeframe, signal.direction.value.upper(),
            spread, stop_buffer, signal.stop_loss, adjusted.stop_loss,
            signal.take_profit, adjusted.take_profit,
        )
        return adjusted

    def process_open_trade(
        self,
        symbol: str,
        timeframe: str,
        ema_set: EMASet,
        current_price: float,
    ) -> None:
        """
        Run one management cycle for the open trade at (symbol, timeframe).
        """
        trades = [
            trade for trade in self._trades.values()
            if trade.symbol == symbol
            and trade.timeframe == timeframe
            and trade.status == TradeStatus.OPEN
        ]
        if not trades:
            return

        for trade in trades:
            process_trailing_for_trade(trade, ema_set, current_price)
            self._check_sl_tp_hit(trade, current_price)

    def _resolve_broker_close(self, trade: Trade) -> None:
        """
        Position disappeared from MT5 — query deal history to find the
        real close reason (TP / SL / manual) instead of guessing.
        """
        import MetaTrader5 as mt5
        from datetime import timedelta

        date_from = trade.opened_at - timedelta(minutes=5)
        date_to   = datetime.now(timezone.utc) + timedelta(minutes=5)
        deals     = mt5.history_deals_get(date_from, date_to)

        close_reason = CloseReason.MANUAL
        close_price  = trade.take_profit
        realized_pnl = None

        if deals:
            for deal in deals:
                if deal.position_id == trade.ticket and deal.entry == 1:
                    close_price  = deal.price
                    realized_pnl = deal.profit
                    if deal.reason == 3:       # DEAL_REASON_TP
                        close_reason = CloseReason.TAKE_PROFIT
                    elif deal.reason == 4:     # DEAL_REASON_SL
                        close_reason = (
                            CloseReason.TRAILING_STOP
                            if trade.trailing_active
                            else CloseReason.STOP_LOSS
                        )
                    else:
                        close_reason = CloseReason.MANUAL
                    break

        trade.close(close_price, close_reason, datetime.now(timezone.utc))
        if realized_pnl is not None:
            trade.realized_pnl = realized_pnl

        reason_label = {
            CloseReason.TAKE_PROFIT:   "✅ Take Profit hit",
            CloseReason.STOP_LOSS:     "🔴 Stop Loss hit",
            CloseReason.TRAILING_STOP: "🔴 Trailing Stop hit",
            CloseReason.MANUAL:        "🟡 Manually closed",
        }.get(close_reason, close_reason.value)

        pnl_str = f"{realized_pnl:+.2f}" if realized_pnl is not None else "unknown"

        logger.trade(
            "%s | ticket=%s %s %s | close=%.5f | pnl=%s",
            reason_label, trade.ticket, trade.symbol,
            trade.timeframe, close_price, pnl_str,
        )

        telegram_notifier.send_message(
            f"══════════════════════\n"
            f"{reason_label}\n"
            f"══════════════════════\n\n"
            f"🎫 Ticket    : {trade.ticket}\n"
            f"📊 Symbol    : {trade.symbol}\n"
            f"⏰ Timeframe : {trade.timeframe}\n"
            f"📉 Direction : {trade.direction.value.upper()}\n\n"
            f"💰 Entry     : {trade.entry_price:.5f}\n"
            f"🔒 Close     : {close_price:.5f}\n"
            f"💵 PnL       : {pnl_str}\n"
            f"══════════════════════"
        )

        self._daily_risk_manager.record_trade_close(trade)

    def _check_sl_tp_hit(self, trade: Trade, current_price: float) -> None:
        """
        Software-side SL/TP safety net. Runs after _resolve_broker_close
        so this only triggers if the broker hasn't already closed the trade.
        """
        hit_tp = False
        hit_sl = False

        if trade.direction == SignalDirection.BUY:
            hit_tp = current_price >= trade.take_profit
            hit_sl = current_price <= trade.stop_loss
        else:
            hit_tp = current_price <= trade.take_profit
            hit_sl = current_price >= trade.stop_loss

        if not hit_tp and not hit_sl:
            return

        reason = CloseReason.TAKE_PROFIT if hit_tp else (
            CloseReason.TRAILING_STOP if trade.trailing_active else CloseReason.STOP_LOSS
        )
        close_price = trade.take_profit if hit_tp else trade.stop_loss

        result = order_manager.close_position(
            ticket=trade.ticket,
            symbol=trade.symbol,
            direction=trade.direction,
            lot_size=trade.lot_size,
        )
        if not result.success:
            logger.error(
                "Failed to close trade | ticket=%s %s | %s",
                trade.ticket, trade.symbol, result.error_message,
            )
            return

        final_close_price = result.fill_price or close_price
        trade.close(final_close_price, reason, datetime.now(timezone.utc))

        reason_label = {
            CloseReason.TAKE_PROFIT:   "✅ Take Profit hit",
            CloseReason.STOP_LOSS:     "🔴 Stop Loss hit",
            CloseReason.TRAILING_STOP: "🔴 Trailing Stop hit",
        }.get(reason, reason.value)

        pnl_str = f"{trade.realized_pnl:+.2f}" if trade.realized_pnl is not None else "unknown"

        logger.trade(
            "%s | ticket=%s %s %s | close=%.5f | pnl=%s",
            reason_label, trade.ticket, trade.symbol,
            trade.timeframe, final_close_price, pnl_str,
        )

        telegram_notifier.send_message(
            f"══════════════════════\n"
            f"{reason_label}\n"
            f"══════════════════════\n\n"
            f"🎫 Ticket    : {trade.ticket}\n"
            f"📊 Symbol    : {trade.symbol}\n"
            f"⏰ Timeframe : {trade.timeframe}\n"
            f"📉 Direction : {trade.direction.value.upper()}\n\n"
            f"💰 Entry     : {trade.entry_price:.5f}\n"
            f"🔒 Close     : {final_close_price:.5f}\n"
            f"💵 PnL       : {pnl_str}\n"
            f"══════════════════════"
        )

        self._daily_risk_manager.record_trade_close(trade)

    def get_all_open_trades(self) -> list[Trade]:
        """Return all trades currently in OPEN status across all timeframes."""
        return [t for t in self._trades.values() if t.status == TradeStatus.OPEN]