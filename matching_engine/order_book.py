from __future__ import annotations

from dataclasses import asdict
from itertools import chain

from matching_engine.models import Order, OrderType, Side, Trade


class OrderBook:
    def __init__(self) -> None:
        self.buys: list[Order] = []
        self.sells: list[Order] = []
        self.trades: list[Trade] = []

    def submit(self, order: Order) -> list[Trade]:
        book = self.sells if order.side == Side.BUY else self.buys
        self._sort_books()

        trades: list[Trade] = []
        while order.remaining > 0 and book:
            best = book[0]
            if not self._is_match(order, best):
                break

            matched_quantity = min(order.remaining, best.remaining)
            trade_price = best.price or 0.0
            trade = self._make_trade(order, best, trade_price, matched_quantity)
            trades.append(trade)
            self.trades.append(trade)

            order.remaining -= matched_quantity
            best.remaining -= matched_quantity
            if best.remaining == 0:
                book.pop(0)

        if order.remaining > 0 and order.order_type == OrderType.LIMIT:
            self._rest(order)

        return trades

    def cancel(self, order_id: str) -> bool:
        for book in (self.buys, self.sells):
            for index, order in enumerate(book):
                if order.order_id == order_id and order.remaining > 0:
                    book.pop(index)
                    return True
        return False

    def _rest(self, order: Order) -> None:
        if order.side == Side.BUY:
            self.buys.append(order)
        else:
            self.sells.append(order)
        self._sort_books()

    def _sort_books(self) -> None:
        self.buys.sort(key=lambda item: (item.price or 0.0, item.timestamp), reverse=True)
        self.sells.sort(key=lambda item: (item.price or 0.0, item.timestamp))

    def _is_match(self, incoming: Order, resting: Order) -> bool:
        if incoming.order_type == OrderType.MARKET:
            return incoming.price is not None
        if incoming.side == Side.BUY:
            return (incoming.price or 0.0) >= (resting.price or 0.0)
        return (incoming.price or 0.0) <= (resting.price or 0.0)

    def _make_trade(self, incoming: Order, resting: Order, price: float, quantity: int) -> Trade:
        if incoming.side == Side.BUY:
            return Trade(
                buy_order_id=incoming.order_id,
                sell_order_id=resting.order_id,
                price=price,
                quantity=quantity,
                aggressor_order_id=incoming.order_id,
                timestamp=incoming.timestamp,
            )
        return Trade(
            buy_order_id=resting.order_id,
            sell_order_id=incoming.order_id,
            price=price,
            quantity=quantity,
            aggressor_order_id=incoming.order_id,
            timestamp=incoming.timestamp,
        )

    def snapshot(self) -> dict[str, list[dict]]:
        return {
            "buys": [asdict(order) for order in self.buys if order.remaining > 0],
            "sells": [asdict(order) for order in self.sells if order.remaining > 0],
            "trades": [asdict(trade) for trade in self.trades],
        }

    def active_orders(self) -> list[dict]:
        orders = [asdict(order) for order in chain(self.buys, self.sells) if order.remaining > 0]
        orders.sort(key=lambda item: item["timestamp"])
        return orders
