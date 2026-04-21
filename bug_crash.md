# Bug Crash Map — Adversarial Analysis

Tài liệu này mô tả các bug chỉ xuất hiện khi **nhiều điều kiện đồng thời** xảy ra.
Mỗi bug được kèm theo sequence tái hiện chính xác, trace code, và giải thích tại sao
các điều kiện phải _cùng lúc_ mới trigger được.

---

## BUG-ADV-1 · Zombie Order — Duplicate order_id không bị từ chối

### Mức độ: CRITICAL

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu điều kiện này |
|---|-----------|--------------------------|
| C1 | Submit 2+ orders với **cùng order_id** | Chỉ 1 order → cancel bình thường, không có zombie |
| C2 | `_rest()` không kiểm tra uniqueness | Có kiểm tra → order thứ 2 bị reject |
| C3 | `cancel()` dùng **early return** (`return True`) sau khi xóa đơn đầu | Duyệt toàn bộ → xóa hết mọi bản cùng ID |

Cả 3 điều kiện **phải đồng thời** mới tạo zombie.

### Sequence tái hiện chính xác

```
1. book.submit(Order("B1", BUY, qty=5, @100, ts=1))
   → _rest() appends vào self.buys
   → buys = [B1(ts=1, remaining=5)]

2. book.submit(Order("B1", BUY, qty=5, @100, ts=2))   ← SAME order_id!
   → submit() không kiểm tra order_id đã tồn tại
   → _rest() lại appends
   → buys = [B1(ts=2, rem=5), B1(ts=1, rem=5)]   ← 2 entries cùng ID

3. book.cancel("B1")
   → duyệt buys, tìm index=0 (B1 ts=2)
   → book.pop(0)  ← xóa B1(ts=2)
   → return True  ← EARLY RETURN! không xét index=1

   buys = [B1(ts=1, rem=5)]   ← ZOMBIE
```

### Trace code

```python
# order_book.py:41-46  — cancel() trả về sau lần khớp đầu tiên
def cancel(self, order_id: str) -> bool:
    for book in (self.buys, self.sells):
        for index, order in enumerate(book):
            if order.order_id == order_id and order.remaining > 0:
                book.pop(index)
                return True   # ← early return; vòng lặp dừng ngay
    return False

# order_book.py:49-54  — _rest() không kiểm tra uniqueness
def _rest(self, order: Order) -> None:
    if order.side == Side.BUY:
        self.buys.append(order)   # ← không check order_id đã tồn tại
    else:
        self.sells.append(order)
    self._sort_books()
```

### Hệ quả

- Sau `cancel("B1")` trả `True`, user tin rằng order đã bị hủy
- Thực tế còn 1 bản "B1" trong sổ → sẽ tiếp tục được khớp với các lệnh đến sau
- Không thể phát hiện qua API thông thường vì `cancel` báo thành công
- Zombie có thể bị fill bởi SELL tương lai → tạo trade không mong muốn

### Test xác nhận

```
TestCancelThenReplace::test_duplicate_order_id_creates_zombie  ✅ PASSED
TestCancelThenReplace::test_duplicate_id_after_partial_fill_then_cancel  ✅ PASSED
```

---

## BUG-ADV-2 · FIFO Hoàn Toàn Đảo Ngược — Bug2 × N Orders Cùng Giá

### Mức độ: HIGH

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu điều kiện này |
|---|-----------|--------------------------|
| C1 | Bug2 active: `reverse=True` trên tuple `(price, timestamp)` | Không có Bug2 → FIFO đúng |
| C2 | **Ít nhất 2** BUY orders **cùng giá** | Khác giá → price dominates sort, timestamp không quan trọng |
| C3 | Các orders có **timestamp khác nhau** | Cùng timestamp → stable sort → thứ tự đúng dù có Bug2 |
| C4 | SELL không đủ fill toàn bộ sổ | Fill hết → thứ tự fill không quan sát được |

### Sequence tái hiện chính xác (N=5)

```
Submit B1 @100 ts=1, qty=1
Submit B2 @100 ts=2, qty=1
Submit B3 @100 ts=3, qty=1
Submit B4 @100 ts=4, qty=1
Submit B5 @100 ts=5, qty=1

_sort_books() với reverse=True:
  key = (price, timestamp)
  reverse → sort giảm dần theo cả hai
  → sắp xếp: ts=5, ts=4, ts=3, ts=2, ts=1
  → buys = [B5, B4, B3, B2, B1]

SELL1 @100 qty=1 → best = buys[0] = B5 → fill B5  (sai! phải B1)
SELL2 @100 qty=1 → best = buys[0] = B4 → fill B4  (sai! phải B2)
SELL3 @100 qty=1 → best = buys[0] = B3 → fill B3  (ngẫu nhiên đúng)
SELL4 @100 qty=1 → best = buys[0] = B2 → fill B2  (sai! phải B4)
SELL5 @100 qty=1 → best = buys[0] = B1 → fill B1  (sai! phải B5)

Kết quả thực tế:  [B5, B4, B3, B2, B1]  ← LIFO
Kết quả đúng:     [B1, B2, B3, B4, B5]  ← FIFO
```

### Trace code

```python
# order_book.py:57
self.buys.sort(
    key=lambda item: (item.price or 0.0, item.timestamp),
    reverse=True   # ← đảo ngược CẢ price lẫn timestamp
)
# Kết quả với B1(100,ts=1) và B2(100,ts=2):
#   key B1 = (100.0, 1) → reversed → B2 trước vì ts=2 > ts=1 khi DESC
#   key B2 = (100.0, 2) → reversed → B2 đứng trước B1 ← SAI
```

### Đặc điểm ẩn: Bug này "ngủ yên" với timestamp bằng nhau

```python
# Nếu tất cả orders có ts giống nhau (ts=42):
B1(100.0, 42) vs B2(100.0, 42)
# reverse=True → sort key giống nhau → Python stable sort giữ insertion order
# → B1 vẫn trước B2 → ĐÚNG
# Bug2 chỉ lộ khi timestamp KHÁC nhau
```

### Test xác nhận

```
TestCompoundBugs::test_bug2_x_large_n_fifo_violation_accumulation  ✅ PASSED
TestCompoundBugs::test_qty1_x_bug2_x_multiple_orders_all_wrong     ✅ PASSED
TestIdenticalPriceAndTimestamp::test_mixed_ts_bug2_reveals_only_when_ts_differs  ✅ PASSED
```

---

## BUG-ADV-3 · Bug1 × Bug2 Double Failure — Market Order vào sổ đầy nhưng không fill được

### Mức độ: CRITICAL (kết hợp 2 bugs)

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu |
|---|-----------|-----------|
| C1 | Bug1 active: `return incoming.price is not None` (line 62) | Market order sẽ match đúng |
| C2 | Bug2 active: LIFO sort (line 57) | Thứ tự sổ đúng dù market vẫn không fill |
| C3 | Submit **MARKET order** (price=None) | LIMIT order fill bình thường |
| C4 | **Sổ đối diện không trống** | Không có resting order → không thể fill dù bug không tồn tại |

### Sequence tái hiện

```
Submit B1 @100 ts=1
Submit B2 @100 ts=2

Bug2: buys = [B2, B1]   ← thứ tự sai

Submit SELL MARKET qty=10, ts=3:
  _is_match(MARKET, B2):
    if incoming.order_type == MARKET:
        return incoming.price is not None   ← price=None → False!
  → _is_match = False → break
  → 0 trades

Final state:
  buys = [B2(rem=5), B1(rem=5)]  ← không có gì thay đổi
  Bug2: thứ tự vẫn sai
  Bug1: SELL MARKET hoàn toàn bị bỏ qua
```

### Hai bugs cùng lúc gây hệ quả phức hợp

```
Không có Bug1, chỉ Bug2:
  → Market fill được nhưng fill sai thứ tự (B2 trước B1)

Không có Bug2, chỉ Bug1:
  → Market không fill được; sổ BUY có thứ tự ĐÚNG nhưng vô dụng

Cả Bug1 + Bug2:
  → Market không fill được VÀ sổ BUY có thứ tự SAI
  → Hai lỗi độc lập nhưng cùng trigger = hệ thống ở trạng thái tồi nhất
```

### Test xác nhận

```
TestCompoundBugs::test_bug1_x_bug2_combined  ✅ PASSED
TestMinimumQuantity::test_qty1_market_order_bug1_no_match  ✅ PASSED
```

---

## BUG-ADV-4 · Partial Fill Accumulation Inversion — Bug2 × Cùng Giá × Nhiều SELL Liên Tiếp

### Mức độ: HIGH

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu |
|---|-----------|-----------|
| C1 | Bug2 active | FIFO đúng, order cũ được fill trước |
| C2 | Ít nhất 2 BUY cùng giá, timestamp khác nhau | Thứ tự sort đúng nếu giá hoặc ts khác |
| C3 | **Nhiều SELL liên tiếp** (không phải 1 SELL lớn sweep hết) | 1 SELL lớn fill xong B2 rồi tiếp tục B1, kết quả cuối sổ giống nhau |
| C4 | Mỗi SELL chỉ fill một phần resting book | Toàn bộ book bị sweep → thứ tự không quan sát được |

### Sequence tái hiện

```
Submit B1 @100 ts=1 qty=10  (đến TRƯỚC, nên được fill TRƯỚC)
Submit B2 @100 ts=2 qty=10  (đến SAU)

Bug2 sort: buys = [B2(ts=2), B1(ts=1)]

Submit SELL_1 @100 qty=10:
  → best = B2 (đứng đầu do Bug2)
  → fill 10 đơn vị từ B2
  → B2.remaining = 0 → pop(0)
  → buys = [B1(ts=1, rem=10)]

Submit SELL_2 @100 qty=10:
  → best = B1 (giờ đứng đầu vì B2 đã bị xóa)
  → fill 10 đơn vị từ B1
  → B1.remaining = 0 → pop(0)

THỰC TẾ:    B2 fill xong trước, rồi B1
ĐÚNG:       B1 fill xong trước, rồi B2

Hệ quả:
  - Giữa SELL_1 và SELL_2: sổ cho thấy [B1(rem=10)] — nhìn bình thường
  - Nhưng B1 đáng lẽ phải bị fill rồi, còn B2 mới nên còn lại
  - Hai người đặt lệnh nhận fill SAI: B2 user đã fill, B1 user chưa
```

### Tại sao cần nhiều SELL (C3) thay vì 1 SELL lớn

```
Nếu dùng 1 SELL qty=20:
  → fill B2(rem=10) → pop B2 → tiếp tục → fill B1(rem=10) → pop B1
  → Cả hai đều fill, tổng = 20, sổ trống
  → Không quan sát được thứ tự fill → bug ẩn

Nếu dùng 2 SELL qty=10:
  → SELL_1 fill B2 thôi (bug lộ: B2 fill trước)
  → SELL_2 fill B1 (bug lộ: B1 fill sau)
  → Hai user rõ ràng nhận fill sai thứ tự
```

### Test xác nhận

```
TestCompoundBugs::test_bug2_x_accumulation_wrong_order_drained  ✅ PASSED
```

---

## BUG-ADV-5 · Zombie × Bug2 × Cancel — Zombie Sai Version Còn Lại

### Mức độ: HIGH (3 điều kiện)

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu |
|---|-----------|-----------|
| C1 | Duplicate order_id (BUG-ADV-1) | Chỉ 1 order → cancel bình thường |
| C2 | Bug2: LIFO sort theo timestamp | FIFO sort → cancel xóa bản cũ nhất trước |
| C3 | Hai bản có timestamp **khác nhau** | Cùng ts → stable sort → thứ tự không bị Bug2 ảnh hưởng |

### Sequence tái hiện

```
Submit B1 @100 ts=1 (original)   → buys = [B1(ts=1)]
Submit B1 @100 ts=2 (duplicate)  → buys = [B1(ts=2), B1(ts=1)]
                                           ↑ Bug2: ts=2 đứng trước ts=1

cancel("B1"):
  → index 0 = B1(ts=2) → pop(0) → return True

buys = [B1(ts=1)]   ← ZOMBIE là bản gốc ts=1

Nếu không có Bug2 (thứ tự đúng: ts=1 trước ts=2):
  buys = [B1(ts=1), B1(ts=2)]
  cancel → pop index 0 = B1(ts=1)
  zombie = B1(ts=2)   ← zombie là bản DUPLICATE (ít nguy hại hơn)
```

### Tại sao sự kết hợp này nguy hiểm hơn riêng lẻ

- **Chỉ BUG-ADV-1**: zombie là bản DUPLICATE (ts=2) → bản mới, có thể là "replace order"
- **BUG-ADV-1 + Bug2**: zombie là bản ORIGINAL (ts=1) → bản gốc với lịch sử partial fill
- Nếu bản ts=1 đã bị partial fill (remaining < qty), zombie mang theo trạng thái cũ
- User nghĩ đã cancel toàn bộ, thực tế order gốc với remaining=7 vẫn đang hoạt động

### Test xác nhận

```
TestCompoundBugs::test_duplicate_id_x_bug2_x_cancel_leaves_wrong_zombie  ✅ PASSED
```

---

## BUG-ADV-6 · Floating Point Price Equality — Ngầm Không Khớp Khi Nên Khớp

### Mức độ: LOW (không phải bug trong code, nhưng là trap cho người dùng)

### Điều kiện kết hợp bắt buộc

| # | Điều kiện | Nếu thiếu |
|---|-----------|-----------|
| C1 | Giá được tính bằng **phép cộng float** (`0.1 + 0.2`) | Giá nhập trực tiếp → không có sai số |
| C2 | Giá resting được nhập là literal `0.3` | Cùng biểu thức → cùng sai số → vẫn match |
| C3 | Kỳ vọng hai giá bằng nhau nhưng thực tế khác nhau | Hiểu rõ floating point → dùng Decimal |

### Sequence tái hiện

```python
incoming_price = 0.1 + 0.2   # = 0.30000000000000004 (IEEE 754)
resting_price  = 0.3          # = 0.2999999999999999...

incoming_price > resting_price   # True! (do floating point)
# → BUY @(0.1+0.2) match được SELL @0.3  ← UNEXPECTED khớp

incoming_price = 0.3          # = 0.2999999999999999...
resting_price  = 0.1 + 0.2   # = 0.30000000000000004

incoming_price < resting_price   # True! BUY @0.3 KHÔNG match SELL @(0.1+0.2)
# → Lệnh trông giống nhau nhưng kết quả khác tùy cách nhập giá
```

### Lưu ý

Code matching engine không có lỗi — đây là hành vi đúng của IEEE 754 float.
Nhưng nếu giá được nhập qua API dạng string rồi `float()`, sai số tích lũy có thể
gây khớp/không khớp ngoài ý muốn. Cần dùng `decimal.Decimal` cho ứng dụng tài chính thực tế.

### Test xác nhận

```
TestPriceEqualityBoundary::test_many_decimal_precision_match  ✅ PASSED
```

---

## BUG-ADV-7 · Sequence-Dependent State — Cancel × Fill Thứ Tự Khác Nhau

### Mức độ: INFO (behavior đúng nhưng cần awareness)

### Điều kiện kết hợp bắt buộc

Không phải bug — đây là hành vi có chủ ý. Tuy nhiên, trong môi trường phân tán
hoặc khi có race condition giữa 2 client, thứ tự operation quyết định outcome.

### Sequence A vs B

```
Sequence A: Fill → Cancel
  Submit B1 qty=5 @100
  Submit SELL qty=5 @100  → B1 fully filled, removed from book
  cancel("B1")             → False (not in book)
  Final: sổ trống, 1 trade

Sequence B: Cancel → Fill
  Submit B1 qty=5 @100
  cancel("B1")             → True, B1 removed
  Submit SELL qty=5 @100  → no match, SELL rests
  Final: sổ có 1 SELL, 0 trades
```

### Tại sao quan trọng

Trong hệ thống đơn luồng (current implementation), thứ tự được đảm bảo.
Nếu sau này thêm concurrency (async handlers trong web.py đã dùng asyncio.Lock),
lock phải đảm bảo cancel và submit không xen kẽ nhau.
`web.py` đã có `async with hub.lock:` — đủ cho single-process async.

### Test xác nhận

```
TestCompoundBugs::test_sequence_dependency_cancel_vs_fill  ✅ PASSED
```

---

## Tổng kết Adversarial Bugs

| Bug | Điều kiện tối thiểu | Nguy hiểm khi thiếu 1 điều kiện? |
|-----|--------------------|------------------------------------|
| ADV-1 | Duplicate ID + early return cancel | Không trigger |
| ADV-2 | Bug2 + same price + different ts + partial sweep | Bug2 phải active; giá phải bằng nhau |
| ADV-3 | Bug1 + Bug2 + MARKET + resting opposite | Bug1 đủ để block market; Bug2 thêm disorder |
| ADV-4 | Bug2 + same price + sequential SELLs | 1 SELL lớn ẩn bug; phải nhiều SELLs nhỏ |
| ADV-5 | Duplicate ID + Bug2 + diff ts + cancel | Bug2 quyết định ZOMBIE nào còn lại |
| ADV-6 | Float arithmetic + literal literal mismatch | Không phải bug code |
| ADV-7 | Cancel × Fill ordering | Behavior đúng, chỉ cần awareness |

### Kết quả test adversarial

```
38 tests collected
38 passed
0 failed
Thời gian: 0.23s
```

Tất cả 38 tests **PASS** — bao gồm các tests xác nhận hành vi bug hiện tại
(tests assert rằng bug đang xảy ra, không phải assert behavior đúng).
