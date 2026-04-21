"""
Unit tests for the matching engine.

Covers:
- Full match (buy price >= sell price, equal quantity)
- Partial fill (unequal quantities)
- No match (buy price < sell price)
- Price-time priority (same price, earlier order matched first)
- Market order matching
"""

import pytest
from matching_engine.order_book import OrderBook
from matching_engine.models import Order, OrderType, Side
from matching_engine.service import MatchingEngineService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_limit(order_id: str, side: str, quantity: int, price: float, ts: int) -> Order:
    return Order(
        order_id=order_id,
        side=Side(side),
        quantity=quantity,
        price=price,
        timestamp=ts,
        order_type=OrderType.LIMIT,
    )


def make_market(order_id: str, side: str, quantity: int, ts: int) -> Order:
    return Order(
        order_id=order_id,
        side=Side(side),
        quantity=quantity,
        price=None,
        timestamp=ts,
        order_type=OrderType.MARKET,
    )


# ===========================================================================
# 1. Full match — giá mua >= giá bán, cùng khối lượng
# ===========================================================================

class TestFullMatch:
    def test_buy_matches_sell_same_price_same_qty(self):
        """BUY 100@10 vs SELL 100@10 → 1 trade, qty=100, sổ trống."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 100, 10.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 100, 10.0, 2))

        assert len(trades) == 1
        assert trades[0].price == 10.0
        assert trades[0].quantity == 100
        assert trades[0].buy_order_id == "B1"
        assert trades[0].sell_order_id == "S1"

    def test_buy_higher_price_matches_sell(self):
        """BUY @105 vs SELL @100 → khớp tại giá sell (resting price)."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 50, 100.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 50, 105.0, 2))

        assert len(trades) == 1
        assert trades[0].price == 100.0   # giá của resting order (sell)
        assert trades[0].quantity == 50

    def test_no_remaining_orders_after_full_match(self):
        """Sau khi khớp hoàn toàn, cả hai sổ phải trống."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 10, 50.0, 1))
        book.submit(make_limit("B1", "BUY", 10, 50.0, 2))

        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"] == []

    def test_aggressor_is_incoming_order(self):
        """aggressor_order_id phải là lệnh vừa đặt (incoming)."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 10.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 5, 10.0, 2))

        assert trades[0].aggressor_order_id == "B1"

    def test_sell_aggressor_matches_resting_buy(self):
        """SELL aggressive vào BUY resting → aggressor là SELL."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 10.0, 1))
        trades = book.submit(make_limit("S1", "SELL", 5, 10.0, 2))

        assert len(trades) == 1
        assert trades[0].aggressor_order_id == "S1"
        assert trades[0].buy_order_id == "B1"
        assert trades[0].sell_order_id == "S1"


# ===========================================================================
# 2. Partial fill — khối lượng không bằng nhau
# ===========================================================================

class TestPartialFill:
    def test_buy_larger_than_sell_leaves_buy_remainder(self):
        """BUY 10 vs SELL 6 → khớp 6, BUY còn 4 trên sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 6, 100.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 10, 100.0, 2))

        assert len(trades) == 1
        assert trades[0].quantity == 6

        snap = book.snapshot()
        assert snap["sells"] == []
        assert len(snap["buys"]) == 1
        assert snap["buys"][0]["remaining"] == 4

    def test_sell_larger_than_buy_leaves_sell_remainder(self):
        """BUY 3 vs SELL 10 → khớp 3, SELL còn 7 trên sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 10, 100.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 3, 100.0, 2))

        assert trades[0].quantity == 3

        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"][0]["remaining"] == 7

    def test_partial_fill_then_another_match(self):
        """Lệnh còn dư trên sổ có thể khớp tiếp với lệnh sau."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 10, 100.0, 1))  # resting buy 10

        # SELL 4 → partial fill, BUY còn 6
        trades1 = book.submit(make_limit("S1", "SELL", 4, 100.0, 2))
        assert trades1[0].quantity == 4

        # SELL 6 → khớp nốt phần còn lại
        trades2 = book.submit(make_limit("S2", "SELL", 6, 100.0, 3))
        assert trades2[0].quantity == 6

        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"] == []

    def test_one_incoming_matches_multiple_resting(self):
        """1 lệnh BUY lớn khớp với nhiều SELL nhỏ hơn."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 3, 100.0, 1))
        book.submit(make_limit("S2", "SELL", 3, 100.0, 2))
        book.submit(make_limit("S3", "SELL", 3, 100.0, 3))

        trades = book.submit(make_limit("B1", "BUY", 9, 100.0, 4))

        assert len(trades) == 3
        assert sum(t.quantity for t in trades) == 9
        assert book.snapshot()["sells"] == []


# ===========================================================================
# 3. Không khớp — giá mua < giá bán
# ===========================================================================

class TestNoMatch:
    def test_buy_below_sell_price_no_trade(self):
        """BUY @90 vs SELL @100 → không khớp, cả hai vào sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 5, 90.0, 2))

        assert trades == []

        snap = book.snapshot()
        assert len(snap["sells"]) == 1
        assert len(snap["buys"]) == 1

    def test_buy_exactly_below_sell_boundary(self):
        """BUY @99.99 vs SELL @100 → không khớp (strict inequality)."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 1, 100.0, 1))
        trades = book.submit(make_limit("B1", "BUY", 1, 99.99, 2))

        assert trades == []

    def test_market_order_no_resting_no_trade(self):
        """MARKET order khi sổ đối diện trống → không có trade, lệnh bị bỏ."""
        book = OrderBook()
        trades = book.submit(make_market("M1", "BUY", 5, 1))

        assert trades == []
        # Market order không được đưa vào sổ
        assert book.snapshot()["buys"] == []

    def test_no_self_match(self):
        """BUY và SELL cùng phía không khớp nhau."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))
        trades = book.submit(make_limit("B2", "BUY", 5, 100.0, 2))

        assert trades == []
        assert len(book.snapshot()["buys"]) == 2


# ===========================================================================
# 4. Price-time priority — ưu tiên giá rồi đến thời gian
# ===========================================================================

class TestPriceTimePriority:
    def test_higher_buy_price_matched_first(self):
        """BUY @102 được ưu tiên hơn BUY @100 khi SELL vào."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))
        book.submit(make_limit("B2", "BUY", 5, 102.0, 2))  # giá cao hơn

        trades = book.submit(make_limit("S1", "SELL", 5, 99.0, 3))

        assert trades[0].buy_order_id == "B2"   # B2 (giá cao hơn) khớp trước

    def test_same_price_earlier_timestamp_matched_first(self):
        """Cùng giá → lệnh cũ hơn (timestamp nhỏ hơn) được khớp trước."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))   # ts=1, đến trước
        book.submit(make_limit("B2", "BUY", 5, 100.0, 2))   # ts=2, đến sau

        # SELL chỉ đủ khớp 1 lệnh
        trades = book.submit(make_limit("S1", "SELL", 5, 100.0, 3))

        assert len(trades) == 1
        assert trades[0].buy_order_id == "B1"   # B1 (đến trước) được khớp

    def test_same_price_buy_book_order_b1_before_b2(self):
        """Snapshot buy book: B1 (ts=1) phải đứng trước B2 (ts=2) cùng giá."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))
        book.submit(make_limit("B2", "BUY", 5, 100.0, 2))

        snap = book.snapshot()
        ids = [o["order_id"] for o in snap["buys"]]
        assert ids == ["B1", "B2"]

    def test_lower_sell_price_matched_first(self):
        """SELL @98 được ưu tiên hơn SELL @100 khi BUY vào."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))
        book.submit(make_limit("S2", "SELL", 5, 98.0, 2))   # giá thấp hơn = ưu tiên hơn

        trades = book.submit(make_limit("B1", "BUY", 5, 105.0, 3))

        assert trades[0].sell_order_id == "S2"   # S2 (giá thấp hơn) khớp trước

    def test_same_price_sell_earlier_timestamp_first(self):
        """SELL cùng giá → lệnh đến trước được khớp trước."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))
        book.submit(make_limit("S2", "SELL", 5, 100.0, 2))

        trades = book.submit(make_limit("B1", "BUY", 5, 100.0, 3))

        assert trades[0].sell_order_id == "S1"

    def test_price_priority_overrides_time(self):
        """Lệnh giá tốt hơn nhưng đến SAU vẫn được ưu tiên hơn lệnh đến trước giá xấu."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))   # giá thấp, đến trước
        book.submit(make_limit("B2", "BUY", 5, 105.0, 2))   # giá cao, đến sau

        trades = book.submit(make_limit("S1", "SELL", 5, 99.0, 3))

        assert trades[0].buy_order_id == "B2"   # B2 (giá cao hơn) thắng dù đến sau


# ===========================================================================
# 5. Market order — lệnh thị trường
# ===========================================================================

class TestMarketOrder:
    def test_buy_market_matches_resting_sell(self):
        """BUY MARKET khớp với SELL LIMIT đang chờ trong sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 10, 100.0, 1))
        trades = book.submit(make_market("M1", "BUY", 10, 2))

        assert len(trades) == 1
        assert trades[0].quantity == 10
        assert trades[0].price == 100.0   # lấy giá của resting sell

    def test_sell_market_matches_resting_buy(self):
        """SELL MARKET khớp với BUY LIMIT đang chờ."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 50.0, 1))
        trades = book.submit(make_market("M1", "SELL", 5, 2))

        assert len(trades) == 1
        assert trades[0].price == 50.0

    def test_market_order_partial_fill(self):
        """MARKET order có thể fill một phần nếu sổ không đủ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 3, 100.0, 1))

        trades = book.submit(make_market("M1", "BUY", 10, 2))

        # Chỉ khớp được 3 (tất cả những gì có trên sổ)
        assert len(trades) == 1
        assert trades[0].quantity == 3
        # MARKET order không được rest trên sổ dù còn dư
        assert book.snapshot()["buys"] == []

    def test_market_order_sweeps_multiple_levels(self):
        """MARKET order quét nhiều mức giá liên tiếp."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 2, 100.0, 1))
        book.submit(make_limit("S2", "SELL", 3, 101.0, 2))
        book.submit(make_limit("S3", "SELL", 5, 102.0, 3))

        trades = book.submit(make_market("M1", "BUY", 10, 4))

        assert len(trades) == 3
        assert sum(t.quantity for t in trades) == 10
        # Giá tốt nhất (thấp nhất) được khớp trước
        assert trades[0].price == 100.0
        assert trades[1].price == 101.0
        assert trades[2].price == 102.0

    def test_market_order_not_added_to_book_when_no_match(self):
        """MARKET order không được vào sổ kể cả khi không khớp được."""
        book = OrderBook()
        trades = book.submit(make_market("M1", "SELL", 5, 1))

        assert trades == []
        assert book.snapshot()["sells"] == []


# ===========================================================================
# 6. Vòng đời lệnh — cancel và trạng thái sổ
# ===========================================================================

class TestOrderLifecycle:
    def test_cancel_removes_buy_from_book(self):
        """Cancel BUY order → biến mất khỏi sổ."""
        book = OrderBook()
        book.submit(make_limit("B1", "BUY", 5, 100.0, 1))

        result = book.cancel("B1")

        assert result is True
        assert book.snapshot()["buys"] == []

    def test_cancel_removes_sell_from_book(self):
        """Cancel SELL order → biến mất khỏi sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))

        result = book.cancel("S1")

        assert result is True
        assert book.snapshot()["sells"] == []

    def test_cancel_nonexistent_order_returns_false(self):
        """Cancel order không tồn tại → trả về False."""
        book = OrderBook()
        assert book.cancel("GHOST") is False

    def test_fully_filled_order_not_in_book(self):
        """Lệnh đã khớp hoàn toàn không còn trên sổ."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))
        book.submit(make_limit("B1", "BUY", 5, 100.0, 2))

        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"] == []

    def test_partially_filled_order_remains_with_correct_remaining(self):
        """Lệnh fill một phần → vẫn trên sổ với remaining chính xác."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 10, 100.0, 1))
        book.submit(make_limit("B1", "BUY", 3, 100.0, 2))

        snap = book.snapshot()
        assert snap["sells"][0]["remaining"] == 7

    def test_cancel_partially_filled_order(self):
        """Lệnh đã fill một phần vẫn có thể bị cancel."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 10, 100.0, 1))
        book.submit(make_limit("B1", "BUY", 3, 100.0, 2))  # S1 còn 7

        result = book.cancel("S1")
        assert result is True
        assert book.snapshot()["sells"] == []

    def test_active_orders_excludes_filled(self):
        """active_orders() chỉ trả về lệnh còn dư."""
        book = OrderBook()
        book.submit(make_limit("S1", "SELL", 5, 100.0, 1))
        book.submit(make_limit("B1", "BUY", 5, 100.0, 2))  # full match

        assert book.active_orders() == []


# ===========================================================================
# 7. Kiểm tra qua Service layer
# ===========================================================================

class TestViaService:
    def test_service_full_match(self):
        svc = MatchingEngineService()
        svc.place_limit_order("S1", "SELL", 5, 100.0, 1)
        trades = svc.place_limit_order("B1", "BUY", 5, 100.0, 2)

        assert len(trades) == 1
        assert trades[0]["quantity"] == 5

    def test_service_market_order(self):
        svc = MatchingEngineService()
        svc.place_limit_order("S1", "SELL", 5, 100.0, 1)
        trades = svc.place_market_order("M1", "BUY", 5, 2)

        assert len(trades) == 1
        assert trades[0]["price"] == 100.0

    def test_service_cancel(self):
        svc = MatchingEngineService()
        svc.place_limit_order("B1", "BUY", 5, 100.0, 1)

        assert svc.cancel_order("B1") is True
        assert svc.get_order_book()["buys"] == []

    def test_service_no_match(self):
        svc = MatchingEngineService()
        svc.place_limit_order("S1", "SELL", 5, 110.0, 1)
        trades = svc.place_limit_order("B1", "BUY", 5, 100.0, 2)

        assert trades == []
        book = svc.get_order_book()
        assert len(book["buys"]) == 1
        assert len(book["sells"]) == 1
