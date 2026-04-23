from __future__ import annotations

from collections import deque
from dataclasses import asdict
from itertools import chain

from matching_engine.models import STOP_ORDER_TYPES, Order, OrderType, Side, Trade

# BUG-09 fix: cap retained trade history so long-running servers don't leak
# memory and /api/state payloads don't grow unbounded.
MAX_TRADES = 10000


class OrderBook:
    def __init__(self) -> None:
        self.buys: list[Order] = []
        self.sells: list[Order] = []
        self.trades: deque[Trade] = deque(maxlen=MAX_TRADES)
        # Stop orders sit here until their trigger price is crossed by last_price.
        self.stop_orders: list[Order] = []
        # Price of the most recent trade — reference point for stop triggers.
        self.last_price: float | None = None

    def submit(self, order: Order) -> list[Trade]:
        # Stop orders: either trigger immediately (fall through to matching as
        # MARKET/LIMIT) or park in the pending-stop list.
        if order.order_type in STOP_ORDER_TYPES:
            if self._stop_triggered(order, self.last_price):
                self._convert_stop(order)
            else:
                if not any(s.order_id == order.order_id for s in self.stop_orders):
                    self.stop_orders.append(order)
                return []

        trades = self._match(order)
        # A newly-executed trade may cross pending stop-order trigger prices.
        # Process cascades iteratively until the system is stable.
        trades.extend(self._process_triggered_stops())
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
        # Pending stops are cancellable too — same order_id semantics.
        stop_indices = [
            i for i, o in enumerate(self.stop_orders)
            if o.order_id == order_id and o.remaining > 0
        ]
        for i in reversed(stop_indices):
            self.stop_orders.pop(i)
        if stop_indices:
            found = True
        return found

    def _match(self, order: Order) -> list[Trade]:
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
            self.last_price = trade_price

            order.remaining -= matched_quantity
            best.remaining -= matched_quantity
            if best.remaining == 0:
                book.pop(0)

        if order.remaining > 0 and order.order_type == OrderType.LIMIT:
            self._rest(order)

        return trades

    def _stop_triggered(self, stop_order: Order, reference_price: float | None) -> bool:
        """BUY stop triggers when market rises to stop_price; SELL stop when it falls."""
        if reference_price is None or stop_order.stop_price is None:
            return False
        if stop_order.side == Side.BUY:
            return reference_price >= stop_order.stop_price
        return reference_price <= stop_order.stop_price

    def _convert_stop(self, stop_order: Order) -> None:
        """Mutate a triggered stop order into its post-trigger type in place."""
        if stop_order.order_type == OrderType.STOP_MARKET:
            stop_order.order_type = OrderType.MARKET
            stop_order.price = None
        else:  # STOP_LIMIT → LIMIT; `price` already holds the limit price.
            stop_order.order_type = OrderType.LIMIT

    def _process_triggered_stops(self) -> list[Trade]:
        """Fire pending stops whose trigger price is crossed by last_price.

        A single trade can cascade — firing one stop can create a new trade
        that crosses yet another stop's trigger price. Loop until no more
        stops are eligible.
        """
        cascade_trades: list[Trade] = []
        while True:
            eligible = [s for s in self.stop_orders if self._stop_triggered(s, self.last_price)]
            if not eligible:
                break
            # Fairness: fire earlier-submitted stops first when multiple cross
            # simultaneously at the same last_price.
            eligible.sort(key=lambda s: s.timestamp)
            for stop in eligible:
                try:
                    self.stop_orders.remove(stop)
                except ValueError:
                    continue
                self._convert_stop(stop)
                cascade_trades.extend(self._match(stop))
        return cascade_trades

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

    def snapshot(self) -> dict:
        return {
            "buys": [asdict(order) for order in self.buys if order.remaining > 0],
            "sells": [asdict(order) for order in self.sells if order.remaining > 0],
            "stops": [asdict(order) for order in self.stop_orders if order.remaining > 0],
            "trades": [asdict(trade) for trade in self.trades],
            "last_price": self.last_price,
        }

    def active_orders(self) -> list[dict]:
        orders = [
            asdict(order)
            for order in chain(self.buys, self.sells, self.stop_orders)
            if order.remaining > 0
        ]
        orders.sort(key=lambda item: item["timestamp"])
        return orders
