# Bug Map

Tài liệu này ghi lại 3 bug đã được inject để bạn luyện tập.

---

## Bug 1: Market order không match được

| Trường | Chi tiết |
|--------|----------|
| **File** | `matching_engine/order_book.py` |
| **Dòng** | 62 |
| **Hàm** | `_is_match` |

**Sai ở đâu:**

```python
# HIỆN TẠI (SAI)
if incoming.order_type == OrderType.MARKET:
    return incoming.price is not None   # ← Market order luôn có price=None → luôn trả False
```

Market order được tạo với `price=None` (xem `service.py:42`). Điều kiện `incoming.price is not None` vì vậy **luôn luôn `False`**, khiến mọi market order đều không bao giờ khớp với bất kỳ resting order nào.

**Hiện tượng:** Đặt `SELL LIMIT` trước, sau đó đặt `BUY MARKET` → không có trade nào được tạo ra dù sổ lệnh có đủ điều kiện khớp.

**Cách sửa:**

```python
# ĐÚNG
if incoming.order_type == OrderType.MARKET:
    return True   # Market order khớp với bất kỳ resting order nào
```

---

## Bug 2: Buy side sai price-time priority

| Trường | Chi tiết |
|--------|----------|
| **File** | `matching_engine/order_book.py` |
| **Dòng** | 57 |
| **Hàm** | `_sort_books` |

**Sai ở đâu:**

```python
# HIỆN TẠI (SAI)
self.buys.sort(key=lambda item: (item.price or 0.0, item.timestamp), reverse=True)
# reverse=True áp dụng cho CẢ price lẫn timestamp
# → price sắp xếp DESC ✓  NHƯNG  timestamp cũng sắp xếp DESC ✗
```

Cờ `reverse=True` đảo chiều **toàn bộ tuple key**, gồm cả `timestamp`. Kết quả: trong cùng mức giá, lệnh có timestamp **lớn hơn (mới hơn)** lại được ưu tiên trước — vi phạm quy tắc FIFO (lệnh đến trước được khớp trước).

**Hiện tượng:** Đặt `B1 BUY LIMIT 100` (timestamp=1) rồi `B2 BUY LIMIT 100` (timestamp=2) → snapshot cho thấy `B2` đứng trước `B1`.

**Cách sửa:**

```python
# ĐÚNG: price DESC, timestamp ASC (FIFO)
self.buys.sort(key=lambda item: (-(item.price or 0.0), item.timestamp))
```

Dùng âm của price (`-price`) để đảo chiều price về DESC mà không ảnh hưởng đến timestamp (vẫn ASC).

---

## Bug 3: Hủy lệnh SELL không broadcast realtime tới client khác

| Trường | Chi tiết |
|--------|----------|
| **File** | `matching_engine/web.py` |
| **Dòng** | 110 |
| **Hàm** | `cancel_order` |

**Sai ở đâu:**

```python
# HIỆN TẠI (SAI)
if cancelled:
    if order_id.startswith("B"):          # ← chỉ broadcast khi order_id bắt đầu bằng "B"
        await hub.broadcast("order_cancelled")
    return web.json_response({"ok": True, "state": state})
```

Điều kiện `order_id.startswith("B")` chỉ broadcast khi hủy lệnh **BUY**. Khi hủy lệnh **SELL** (order_id thường bắt đầu bằng "S"), `broadcast` không được gọi → các client đang kết nối WebSocket không nhận được cập nhật.

**Hiện tượng:** Mở 2 tab frontend. Client A hủy một SELL order → client B không thấy thay đổi cho đến khi tự refresh.

**Cách sửa:**

```python
# ĐÚNG: luôn broadcast khi hủy thành công
if cancelled:
    await hub.broadcast("order_cancelled")
    return web.json_response({"ok": True, "state": state})
```

---

## Tóm tắt

| # | File | Dòng | Loại lỗi | Mức độ |
|---|------|------|-----------|--------|
| 1 | `order_book.py` | 62 | Logic sai (điều kiện luôn False) | Nghiêm trọng — chức năng Market Order hoàn toàn không hoạt động |
| 2 | `order_book.py` | 57 | Sort sai chiều timestamp | Cao — vi phạm FIFO, ưu tiên lệnh mới hơn lệnh cũ cùng giá |
| 3 | `web.py` | 110 | Điều kiện broadcast thiếu | Cao — mất đồng bộ realtime khi hủy SELL order |
