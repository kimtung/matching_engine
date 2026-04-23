# Cancel Pending Order — Module Interaction Sequence

Sequence diagram tả lại toàn bộ đường đi của một request cancel, từ click UI đến re-render realtime, có đủ các nhánh: hủy LIMIT resting, hủy STOP pending, và case not-found.

> **Quy ước đặt tên module (trong tất cả diagram)**
>
> | Tên trong diagram | Vai trò | Thực tế trong codebase |
> |-------------------|---------|------------------------|
> | **Trader** | Người dùng cuối click UI | - |
> | **Web UI** | Giao diện trình duyệt | `frontend/static/app.js` |
> | **HTTP API** | Cổng nhận request, validate, trả response | `matching_engine/web.py` handlers |
> | **Khoá Engine** | Serialize truy cập engine (mutex) | `hub.lock` (asyncio.Lock) |
> | **Điều phối Engine** | Giữ service + clients + broadcast | `EngineHub` |
> | **Tầng Dịch vụ** | API domain (place/cancel/query) | `MatchingEngineService` |
> | **Sổ Lệnh** | Core matching state | `OrderBook` |
> | **Bộ Phát WS** | Fan-out message cho mọi client | `hub.broadcast()` |
> | **Các Trader Khác** | WS subscribers khác | connected `WebSocketResponse` |
> | **Sổ Mua / Bán** | Hai phía của LIMIT book | `self.buys`, `self.sells` |
> | **Hàng Chờ Stop** | Nơi parked các stop chưa trigger | `self.stop_orders` |

---

## 1. Happy Path Đầy Đủ (End-to-end All Modules)

```mermaid
sequenceDiagram
    actor Trader
    participant UI as Web UI<br/>(trình duyệt)
    participant API as HTTP API<br/>(/api/cancel)
    participant Guard as Khoá Engine<br/>(asyncio.Lock)
    participant Hub as Điều phối Engine
    participant Svc as Tầng Dịch vụ<br/>(Matching)
    participant Book as Sổ Lệnh
    participant WSBus as Bộ Phát WS
    participant Peers as Các Trader Khác<br/>(WS subscribers)

    Trader->>UI: Click "Cancel" trên row SM1
    UI->>UI: handleCancelClick(event)<br/>orderId = target.dataset.cancel
    UI->>API: fetch POST /api/cancel<br/>{"order_id":"SM1"}

    API->>API: payload = await request.json()
    alt JSON malformed
        API-->>UI: 400 {ok:false, error:"invalid JSON body"}
        UI-->>Trader: flash "error"
    end
    API->>API: order_id = payload.get("order_id")
    alt order_id thiếu / empty
        API-->>UI: 400 {ok:false, error:"order_id is required"}
        UI-->>Trader: flash "error"
    end

    API->>Guard: async with hub.lock
    activate Guard
    API->>Hub: service.cancel_order("SM1")
    Hub->>Svc: cancel_order("SM1")
    Svc->>Book: cancel("SM1")

    activate Book
    Note over Book: Rà cả 3 chỗ order có thể nằm:<br/>Sổ Mua · Sổ Bán · Hàng Chờ Stop
    Book->>Book: quét Sổ Mua theo order_id
    Book->>Book: (không thấy trong Sổ Mua)
    Book->>Book: quét Sổ Bán
    Book->>Book: (không thấy trong Sổ Bán)
    Book->>Book: quét Hàng Chờ Stop
    Note over Book: Tìm thấy SM1 trong Hàng Chờ Stop
    Book->>Book: pop SM1 (reverse-index)
    Book-->>Svc: True
    deactivate Book

    Svc-->>Hub: True
    Hub->>Hub: state = snapshot()<br/>(sổ + active_orders + stops + last_price)
    Note over Guard,Hub: State chụp NGAY trong lock<br/>→ broadcast sau khi release vẫn consistent
    deactivate Guard

    API->>WSBus: await broadcast("order_cancelled", state=state)
    activate WSBus
    loop với mỗi WS client đang kết nối
        WSBus->>Peers: send_json({event, state, trades:[]})
        Peers->>Peers: socket.onmessage → renderBook(state)
    end
    deactivate WSBus

    API-->>UI: 200 {ok:true, state}
    UI->>UI: data.ok === true → renderBook
    UI-->>Trader: flash "Cancelled SM1" (success)

    Note over UI,Peers: UI của chính Trader nhận update 2 kênh<br/>(HTTP response + WS broadcast)<br/>— renderBook idempotent nên an toàn
```

---

## 2. Nhánh Not-Found (Order Không Tồn Tại / Đã Filled / Đã Cancel Trước Đó)

```mermaid
sequenceDiagram
    actor Trader
    participant UI as Web UI
    participant API as HTTP API<br/>(/api/cancel)
    participant Guard as Khoá Engine
    participant Svc as Tầng Dịch vụ
    participant Book as Sổ Lệnh

    Trader->>UI: Click "Cancel" trên row đã stale
    UI->>API: POST /api/cancel<br/>{"order_id":"GHOST"}

    API->>Guard: async with hub.lock
    activate Guard
    API->>Svc: cancel_order("GHOST")
    Svc->>Book: cancel("GHOST")

    activate Book
    Book->>Book: quét Sổ Mua → rỗng
    Book->>Book: quét Sổ Bán → rỗng
    Book->>Book: quét Hàng Chờ Stop → rỗng
    Book-->>Svc: False
    deactivate Book

    Svc-->>API: False
    API->>API: state = snapshot() (không đổi)
    deactivate Guard

    Note over API: KHÔNG broadcast — state không thay đổi
    API-->>UI: 404 {ok:false, state}
    UI-->>Trader: flash "GHOST not found" (error)
```

---

## 3. Bên Trong Sổ Lệnh — Quét 3 Container

Zoom vào `OrderBook.cancel()` để thấy vì sao một API duy nhất cover được cả LIMIT resting, triggered LIMIT sau cascade, và STOP pending:

```mermaid
sequenceDiagram
    participant Caller as Tầng Dịch vụ<br/>(cancel_order)
    participant Book as Sổ Lệnh
    participant Buys as Sổ Mua
    participant Sells as Sổ Bán
    participant Stops as Hàng Chờ Stop

    Caller->>Book: cancel("SM1")
    activate Book

    Book->>Book: found = False

    Book->>Buys: indices = tìm order_id=="SM1"<br/>và remaining>0
    alt indices khác rỗng
        Book->>Buys: pop từng entry (reverse)
        Book->>Book: found = True
    end

    Book->>Sells: indices = tìm trong Sổ Bán
    alt indices khác rỗng
        Book->>Sells: pop reverse
        Book->>Book: found = True
    end

    Book->>Stops: stop_indices = tìm trong Hàng Chờ Stop
    alt stop_indices khác rỗng
        Book->>Stops: pop reverse
        Book->>Book: found = True
    end

    Book-->>Caller: found (True/False)
    deactivate Book

    Note over Book: Reverse-pop là key:<br/>xóa từ cuối lên tránh shift index<br/>khi có nhiều entries cùng ID
```

---

## 4. Race Với Place Order — Vì Sao Cần Khoá Engine

Minh họa lý do phải có mutex: nếu `place_order` đang cascade trigger stops mà `cancel` đọc state giữa chừng, có thể cancel một order đang được match → zombie. Khoá Engine loại bỏ khả năng này.

```mermaid
sequenceDiagram
    actor A as Trader A
    actor B as Trader B
    participant ApiA as HTTP API<br/>(request của A)
    participant ApiB as HTTP API<br/>(request của B)
    participant Guard as Khoá Engine
    participant Book as Sổ Lệnh

    par đồng thời
        A->>ApiA: POST /api/orders<br/>BUY 5 @ 100
    and
        B->>ApiB: POST /api/cancel<br/>{order_id:"SM1"}
    end

    ApiA->>Guard: async with hub.lock
    Note over Guard: A giành khoá trước
    activate Guard
    ApiA->>Book: submit(order)
    Book->>Book: match → update last_price<br/>→ cascade trigger stops → ...
    Book-->>ApiA: trades
    ApiA->>ApiA: state = snapshot()
    deactivate Guard

    ApiB->>Guard: async with hub.lock
    Note over Guard: B bị chặn đến khi A release
    activate Guard
    ApiB->>Book: cancel("SM1")
    Note over Book: SM1 có thể đã bị fire bởi cascade của A<br/>→ không còn trong Hàng Chờ Stop<br/>→ cancel trả False
    Book-->>ApiB: True / False (tùy state sau khi A xong)
    deactivate Guard

    ApiB-->>B: response phản ánh state THỰC TẾ<br/>(không phải stale pre-A)
```

---

## Invariants Được Bảo Vệ

| Invariant | Đảm bảo bởi |
|-----------|-------------|
| Cancel là atomic với place/match | `async with hub.lock` bao cả scan + pop + state snapshot |
| Cancel cover mọi nơi order có thể ở | Sổ Lệnh quét Sổ Mua + Sổ Bán + Hàng Chờ Stop |
| Nhiều entries cùng ID không leak zombie | Reverse-pop toàn bộ indices (BUG-03 fix) |
| Broadcast không gửi state sai sau cancel failed | `if cancelled: broadcast(...)` — bỏ qua khi không đổi |
| Frontend không out-of-sync với backend | Response có `state` field + WS broadcast đồng thời (cùng `renderBook`) |
| Không có race "cancel sau khi đã fill" | State chỉ mutate trong lock, scan cũng trong lock → thấy đúng state thời điểm cancel |

---

## Mapping Module → File

| Tên trong diagram | File / Symbol |
|-------------------|----------------|
| Web UI (trình duyệt) | `frontend/static/app.js` → `handleCancelClick` |
| HTTP API (/api/cancel) | `matching_engine/web.py` → `cancel_order` |
| Khoá Engine | `matching_engine/web.py` → `EngineHub.lock` (asyncio.Lock) |
| Điều phối Engine | `matching_engine/web.py` → `EngineHub` |
| Tầng Dịch vụ | `matching_engine/service.py` → `MatchingEngineService.cancel_order` |
| Sổ Lệnh | `matching_engine/order_book.py` → `OrderBook.cancel` |
| Sổ Mua / Sổ Bán / Hàng Chờ Stop | `OrderBook.buys` / `sells` / `stop_orders` |
| Bộ Phát WS | `matching_engine/web.py` → `EngineHub.broadcast` |
| Các Trader Khác | connected `aiohttp.web.WebSocketResponse` trong `hub.clients` |
