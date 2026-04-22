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

    def test_duplicate_order_id_rejected_by_dedup(self):
        """
        FIX BUG-03: Submit cùng order_id 2 lần → lần 2 bị reject (dedup).
        cancel() xóa đúng 1 entry → sổ trống hoàn toàn.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B1", "BUY", 5, 100.0, 2))  # duplicate — bị reject bởi _rest()

        buys_before = book.snapshot()["buys"]
        assert len(buys_before) == 1, "Dedup: chỉ 1 entry được chấp nhận"

        cancelled = book.cancel("B1")
        assert cancelled is True

        buys_after = book.snapshot()["buys"]
        assert buys_after == [], "Sau cancel, sổ phải trống — không có zombie"

    def test_duplicate_id_after_partial_fill_then_cancel(self):
        """
        FIX BUG-03 COMPOUND: partial fill → submit duplicate ID (bị reject) → cancel.

        Điều kiện kết hợp:
          1. B1 resting, bị partial fill (remaining=7)
          2. Submit B1 lần 2 → bị reject vì B1 đã tồn tại trong sổ
          3. Cancel B1 → xóa đúng bản gốc, sổ trống

        Kết quả: không có zombie.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))   # resting

        # Partial fill B1: remaining = 7
        book.submit(lim("S_partial", "SELL", 3, 100.0, 2))
        assert book.snapshot()["buys"][0]["remaining"] == 7

        # Submit B1 lần 2 — bị dedup reject vì B1 vẫn đang resting
        book.submit(lim("B1", "BUY", 5, 100.0, 3))
        buys = book.snapshot()["buys"]
        assert len(buys) == 1, "Dedup: duplicate bị reject, chỉ có 1 entry"
        assert buys[0]["remaining"] == 7, "Entry duy nhất là bản gốc đã partial fill"

        book.cancel("B1")
        buys_after = book.snapshot()["buys"]
        assert buys_after == [], "Sau cancel, sổ phải trống — không có zombie"

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

    def test_qty1_buy_fifo_correct_after_fix(self):
        """
        FIX BUG-02: qty=1 — lệnh đến TRƯỚC (B1) phải được khớp trước (FIFO).

        Sau fix _sort_books, buys sort bằng (−price, timestamp) ascending:
          B1(ts=1) đứng trước B2(ts=2) cùng giá → B1 khớp trước.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 1, 100.0, 1))  # ts=1, đến trước
        book.submit(lim("B2", "BUY", 1, 100.0, 2))  # ts=2, đến sau

        trades = book.submit(lim("S1", "SELL", 1, 100.0, 3))

        assert len(trades) == 1
        assert trades[0].buy_order_id == "B1", (
            f"FIFO: B1 (đến trước) phải được khớp. Got: {trades[0].buy_order_id}"
        )

        # B2 còn lại trong sổ (chưa được fill)
        buys = book.snapshot()["buys"]
        assert len(buys) == 1
        assert buys[0]["order_id"] == "B2"

    def test_qty1_market_order_matches_after_fix(self):
        """
        FIX BUG-01: qty=1 + MARKET order → phải tạo 1 trade.

        Sau fix _is_match: MARKET order trả True vô điều kiện → khớp ngay.
        """
        book = OrderBook()
        book.submit(lim("S1", "SELL", 1, 100.0, 1))
        trades = book.submit(mkt("M1", "BUY", 1, 2))

        assert len(trades) == 1, (
            f"Market order phải tạo 1 trade. Got: {len(trades)}"
        )
        assert trades[0].quantity == 1
        assert trades[0].price == 100.0

        snap = book.snapshot()
        assert snap["sells"] == [], "S1 đã được fill hoàn toàn"

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

    def test_fix01_fix02_market_and_fifo_correct(self):
        """
        REGRESSION FIX BUG-01 + BUG-02: market works + buy side FIFO.

        Sau fix:
          1. B1 @100 ts=1 đứng trước B2 @100 ts=2 trong sổ (FIFO)
          2. SELL MARKET qty=10 → khớp B1 trước, rồi B2
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B2", "BUY", 5, 100.0, 2))

        # FIFO: B1 phải đứng trước B2
        ids_before = [o["order_id"] for o in book.snapshot()["buys"]]
        assert ids_before == ["B1", "B2"], f"FIFO: Got {ids_before}"

        # Market order phải khớp được
        trades = book.submit(mkt("SM", "SELL", 10, 3))
        assert len(trades) == 2, f"Market phải tạo 2 trades. Got: {len(trades)}"
        assert trades[0].buy_order_id == "B1", "B1 phải được fill trước (FIFO)"
        assert trades[1].buy_order_id == "B2"
        assert book.snapshot()["buys"] == []

    def test_fix02_fifo_order_filled_correctly(self):
        """
        REGRESSION FIX BUG-02: B1 (ts=1, đến trước) phải được fill trước B2 (ts=2).
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))
        book.submit(lim("B2", "BUY", 10, 100.0, 2))

        trades = book.submit(lim("S1", "SELL", 10, 100.0, 3))

        assert len(trades) == 1
        assert trades[0].quantity == 10
        assert trades[0].buy_order_id == "B1", \
            f"FIFO: B1 phải được fill trước. Got: {trades[0].buy_order_id}"

        snap = book.snapshot()
        assert len(snap["buys"]) == 1
        assert snap["buys"][0]["order_id"] == "B2"  # B2 còn lại, chưa được fill
        assert snap["buys"][0]["remaining"] == 10

    def test_fix02_large_n_fifo_order(self):
        """
        REGRESSION FIX BUG-02: N orders cùng giá → fill theo đúng thứ tự FIFO.
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

        expected_fifo = [f"B{i + 1}" for i in range(n)]  # ["B1","B2","B3","B4","B5"]
        assert filled_order == expected_fifo, (
            f"FIFO phải fill B1→B5. Got: {filled_order}"
        )

    def test_fix02_qty1_multiple_orders_fifo(self):
        """
        REGRESSION FIX BUG-02: qty=1 × N orders → fill đúng thứ tự FIFO.
        """
        book = OrderBook()
        for bid, ts in zip(["B1", "B2", "B3"], [10, 20, 30]):
            book.submit(lim(bid, "BUY", 1, 100.0, ts))

        filled_ids = []
        for i in range(3):
            trades = book.submit(lim(f"S{i}", "SELL", 1, 100.0, 100 + i))
            filled_ids.append(trades[0].buy_order_id)

        assert filled_ids == ["B1", "B2", "B3"], \
            f"FIFO: expected [B1,B2,B3] got {filled_ids}"

    def test_fix03_duplicate_id_and_cancel_no_zombie(self):
        """
        REGRESSION FIX BUG-03: duplicate ID bị reject → cancel hoàn toàn → không zombie.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 5, 100.0, 1))
        book.submit(lim("B1", "BUY", 5, 100.0, 2))  # bị dedup reject

        buys = book.snapshot()["buys"]
        assert len(buys) == 1, "Dedup: chỉ 1 entry"
        assert buys[0]["timestamp"] == 1, "Entry là bản gốc ts=1"

        book.cancel("B1")
        assert book.snapshot()["buys"] == [], "Không có zombie sau cancel"

    def test_fix03_partial_fill_dedup_sweep(self):
        """
        REGRESSION FIX BUG-03: partial fill → duplicate reject → sweep chỉ fill remaining.

        Sau fix dedup: submit B1 lần 2 bị reject vì B1 vẫn đang resting.
        Sweep chỉ fill 7 (remaining của B1 gốc), không phải 12.
        """
        book = OrderBook()
        book.submit(lim("B1", "BUY", 10, 100.0, 1))

        book.submit(lim("S_partial", "SELL", 3, 100.0, 2))
        assert book.snapshot()["buys"][0]["remaining"] == 7

        # Duplicate bị reject
        book.submit(lim("B1", "BUY", 5, 100.0, 3))
        assert len(book.snapshot()["buys"]) == 1, "Dedup: vẫn chỉ 1 entry"
        assert book.snapshot()["buys"][0]["remaining"] == 7

        trades = book.submit(lim("S_sweep", "SELL", 7, 100.0, 4))
        assert sum(t.quantity for t in trades) == 7
        assert book.snapshot()["buys"] == []

    def test_fix01_fix03_market_with_dedup_sell(self):
        """
        REGRESSION FIX BUG-01 + BUG-03: market order khớp với sells đã dedup.

        Sau fix: duplicate SELL bị reject → 1 SELL trong sổ.
        Market BUY khớp ngay với SELL đó.
        """
        book = OrderBook()
        book.submit(lim("S1", "SELL", 5, 100.0, 1))
        book.submit(lim("S1", "SELL", 5, 100.0, 2))  # bị dedup reject

        sells_before = len(book.snapshot()["sells"])
        assert sells_before == 1, "Dedup: chỉ 1 SELL entry"

        trades = book.submit(mkt("M1", "BUY", 5, 3))
        assert len(trades) == 1, f"Market phải khớp. Got: {len(trades)} trades"
        assert trades[0].quantity == 5
        assert book.snapshot()["sells"] == []

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

    def test_fix02_fifo_stable_across_multiple_sort_calls(self):
        """
        REGRESSION FIX BUG-02: FIFO ổn định dù _sort_books() được gọi nhiều lần.

        Sau fix: B1 (ts=1) phải đứng trước B2 (ts=999) cùng giá @100,
        dù _sort_books() được gọi nhiều lần qua các submit trung gian.
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

        # FIFO: B1 (ts=1, đến trước) phải đứng trước B2 (ts=999) trong nhóm @100
        buys_fresh = book.snapshot()["buys"]
        top_ids = [o["order_id"] for o in buys_fresh if o["price"] == 100.0]
        assert top_ids == ["B1", "B2"], \
            f"FIFO: B1 (ts=1) phải đứng trước B2 (ts=999). Got: {top_ids}"
