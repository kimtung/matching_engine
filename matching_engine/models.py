from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(slots=True)
class Order:
    order_id: str
    side: Side
    quantity: int
    timestamp: int
    price: float | None = None
    order_type: OrderType = OrderType.LIMIT
    remaining: int = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.quantity


@dataclass(slots=True)
class Trade:
    buy_order_id: str
    sell_order_id: str
    price: float
    quantity: int
    aggressor_order_id: str
    timestamp: int
