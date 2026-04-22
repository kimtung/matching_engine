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
        # BUG-03 fix: remove ALL copies with this order_id (not just the first)
        found = False
        for book in (self.buys, self.sells):
            indices = [i for i, o in enumerate(book) if o.order_id == order_id and o.remaining > 0]
            for i in reversed(indices):
                book.pop(i)
            if indices:
                found = True
        return found

    def _rest(self, order: Order) -> None:
        book = self.buys if order.side == Side.BUY else self.sells
        # BUG-03 fix: reject duplicate order_id
        if any(o.order_id == order.order_id for o in book):
            return
        book.append(order)
        self._sort_books()

    def _sort_books(self) -> None:
        # BUG-02 fix: negate price so buys sort high→low on price, low→high on timestamp (FIFO)
        self.buys.sort(key=lambda item: (-(item.price or 0.0), item.timestamp))
        self.sells.sort(key=lambda item: (item.price or 0.0, item.timestamp))

    def _is_match(self, incoming: Order, resting: Order) -> bool:
        # BUG-01 fix: market orders match any resting order unconditionally
        if incoming.order_type == OrderType.MARKET:
            return True
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
