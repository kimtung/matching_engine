# Bug Report — QA Engineer
**Role:** QA Engineer — Break the System  
**Phương pháp:** Edge case injection, sequence-based attacks, concurrency simulation, boundary testing  
**Nguyên tắc:** Tìm điều kiện kết hợp để trigger bugs tinh vi; mọi bug phải có reproduction steps cụ thể

---

## Tổng quan

| ID | Tên Bug | Severity | Status |
|----|---------|----------|--------|
| QA-01 | Market Order Hoàn Toàn Bị Liệt | **CRITICAL** | ✅ FIXED |
| QA-02 | FIFO Bị Đảo Ngược Trên Buy Side | **HIGH** | ✅ FIXED |
| QA-03 | Zombie Order Sau Duplicate Submit + Cancel | **HIGH** | ✅ FIXED |
| QA-04 | SELL Cancel Không Notify WebSocket Clients | **MEDIUM** | ✅ FIXED |
| QA-05 | Negative Price — Engine Layer Chưa Validate | **HIGH** | ✅ FIXED |
| QA-06 | Invalid order_type Silently Treated as LIMIT | **MEDIUM** | ✅ FIXED |
| QA-07 | Partial Fill → Duplicate ID → Cancel → Zombie | **HIGH** | ✅ FIXED (by QA-03) |
| QA-08 | WebSocket Client Nhận Event Trước Initial State | **MEDIUM** | ✅ FIXED |
| QA-09 | Trade History Tăng Vô Hạn — Memory Leak | **MEDIUM** | ✅ FIXED |
| QA-10 | GET /api/state Không Có Lock | **MEDIUM** | ✅ FIXED |
| QA-11 | Auto Order-ID Collision → Zombie Dưới Load | **MEDIUM** | ✅ FIXED |
| QA-12 | Quantity = 0 Silently No-Op | **LOW** | ✅ FIXED |
| QA-13 | Reset Endpoint Không Cần Auth | **CRITICAL** | ✅ FIXED |
| QA-14 | Broadcast Crash Do Set Mutation Khi Iterate | **HIGH** | ✅ FIXED |

---

## Bugs Còn Mở (0 bugs)

Tất cả bug đã được fix. Xem phần từng bug bên dưới để biết cụ thể.

---

---

## QA-01 — Market Order Hoàn Toàn Bị Liệt

### Mô tả
Mọi MARKET order đều trả về 0 trades dù sổ đối diện đang có sẵn lệnh. Đây là bug được inject vào `_is_match()`.

### Điều kiện kết hợp để xảy ra
- Order type = `MARKET`
- `price=None` (luôn đúng với mọi market order được tạo qua `service.py`)
- Có ≥1 resting LIMIT order trên sổ đối diện

### Kịch bản tái hiện (Step-by-step)

```
Setup:
  POST /api/orders
  Body: {"side":"SELL","quantity":10,"price":100.0,"order_type":"LIMIT","order_id":"S1"}
  → Expected: S1 resting trong sells book

Test:
  POST /api/orders
  Body: {"side":"BUY","quantity":5,"order_type":"MARKET","order_id":"M1"}
  → Expected: 1 trade {qty:5, price:100.0}
  → Actual:   trades=[], S1 vẫn còn trong sổ

Verify:
  GET /api/state
  → sells: [{order_id:"S1", remaining:10}]  ← không bị fill
  → trades: []
```

### Root Cause
```python
# order_book.py:61-62
def _is_match(self, incoming: Order, resting: Order) -> bool:
    if incoming.order_type == OrderType.MARKET:
        return incoming.price is not None  # price=None → luôn False
```

### Automation Test
```python
def test_market_order_must_match():
    svc = MatchingEngineService()
    svc.place_limit_order("S1", "SELL", 10, 100.0, 1)
    trades = svc.place_market_order("M1", "BUY", 5, 2)
    assert len(trades) == 1, f"Expected 1 trade, got {len(trades)}"
    assert trades[0]["quantity"] == 5
    assert trades[0]["price"] == 100.0
```

**Severity: CRITICAL** — Market order là core feature; 100% bị vô hiệu hóa.

---

---

## QA-02 — FIFO Bị Đảo Ngược Trên Buy Side (LIFO Bug)

### Mô tả
Khi nhiều BUY limit order cùng giá, lệnh đến **muộn hơn** được khớp trước. Vi phạm nguyên tắc Price-Time Priority. Không ảnh hưởng SELL side.

### Điều kiện kết hợp để xảy ra
- Tồn tại ≥2 BUY LIMIT orders với **cùng price**
- Timestamps phải **khác nhau** (nếu cùng timestamp, Python stable sort giữ insertion order → bug ẩn)
- Có SELL order đến sau với quantity ≤ min(buy quantities)

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_id":"B1","timestamp":1}
Bước 2: POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_id":"B2","timestamp":2}
        (B1 đến trước, B2 đến sau — cùng price)

State kiểm tra: GET /api/state
  → buys sorted: [B2(ts=2), B1(ts=1)]  ← BUG: B2 đứng trước dù đến sau

Bước 3: POST /api/orders {"side":"SELL","quantity":5,"price":100.0,"order_id":"S1","timestamp":3}
        → Expected trade: S1 × B1  (FIFO — B1 đến trước)
        → Actual trade:   S1 × B2  (LIFO — B2 timestamp cao hơn)

Verify:
  → trades[0].buy_order_id = "B2"  ← SAI
  → buys remaining: [B1, remaining=5]  ← B1 chưa được fill dù đến trước
```

### Điều kiện ẩn (Khó Phát Hiện)
```
Nếu B1.timestamp == B2.timestamp:
  → Python stable sort giữ insertion order → B1 đứng trước → đúng
  → Bug chỉ bộc lộ khi timestamps KHÁC nhau
```

### Root Cause
```python
# order_book.py:57
self.buys.sort(key=lambda item: (item.price or 0.0, item.timestamp), reverse=True)
# reverse=True đảo ngược CẢ timestamp → ts cao (đến sau) lên đầu = LIFO
```

### Automation Test
```python
def test_fifo_violation_buy_side():
    svc = MatchingEngineService()
    svc.place_limit_order("B1", "BUY", 5, 100.0, 1)   # đến trước
    svc.place_limit_order("B2", "BUY", 5, 100.0, 2)   # đến sau

    trades = svc.place_limit_order("S1", "SELL", 5, 100.0, 3)
    assert trades[0]["buy_order_id"] == "B1", \
        f"FIFO violation: B2 filled instead of B1. Got: {trades[0]['buy_order_id']}"
```

**Severity: HIGH** — Vi phạm exchange rules; traders có thể bị thiệt hại tài chính.

---

---

## QA-03 — Zombie Order: Duplicate Submit + Cancel

### Mô tả
Submit cùng `order_id` hai lần → cả hai vào sổ. `cancel()` chỉ xóa entry đầu tiên → entry thứ hai tồn tại như "zombie", tiếp tục khớp với lệnh tương lai dù đã bị "cancel".

### Điều kiện kết hợp để xảy ra
- Submit order với `order_id = X` lần 1
- Submit order với `order_id = X` lần 2 (cùng hoặc khác timestamp)
- Gọi `cancel(X)` — chỉ xóa entry đầu tiên
- Kết hợp BUG-02: nếu timestamps khác nhau, zombie là bản gốc (ts nhỏ hơn)

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_id":"B1","timestamp":1}
        → buys = [B1(ts=1, remaining=5)]

Bước 2: POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_id":"B1","timestamp":2}
        → Không bị reject! buys = [B1(ts=2), B1(ts=1)]  (BUG-02: ts=2 đứng trước)

GET /api/state → buys: [{order_id:"B1",remaining:5,ts:2}, {order_id:"B1",remaining:5,ts:1}]
                         ← 2 entries cùng ID là dấu hiệu bug

Bước 3: POST /api/cancel {"order_id":"B1"}
        → Response: {"ok":true}  ← cancel "thành công"
        → cancel() tìm B1(ts=2) trước, xóa, return True
        → B1(ts=1) còn lại: ZOMBIE

GET /api/state → buys: [{order_id:"B1",remaining:5,ts:1}]
                         ← zombie vẫn sống

Bước 4: POST /api/orders {"side":"SELL","quantity":5,"price":100.0,"order_id":"S1","timestamp":3}
        → Khớp với zombie B1(ts=1)!
        → Trade: S1 × B1, qty=5, price=100.0
        → Lệnh đã "cancel" vẫn tham gia trade ← NGHIÊM TRỌNG
```

### Root Cause
```python
# _rest() — order_book.py:49
def _rest(self, order: Order) -> None:
    self.buys.append(order)   # không check duplicate
    self._sort_books()

# cancel() — order_book.py:41
for index, order in enumerate(book):
    if order.order_id == order_id and order.remaining > 0:
        book.pop(index)
        return True    # early return, bản sao 2 thoát
```

### Automation Test
```python
def test_zombie_order_after_duplicate_cancel():
    book = OrderBook()
    book.submit(lim("B1", "BUY", 5, 100.0, 1))
    book.submit(lim("B1", "BUY", 5, 100.0, 2))   # duplicate

    book.cancel("B1")
    remaining_buys = book.snapshot()["buys"]

    # Zombie check
    assert remaining_buys == [], \
        f"Zombie detected: {len(remaining_buys)} order(s) still in book after cancel"

    # Extra: zombie không được trade
    trades = book.submit(lim("S1", "SELL", 5, 100.0, 3))
    assert trades == [], f"Zombie traded! {len(trades)} trade(s) generated"
```

**Severity: HIGH** — Trade xuất hiện sau khi cancel; phantom liquidity.

---

---

## QA-04 — SELL Cancel Silent: WebSocket Không Nhận Event

### Mô tả
Cancel SELL order thành công ở server, HTTP caller nhận `{"ok":true}`, nhưng tất cả WebSocket client không nhận event nào. UI hiển thị SELL order vẫn active dù đã bị hủy.

### Điều kiện kết hợp để xảy ra
- `order_id` của SELL order **không bắt đầu bằng "B"**
- Có ≥1 WebSocket client đang kết nối
- SELL order tồn tại trong sổ và bị cancel thành công

### Kịch bản tái hiện (Step-by-step)

```
Setup: Client WS kết nối ws://localhost:8000/ws
       → Nhận: {"event":"connected", "state": {...}}

Bước 1: POST /api/orders {"side":"SELL","quantity":10,"price":100.0,"order_id":"S1"}
        WS Client nhận: {"event":"order_placed", "state":{sells:[S1]}, "trades":[]}

Bước 2: POST /api/cancel {"order_id":"S1"}
        HTTP Response: {"ok":true}  ← thành công
        WS Client nhận: [NOTHING]  ← không có event

Verify WS state:
  WS Client vẫn thấy: sells=[{order_id:"S1", remaining:10}]
  Actual state:       sells=[]

Bước 3: POST /api/orders {"side":"BUY","quantity":5,"price":100.0}
        WS Client nhận: {"event":"order_placed", "state":{sells:[]}, "trades":[]}
        → Đây là lần duy nhất client biết S1 đã biến mất — quá muộn
```

### Điều kiện "may mắn" không trigger bug
```
order_id = "B_sell_123"  → startswith("B") = True → broadcast vẫn chạy
order_id = "BSELL1"      → startswith("B") = True → broadcast vẫn chạy
```

### Root Cause
```python
# web.py:109-113
if cancelled:
    if order_id.startswith("B"):    # ← chỉ broadcast khi ID bắt đầu bằng "B"
        await hub.broadcast("order_cancelled")
    return web.json_response({"ok": True, "state": state})
```

### Automation Test
```python
async def test_sell_cancel_broadcasts():
    # Dùng aiohttp test client + websocket mock
    ws_events = []
    async with ws_client.connect("ws://localhost:8000/ws") as ws:
        await place_order("S1", "SELL", 10, 100.0)
        await cancel_order("S1")
        event = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
        ws_events.append(event)

    assert any(e["event"] == "order_cancelled" for e in ws_events), \
        "No order_cancelled event received after cancelling SELL order"
```

**Severity: MEDIUM** — UI desync; nguy hiểm hơn trong live trading khi timing là tất cả.

---

---

## QA-05 — Negative Price Cho Phép SELL Khớp Vô Điều Kiện

### Mô tả
SELL LIMIT order với `price` âm (ví dụ `-100.0`) sẽ khớp với **mọi** BUY order bất kể giá, vì `-100 <= any_positive_price` luôn đúng. Đây là lỗ hổng input validation nghiêm trọng — trade xảy ra tại giá của BUY (resting price), không phải giá âm — nhưng SELL order không bao giờ nên được chấp nhận với giá âm.

### Điều kiện kết hợp để xảy ra
- `price < 0.0` trong SELL LIMIT order payload
- Không có validation tại `web.py` hoặc `service.py`
- BUY LIMIT order đang resting với bất kỳ price dương nào

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_id":"B1","timestamp":1}
        → B1 resting tại 100.0

Bước 2: POST /api/orders {"side":"SELL","quantity":5,"price":-999.0,"order_id":"S1","timestamp":2}
        → _is_match: (-999.0 or 0.0) <= (100.0 or 0.0)
                     → -999.0 <= 100.0 → True
        → Trade tạo ra: S1 × B1, qty=5, price=100.0

Kết quả:
  HTTP Response: {"ok":true, "trades":[{qty:5, price:100.0}]}
  Bookseller:    S1 khớp hoàn toàn
  Problem:       SELL tại giá -999 được hệ thống chấp nhận và trade thành công
                 Người đặt SELL có thể là lỗi nhập liệu, nhận trade không mong muốn

Attack scenario:
  Trader gõ nhầm: price="-100" thay vì "100"
  → Hệ thống không reject, trade ngay lập tức tại giá market (của resting BUY)
  → Trader không có cơ hội confirm

Negative BUY scenario:
  POST /api/orders {"side":"BUY","quantity":5,"price":-100.0}
  → BUY @-100 không khớp được với SELL @100 (vì -100 >= 100 = False)
  → BUY @-100 REST trong sổ vô thời hạn — book pollution
```

### Root Cause
```python
# web.py:92
price = float(payload["price"])   # float("-999") = -999.0, không bị reject

# order_book.py:64
if incoming.side == Side.BUY:
    return (incoming.price or 0.0) >= (resting.price or 0.0)
return (incoming.price or 0.0) <= (resting.price or 0.0)
# -999 <= 100 → True → match
```

### Automation Test
```python
def test_negative_price_rejected():
    svc = MatchingEngineService()
    svc.place_limit_order("B1", "BUY", 5, 100.0, 1)

    # Negative SELL price phải bị reject, không được trade
    with pytest.raises((ValueError, Exception)):
        svc.place_limit_order("S1", "SELL", 5, -100.0, 2)

def test_negative_price_does_not_match():
    svc = MatchingEngineService()
    svc.place_limit_order("B1", "BUY", 5, 100.0, 1)
    trades = svc.place_limit_order("S1", "SELL", 5, -100.0, 2)
    assert trades == [], "Negative price SELL không được khớp với BUY"
```

**Severity: HIGH** — Inadvertent trades; potential financial loss; input sanitization hoàn toàn thiếu.

---

---

## QA-06 — Missing Required Field → HTTP 500 + Stack Trace Lộ Ra Client

### Mô tả
Các endpoint POST không có exception handling. Khi payload thiếu field bắt buộc hoặc có giá trị không hợp lệ, server trả về HTTP 500 với Python traceback thay vì 400 Bad Request. Stack trace lộ ra internal file paths, class names, và logic của hệ thống.

### Điều kiện kết hợp để xảy ra
Bất kỳ POST nào với payload không hợp lệ:
- Thiếu `"side"` → `KeyError: 'side'`
- Thiếu `"quantity"` → `KeyError: 'quantity'`
- `"quantity": "abc"` → `ValueError: invalid literal for int()`
- `"side": "INVALID"` → `ValueError: 'INVALID' is not a valid Side`
- Thiếu `"price"` cho LIMIT order → `KeyError: 'price'`
- Body không phải JSON hợp lệ → `json.JSONDecodeError`

### Kịch bản tái hiện (Step-by-step)

```
Test Case A — Missing "side":
  POST /api/orders {"quantity":5,"price":100.0,"order_type":"LIMIT"}
  → HTTP 500
  → Body: traceback ... KeyError: 'side'
           File "web.py", line 84, in place_order
             side = payload["side"]

Test Case B — Invalid order_type enum:
  POST /api/orders {"side":"BUY","quantity":5,"price":100.0,"order_type":"FOO"}
  → Không crash vì order_type có default="LIMIT" nếu không validate
  → Actually: order_type = payload.get("order_type", "LIMIT") → "FOO"
  → if order_type == "MARKET": False → treated as LIMIT (silently wrong)

Test Case C — Non-numeric quantity:
  POST /api/orders {"side":"BUY","quantity":"five","price":100.0,"order_type":"LIMIT"}
  → HTTP 500
  → Body: ValueError: invalid literal for int() with base 10: 'five'

Test Case D — Malformed JSON body:
  POST /api/orders
  Body: {side: BUY}   ← invalid JSON (no quotes)
  → HTTP 500
  → json.JSONDecodeError

Test Case E — Missing order_id for cancel:
  POST /api/cancel {}
  → HTTP 500
  → KeyError: 'order_id'
```

### Root Cause
```python
# web.py:81-93 — không có try/except
async def place_order(request: web.Request) -> web.Response:
    payload = await request.json()           # JSONDecodeError uncaught
    order_type = payload.get("order_type", "LIMIT")
    order_id = payload.get("order_id") or f"ORD-{int(time.time() * 1000)}"
    side = payload["side"]                   # KeyError uncaught
    quantity = int(payload["quantity"])      # KeyError / ValueError uncaught
    ...
    price = float(payload["price"])          # KeyError / ValueError uncaught
```

### Automation Test
```python
@pytest.mark.parametrize("payload,expected_status", [
    ({"quantity":5,"price":100.0,"order_type":"LIMIT"}, 400),   # missing side
    ({"side":"BUY","price":100.0,"order_type":"LIMIT"}, 400),   # missing quantity
    ({"side":"BUY","quantity":"abc","price":100.0}, 400),        # invalid quantity
    ({"side":"INVALID","quantity":5,"price":100.0}, 400),        # invalid side
    ({"side":"BUY","quantity":5,"order_type":"LIMIT"}, 400),    # missing price for LIMIT
])
async def test_invalid_payload_returns_400(aiohttp_client, payload, expected_status):
    client = await aiohttp_client(create_app())
    resp = await client.post("/api/orders", json=payload)
    assert resp.status == expected_status, f"Expected {expected_status}, got {resp.status}"
```

**Severity: HIGH** — Information disclosure (stack trace); lỗ hổng bảo mật + UX tệ.

---

---

## QA-07 — Partial Fill → Duplicate ID → Cancel → Zombie Sequence

### Mô tả
Bug phức tạp nhất — yêu cầu **3 điều kiện kết hợp** theo đúng thứ tự: partial fill làm `remaining < quantity`, sau đó submit duplicate ID, rồi cancel. Zombie còn lại là bản gốc đã partial fill với `remaining` không đúng.

### Điều kiện kết hợp để xảy ra
1. Order resting bị **partial fill** (remaining < quantity)
2. Submit lại **cùng order_id** sau khi partial fill (không bị reject)
3. Gọi **cancel()** — xóa bản được sort đầu tiên (do BUG-02, bản có ts lớn hơn)
4. Bản gốc với `remaining` sau partial fill trở thành zombie

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: Submit BUY B1, qty=10, price=100.0, ts=1
        → buys = [B1(remaining=10)]

Bước 2: Submit SELL S_partial, qty=3, price=100.0, ts=2
        → Partial fill B1: remaining = 10 - 3 = 7
        → buys = [B1(remaining=7)]
        → 1 trade: qty=3

Bước 3: Submit BUY B1, qty=5, price=100.0, ts=3  ← duplicate ID!
        → _rest() append: buys = [B1(ts=3,rem=5), B1(ts=1,rem=7)]
                          (BUG-02: ts=3 đứng trước)

GET /api/state:
  buys = [
    {order_id:"B1", remaining:5, timestamp:3},    ← duplicate mới
    {order_id:"B1", remaining:7, timestamp:1},    ← gốc đã partial fill
  ]
  total_exposure = 12 (thay vì 5 nếu cancel B1 cũ đúng cách)

Bước 4: POST /api/cancel {"order_id":"B1"}
        → cancel() tìm B1(ts=3) trước (đứng đầu do BUG-02), xóa nó
        → return True ← thoát sớm
        → buys = [B1(ts=1, remaining=7)]  ← ZOMBIE: bản gốc đã partial fill

GET /api/state sau cancel:
  buys = [{order_id:"B1", remaining:7, timestamp:1}]
  User nghĩ đã cancel hoàn toàn, nhưng zombie còn 7 units exposure!

Bước 5: Submit SELL S2, qty=7, price=100.0, ts=4
        → Khớp với zombie B1(ts=1): Trade qty=7
        → Trader B1 bị filled 7 units mà họ không biết (đã cancel)!

Tổng thiệt hại: B1 trader bị filled tổng 10 units (3 + 7) dù chỉ muốn 10
                Và đã cancel nhưng vẫn bị fill thêm 7 sau cancel
```

### Điều kiện nào khiến bug ẩn
```
Nếu B1 ở bước 3 dùng ts=0 (ts nhỏ hơn bản gốc ts=1):
  → BUG-02 sort: [B1(ts=1), B1(ts=0)] → cancel xóa B1(ts=1) trước
  → Zombie là B1(ts=0) — bản mới, không có partial fill → harder to detect

Nếu không có BUG-02 (sort đúng FIFO):
  → [B1(ts=1), B1(ts=3)] → cancel xóa B1(ts=1) trước
  → Zombie là B1(ts=3) — bản mới
```

### Automation Test
```python
def test_partial_fill_duplicate_cancel_no_zombie():
    book = OrderBook()
    # Step 1: rest
    book.submit(lim("B1", "BUY", 10, 100.0, 1))
    # Step 2: partial fill
    book.submit(lim("S_p", "SELL", 3, 100.0, 2))
    assert book.snapshot()["buys"][0]["remaining"] == 7

    # Step 3: duplicate
    book.submit(lim("B1", "BUY", 5, 100.0, 3))
    # Step 4: cancel
    book.cancel("B1")

    # Verify: no zombie
    buys = book.snapshot()["buys"]
    assert buys == [], f"Zombie detected: {buys}"

    # Step 5: no trade after cancel
    trades = book.submit(lim("S_sweep", "SELL", 7, 100.0, 4))
    assert trades == [], f"Zombie traded: {trades}"
```

**Severity: HIGH** — Trader bị filled sau khi cancel; phantom financial exposure.

---

---

## QA-08 — WebSocket Client Nhận Event Trước Initial State

### Mô tả
Khi client mới kết nối WS, server thêm client vào `hub.clients` trước khi gửi initial state. Nếu có `broadcast()` đang chạy (từ order concurrent), client nhận event update trước khi nhận `{"event":"connected"}`. Client-side state bắt đầu từ sai baseline.

### Điều kiện kết hợp để xảy ra
- WebSocket client kết nối (trong khoảng `hub.clients.add()` → `socket.send_json()`)
- Đồng thời có order được place → `broadcast()` chạy
- `broadcast()` và `websocket_handler` interleave tại asyncio context switch

### Kịch bản tái hiện (Step-by-step)

```
Timeline asyncio:
  T0: Client C mới kết nối → websocket_handler bắt đầu
  T1: hub.clients.add(C)                    ← C được add vào clients set
  T2: [await socket.prepare(request)]       ← yield control
  T3: Order được place → broadcast() chạy
  T4: broadcast() gửi cho TẤT CẢ clients trong hub.clients (bao gồm C)
      → C nhận: {"event":"order_placed", "state":{...}, "trades":[...]}
  T5: websocket_handler resume
  T6: await socket.send_json({"event":"connected", "state":..., "trades":[]})
      → C nhận: {"event":"connected", "state":{...}}

Client C nhận theo thứ tự:
  1. {"event":"order_placed", ...}   ← TRƯỚC initial state!
  2. {"event":"connected", ...}      ← initial state đến SAU

Client-side handler:
  - Xử lý "order_placed" trước: apply delta lên... state chưa có
  - Xử lý "connected": overwrite với old state
  → Client thiếu trade của order vừa placed
```

### Root Cause
```python
# web.py:128-130
hub.clients.add(socket)                                    # add trước
await socket.send_json({"event": "connected", ...})        # send sau

# Khoảng giữa 2 dòng: client có thể nhận broadcast() event
```

### Automation Test
```python
async def test_ws_initial_state_arrives_first():
    events_received = []

    async def ws_client():
        async with aiohttp.ClientSession().ws_connect("/ws") as ws:
            # Đặt order ngay khi connect
            asyncio.create_task(place_order_concurrent())
            async for msg in ws:
                events_received.append(msg.json()["event"])
                if len(events_received) >= 2:
                    break

    await ws_client()
    assert events_received[0] == "connected", \
        f"First event must be 'connected', got '{events_received[0]}'"
```

**Severity: MEDIUM** — Race condition trong UI initialization; cần concurrent load để trigger.

---

---

## QA-09 — Trade History Tăng Vô Hạn: Memory Leak

### Mô tả
`OrderBook.trades` là list append-only, không bao giờ bị prune. Mỗi `snapshot()` call serialize toàn bộ trade history và đưa vào HTTP response. Trong long-running server với nhiều trades, response payload tăng vô hạn và memory footprint tăng tuyến tính.

### Điều kiện kết hợp để xảy ra
- Server chạy trong thời gian dài
- Nhiều trades được thực hiện (bình thường với exchange)
- Không có restart hay reset

### Kịch bản tái hiện (Step-by-step)

```
Simulate N trades:
  for i in range(10000):
      svc.place_limit_order(f"S{i}", "SELL", 1, 100.0, i)
      svc.place_limit_order(f"B{i}", "BUY", 1, 100.0, i+1)

After N=10,000 trades:
  GET /api/state
  → Response body: {"trades": [...10000 trade objects...]}
  → Payload size: ~10000 × ~150 bytes = ~1.5 MB per request
  → Memory: trades list holds 10000 Trade objects trong RAM mãi mãi

After N=1,000,000 trades:
  → Payload: ~150 MB per /api/state request
  → Memory: hàng chục GB
  → aiohttp timeout khi serialize response quá lớn
```

### Root Cause
```python
# order_book.py:13
self.trades: list[Trade] = []

# order_book.py:29
self.trades.append(trade)    # append mãi mãi

# order_book.py:90
"trades": [asdict(trade) for trade in self.trades],   # serialize tất cả
```

### Automation Test
```python
def test_trade_history_not_unbounded():
    book = OrderBook()
    for i in range(1000):
        book.submit(lim(f"S{i}", "SELL", 1, 100.0, i))
        book.submit(lim(f"B{i}", "BUY", 1, 100.0, i+1))

    snap = book.snapshot()
    # Trade history nên có pagination hoặc cap
    assert len(snap["trades"]) <= 100, \
        f"Trade history unbounded: {len(snap['trades'])} trades in response"

def test_state_response_size_bounded():
    # Response phải có size limit
    response_size = len(json.dumps(svc.get_order_book()))
    assert response_size < 1_000_000, f"Response too large: {response_size} bytes"
```

**Severity: MEDIUM** — Không crash ngay nhưng gây OOM và service degradation dần dần.

---

---

## QA-10 — GET /api/state Không Có Lock: Inconsistent Read

### Mô tả
`get_state` đọc order book **không có lock** trong khi `place_order` và `cancel_order` dùng lock khi write. Trong Python asyncio (single-threaded cooperative), điều này an toàn về mặt thread-safety nhưng có thể trả về state giữa chừng của một operation nếu engine có IO operations.

### Điều kiện kết hợp để xảy ra
- `GET /api/state` và `POST /api/orders` execute concurrently
- Python asyncio context switch xảy ra trong khi engine đang process
- (Hiện tại ít nguy hiểm vì engine là synchronous, nhưng nguy hiểm nếu engine được refactor thành async)

### Kịch bản tái hiện

```python
# web.py:74-76
async def get_state(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    return web.json_response(hub.state())   # ← không có async with hub.lock

# Scenario hiện tại (ít nguy hiểm):
# Engine ops là sync → không có yield trong matching loop
# → GET /api/state không thể interleave VÀO GIỮA matching

# Scenario nguy hiểm nếu engine trở thành async:
# T1: POST /api/orders → lock → matching bắt đầu
#     best.remaining -= matched_qty  ← state giữa chừng
#     [async yield]
# T2: GET /api/state → state() → đọc state giữa chừng:
#     best.remaining = 3 (đã trừ), nhưng order.remaining = 10 (chưa trừ)
#     → Response: sell remaining=3 nhưng không có trade tương ứng
```

### Hiện trạng
```python
# Đúng pattern nên là:
async def get_state(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    async with hub.lock:                    # ← cần thêm lock
        state = hub.state()
    return web.json_response(state)
```

**Severity: MEDIUM** — Latent bug; an toàn hiện tại nhưng time bomb nếu engine async hóa.

---

---

## QA-11 — Auto Order-ID Collision → Zombie Dưới Load

### Mô tả
Auto-generated order IDs dùng timestamp millisecond. Hai requests trong cùng 1ms nhận cùng ID. Kết hợp với BUG-03 (không dedup), tạo zombie tự động mà không cần action cố ý từ phía client.

### Điều kiện kết hợp để xảy ra
- Client gửi orders **không có** `order_id` trong payload
- Hai requests arrive trong cùng **1 millisecond**
- Server sử dụng `int(time.time() * 1000)` → cùng integer
- asyncio.Lock serialize chúng nhưng timestamp đã bị generate trước khi lock

### Kịch bản tái hiện (Step-by-step)

```python
# web.py:83
order_id = payload.get("order_id") or f"ORD-{int(time.time() * 1000)}"
# → T=1700000000000ms: ORD-1700000000000
# → T=1700000000000ms: ORD-1700000000000  ← collision!

Simulate:
import asyncio, aiohttp, time

async def flood_orders():
    async with aiohttp.ClientSession() as s:
        tasks = [
            s.post("/api/orders", json={
                "side":"BUY","quantity":5,"price":100.0,"order_type":"LIMIT"
            })
            for _ in range(100)
        ]
        responses = await asyncio.gather(*tasks)

# Trong 100 requests đồng thời, nhiều cặp sẽ có cùng timestamp
# → Nhiều zombie orders trong book
# → Impossible để cancel hết vì cancel chỉ xóa 1 per call

Verify:
GET /api/state
→ buys: [
    {order_id:"ORD-1700000000000", remaining:5},
    {order_id:"ORD-1700000000000", remaining:5},
    {order_id:"ORD-1700000000000", remaining:5},
    ...  ← nhiều zombie cùng ID
  ]
```

### Automation Test
```python
def test_concurrent_orders_no_duplicate_ids():
    seen_ids = set()
    duplicates = []

    async def place_100_orders():
        async with aiohttp.ClientSession() as s:
            responses = await asyncio.gather(*[
                s.post("/api/orders", json={"side":"BUY","quantity":1,"price":100.0,"order_type":"LIMIT"})
                for _ in range(100)
            ])
            for r in responses:
                data = await r.json()
                oid = data.get("order_id")
                if oid in seen_ids:
                    duplicates.append(oid)
                seen_ids.add(oid)

    asyncio.run(place_100_orders())
    assert len(duplicates) == 0, f"Duplicate order IDs: {duplicates}"
```

**Severity: MEDIUM** — Rare trong development; phổ biến dưới production load.

---

---

## QA-12 — Quantity = 0: Silently No-Op, Không Có Error

### Mô tả
Order với `quantity=0` được chấp nhận nhưng không làm gì cả — không trade, không rest vào sổ, không error. Client nhận HTTP 201 Created nhưng không có gì xảy ra. Đây là silent failure.

### Kịch bản tái hiện

```
POST /api/orders {"side":"BUY","quantity":0,"price":100.0,"order_type":"LIMIT"}
→ HTTP 201 {"ok":true, "trades":[], "state":{...}}

Internal flow:
  order.remaining = 0 (từ __post_init__)
  while order.remaining > 0:  → False ngay lập tức
  if order.remaining > 0 and LIMIT:  → False
  → Không trade, không rest

Client không có cách nào biết order bị ignored.
```

### Automation Test
```python
def test_zero_quantity_returns_error():
    client.post("/api/orders", json={"side":"BUY","quantity":0,"price":100.0,"order_type":"LIMIT"})
    # Phải trả 400 Bad Request, không phải 201
    assert response.status == 400
    assert "quantity must be positive" in response.json()["error"]
```

**Severity: LOW** — UX bug; không crash nhưng confusing cho client.

---

---

## QA-13 — Reset Endpoint Không Cần Auth: Instant Book Wipe

### Mô tả
`POST /api/reset` xóa toàn bộ order book và trade history trong 1 request. Không cần token, không cần session, không cần IP whitelist. Có thể trigger từ browser (do CORS wildcard).

### Kịch bản tái hiện

```bash
# Từ bất kỳ terminal:
curl -X POST http://localhost:8000/api/reset
# → {"ok":true, "state":{"book":{"buys":[],"sells":[],"trades":[]}}}

# Từ browser (do CORS *):
fetch("http://localhost:8000/api/reset", {method:"POST"})
  .then(r => r.json())
  .then(console.log)  // {"ok":true, ...}

# DoS script:
while true; do curl -sX POST http://target:8000/api/reset; sleep 0.1; done
```

### Automation Test
```python
async def test_reset_requires_auth(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.post("/api/reset")
    assert resp.status in [401, 403], \
        f"Reset endpoint should require auth, got {resp.status}"
```

**Severity: CRITICAL** — Total service disruption, irreversible (no audit log).

---

---

## QA-14 — Broadcast Crash: Set Mutation Trong Async Iteration

### Mô tả
`broadcast()` iterate qua `hub.clients` (type `set`) và `await` bên trong loop. `await` là điểm yield — event loop có thể schedule `websocket_handler` của client mới kết nối, gọi `hub.clients.add()` → `RuntimeError: Set changed size during iteration`.

### Điều kiện kết hợp để xảy ra
- ≥2 WebSocket clients đang kết nối
- Order được place → trigger `broadcast()`
- Client mới kết nối (hoặc disconnect) đúng lúc `broadcast()` đang iterate
- Phải có **actual network I/O** để asyncio yield tại `await client.send_json()`

### Kịch bản tái hiện (Step-by-step)

```python
# Concurrent scenario:

async def scenario():
    # 1. Connect 10 WS clients
    clients = [await connect_ws() for _ in range(10)]

    # 2. Simultaneously:
    async def place_order_trigger():
        await http_post("/api/orders", {...})   # triggers broadcast()

    async def new_client_connect():
        await asyncio.sleep(0.001)              # nhỏ delay để vào giữa broadcast
        new_ws = await connect_ws()             # hub.clients.add() trong lúc iterate

    await asyncio.gather(place_order_trigger(), new_client_connect())
    # → RuntimeError: Set changed size during iteration
    # → broadcast() crash ở giữa chừng
    # → Clients 5-10 không nhận được event
```

### Root Cause
```python
# web.py:36-43
async def broadcast(self, event, trades=None):
    ...
    for client in self.clients:              # ← iterate set TRỰC TIẾP
        ...
        await client.send_json(payload)      # ← yield point
    # client kết nối mới → hub.clients.add() → RuntimeError

# Fix:
    for client in list(self.clients):       # snapshot trước khi iterate
```

### Automation Test
```python
async def test_broadcast_survives_concurrent_connect():
    app = create_app()
    clients = []

    async def keep_connecting():
        for _ in range(20):
            ws = await connect_ws(app)
            clients.append(ws)
            await asyncio.sleep(0.005)

    async def keep_ordering():
        for i in range(20):
            await place_order(app, f"B{i}", "BUY", 1, 100.0)
            await asyncio.sleep(0.003)

    # Không nên raise RuntimeError
    await asyncio.gather(keep_connecting(), keep_ordering())
```

**Severity: HIGH** — Crash production broadcast; cần concurrent load để trigger.

---

---

## Bug Interaction Matrix (QA Perspective)

```
Điều kiện A × B → Hậu quả

QA-01 (market liệt) × QA-02 (LIFO) × QA-03 (zombie):
  → Market order không clear zombie → zombie tích lũy → book ngày càng polluted

QA-03 (zombie) × QA-11 (ID collision):
  → Zombie tự động tạo ra dưới load mà không cần client action nào

QA-05 (negative price) × QA-01 (market liệt):
  → Market order không thể sweep sai price out → negative-price orders resting mãi

QA-06 (HTTP 500) × QA-14 (broadcast crash):
  → Double failure: order placement fail + broadcast crash → clients stuck với stale state

QA-13 (unauthenticated reset) × QA-11 (ID collision):
  → Reset xóa book → clients tiếp tục gửi orders → ID collision tái tạo zombie ngay
```

---

## Test Automation Strategy

```python
# conftest.py
import pytest
from matching_engine.order_book import OrderBook
from matching_engine.models import Order, OrderType, Side

def lim(order_id, side, qty, price, ts):
    return Order(order_id=order_id, side=Side(side),
                 quantity=qty, price=price, timestamp=ts,
                 order_type=OrderType.LIMIT)

def mkt(order_id, side, qty, ts):
    return Order(order_id=order_id, side=Side(side),
                 quantity=qty, price=None, timestamp=ts,
                 order_type=OrderType.MARKET)

@pytest.fixture
def book():
    return OrderBook()

# Chạy toàn bộ:
# pytest tests/ -v --tb=short
# pytest tests/test_adversarial.py -v  ← tập trung vào bug test
```

### Priority Automation Order

```
1. QA-01 — 1 test, unblock toàn bộ MARKET flow
2. QA-13 — 1 curl test, xác nhận security gap nghiêm trọng nhất
3. QA-02 — parametrized FIFO test với ts khác nhau
4. QA-03 — zombie sequence test
5. QA-05 — negative/zero price validation
6. QA-06 — parametrized invalid payload tests
7. QA-07 — compound sequence test (partial + duplicate + cancel)
8. QA-14 — async concurrent WS test (cần aiohttp test client)
9. QA-08 — WS ordering test (requires careful timing)
10. QA-09 — load test với N=10000 trades, measure payload size
```
