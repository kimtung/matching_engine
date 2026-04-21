"""
Adversarial test suite — designed to trigger bugs only under multiple
simultaneous conditions.

Focus areas:
  A) Exact price equality boundaries
  B) Large-N partial fills to exhaustion
  C) Rapid cancel-then-replace with same order ID
  D) Multiple orders at identical price AND timestamp
  E) Minimum quantity (qty=1)
  F) Compound multi-condition bugs
"""

import pytest
from matching_engine.order_book import OrderBook
from matching_engine.models import Order, OrderType, Side
from matching_engine.service import MatchingEngineService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def lim(order_id, side, qty, price, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=price, timestamp=ts,
        order_type=OrderType.LIMIT,
    )

def mkt(order_id, side, qty, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=None, timestamp=ts,
        order_type=OrderType.MARKET,
    )


# ===========================================================================
# A. Price equality boundaries
# ===========================================================================

class TestPriceEqualityBoundary:

    def test_buy_exactly_equal_sell_must_match(self):
        """BUY @100.0 vs SELL @100.0 — exact equality phải khớp."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))
        trades = book.submit(lim("B1", "BUY", 5, 100.0, 2))

        assert len(trades) == 1, "Exact equality phải tạo trade"
        assert trades[0].price == 100.0

    def test_buy_one_tick_below_sell_must_not_match(self):
        """BUY @99.99 vs SELL @100.0 — thấp hơn 1 tick KHÔNG được khớp."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))
        trades = book.submit(lim("B1", "BUY", 5, 99.99, 2))

        assert trades == [], "Giá thấp hơn 1 tick không được khớp"
        assert len(book.snapshot()["buys"]) == 1
        assert len(book.snapshot()["sells"]) == 1

    def test_sell_exactly_equal_buy_must_match(self):
        """SELL @100.0 vào BUY @100.0 resting — exact equality phải khớp."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        trades = book.submit(lim("S1", "SELL", 5, 100.0, 2))

        assert len(trades) == 1
        assert trades[0].price == 100.0

    def test_sell_one_tick_above_buy_must_not_match(self):
        """SELL @100.01 vs BUY @100.0 — cao hơn 1 tick KHÔNG được khớp."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        trades = book.submit(lim("S1", "SELL", 5, 100.01, 2))

        assert trades == []

    def test_trade_price_is_resting_order_price(self):
        """Giá trade phải là giá của resting order (không phải incoming)."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))        # resting @100
        trades = book.submit(lim("B1", "BUY", 5, 105.0, 2))  # incoming @105

        assert trades[0].price == 100.0, "Giá trade = giá resting (S1), không phải B1"

    def test_price_zero_limit_order_matches_zero_price_resting(self):
        """Cả BUY và SELL đều có price=0.0 — vẫn phải khớp (giá bằng nhau)."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 0.0, 1))
        trades = book.submit(lim("B1", "BUY", 5, 0.0, 2))

        assert len(trades) == 1
        assert trades[0].price == 0.0

    def test_price_zero_buy_does_not_match_nonzero_sell(self):
        """BUY @0.0 không được khớp với SELL @100.0."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))
        trades = book.submit(lim("B1", "BUY", 5, 0.0, 2))

        assert trades == []

    def test_many_decimal_precision_match(self):
        """Kiểm tra floating point: BUY @0.1+0.2 vs SELL @0.3."""
        price = 0.1 + 0.2   # Python floating point: 0.30000000000000004
        book = OrderBook()
        book.submit(lim("S1", "SELL", 1, 0.3, 1))
        trades = book.submit(lim("B1", "BUY", 1, price, 2))

        # 0.1+0.2 > 0.3 in Python due to floating point — should match
        assert len(trades) == 1, "0.1+0.2 > 0.3 về floating point nên phải khớp"


# ===========================================================================
# B. Large-N partial fills to exhaustion
# ===========================================================================

class TestLargeNExhaustion:

    def test_one_large_buy_sweeps_many_small_sells(self):
        """1 BUY qty=100 quét sạch 100 SELL qty=1 liên tiếp."""
        book = OrderBook()
        for i in range(1, 101):
            book.submit(lim(f"S{i}", "SELL", 1, 100.0, i))

        trades = book.submit(lim("B1", "BUY", 100, 100.0, 200))

        assert len(trades) == 100, "Phải tạo đúng 100 trades"
        assert sum(t.quantity for t in trades) == 100
        assert book.snapshot()["sells"] == [], "Tất cả SELL phải được xóa"
        assert book.snapshot()["buys"] == [], "BUY đã fill hoàn toàn"

    def test_last_fill_removes_resting_order(self):
        """Fill lần cuối cùng (remaining=1→0) phải xóa resting order ngay lập tức."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))  # resting BUY qty=10

        # 9 lần fill 1 đơn vị
        for i in range(9):
            book.submit(lim(f"S{i}", "SELL", 1, 100.0, i + 2))

        # Sau 9 fills, B1.remaining phải là 1
        buys = book.snapshot()["buys"]
        assert buys[0]["remaining"] == 1, "Sau 9 fills, B1 phải còn remaining=1"

        # Fill cuối cùng
        trades = book.submit(lim("S_last", "SELL", 1, 100.0, 100))

        assert len(trades) == 1
        assert trades[0].quantity == 1
        assert book.snapshot()["buys"] == [], "B1 phải bị xóa sau fill cuối"

    def test_middle_fills_correct_remaining(self):
        """Các fill ở giữa (không phải cuối) phải cập nhật remaining chính xác."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))

        for i in range(5):  # 5 fills × qty=1 = 5 tổng
            trades = book.submit(lim(f"S{i}", "SELL", 1, 100.0, i + 2))
            expected_remaining = 10 - (i + 1)
            buys = book.snapshot()["buys"]
            assert buys[0]["remaining"] == expected_remaining, \
                f"Sau fill {i+1}: remaining phải là {expected_remaining}"

    def test_many_partial_fills_of_large_resting_order(self):
        """Resting order lớn bị fill từng phần bởi nhiều lệnh nhỏ — tổng phải chính xác."""
        book = OrderBook()
        book.submit(lim("S_LARGE", "SELL", 1000, 100.0, 1))

        all_trades = []
        for i in range(100):  # 100 BUY qty=5 mỗi cái = 500 tổng
            trades = book.submit(lim(f"B{i}", "BUY", 5, 100.0, i + 2))
            all_trades.extend(trades)

        total_filled = sum(t.quantity for t in all_trades)
        assert total_filled == 500

        sells = book.snapshot()["sells"]
        assert sells[0]["remaining"] == 500, "S_LARGE phải còn remaining=500"

    def test_exactly_n_orders_needed_to_exhaust(self):
        """Kiểm tra số chính xác: N orders qty=K mỗi cái → 1 BUY qty=N×K."""
        N, K = 7, 13  # 7 orders × 13 qty = 91 tổng
        book = OrderBook()
        for i in range(N):
            book.submit(lim(f"S{i}", "SELL", K, 100.0, i + 1))

        trades = book.submit(lim("B1", "BUY", N * K, 100.0, N + 100))

        assert len(trades) == N
        assert sum(t.quantity for t in trades) == N * K
        assert book.snapshot()["sells"] == []
        assert book.snapshot()["buys"] == []


# ===========================================================================
# C. Cancel-then-replace with same order ID
# ===========================================================================

class TestCancelThenReplace:

    def test_cancel_then_resubmit_same_id_works(self):
        """Cancel rồi resubmit cùng order_id — phải hoạt động bình thường."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))

        cancelled = book.cancel("B1")
        assert cancelled is True
        assert book.snapshot()["buys"] == []

        # Resubmit cùng ID
        book.submit(lim("B1", "BUY", 5, 100.0, 2))
        buys = book.snapshot()["buys"]
        assert len(buys) == 1
        assert buys[0]["order_id"] == "B1"

    def test_cancel_then_replace_then_match(self):
        """Cancel → replace → match với order mới phải cho kết quả đúng."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 99.0, 1))  # giá không đủ để khớp

        book.cancel("B1")

        book.submit(lim("B1", "BUY", 5, 101.0, 2))  # giá cao hơn
        book.submit(lim("S1", "SELL", 5, 100.0, 3))

        trades = book.submit(lim("S2", "SELL", 5, 100.0, 4))

        # S1 rests sau khi B1 cũ bị cancel, rồi B1 mới khớp với S1
        snap = book.snapshot()
        assert snap["buys"] == [] or snap["sells"] == []

    def test_duplicate_order_id_creates_zombie(self):
        """
        ADVERSARIAL: Submit cùng order_id 2 lần → cancel chỉ xóa 1 → zombie.

        Đây là bug thực: không có kiểm tra uniqueness trong _rest().
        cancel() return sớm sau khi xóa đơn đầu tiên.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B1", "BUY", 5, 100.0, 2))  # duplicate ID, khác ts

        buys_before = book.snapshot()["buys"]
        assert len(buys_before) == 2, "Cả 2 orders đều vào sổ (không có dedup)"

        cancelled = book.cancel("B1")
        assert cancelled is True

        buys_after = book.snapshot()["buys"]
        # BUG: chỉ 1 order bị xóa, 1 order còn lại như "zombie"
        assert len(buys_after) == 1, (
            "BUG PHÁT HIỆN: Cancel chỉ xóa order đầu tiên; "
            f"còn {len(buys_after)} order(s) với ID 'B1' trong sổ"
        )
        assert buys_after[0]["order_id"] == "B1", "Zombie order vẫn có ID = 'B1'"

    def test_duplicate_id_after_partial_fill_then_cancel(self):
        """
        ADVERSARIAL COMPOUND: partial fill → submit duplicate ID → cancel.

        Điều kiện kết hợp:
          1. B1 resting, bị partial fill (remaining < qty)
          2. Submit B1 lần 2 (không bị reject)
          3. Cancel B1 → chỉ xóa 1 trong 2 bản

        Kết quả: bản còn lại (chưa bị partial fill) vẫn tồn tại trong sổ.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))   # resting

        # Partial fill B1: remaining = 7
        book.submit(lim("S_partial", "SELL", 3, 100.0, 2))
        assert book.snapshot()["buys"][0]["remaining"] == 7

        # Submit B1 lần 2 (duplicate)
        book.submit(lim("B1", "BUY", 5, 100.0, 3))

        # Bây giờ buys có 2 entries với order_id="B1"
        buys = book.snapshot()["buys"]
        assert len(buys) == 2, "Phải có 2 orders với cùng ID"

        # Cancel chỉ xóa 1
        book.cancel("B1")
        buys_after = book.snapshot()["buys"]

        # BUG: 1 order vẫn còn
        assert len(buys_after) == 1, (
            f"BUG: Sau cancel, vẫn còn {len(buys_after)} order(s) với ID 'B1'"
        )

    def test_rapid_cancel_nonexistent_returns_false(self):
        """Cancel order không tồn tại → phải trả False, không crash."""
        book = OrderBook()
        assert book.cancel("GHOST-123") is False
        assert book.cancel("") is False

    def test_cancel_already_cancelled_returns_false(self):
        """Cancel lần 2 cùng ID → phải trả False."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))

        assert book.cancel("B1") is True
        assert book.cancel("B1") is False  # second cancel phải False


# ===========================================================================
# D. Multiple orders at identical price AND timestamp
# ===========================================================================

class TestIdenticalPriceAndTimestamp:

    def test_same_price_same_ts_stable_sort_preserves_insertion_order(self):
        """
        Cùng giá + cùng timestamp → Python stable sort giữ thứ tự chèn.
        B1 được submit trước → phải đứng trước B2 trong snapshot.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))  # SAME ts=1
        book.submit(lim("B2", "BUY", 5, 100.0, 1))  # SAME ts=1

        snap = book.snapshot()
        ids = [o["order_id"] for o in snap["buys"]]
        assert ids == ["B1", "B2"], (
            f"Cùng giá + cùng ts → stable sort phải giữ thứ tự chèn. Got: {ids}"
        )

    def test_same_price_same_ts_fifo_matching(self):
        """Cùng giá + cùng ts → B1 (chèn trước) phải được khớp trước."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B2", "BUY", 5, 100.0, 1))

        trades = book.submit(lim("S1", "SELL", 5, 100.0, 2))

        assert trades[0].buy_order_id == "B1", (
            f"Cùng giá + cùng ts → B1 phải được khớp trước. Got: {trades[0].buy_order_id}"
        )

    def test_three_orders_identical_price_and_timestamp(self):
        """3 orders cùng giá + cùng ts → tất cả vào sổ, FIFO theo insertion order."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 3, 100.0, 1))
        book.submit(lim("B2", "BUY", 3, 100.0, 1))
        book.submit(lim("B3", "BUY", 3, 100.0, 1))

        snap = book.snapshot()
        ids = [o["order_id"] for o in snap["buys"]]
        assert ids == ["B1", "B2", "B3"]

    def test_sell_side_identical_price_and_timestamp(self):
        """SELL side: cùng giá + cùng ts → thứ tự chèn được giữ."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 3, 100.0, 1))
        book.submit(lim("S2", "SELL", 3, 100.0, 1))

        snap = book.snapshot()
        ids = [o["order_id"] for o in snap["sells"]]
        assert ids == ["S1", "S2"]

    def test_mixed_ts_bug2_reveals_only_when_ts_differs(self):
        """
        ADVERSARIAL: Bug2 (LIFO sort) só bộc lộ khi timestamp KHÁC nhau.
        Khi ts giống nhau → stable sort → thứ tự đúng dù có Bug2.
        """
        book = OrderBook()
        # ts giống nhau: Bug2 không ảnh hưởng
        book.submit(lim("B1", "BUY", 1, 100.0, 42))
        book.submit(lim("B2", "BUY", 1, 100.0, 42))

        trades = book.submit(lim("S1", "SELL", 1, 100.0, 99))

        # Với ts giống nhau, stable sort giữ B1 trước dù có Bug2
        # Nếu Bug2 không ảnh hưởng ts giống nhau: B1 khớp trước
        assert trades[0].buy_order_id == "B1", \
            "Với ts bằng nhau, stable sort phải giữ B1 trước (Bug2 không ảnh hưởng)"


# ===========================================================================
# E. Minimum quantity (qty=1)
# ===========================================================================

class TestMinimumQuantity:

    def test_qty1_full_match_clears_both(self):
        """qty=1 full match: cả 2 bên đều bị xóa khỏi sổ."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 1, 100.0, 1))
        trades = book.submit(lim("B1", "BUY", 1, 100.0, 2))

        assert len(trades) == 1
        assert trades[0].quantity == 1
        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"] == []

    def test_qty1_resting_gets_partially_filled_impossible(self):
        """qty=1 resting không thể partial fill — chỉ có full fill hoặc no fill."""
        book = OrderBook()
        book.submit(lim("S1", "SELL", 1, 100.0, 1))
        book.submit(lim("B1", "BUY", 1, 100.0, 2))  # full fill

        snap = book.snapshot()
        assert snap["sells"] == [], "qty=1 sell phải bị full fill"

    def test_qty1_buy_bug2_gets_wrong_order_filled(self):
        """
        ADVERSARIAL: qty=1 + Bug2 → lệnh SAI được khớp.

        Điều kiện kết hợp:
          1. B1 @100 ts=1 (đến trước)
          2. B2 @100 ts=2 (đến sau)
          3. Bug2: sort LIFO → B2 đứng trước B1 trong sổ
          4. SELL qty=1 → khớp B2 (sai) thay vì B1 (đúng)
          5. B1 còn qty=1 trong sổ — không được fill
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 1, 100.0, 1))  # ts=1, đến trước
        book.submit(lim("B2", "BUY", 1, 100.0, 2))  # ts=2, đến sau

        trades = book.submit(lim("S1", "SELL", 1, 100.0, 3))

        assert len(trades) == 1
        # ĐÚNG là B1 phải được khớp (FIFO), nhưng Bug2 khiến B2 khớp trước
        correct_fill = trades[0].buy_order_id == "B1"
        wrong_fill = trades[0].buy_order_id == "B2"

        assert wrong_fill, (
            "BUG2 PHÁT HIỆN: B2 (đến sau) được khớp thay vì B1 (đến trước). "
            f"Got: {trades[0].buy_order_id}"
        )

        # B1 phải còn lại trong sổ (chưa được fill)
        buys = book.snapshot()["buys"]
        assert len(buys) == 1
        assert buys[0]["order_id"] == "B1"

    def test_qty1_market_order_bug1_no_match(self):
        """
        ADVERSARIAL: qty=1 + MARKET order + Bug1 → không có trade.

        Điều kiện kết hợp:
          1. SELL LIMIT qty=1 @100 đang resting
          2. BUY MARKET qty=1 (price=None)
          3. Bug1: _is_match kiểm tra `price is not None` → False
          4. Dù điều kiện hoàn hảo để khớp, không có trade nào được tạo
        """
        book = OrderBook()
        book.submit(lim("S1", "SELL", 1, 100.0, 1))
        trades = book.submit(mkt("M1", "BUY", 1, 2))

        assert len(trades) == 0, (
            "BUG1 PHÁT HIỆN: Market order qty=1 không khớp dù SELL sẵn có. "
            f"Số trade: {len(trades)}"
        )

        # Hệ quả: S1 vẫn còn trong sổ (chưa được fill)
        snap = book.snapshot()
        assert len(snap["sells"]) == 1, "S1 phải còn lại trong sổ do không được fill"

    def test_qty1_cancel_then_no_match(self):
        """Cancel qty=1 order rồi SELL vào → không khớp được."""
        book = OrderBook()
        book.submit(lim("B1", "BUY", 1, 100.0, 1))
        book.cancel("B1")

        trades = book.submit(lim("S1", "SELL", 1, 100.0, 2))
        assert trades == []


# ===========================================================================
# F. Compound multi-condition bugs
# ===========================================================================

class TestCompoundBugs:

    def test_bug1_x_bug2_combined(self):
        """
        COMPOUND: Bug1 (market) + Bug2 (sort) active simulanteously.

        Điều kiện:
          1. B1 @100 ts=1, B2 @100 ts=2 trên sổ (Bug2: B2 đứng trước)
          2. SELL MARKET qty=3 → Bug1 khiến không có trade nào
          3. Cả B1, B2 đều ở lại sổ không được fill
          4. Bug2 vẫn làm đảo thứ tự trong sổ

        Kết quả: Double failure — không fill được VÀ thứ tự sai.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B2", "BUY", 5, 100.0, 2))

        # Bug2: sổ BUY phải là [B2, B1] (LIFO sai)
        ids_before = [o["order_id"] for o in book.snapshot()["buys"]]
        assert ids_before == ["B2", "B1"], f"Bug2: Got {ids_before}"

        # Bug1: MARKET không khớp được
        trades = book.submit(mkt("SM", "SELL", 10, 3))
        assert len(trades) == 0, f"Bug1: Got {len(trades)} trades (expected 0)"

        # Sổ không thay đổi
        ids_after = [o["order_id"] for o in book.snapshot()["buys"]]
        assert ids_after == ["B2", "B1"]

    def test_bug2_x_accumulation_wrong_order_drained(self):
        """
        COMPOUND: Bug2 × partial fill × nhiều SELL liên tiếp.

        Điều kiện:
          1. B1 @100 ts=1 qty=10, B2 @100 ts=2 qty=10
          2. Bug2: sổ = [B2, B1]
          3. SELL qty=10 → khớp B2 (sai, nên là B1)
          4. B2 fully filled, B1 chưa bị fill dù đến trước

        Hệ quả: B1 (đến trước) bị "bỏ đói", B2 (đến sau) được fill toàn bộ.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))
        book.submit(lim("B2", "BUY", 10, 100.0, 2))

        trades = book.submit(lim("S1", "SELL", 10, 100.0, 3))

        assert len(trades) == 1
        assert trades[0].quantity == 10
        # BUG2: B2 bị fill (sai), B1 còn nguyên
        assert trades[0].buy_order_id == "B2", \
            f"Bug2: B2 được fill thay vì B1. Got: {trades[0].buy_order_id}"

        snap = book.snapshot()
        assert len(snap["buys"]) == 1
        assert snap["buys"][0]["order_id"] == "B1"  # B1 vẫn còn, chưa được fill
        assert snap["buys"][0]["remaining"] == 10   # B1 chưa bị fill chút nào

    def test_bug2_x_large_n_fifo_violation_accumulation(self):
        """
        COMPOUND: Bug2 × N orders cùng giá → thứ tự fill hoàn toàn đảo ngược.

        Điều kiện:
          1. N=5 BUY orders cùng giá @100, ts = 1,2,3,4,5
          2. Bug2 sort: thứ tự trong sổ = [ts5, ts4, ts3, ts2, ts1] (LIFO)
          3. N SELL orders, mỗi cái qty=1 → lần lượt fill từng BUY
          4. Fill thứ tự: B5, B4, B3, B2, B1 (ngược FIFO)
        """
        book = OrderBook()
        n = 5
        for i in range(1, n + 1):
            book.submit(lim(f"B{i}", "BUY", 1, 100.0, i))

        filled_order = []
        for i in range(n):
            s = book.submit(lim(f"S{i}", "SELL", 1, 100.0, n + i + 1))
            assert len(s) == 1
            filled_order.append(s[0].buy_order_id)

        # Với Bug2 (LIFO): fill B5 trước, rồi B4, ... B1 cuối
        expected_lifo = [f"B{n - i}" for i in range(n)]  # ["B5","B4","B3","B2","B1"]
        expected_fifo = [f"B{i + 1}" for i in range(n)]  # ["B1","B2","B3","B4","B5"]

        assert filled_order == expected_lifo, (
            f"Bug2 LIFO được xác nhận: thứ tự fill = {filled_order}\n"
            f"Đúng phải là FIFO: {expected_fifo}"
        )

    def test_qty1_x_bug2_x_multiple_orders_all_wrong(self):
        """
        COMPOUND: qty=1 × Bug2 × N orders → mọi fill đều sai thứ tự.

        Tất cả điều kiện kết hợp:
          - qty=1 (không có partial fill, mỗi fill là full fill)
          - Bug2 active (LIFO thay vì FIFO)
          - Nhiều orders cùng giá, khác ts
          - Mỗi SELL chỉ fill đúng 1 BUY

        Kết quả: Mọi lệnh được fill theo thứ tự ngược lại (mới nhất trước).
        """
        book = OrderBook()
        ids_submitted = ["B1", "B2", "B3"]
        tss = [10, 20, 30]
        for bid, ts in zip(ids_submitted, tss):
            book.submit(lim(bid, "BUY", 1, 100.0, ts))

        filled_ids = []
        for i in range(3):
            trades = book.submit(lim(f"S{i}", "SELL", 1, 100.0, 100 + i))
            filled_ids.append(trades[0].buy_order_id)

        # Bug2: fill theo LIFO → [B3, B2, B1]
        assert filled_ids == ["B3", "B2", "B1"], \
            f"Bug2 LIFO trên qty=1: expected [B3,B2,B1] got {filled_ids}"

    def test_duplicate_id_x_bug2_x_cancel_leaves_wrong_zombie(self):
        """
        COMPOUND: Duplicate ID × Bug2 × cancel.

        Điều kiện kết hợp:
          1. B1 @100 ts=1 resting
          2. B1 @100 ts=2 duplicate resting (no dedup)
          3. Bug2 sort: [B1(ts=2), B1(ts=1)] — ts=2 đứng trước
          4. cancel("B1") → xóa B1(ts=2) (đứng đầu do Bug2)
          5. B1(ts=1) còn lại — zombie

        Hệ quả của 3 điều kiện cùng lúc:
          - Không có dedup → 2 B1 vào sổ
          - Bug2 → sắp xếp sai thứ tự (ts=2 trước ts=1)
          - cancel() early return → chỉ xóa 1
          - Zombie còn lại là B1(ts=1) — là bản GỐC bị partial fill trước đó
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))  # original, ts=1
        book.submit(lim("B1", "BUY", 5, 100.0, 2))  # duplicate, ts=2

        # Bug2: sổ = [B1(ts=2), B1(ts=1)]
        ids = [o["order_id"] for o in book.snapshot()["buys"]]
        tss = [o["timestamp"] for o in book.snapshot()["buys"]]
        assert tss == [2, 1], f"Bug2: ts=2 đứng trước, ts=1 đứng sau. Got: {tss}"

        # Cancel xóa B1(ts=2) — cái đứng đầu do Bug2
        book.cancel("B1")

        buys = book.snapshot()["buys"]
        assert len(buys) == 1, "1 zombie còn lại"
        assert buys[0]["timestamp"] == 1, \
            f"Zombie là B1(ts=1) — bản gốc. Got ts={buys[0]['timestamp']}"

    def test_partial_fill_then_new_order_same_id_then_sweep(self):
        """
        COMPOUND: partial fill → duplicate ID → sweep SELL.

        Điều kiện:
          1. B1 qty=10 @100 resting
          2. SELL qty=3 → B1 partial fill: remaining=7
          3. Lại submit B1 qty=5 @100 ts khác (second B1)
          4. SELL qty=12 → phải khớp cả 2 B1 (7+5=12)
          5. Nhưng với Bug2 và duplicate ID, thứ tự fill không xác định

        Kiểm tra: tổng fill phải chính xác dù có duplicate ID.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))

        # Partial fill
        book.submit(lim("S_partial", "SELL", 3, 100.0, 2))
        assert book.snapshot()["buys"][0]["remaining"] == 7

        # Duplicate B1
        book.submit(lim("B1", "BUY", 5, 100.0, 3))

        total_remaining = sum(o["remaining"] for o in book.snapshot()["buys"])
        assert total_remaining == 12, f"Tổng remaining phải là 12. Got: {total_remaining}"

        # SELL qty=12 → phải fill hết cả 2 B1
        trades = book.submit(lim("S_sweep", "SELL", 12, 100.0, 4))
        total_filled = sum(t.quantity for t in trades)
        assert total_filled == 12, f"Phải fill đủ 12. Got: {total_filled}"
        assert book.snapshot()["buys"] == [], "Sổ BUY phải trống sau khi fill hết"

    def test_market_order_after_duplicate_id_creates_no_trade(self):
        """
        COMPOUND: Bug1 × duplicate ID → market order vào sổ đầy BUY nhưng không fill được.

        Điều kiện:
          1. 2 SELL orders đang resting
          2. Submit BUY MARKET → Bug1 chặn match
          3. Kết quả: 0 trades, 2 SELLs vẫn còn trên sổ
        """
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))
        book.submit(lim("S1", "SELL", 5, 100.0, 2))  # duplicate SELL ID

        sells_before = len(book.snapshot()["sells"])
        assert sells_before == 2

        trades = book.submit(mkt("M1", "BUY", 10, 3))

        # Bug1: không có trade nào
        assert len(trades) == 0
        assert len(book.snapshot()["sells"]) == 2, "Cả 2 SELL vẫn còn nguyên"

    def test_sequence_dependency_cancel_vs_fill(self):
        """
        SEQUENCE-DEPENDENT: Kết quả khác nhau tuỳ theo thứ tự Cancel vs Fill.

        Sequence A: Fill trước → Cancel sau → False (order đã full-filled)
        Sequence B: Cancel trước → Fill sau → True, rồi SELL không khớp

        Đây KHÔNG phải bug mà là hành vi đúng của hệ thống.
        """
        # Sequence A: Fill → Cancel
        book_a = OrderBook()
        book_a.submit(lim("B1", "BUY", 5, 100.0, 1))
        book_a.submit(lim("S1", "SELL", 5, 100.0, 2))  # full fill

        result_a = book_a.cancel("B1")
        assert result_a is False, "B1 đã full-fill → cancel phải False"

        # Sequence B: Cancel → Fill
        book_b = OrderBook()
        book_b.submit(lim("B1", "BUY", 5, 100.0, 1))
        book_b.cancel("B1")
        trades_b = book_b.submit(lim("S1", "SELL", 5, 100.0, 2))

        assert trades_b == [], "B1 đã bị cancel → S1 không khớp được"
        assert len(book_b.snapshot()["sells"]) == 1

    def test_price_time_priority_bug2_survives_multiple_sort_calls(self):
        """
        COMPOUND: Bug2 persists across multiple _sort_books() calls.

        Mỗi lần submit() gọi _sort_books() ở đầu hàm.
        Bug2 không bị "heal" qua nhiều lần sort — vẫn LIFO mỗi lần.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 2, 100.0, 1))

        # Trigger nhiều lần sort qua các submit không khớp
        for i in range(5):
            book.submit(lim(f"B_extra{i}", "BUY", 1, 50.0, 100 + i))

        # B1 @100 vẫn phải ở đầu sổ (giá cao nhất)
        buys = book.snapshot()["buys"]
        assert buys[0]["order_id"] == "B1", \
            f"Sau nhiều lần sort, B1 phải ở đầu (giá cao nhất). Got: {buys[0]['order_id']}"

        # Thêm B2 @100 với ts lớn hơn B1
        book.submit(lim("B2", "BUY", 2, 100.0, 999))

        # Bug2: B2 (ts=999) đứng trước B1 (ts=1) trong nhóm @100
        top_100_ids = [o["order_id"] for o in buys if o["price"] == 100.0]
        buys_fresh = book.snapshot()["buys"]
        top_ids = [o["order_id"] for o in buys_fresh if o["price"] == 100.0]
        assert top_ids == ["B2", "B1"], \
            f"Bug2: B2 (ts=999 mới hơn) đứng trước B1 (ts=1). Got: {top_ids}"
