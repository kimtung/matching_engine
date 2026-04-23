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
        # BUG-05 fix: reject negative price at engine layer so direct service
        # calls (bypassing web validation) cannot resting or trade at < 0.
        if price is None or price < 0:
            raise ValueError("limit order price must be non-negative")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
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
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        order = Order(
            order_id=order_id,
            side=Side(side),
            quantity=quantity,
            price=None,
            timestamp=timestamp,
            order_type=OrderType.MARKET,
        )
        return [asdict(trade) for trade in self.order_book.submit(order)]

    def place_stop_order(
        self,
        order_id: str,
        side: str,
        quantity: int,
        stop_price: float,
        timestamp: int,
        limit_price: float | None = None,
    ) -> list[dict]:
        """Place a stop order.

        If ``limit_price`` is None → STOP_MARKET (converts to MARKET on trigger).
        If ``limit_price`` is given → STOP_LIMIT (converts to LIMIT at that price).
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if stop_price is None or stop_price <= 0:
            raise ValueError("stop_price must be positive")

        if limit_price is None:
            order_type = OrderType.STOP_MARKET
            price = None
        else:
            if limit_price <= 0:
                raise ValueError("limit price must be positive")
            order_type = OrderType.STOP_LIMIT
            price = limit_price

        order = Order(
            order_id=order_id,
            side=Side(side),
            quantity=quantity,
            price=price,
            timestamp=timestamp,
            order_type=order_type,
            stop_price=stop_price,
        )
        return [asdict(trade) for trade in self.order_book.submit(order)]

    def get_order_book(self) -> dict[str, list[dict]]:
        return self.order_book.snapshot()

    def cancel_order(self, order_id: str) -> bool:
        return self.order_book.cancel(order_id)

    def get_active_orders(self) -> list[dict]:
        return self.order_book.active_orders()
