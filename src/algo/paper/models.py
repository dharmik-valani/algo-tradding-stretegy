from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class SignalAction(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"  # close any open position


class Bar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Signal(BaseModel):
    action: SignalAction
    reason: str = ""
    strength: float = 1.0
    meta: dict[str, Any] = Field(default_factory=dict)


class Order(BaseModel):
    id: str
    strategy_id: str
    instrument_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    status: OrderStatus = OrderStatus.NEW
    created_at: datetime
    filled_at: datetime | None = None
    fill_price: float | None = None
    reject_reason: str | None = None


class Fill(BaseModel):
    order_id: str
    strategy_id: str
    instrument_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    timestamp: datetime
    fee: float = 0.0


class Position(BaseModel):
    strategy_id: str
    instrument_id: str
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


class StrategyState(BaseModel):
    strategy_id: str
    instance_id: str = ""
    name: str
    enabled: bool = True
    instrument: str = "NIFTY"
    asset_kind: str = "index"
    timeframe: str = "5m"
    quantity: int = 1
    cash: float = 100_000.0
    starting_cash: float = 100_000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_price: float | None = None
    last_signal: str = "HOLD"
    last_bar_at: datetime | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    position: Position | None = None
    fills: list[Fill] = Field(default_factory=list)
    orders: list[Order] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    basket: list[dict[str, Any]] = Field(default_factory=list)
    # Desk glance fields (open trade / basket summary)
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    legs_selected: int = 0
    legs_in_trade: int = 0
    note: str = ""


class SessionSnapshot(BaseModel):
    mode: str
    running: bool
    started_at: datetime | None = None
    updated_at: datetime | None = None
    strategies: list[StrategyState] = Field(default_factory=list)
    message: str = ""
