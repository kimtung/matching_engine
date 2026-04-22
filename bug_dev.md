# Bug Report — Matching Engine
**Role:** Senior Backend Engineer  
**Scope:** `matching_engine/order_book.py`, `matching_engine/web.py`, `matching_engine/service.py`  
**Nguyên tắc:** Một số bug được inject có chủ đích — tìm lỗi tinh vi, không phải syntax error

---

## Tổng quan

| ID | Tên Bug | File | Dòng | Severity | Rare? |
|----|---------|------|------|----------|-------|
| BUG-01 | Market Order Never Matches | `order_book.py` | 61–62 | **CRITICAL** | Không |
| BUG-02 | FIFO Violation — BUY Side Sorted LIFO | `order_book.py` | 57 | **HIGH** | Không |
| BUG-03 | Zombie Order — Duplicate ID Not Rejected | `order_book.py` | 49–53, 41–46 | **HIGH** | Không |
| BUG-04 | SELL Cancel Silent — WebSocket Not Notified | `web.py` | 110 | **MEDIUM** | Không |
| BUG-05 | Unauthenticated Reset Wipes Entire Book | `web.py` | 116–122 | **CRITICAL** | Không |
| BUG-06 | Any Client Can Cancel Any Order | `web.py` | 100–113 | **HIGH** | Không |
| BUG-07 | Set Mutation During Async Iteration (Broadcast Crash) | `web.py` | 36–43 | **HIGH** | Có |
| BUG-08 | Broadcast Reads State Outside Lock | `web.py` | 30–43 | **MEDIUM** | Có |
| BUG-09 | Auto order_id Collision in Same Millisecond | `web.py` | 83 | **MEDIUM** | Có |
| BUG-10 | No Quantity Validation — Negative/Zero Allowed | `web.py` | 85 | **MEDIUM** | Không |
| BUG-11 | CORS Wildcard on All Endpoints | `web.py` | 10–14 | **LOW** | Không |

---

---

## BUG-01 — Market Order Never Matches

### Mô tả chi tiết

Mọi `MARKET` order được submit vào hệ thống đều không thể khớp với bất kỳ lệnh nào trong sổ. Kết quả là 0 trade được tạo ra và market order bị drop silently.

**Code lỗi** (`order_book.py:60–62`):
```python
def _is_match(self, incoming: Order, resting: Order) -> bool:
    if incoming.order_type == OrderType.MARKET:
        return incoming.price is not None   # ← BUG ở đây
```

**Code tạo Market Order** (`service.py:39`):
```python
order = Order(
    order_id=order_id,
    side=Side(side),
    quantity=quantity,
    price=None,             # ← market order luôn có price=None
    timestamp=timestamp,
    order_type=OrderType.MARKET,
)
```

### Tại sao xảy ra

`incoming.price is not None` trả về `True` chỉ khi `price` không phải `None`. Nhưng mọi market order đều được khởi tạo với `price=None` trong `service.py`. Do đó biểu thức **luôn là `False`**, khiến vòng matching loop thoát ngay ở lần kiểm tra đầu tiên.

Logic đúng: market order không có giới hạn giá — nó khớp với **bất kỳ** resting order nào có sẵn. Phải trả về `True` vô điều kiện.

```python
# Fix:
if incoming.order_type == OrderType.MARKET:
    return True
```

### Kịch bản tái hiện

```
Bước 1: POST /api/orders
        { "side": "SELL", "quantity": 10, "price": 100.0, "order_type": "LIMIT" }
        → S1 resting trong sells book

Bước 2: POST /api/orders
        { "side": "BUY", "quantity": 5, "order_type": "MARKET" }
        → Gọi _is_match(incoming=MARKET_BUY, resting=S1)
        → incoming.price is not None  ≡  None is not None  ≡  False
        → Loop break ngay lập tức

Kết quả thực tế:   trades = []  |  S1 vẫn còn nguyên trong sổ
Kết quả kỳ vọng:   1 trade: qty=5, price=100.0
```

**Severity: CRITICAL** — Toàn bộ chức năng MARKET order bị vô hiệu hóa.

---

---

## BUG-02 — FIFO Violation: BUY Side Sorted LIFO

### Mô tả chi tiết

Khi nhiều BUY order cùng giá nằm trong sổ, lệnh đến **muộn hơn** (timestamp cao hơn) lại được khớp trước. Đây là vi phạm nguyên tắc Price-Time Priority (FIFO): cùng giá thì lệnh đến trước phải được fill trước.

**Code lỗi** (`order_book.py:57`):
```python
def _sort_books(self) -> None:
    self.buys.sort(
        key=lambda item: (item.price or 0.0, item.timestamp),
        reverse=True     # ← reverse=True áp dụng cho TOÀN BỘ tuple
    )
    self.sells.sort(key=lambda item: (item.price or 0.0, item.timestamp))
```

### Tại sao xảy ra

`reverse=True` đảo ngược **cả hai chiều** của key tuple `(price, timestamp)`:
- Price: cao hơn lên đầu ✓ (đúng — BUY tốt nhất là giá cao nhất)
- Timestamp: **cao hơn lên đầu** ✗ (sai — timestamp cao = đến sau = phải ưu tiên thấp hơn)

Kết quả: Với hai BUY cùng giá, lệnh đến **sau** (ts lớn hơn) đứng đầu sổ → được fill trước → LIFO thay vì FIFO.

SELL side không bị ảnh hưởng vì dùng ascending sort (không có `reverse=True`).

```python
# Fix — tách price và timestamp riêng:
self.buys.sort(key=lambda item: (-(item.price or 0.0), item.timestamp))
```

### Kịch bản tái hiện

```
Bước 1: Submit BUY LIMIT  B1, qty=5, price=100.0, timestamp=1
Bước 2: Submit BUY LIMIT  B2, qty=5, price=100.0, timestamp=2
        → sort key: B1=(100, 1), B2=(100, 2)
        → reverse=True: B2 > B1 → sổ = [B2, B1]   ← LIFO

Bước 3: Submit SELL LIMIT S1, qty=5, price=100.0, timestamp=3
        → match với book[0] = B2 (đến SAU)
        → Trade: S1 × B2, qty=5

Kết quả thực tế:   B2 được fill, B1 còn nguyên trong sổ
Kết quả kỳ vọng:   B1 được fill (đến TRƯỚC), B2 còn lại
```

**Severity: HIGH** — Vi phạm quy tắc cơ bản của exchange; có thể bị khai thác bởi trader biết bug.

---

---

## BUG-03 — Zombie Order: Duplicate Order ID Không Bị Reject

### Mô tả chi tiết

Hệ thống cho phép submit nhiều order có cùng `order_id`. Tất cả đều được chèn vào sổ. Khi `cancel()` được gọi, chỉ entry đầu tiên tìm thấy bị xóa — entry thứ hai tồn tại mãi như "zombie", tiếp tục chiếm thanh khoản và khớp với lệnh tương lai.

**Code lỗi — `_rest()` không kiểm tra uniqueness** (`order_book.py:49–53`):
```python
def _rest(self, order: Order) -> None:
    if order.side == Side.BUY:
        self.buys.append(order)   # ← append vô điều kiện
    else:
        self.sells.append(order)
    self._sort_books()
```

**Code lỗi — `cancel()` early return** (`order_book.py:41–46`):
```python
def cancel(self, order_id: str) -> bool:
    for book in (self.buys, self.sells):
        for index, order in enumerate(book):
            if order.order_id == order_id and order.remaining > 0:
                book.pop(index)
                return True    # ← thoát ngay, không xóa bản sao thứ hai
    return False
```

### Tại sao xảy ra

Hai lỗi thiết kế kết hợp:
1. `_rest()` thiếu kiểm tra `order_id` đã tồn tại trong sổ chưa
2. `cancel()` dùng `return True` ngay sau khi xóa entry đầu tiên — không tiếp tục tìm các entry trùng

### Kịch bản tái hiện

```
Bước 1: Submit BUY B1, qty=10, price=100.0, ts=1
        → buys = [B1(remaining=10)]

Bước 2: Submit BUY B1, qty=5, price=100.0, ts=2  ← cùng order_id!
        → _rest() append không check → buys = [B1(ts=2), B1(ts=1)]  (do BUG-02)

Bước 3: cancel("B1")
        → tìm thấy B1(ts=2) trước (do BUG-02 đặt ts=2 đầu), xóa nó
        → return True ← thoát sớm
        → buys = [B1(ts=1, remaining=10)]  ← ZOMBIE còn lại

Bước 4: Submit SELL S1, qty=10, price=100.0
        → khớp với zombie B1(ts=1)
        → Trade được tạo cho order đã bị "cancel"!
```

**Severity: HIGH** — Phantom liquidity; lệnh đã cancel vẫn có thể trade. Kết hợp với BUG-02 làm zombie càng khó phát hiện.

---

---

## BUG-04 — SELL Cancel Silent: WebSocket Clients Không Được Thông Báo

### Mô tả chi tiết

Khi một SELL order bị cancel thành công, các WebSocket client không nhận được event `order_cancelled`. Chỉ những order có `order_id` bắt đầu bằng ký tự `"B"` mới trigger broadcast. Mọi SELL order (`"S1"`, `"ORD-xxx"`, v.v.) bị cancel trong im lặng.

**Code lỗi** (`web.py:109–113`):
```python
if cancelled:
    if order_id.startswith("B"):          # ← chỉ broadcast cho "B"-prefix
        await hub.broadcast("order_cancelled")
    return web.json_response({"ok": True, "state": state})
```

### Tại sao xảy ra

Điều kiện `startswith("B")` trông như một guard hợp lệ (kiểm tra BUY side) nhưng thực chất không có lý do logic để phân biệt BUY/SELL khi broadcast cancel. Đây là lỗi thiếu nhánh hoặc điều kiện sai.

### Kịch bản tái hiện

```
Bước 1: Client A kết nối WebSocket → nhận state ban đầu
Bước 2: Submit SELL LIMIT S1, qty=10, price=100.0
        → Client A nhận event "order_placed", thấy S1 trong book
Bước 3: POST /api/cancel  {"order_id": "S1"}
        → HTTP response: {"ok": true}   ✓
        → WebSocket: KHÔNG có event nào được gửi   ✗
        → Client A vẫn hiển thị S1 đang active trong order book

Bước 4: Submit BUY B1 → Client A nhận state mới → S1 biến mất
        → Nhưng trong khoảng thời gian giữa Bước 3 và 4, UI hoàn toàn sai
```

**Severity: MEDIUM** — UI desync; trong môi trường live trading, khoảng thời gian stale state có thể gây ra quyết định giao dịch sai.

---

---

## BUG-05 — Unauthenticated Reset: Bất Kỳ Client Nào Xóa Được Toàn Bộ Sổ

### Mô tả chi tiết

Endpoint `POST /api/reset` xóa toàn bộ order book và trade history mà không yêu cầu bất kỳ xác thực nào. Bất kỳ HTTP client nào có thể reach port 8000 đều có quyền admin đầy đủ.

**Code lỗi** (`web.py:116–122`):
```python
async def reset_book(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    async with hub.lock:
        hub.service = MatchingEngineService()   # ← toàn bộ state bị hủy
        state = hub.state()
    await hub.broadcast("book_reset")
    return web.json_response({"ok": True, "state": state})
```

### Tại sao xảy ra

Không có middleware xác thực. Không có API key, session token, hay IP whitelist. Endpoint được đăng ký công khai:

```python
app.router.add_post("/api/reset", reset_book)   # web.py:54
```

### Kịch bản tái hiện

```bash
# Từ bất kỳ máy nào trong network:
curl -X POST http://127.0.0.1:8000/api/reset

# Response:
# {"ok": true, "state": {"book": {"buys": [], "sells": [], "trades": []}}}
# Toàn bộ lệnh, trade history bị xóa sạch.
```

**Tấn công DoS đơn giản:**
```bash
while true; do curl -sX POST http://target:8000/api/reset; done
```

**Severity: CRITICAL** — Disruption hoàn toàn; xóa sạch trade history không thể phục hồi.

---

---

## BUG-06 — No Ownership Check: Bất Kỳ Client Nào Cancel Được Lệnh Của Người Khác

### Mô tả chi tiết

`POST /api/cancel` không xác minh người gửi request có phải chủ sở hữu của order hay không. Chỉ cần biết (hoặc đoán) `order_id` là đủ để cancel lệnh của bất kỳ ai.

**Code lỗi** (`web.py:100–113`):
```python
async def cancel_order(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    payload = await request.json()
    order_id = payload["order_id"]    # ← lấy trực tiếp, không verify ownership

    async with hub.lock:
        cancelled = hub.service.cancel_order(order_id)
```

### Tại sao xảy ra

Không có hệ thống authentication/authorization nào được implement. User identity không được track ở bất kỳ đâu trong codebase.

### Kịch bản tái hiện

```
Trader A: Submit BUY LIMIT ORD-1700000000000, qty=1000, price=150.0
          (large order, market-moving)

Adversary: POST /api/cancel {"order_id": "ORD-1700000000000"}
           → Cancelled thành công
           → Trader A mất lệnh không hay biết

Market manipulation scenario:
1. Adversary biết Trader A đặt lệnh BUY lớn tại 150.0
2. Adversary cancel lệnh đó
3. Price không bị đẩy lên như kỳ vọng
4. Adversary mua tại giá thấp hơn

Auto-generated IDs (ORD-{timestamp_ms}) brute-forceable:
→ Kết hợp với BUG-09, ID có thể đoán được trong window 1ms
```

**Severity: HIGH** — Thao túng thị trường; có thể kết hợp với BUG-09 để brute-force IDs.

---

---

## BUG-07 — Broadcast Crash: Set Mutation During Async Iteration

### Mô tả chi tiết

Hàm `broadcast()` iterate qua `self.clients` (kiểu `set`) và `await` bên trong loop. Khi coroutine bị suspend tại `await`, event loop có thể chạy coroutine khác add/remove client khỏi `self.clients`. Mutation một `set` đang được iterate gây `RuntimeError: Set changed size during iteration`.

**Code lỗi** (`web.py:36–43`):
```python
async def broadcast(self, event: str, trades: list[dict] | None = None) -> None:
    payload = { ... }
    stale_clients: list[web.WebSocketResponse] = []
    for client in self.clients:              # ← iterate set
        if client.closed:
            stale_clients.append(client)
            continue
        await client.send_json(payload)      # ← yield tại đây → set có thể bị mutate
    for client in stale_clients:
        self.clients.discard(client)
```

**Nơi mutation xảy ra** (`web.py:129, 136`):
```python
async def websocket_handler(request):
    ...
    hub.clients.add(socket)      # ← add trong lúc broadcast đang chạy
    ...
    hub.clients.discard(socket)  # ← discard trong lúc broadcast đang chạy
```

### Tại sao xảy ra

Python asyncio là single-threaded nhưng cooperative — mỗi `await` là điểm switch. Khi `broadcast()` await tại `client.send_json()`, `websocket_handler()` của một client mới kết nối có thể resume và gọi `hub.clients.add()`.

```python
# Fix:
for client in list(self.clients):    # snapshot set trước khi iterate
```

### Kịch bản tái hiện

```
Setup: 50 WebSocket clients đang kết nối, order flow liên tục

T=0ms: Order được place → broadcast() bắt đầu iterate self.clients
T=1ms: broadcast() await send_json cho client[3] → yields
T=1ms: Client 51 kết nối → websocket_handler resume → hub.clients.add(client51)
T=1ms: broadcast() resume → self.clients đã thay đổi size

RuntimeError: Set changed size during iteration
→ broadcast() crash ở giữa chừng
→ Clients [4..50] không nhận được event
→ State diverge: clients [0..3] up-to-date, [4..50] stale
```

**Severity: HIGH** — Crash production service dưới load; khó reproduce trong single-user testing.

---

---

## BUG-08 — Race Condition: Broadcast Đọc State Không Có Lock

### Mô tả chi tiết

`place_order()` capture state bên trong lock, nhưng `broadcast()` đọc lại state một lần nữa bên ngoài lock. State gửi đến WebSocket clients có thể phản ánh state **khác** với trades vừa được announce.

**Flow lỗi** (`web.py:88–97`):
```python
async with hub.lock:
    trades = hub.service.place_limit_order(...)
    state = hub.state()        # state được capture TRONG lock

await hub.broadcast("order_placed", trades)   # ngoài lock
```

**Bên trong `broadcast()`** (`web.py:23–28`):
```python
def state(self) -> dict:
    book = self.service.get_order_book()   # đọc lại KHÔNG có lock
    return {"book": book, "active_orders": ...}

async def broadcast(self, event, trades=None):
    payload = {
        "event": event,
        "state": self.state(),   # ← gọi self.state() lại, không dùng captured state
        "trades": trades or [],
    }
```

Biến `state` được capture trong lock tại `web.py:94` chỉ được dùng trong HTTP response — không được truyền vào `broadcast()`.

### Tại sao xảy ra

`broadcast()` luôn gọi `self.state()` fresh thay vì nhận state làm parameter. Khoảng thời gian giữa `hub.lock` release và `broadcast()` execute đủ để request khác modify sổ.

### Kịch bản tái hiện

```
T=0ms: Request A: lock → place BUY B1 (fills S1) → state_A captured → unlock
T=1ms: Request B: lock → place SELL S2 (new order) → state_B captured → unlock
T=2ms: Request A: broadcast("order_placed", trades=[B1×S1])
       → self.state() đọc state HIỆN TẠI = state sau khi S2 đã vào sổ
       → Clients nhận: "Trade B1×S1" + book chứa S2
       → Nhưng S2 chưa được announce qua broadcast của B
       → Client thấy S2 xuất hiện "ma" không có event tương ứng
```

**Severity: MEDIUM** — State inconsistency trong WebSocket messages; cần concurrent requests để trigger.

---

---

## BUG-09 — Auto order_id Collision Trong Cùng Millisecond

### Mô tả chi tiết

Khi client không cung cấp `order_id`, server tự sinh dựa trên timestamp millisecond. Hai request trong cùng 1ms sẽ nhận cùng `order_id`. Kết hợp với BUG-03 (không dedup), cả hai order vào sổ với cùng ID — tạo zombie tự động mà không cần action nào từ phía attacker.

**Code lỗi** (`web.py:83`):
```python
order_id = payload.get("order_id") or f"ORD-{int(time.time() * 1000)}"
```

### Tại sao xảy ra

`int(time.time() * 1000)` có độ phân giải 1ms. Trên hardware hiện đại hoặc dưới load, nhiều request có thể arrive trong cùng 1ms. Không có sequence counter, UUID, hay entropy nguồn khác.

### Kịch bản tái hiện

```python
# Client gửi 2 orders đồng thời, không có order_id
import asyncio, aiohttp

async def send_orders():
    async with aiohttp.ClientSession() as s:
        t1 = s.post("/api/orders", json={"side":"BUY","quantity":5,"price":100.0,"order_type":"LIMIT"})
        t2 = s.post("/api/orders", json={"side":"BUY","quantity":5,"price":100.0,"order_type":"LIMIT"})
        r1, r2 = await asyncio.gather(t1, t2)

# Nếu cả 2 arrive trong cùng 1ms:
# → cả 2 nhận order_id = "ORD-1700000000000"
# → buys = [ORD-xxx(ts), ORD-xxx(ts)] ← duplicate, theo BUG-03
# → cancel("ORD-xxx") chỉ xóa 1 → zombie
```

**Severity: MEDIUM** — Rare trong development, phổ biến hơn dưới load; cần fix bằng UUID hoặc counter.

---

---

## BUG-10 — No Input Validation: Quantity Âm Hoặc Bằng 0

### Mô tả chi tiết

`quantity` được lấy trực tiếp từ JSON payload và cast sang `int` mà không kiểm tra bounds. Quantity âm phá vỡ invariant của matching loop và tạo trade records với quantity âm.

**Code lỗi** (`web.py:85`):
```python
quantity = int(payload["quantity"])   # không có lower-bound check
```

### Tại sao xảy ra

Thiếu input validation tại API boundary. Engine giả định quantity luôn dương nhưng không enforce invariant này.

### Kịch bản tái hiện — Quantity = 0

```
POST /api/orders { "side": "BUY", "quantity": 0, "price": 100, "order_type": "LIMIT" }
→ order.remaining = 0 ngay từ __post_init__
→ while order.remaining > 0: ← false ngay, loop không chạy
→ order.remaining > 0 → false → không rest vào sổ
→ Kết quả: silently no-op, không có error
```

### Kịch bản tái hiện — Quantity Âm

```
Với SELL S1 qty=10 đang resting:

POST /api/orders { "side": "BUY", "quantity": -3, "price": 100, "order_type": "LIMIT" }
→ order.remaining = -3
→ while order.remaining > 0: → false, loop không chạy
→ order.remaining (-3) > 0 → false → không rest
→ Kết quả: silently no-op

Tuy nhiên nếu logic kiểm tra được thay đổi trong tương lai:
matched_quantity = min(-3, 10) = -3
order.remaining  -= -3  → order.remaining = 0  (loop dừng)
best.remaining   -= -3  → best.remaining  = 13  (SELL tăng lên!)
Trade emitted với quantity=-3 ← corrupted record
```

**Severity: MEDIUM** — Không crash hiện tại nhưng tạo silently incorrect behavior; tiềm năng critical nếu code thay đổi.

---

---

## BUG-11 — CORS Wildcard trên Toàn Bộ Endpoints

### Mô tả chi tiết

Tất cả response trả về header `Access-Control-Allow-Origin: *`, cho phép bất kỳ web origin nào gửi cross-origin request đến API.

**Code** (`web.py:10–14`):
```python
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
```

### Tại sao xảy ra

Thường được set trong development để tránh CORS errors, và bị quên không restrict trước khi deploy.

### Kịch bản tái hiện

```javascript
// Trang web độc hại tại evil.com:
fetch("http://exchange.internal:8000/api/reset", { method: "POST" })
// → Browser gửi request, nhận response (wildcard CORS cho phép)
// → Kết hợp BUG-05: wipe entire book từ browser của victim

// Hoặc cancel lệnh của victim khi họ visit evil.com:
fetch("http://exchange.internal:8000/api/cancel", {
    method: "POST",
    body: JSON.stringify({order_id: "B1"}),
    headers: {"Content-Type": "application/json"}
})
```

**Severity: LOW** standalone, **HIGH** khi kết hợp với BUG-05 và BUG-06 trong browser context.

---

---

## Bug Interaction Map

```
BUG-01 (market never matches)
  └─ kết hợp BUG-02 (LIFO) → market order không clear được book dù book đầy
  └─ kết hợp BUG-03 (zombie) → zombie order không bị market order sweep

BUG-02 (LIFO sort)
  └─ kết hợp BUG-03 (zombie) → zombie là bản gốc (ts nhỏ), bản duplicate bị cancel trước
  └─ kết hợp BUG-03 + BUG-09 (ID collision) → auto zombie dưới load, không cần action của attacker

BUG-05 (unauthenticated reset)
  └─ kết hợp BUG-11 (CORS *) → có thể trigger từ browser của victim
BUG-06 (no ownership check)
  └─ kết hợp BUG-09 (ID collision) → guessable auto-IDs làm cancel dễ hơn
  └─ kết hợp BUG-11 (CORS *) → cross-site request forgery cancel
```

---

## Prioritized Fix Order

```
1. BUG-01  → 1-line fix, unblocks toàn bộ MARKET order flow
2. BUG-05  → thêm auth middleware, ngăn DoS admin
3. BUG-06  → thêm ownership verification trên cancel
4. BUG-02  → fix sort key: -(price) thay vì reverse=True
5. BUG-07  → list(self.clients) snapshot trước khi iterate
6. BUG-03  → thêm uniqueness check trong _rest()
7. BUG-04  → bỏ điều kiện startswith("B")
8. BUG-08  → truyền captured state vào broadcast()
9. BUG-10  → validate quantity > 0 tại API boundary
10. BUG-09 → dùng UUID hoặc atomic counter cho auto order_id
11. BUG-11 → restrict CORS origin về frontend domain cụ thể
```
