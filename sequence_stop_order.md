# Stop Order — Sequence Diagrams

Bốn flow chính của tính năng stop order (STOP_MARKET / STOP_LIMIT):

1. **Submit stop — chưa trigger** (pending vào `stop_orders`)
2. **Trade thường trigger cascade** (giá vượt `stop_price` → fire)
3. **Immediate trigger at submit** (`last_price` đã vượt ngưỡng ngay lúc submit)
4. **Cancel pending stop**

---

## 1. Submit Stop Order — Chưa Trigger (Pending)

Client gửi stop order khi `last_price` chưa chạm `stop_price`. Order được parked vào `stop_orders`.

```mermaid
sequenceDiagram
    actor Client
    participant Web as web.py<br/>place_order
    participant Hub as EngineHub<br/>(asyncio.Lock)
    participant Svc as MatchingEngineService
    participant Book as OrderBook
    participant WS as WS Clients

    Client->>Web: POST /api/orders<br/>{side:BUY, qty:5, stop_price:105,<br/>order_type:STOP_MARKET}
    Web->>Web: validate side / qty / order_type /<br/>stop_price > 0
    Web->>Hub: async with hub.lock
    activate Hub
    Hub->>Svc: place_stop_order(id,side,qty,stop_price,ts)
    Svc->>Svc: guard: qty>0, stop_price>0
    Svc->>Book: submit(Order[STOP_MARKET])

    activate Book
    Book->>Book: _stop_triggered(order, last_price)
    Note over Book: last_price (100) < stop_price (105)<br/>→ False
    Book->>Book: dedup check by order_id
    Book->>Book: stop_orders.append(order)
    Book-->>Svc: []
    deactivate Book

    Svc-->>Hub: trades = []
    Hub->>Hub: state = snapshot()<br/>(stops now includes new order)
    deactivate Hub

    Web->>WS: broadcast("order_placed", [], state)
    WS-->>Client: event: order_placed<br/>state.stops = [SM1 pending]
    Web-->>Client: 201 {ok:true, trades:[], state}
```

---

## 2. Trade Thường Trigger Cascade

Một trade LIMIT/MARKET bình thường cập nhật `last_price`. Sau khi match xong, engine lặp kiểm tra mọi pending stop — stop nào cross ngưỡng thì được convert và match như MARKET/LIMIT. Trade mới có thể tiếp tục trigger stop tiếp theo → cascade cho đến stable.

```mermaid
sequenceDiagram
    actor Client
    participant Web as web.py<br/>place_order
    participant Hub as EngineHub
    participant Book as OrderBook
    participant WS as WS Clients

    Note over Book: Pre-state:<br/>last_price = 100<br/>stop_orders = [ SM_A BUY stop=105,<br/>                SM_B BUY stop=110 ]<br/>sells = [ S1@105 qty=5,<br/>          S2@110 qty=5,<br/>          S3@115 qty=100 ]

    Client->>Web: POST /api/orders<br/>BUY B1 qty=5 @105 (LIMIT)
    Web->>Hub: async with hub.lock
    activate Hub
    Hub->>Book: submit(B1 as LIMIT)

    activate Book
    Book->>Book: _match(B1)
    Note over Book: B1 × S1 @105, qty=5<br/>last_price ← 105

    Book->>Book: _process_triggered_stops() — round 1
    Note over Book: eligible = [SM_A]<br/>(last_price 105 ≥ stop 105)
    Book->>Book: stop_orders.remove(SM_A)
    Book->>Book: _convert_stop(SM_A) → MARKET
    Book->>Book: _match(SM_A as MARKET)
    Note over Book: SM_A × S2 @110, qty=5<br/>last_price ← 110

    Book->>Book: _process_triggered_stops() — round 2
    Note over Book: eligible = [SM_B]<br/>(last_price 110 ≥ stop 110)
    Book->>Book: stop_orders.remove(SM_B)
    Book->>Book: _convert_stop(SM_B) → MARKET
    Book->>Book: _match(SM_B as MARKET)
    Note over Book: SM_B × S3 @115, qty=5<br/>last_price ← 115

    Book->>Book: _process_triggered_stops() — round 3
    Note over Book: eligible = [] → exit loop

    Book-->>Hub: [trade_B1, trade_SM_A, trade_SM_B]
    deactivate Book

    Hub->>Hub: state = snapshot()
    deactivate Hub

    Web->>WS: broadcast("order_placed", 3 trades, state)
    WS-->>Client: 3 trades + updated state<br/>(stops = [], last_price=115)
    Web-->>Client: 201 {ok:true, trades:[3]}
```

---

## 3. Immediate Trigger On Submit

Khi submit stop mà `last_price` đã cross ngưỡng rồi, stop fire ngay trong chính request đó — không parked vào `stop_orders`.

```mermaid
sequenceDiagram
    actor Client
    participant Web as web.py<br/>place_order
    participant Hub as EngineHub
    participant Svc as MatchingEngineService
    participant Book as OrderBook

    Note over Book: Pre-state:<br/>last_price = 110<br/>sells = [ S1@111 qty=5 ]

    Client->>Web: POST /api/orders<br/>BUY STOP_MARKET stop=105 qty=5
    Web->>Hub: async with hub.lock
    activate Hub
    Hub->>Svc: place_stop_order(...)
    Svc->>Book: submit(Order[STOP_MARKET stop=105])

    activate Book
    Book->>Book: _stop_triggered(order, last_price=110)
    Note over Book: 110 ≥ 105 → True<br/>(fire immediately)
    Book->>Book: _convert_stop(order)<br/>STOP_MARKET → MARKET
    Book->>Book: _match(order as MARKET)
    Note over Book: order × S1 @111, qty=5<br/>last_price ← 111

    Book->>Book: _process_triggered_stops()
    Note over Book: no other pending stops → return []

    Book-->>Svc: [trade]
    deactivate Book

    Svc-->>Hub: 1 trade (as dict)
    deactivate Hub

    Web-->>Client: 201 {ok:true, trades:[1],<br/>stops=[], last_price=111}
```

---

## 4. Cancel Pending Stop

`cancel()` tìm `order_id` ở cả `buys`, `sells`, và `stop_orders`. Pending stop bị remove trước khi có thể fire.

```mermaid
sequenceDiagram
    actor Client
    participant Web as web.py<br/>cancel_order
    participant Hub as EngineHub
    participant Svc as MatchingEngineService
    participant Book as OrderBook
    participant WS as WS Clients

    Note over Book: stop_orders = [SM1 BUY stop=200 qty=5]

    Client->>Web: POST /api/cancel<br/>{order_id:"SM1"}
    Web->>Web: validate order_id present
    Web->>Hub: async with hub.lock
    activate Hub
    Hub->>Svc: cancel_order("SM1")
    Svc->>Book: cancel("SM1")

    activate Book
    Book->>Book: scan buys for SM1 → none
    Book->>Book: scan sells for SM1 → none
    Book->>Book: scan stop_orders for SM1 → found
    Book->>Book: stop_orders.pop(index)
    Book-->>Svc: True
    deactivate Book

    Svc-->>Hub: True
    Hub->>Hub: state = snapshot()
    deactivate Hub

    Web->>WS: broadcast("order_cancelled", state)
    WS-->>Client: event: order_cancelled<br/>stops = []
    Web-->>Client: 200 {ok:true, state}

    Note over Book: Future trades crossing stop=200<br/>will NOT fire SM1 (đã bị remove)
```

---

## Invariants Được Bảo Vệ Qua Các Flow

| Invariant | Đảm bảo bởi |
|-----------|-------------|
| Stop chỉ fire khi `last_price` cross ngưỡng (inclusive) | `_stop_triggered` với `>=` / `<=` |
| Cascade chạy đến stable — không mất trigger | `while eligible: ...` trong `_process_triggered_stops` |
| Cùng trigger moment → FIFO theo timestamp | `eligible.sort(key=lambda s: s.timestamp)` |
| Stop chưa trigger không xuất hiện trong sổ matching | Parked ở `stop_orders` riêng biệt |
| Cancel loại bỏ stop khỏi cả 3 containers | `cancel()` scan buys + sells + stop_orders |
| Atomic vs. concurrent requests | `async with hub.lock` bao quanh mọi mutation |
| WS client thấy state đúng sau cascade | Broadcast dùng state snapshot đã chụp **trong** lock |
