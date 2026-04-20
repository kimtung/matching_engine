from __future__ import annotations

from dataclasses import asdict

from matching_engine.models import Order, OrderType, Side
from matching_engine.order_book import OrderBook


class MatchingEngineService:
    def __init__(self) -> None:
        self.order_book = OrderBook()

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quantity: int,
        price: float,
        timestamp: int,
    ) -> list[dict]:
        order = Order(
            order_id=order_id,
            side=Side(side),
            quantity=quantity,
            price=price,
            timestamp=timestamp,
            order_type=OrderType.LIMIT,
        )
        return [asdict(trade) for trade in self.order_book.submit(order)]

    def place_market_order(
        self,
        order_id: str,
        side: str,
        quantity: int,
        timestamp: int,
    ) -> list[dict]:
        order = Order(
            order_id=order_id,
            side=Side(side),
            quantity=quantity,
            price=None,
            timestamp=timestamp,
            order_type=OrderType.MARKET,
        )
        return [asdict(trade) for trade in self.order_book.submit(order)]

    def get_order_book(self) -> dict[str, list[dict]]:
        return self.order_book.snapshot()

    def cancel_order(self, order_id: str) -> bool:
        return self.order_book.cancel(order_id)

    def get_active_orders(self) -> list[dict]:
        return self.order_book.active_orders()
