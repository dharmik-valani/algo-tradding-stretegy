from __future__ import annotations

import uuid
from datetime import datetime, timezone

from algo.paper.models import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    StrategyState,
)


class PaperBroker:
    """Virtual cash account. Never talks to Dhan order APIs."""

    def __init__(
        self,
        *,
        starting_cash: float = 100_000.0,
        fee_bps: float = 1.0,
        slippage_bps: float = 2.0,
    ) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.position = Position(
            strategy_id="",
            instrument_id="",
            symbol="",
            quantity=0,
            avg_price=0.0,
        )
        self.fills: list[Fill] = []
        self.orders: list[Order] = []
        self.closed_trades: list[dict] = []
        self.realized_pnl = 0.0

    def bind(self, strategy_id: str, instrument_id: str, symbol: str) -> None:
        self.position.strategy_id = strategy_id
        self.position.instrument_id = instrument_id
        self.position.symbol = symbol

    def submit_market(
        self,
        *,
        strategy_id: str,
        instrument_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        last_price: float,
        ts: datetime,
    ) -> Order:
        order = Order(
            id=str(uuid.uuid4())[:8],
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            status=OrderStatus.NEW,
            created_at=ts,
        )
        self.orders.append(order)
        if quantity <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "quantity must be > 0"
            return order
        if last_price <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "invalid last price"
            return order

        slip = last_price * (self.slippage_bps / 10_000)
        fill_price = last_price + slip if side is Side.BUY else max(0.01, last_price - slip)
        notional = fill_price * quantity
        fee = notional * (self.fee_bps / 10_000)

        if side is Side.BUY:
            cost = notional + fee
            if cost > self.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"insufficient cash (need {cost:.2f}, have {self.cash:.2f})"
                return order
            # average into long, or reduce short
            self._apply_buy(quantity, fill_price, fee)
        else:
            # sell / short: for v1 only allow closing or opening short if flat/long
            self._apply_sell(quantity, fill_price, fee)

        order.status = OrderStatus.FILLED
        order.filled_at = ts
        order.fill_price = fill_price
        fill = Fill(
            order_id=order.id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=fill_price,
            timestamp=ts,
            fee=fee,
        )
        self.fills.append(fill)
        return order

    def unrealized_pnl(self, last_price: float | None) -> float:
        if last_price is None or self.position.quantity == 0:
            return 0.0
        return (last_price - self.position.avg_price) * self.position.quantity

    def equity(self, last_price: float | None) -> float:
        return self.cash + (
            self.position.quantity * (last_price or self.position.avg_price or 0.0)
        )

    def snapshot(self, state: StrategyState, last_price: float | None) -> StrategyState:
        state.cash = self.cash
        state.starting_cash = self.starting_cash
        state.realized_pnl = self.realized_pnl
        state.unrealized_pnl = self.unrealized_pnl(last_price)
        state.last_price = last_price
        state.position = self.position.model_copy()
        state.fills = list(self.fills[-50:])
        state.orders = list(self.orders[-50:])
        return state

    def _apply_buy(self, qty: int, price: float, fee: float) -> None:
        pos = self.position
        notional = price * qty
        self.cash -= notional + fee
        if pos.quantity >= 0:
            new_qty = pos.quantity + qty
            if new_qty == 0:
                pos.avg_price = 0.0
            else:
                pos.avg_price = ((pos.avg_price * pos.quantity) + notional) / new_qty
            pos.quantity = new_qty
        else:
            # covering short
            cover = min(qty, abs(pos.quantity))
            pnl = (pos.avg_price - price) * cover
            self.realized_pnl += pnl
            self._record_closed_trade(pnl=pnl, quantity=cover, entry=pos.avg_price, exit_=price, side="cover")
            pos.quantity += qty
            if pos.quantity == 0:
                pos.avg_price = 0.0
            elif pos.quantity > 0:
                pos.avg_price = price

    def _apply_sell(self, qty: int, price: float, fee: float) -> None:
        pos = self.position
        notional = price * qty
        self.cash += notional - fee
        if pos.quantity > 0:
            close = min(qty, pos.quantity)
            pnl = (price - pos.avg_price) * close
            self.realized_pnl += pnl
            self._record_closed_trade(pnl=pnl, quantity=close, entry=pos.avg_price, exit_=price, side="long")
            pos.quantity -= close
            leftover = qty - close
            if pos.quantity == 0:
                pos.avg_price = 0.0
            if leftover > 0:
                pos.quantity = -leftover
                pos.avg_price = price
        else:
            # add to short or open short
            new_qty = pos.quantity - qty
            if pos.quantity == 0:
                pos.avg_price = price
            else:
                # average short entry
                old_abs = abs(pos.quantity)
                pos.avg_price = ((pos.avg_price * old_abs) + notional) / (old_abs + qty)
            pos.quantity = new_qty

    def _record_closed_trade(
        self,
        *,
        pnl: float,
        quantity: int,
        entry: float,
        exit_: float,
        side: str,
    ) -> None:
        self.closed_trades.append(
            {
                "pnl": round(pnl, 4),
                "quantity": quantity,
                "entry": round(entry, 4),
                "exit": round(exit_, 4),
                "side": side,
            }
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
