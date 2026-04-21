# Test Results

Thực thi: `pytest tests/test_matching_engine.py -v`
Thời gian chạy: 2026-04-21
Trạng thái code: **chưa vá bug** (3 bug đã inject)

---

## Tóm tắt

| Tổng | PASSED | FAILED |
|------|--------|--------|
| 35   | 28     | 7      |

---

## Kết quả từng test

| # | Test | Kết quả |
|---|------|---------|
| 1  | `TestFullMatch::test_buy_matches_sell_same_price_same_qty` | ✅ PASSED |
| 2  | `TestFullMatch::test_buy_higher_price_matches_sell` | ✅ PASSED |
| 3  | `TestFullMatch::test_no_remaining_orders_after_full_match` | ✅ PASSED |
| 4  | `TestFullMatch::test_aggressor_is_incoming_order` | ✅ PASSED |
| 5  | `TestFullMatch::test_sell_aggressor_matches_resting_buy` | ✅ PASSED |
| 6  | `TestPartialFill::test_buy_larger_than_sell_leaves_buy_remainder` | ✅ PASSED |
| 7  | `TestPartialFill::test_sell_larger_than_buy_leaves_sell_remainder` | ✅ PASSED |
| 8  | `TestPartialFill::test_partial_fill_then_another_match` | ✅ PASSED |
| 9  | `TestPartialFill::test_one_incoming_matches_multiple_resting` | ✅ PASSED |
| 10 | `TestNoMatch::test_buy_below_sell_price_no_trade` | ✅ PASSED |
| 11 | `TestNoMatch::test_buy_exactly_below_sell_boundary` | ✅ PASSED |
| 12 | `TestNoMatch::test_market_order_no_resting_no_trade` | ✅ PASSED |
| 13 | `TestNoMatch::test_no_self_match` | ✅ PASSED |
| 14 | `TestPriceTimePriority::test_higher_buy_price_matched_first` | ✅ PASSED |
| 15 | `TestPriceTimePriority::test_same_price_earlier_timestamp_matched_first` | ❌ FAILED |
| 16 | `TestPriceTimePriority::test_same_price_buy_book_order_b1_before_b2` | ❌ FAILED |
| 17 | `TestPriceTimePriority::test_lower_sell_price_matched_first` | ✅ PASSED |
| 18 | `TestPriceTimePriority::test_same_price_sell_earlier_timestamp_first` | ✅ PASSED |
| 19 | `TestPriceTimePriority::test_price_priority_overrides_time` | ✅ PASSED |
| 20 | `TestMarketOrder::test_buy_market_matches_resting_sell` | ❌ FAILED |
| 21 | `TestMarketOrder::test_sell_market_matches_resting_buy` | ❌ FAILED |
| 22 | `TestMarketOrder::test_market_order_partial_fill` | ❌ FAILED |
| 23 | `TestMarketOrder::test_market_order_sweeps_multiple_levels` | ❌ FAILED |
| 24 | `TestMarketOrder::test_market_order_not_added_to_book_when_no_match` | ✅ PASSED |
| 25 | `TestOrderLifecycle::test_cancel_removes_buy_from_book` | ✅ PASSED |
| 26 | `TestOrderLifecycle::test_cancel_removes_sell_from_book` | ✅ PASSED |
| 27 | `TestOrderLifecycle::test_cancel_nonexistent_order_returns_false` | ✅ PASSED |
| 28 | `TestOrderLifecycle::test_fully_filled_order_not_in_book` | ✅ PASSED |
| 29 | `TestOrderLifecycle::test_partially_filled_order_remains_with_correct_remaining` | ✅ PASSED |
| 30 | `TestOrderLifecycle::test_cancel_partially_filled_order` | ✅ PASSED |
| 31 | `TestOrderLifecycle::test_active_orders_excludes_filled` | ✅ PASSED |
| 32 | `TestViaService::test_service_full_match` | ✅ PASSED |
| 33 | `TestViaService::test_service_market_order` | ❌ FAILED |
| 34 | `TestViaService::test_service_cancel` | ✅ PASSED |
| 35 | `TestViaService::test_service_no_match` | ✅ PASSED |

---

## Phân tích các test FAILED

### Nhóm 1 — Bug 2: Sai sort buy book (2 test fail)

**Tests:** #15, #16

**Nguyên nhân:** `order_book.py:57` dùng `reverse=True` đảo chiều cả tuple `(price, timestamp)`, khiến timestamp cũng sắp xếp giảm dần (mới nhất trước) thay vì tăng dần (FIFO).

```
Test #16: assert ['B2', 'B1'] == ['B1', 'B2']
          B2 (ts=2) đứng trước B1 (ts=1) — SAI, phải là B1 trước
```

**Cách sửa:** Dòng 57 → `self.buys.sort(key=lambda item: (-(item.price or 0.0), item.timestamp))`

---

### Nhóm 2 — Bug 1: Market order không khớp được (5 test fail)

**Tests:** #20, #21, #22, #23, #33

**Nguyên nhân:** `order_book.py:62` kiểm tra `incoming.price is not None`, nhưng market order luôn có `price=None` → điều kiện luôn `False` → không bao giờ match.

```
Test #20: assert 0 == 1   (expected 1 trade, got 0)
Test #21: assert 0 == 1   (expected 1 trade, got 0)
Test #22: assert 0 == 1   (expected 1 trade, got 0)
Test #23: assert 0 == 3   (expected 3 trades, got 0)
Test #33: assert 0 == 1   (expected 1 trade, got 0)
```

**Cách sửa:** Dòng 62 → `return True`

---

### Bug 3 — Không có test fail (vì không phải logic matching)

Bug 3 (`web.py:110`) là lỗi WebSocket broadcast — chỉ broadcast khi cancel BUY order, không broadcast khi cancel SELL order. Bug này không thể phát hiện qua unit test đồng bộ của `OrderBook`/`Service` (cần integration test với WebSocket client). Cần viết async test với `aiohttp.TestClient` để phát hiện.

---

## Ghi chú

- Bug 3 (`web.py`) không ảnh hưởng logic matching nên không làm fail bất kỳ test nào trong bộ test này.
- Sau khi vá cả 3 bug, toàn bộ 35 test nên trả về **PASSED**.
