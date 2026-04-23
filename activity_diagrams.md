# Activity Diagrams — Matching Engine Project

Các activity diagram ở dạng Mermaid mô tả workflow & control flow của các tính năng chính.

> **Quy ước ký hiệu**
> - `([...])` — điểm bắt đầu / kết thúc
> - `[...]` — action / step
> - `{...}` — decision point
> - `-->|nhãn|` — nhánh có điều kiện

---

## 1. Place Order — End-to-End Flow

Từ `POST /api/orders` đến broadcast WS. Bao phủ tất cả 4 order_type (LIMIT / MARKET / STOP_MARKET / STOP_LIMIT) cùng mọi validation early-return.

```mermaid
flowchart TD
    Start([Client POST /api/orders]) --> ParseJSON[Đọc JSON payload]
    ParseJSON --> JSONOk{JSON hợp lệ?}
    JSONOk -->|no| Err400Json[Response 400<br/>invalid JSON body]
    JSONOk -->|yes| ValSide[Lấy side từ payload]

    ValSide --> SideOk{side ∈<br/>BUY / SELL?}
    SideOk -->|no| Err400Side[400 side must be BUY or SELL]
    SideOk -->|yes| ValQty[Parse quantity = int]

    ValQty --> QtyOk{quantity > 0<br/>và parse được?}
    QtyOk -->|no| Err400Qty[400 quantity must be positive integer]
    QtyOk -->|yes| ValType[order_type = payload.get order_type, LIMIT]

    ValType --> TypeOk{order_type ∈<br/>LIMIT / MARKET /<br/>STOP_MARKET / STOP_LIMIT?}
    TypeOk -->|no| Err400Type[400 order_type invalid]
    TypeOk -->|yes| GenId[Gen order_id nếu client không cho<br/>UUID hex 12 ký tự]

    GenId --> NeedPrice{Loại cần price?<br/>LIMIT hoặc STOP_LIMIT}
    NeedPrice -->|yes| ParsePrice[Parse price = float]
    NeedPrice -->|no| CheckStop{Loại cần stop_price?<br/>STOP_MARKET hoặc STOP_LIMIT}

    ParsePrice --> PriceOk{price > 0<br/>và parse được?}
    PriceOk -->|no| Err400Price[400 price must be positive]
    PriceOk -->|yes| CheckStop

    CheckStop -->|yes| ParseStop[Parse stop_price = float]
    CheckStop -->|no| AcqLock[async with hub.lock]

    ParseStop --> StopOk{stop_price > 0<br/>và parse được?}
    StopOk -->|no| Err400Stop[400 stop_price must be positive]
    StopOk -->|yes| AcqLock

    AcqLock --> Dispatch{Dispatch theo type}
    Dispatch -->|MARKET| CallMkt[service.place_market_order]
    Dispatch -->|LIMIT| CallLim[service.place_limit_order]
    Dispatch -->|STOP_MARKET| CallStopM[service.place_stop_order<br/>limit_price=None]
    Dispatch -->|STOP_LIMIT| CallStopL[service.place_stop_order<br/>limit_price=price]

    CallMkt --> Snap[state = hub.state]
    CallLim --> Snap
    CallStopM --> Snap
    CallStopL --> Snap

    Snap --> RelLock[Release lock]
    RelLock --> Broadcast[await hub.broadcast<br/>order_placed, trades, state]
    Broadcast --> Resp201[Response 201<br/>ok:true, trades, state]
    Resp201 --> End([End])

    Err400Json --> End
    Err400Side --> End
    Err400Qty --> End
    Err400Type --> End
    Err400Price --> End
    Err400Stop --> End
```

---

## 2. OrderBook.submit — Core Matching + Cascade

Bên trong `OrderBook.submit()`: dispatch stop vs. non-stop, match loop, rest logic, và cascade trigger pending stops.

```mermaid
flowchart TD
    Start([submit order]) --> IsStop{order.order_type<br/>∈ STOP_MARKET / STOP_LIMIT?}

    IsStop -->|yes| CheckTrigger{_stop_triggered<br/>order, last_price?}
    CheckTrigger -->|False<br/>chưa cross ngưỡng| DedupStop{order_id đã có<br/>trong stop_orders?}
    DedupStop -->|yes| RetEmpty[return empty list]
    DedupStop -->|no| Park[stop_orders.append order]
    Park --> RetEmpty

    CheckTrigger -->|True<br/>đã cross| Convert[_convert_stop:<br/>STOP_MARKET→MARKET<br/>STOP_LIMIT→LIMIT]
    Convert --> PrepMatch

    IsStop -->|no| PrepMatch[Chọn opposite book<br/>buy→sells, sell→buys]

    PrepMatch --> SortBooks[_sort_books]
    SortBooks --> MatchLoop{order.remaining > 0<br/>AND book rỗng?}

    MatchLoop -->|rỗng hoặc remaining=0| AfterLoop[Kết thúc match loop]
    MatchLoop -->|còn| PickBest[best = book 0]
    PickBest --> IsMatch{_is_match<br/>incoming, best?}
    IsMatch -->|no<br/>giá không đủ| AfterLoop
    IsMatch -->|yes| MakeTrade[qty = min remaining,<br/>price = best.price<br/>Tạo Trade]

    MakeTrade --> UpdateLP[last_price = trade.price<br/>append vào trades deque]
    UpdateLP --> DecQty[order.remaining -= qty<br/>best.remaining -= qty]
    DecQty --> BestDone{best.remaining == 0?}
    BestDone -->|yes| PopBest[book.pop 0]
    BestDone -->|no| MatchLoop
    PopBest --> MatchLoop

    AfterLoop --> RemainLim{remaining > 0<br/>AND order_type == LIMIT?}
    RemainLim -->|yes| RestIt[_rest: dedup check<br/>→ append vào book đúng phía]
    RemainLim -->|no| Cascade

    RestIt --> Cascade[_process_triggered_stops]

    Cascade --> FindElig[eligible = mọi stop có<br/>_stop_triggered True]
    FindElig --> HasElig{eligible rỗng?}
    HasElig -->|yes<br/>không còn gì fire| Return[return trades]
    HasElig -->|no| SortElig[Sort eligible theo timestamp<br/>FIFO fairness]
    SortElig --> FireNext[for stop in eligible:<br/>remove khỏi stop_orders<br/>_convert_stop<br/>_match stop]
    FireNext --> FindElig

    Return --> End([End])
    RetEmpty --> End
```

---

## 3. Cancel Order Flow

Flow của `POST /api/cancel` và bên trong `OrderBook.cancel()`.

```mermaid
flowchart TD
    Start([Client POST /api/cancel]) --> ParseJson[Đọc JSON payload]
    ParseJson --> JsonOk{JSON hợp lệ?}
    JsonOk -->|no| Err400J[400 invalid JSON body]
    JsonOk -->|yes| GetId[order_id = payload.get order_id]

    GetId --> IdOk{order_id có giá trị?}
    IdOk -->|no| Err400Id[400 order_id is required]
    IdOk -->|yes| Lock[async with hub.lock]

    Lock --> InvokeCancel[service.cancel_order id]
    InvokeCancel --> ScanBuys[Quét Sổ Mua:<br/>indices matching order_id<br/>và remaining > 0]
    ScanBuys --> BuysHit{Có index nào?}
    BuysHit -->|yes| PopBuys[Reverse-pop khỏi Sổ Mua<br/>found = True]
    BuysHit -->|no| ScanSells
    PopBuys --> ScanSells[Quét Sổ Bán]

    ScanSells --> SellsHit{Có index nào?}
    SellsHit -->|yes| PopSells[Reverse-pop khỏi Sổ Bán<br/>found = True]
    SellsHit -->|no| ScanStops
    PopSells --> ScanStops[Quét Hàng Chờ Stop]

    ScanStops --> StopsHit{Có index nào?}
    StopsHit -->|yes| PopStops[Reverse-pop khỏi Hàng Chờ Stop<br/>found = True]
    StopsHit -->|no| TakeSnap
    PopStops --> TakeSnap[state = hub.state]

    TakeSnap --> Release[Release lock]
    Release --> WasCancelled{cancelled == True?}

    WasCancelled -->|yes| BroadcastCxl[broadcast order_cancelled, state]
    BroadcastCxl --> Resp200[200 ok:true, state]
    WasCancelled -->|no| Resp404[404 ok:false, state<br/>KHÔNG broadcast]

    Resp200 --> End([End])
    Resp404 --> End
    Err400J --> End
    Err400Id --> End
```

---

## 4. Stop Order Lifecycle (State Diagram)

Vòng đời một stop order từ lúc được submit đến khi kết thúc.

```mermaid
stateDiagram-v2
    [*] --> Submitted

    Submitted --> Pending: last_price chưa cross<br/>stop_price<br/>(hoặc last_price = None)
    Submitted --> Triggered: last_price đã cross<br/>tại thời điểm submit<br/>(fire ngay)

    Pending --> Triggered: trade sau đó đẩy<br/>last_price qua ngưỡng<br/>(cascade)
    Pending --> Cancelled: cancel API

    Triggered --> Matched: STOP_MARKET<br/>match hết<br/>HOẶC STOP_LIMIT<br/>match tại limit_price
    Triggered --> Resting: STOP_LIMIT<br/>không match hết<br/>→ rest ở Sổ Mua/Bán
    Triggered --> Dropped: STOP_MARKET<br/>không còn liquidity<br/>(không rest theo semantics)

    Resting --> Matched: trade khớp<br/>ở lần submit sau
    Resting --> Cancelled: cancel API
    Resting --> PartiallyFilled: match một phần
    PartiallyFilled --> Matched: phần còn lại khớp
    PartiallyFilled --> Cancelled: cancel API

    Matched --> [*]
    Cancelled --> [*]
    Dropped --> [*]
```

---

## 5. WebSocket Connection Handler

Flow của `/ws` upgrade request, từ client connect đến disconnect.

```mermaid
flowchart TD
    Start([Client mở ws:/ws]) --> Prep[socket = WebSocketResponse heartbeat=20<br/>await socket.prepare request]

    Prep --> Lock[async with hub.lock]
    Lock --> Snap[state = hub.state]
    Snap --> SendInit[await socket.send_json<br/>event:connected, state, trades:empty]
    SendInit --> AddMember[hub.clients.add socket]
    AddMember --> Release[Release lock]

    Release --> Listen[async for message in socket]
    Listen --> MsgType{message.type?}
    MsgType -->|ERROR| BreakLoop[break loop]
    MsgType -->|khác| Listen

    BreakLoop --> Cleanup[hub.clients.discard socket]
    Listen -->|client disconnect| Cleanup

    Cleanup --> End([Close WS])

    Note1[Giữ lock suốt: state capture +<br/>send_json initial + clients.add<br/>→ không race với broadcast<br/>→ connected luôn là msg đầu tiên]
    SendInit -.- Note1
```

---

## 6. Broadcast Activity (Fan-out)

Bên trong `EngineHub.broadcast()`.

```mermaid
flowchart TD
    Start([await broadcast event, trades, state]) --> BuildPayload[payload = event, state, trades]

    BuildPayload --> InitStale[stale_clients = empty list]
    InitStale --> Iterate[for client in list hub.clients<br/>snapshot bằng list để không mutate khi await]

    Iterate --> IsClosed{client.closed?}
    IsClosed -->|yes| MarkStale[stale_clients.append client]
    MarkStale --> NextClient
    IsClosed -->|no| Send[await client.send_json payload]

    Send --> NextClient{Còn client trong snapshot?}
    NextClient -->|yes| Iterate
    NextClient -->|no| Cleanup[for s in stale_clients:<br/>hub.clients.discard s]

    Cleanup --> End([return])
```

---

## 7. Reset Book Flow

`POST /api/reset` (admin-gated sau BUG-13 fix).

```mermaid
flowchart TD
    Start([Client POST /api/reset]) --> CheckEnv{ADMIN_TOKEN<br/>đã set?}
    CheckEnv -->|no| Err403[403 reset is disabled]
    CheckEnv -->|yes| ReadHeader[token = request.headers X-Admin-Token]

    ReadHeader --> Compare{token == ADMIN_TOKEN?}
    Compare -->|no| Err401[401 unauthorized]
    Compare -->|yes| Lock[async with hub.lock]

    Lock --> Swap[hub.service = MatchingEngineService fresh instance]
    Swap --> Snap[state = hub.state]
    Snap --> Release[Release lock]
    Release --> Broadcast[broadcast book_reset, state]
    Broadcast --> Resp200[200 ok:true, state]

    Resp200 --> End([End])
    Err403 --> End
    Err401 --> End
```

---

## 8. Decision Matrix — Order Type × Validation

Tóm tắt nhanh fields nào cần cho loại nào (activity-level checklist):

```mermaid
flowchart LR
    T{order_type}
    T -->|LIMIT| L[side + qty + price]
    T -->|MARKET| M[side + qty]
    T -->|STOP_MARKET| SM[side + qty + stop_price]
    T -->|STOP_LIMIT| SL[side + qty + price + stop_price]

    L --> OK([Pass to service])
    M --> OK
    SM --> OK
    SL --> OK
```

---

## Mapping Activity → Source

| Activity Diagram | File / Hàm |
|------------------|-------------|
| Place Order (1) | `matching_engine/web.py:place_order` |
| OrderBook.submit (2) | `matching_engine/order_book.py:submit` + `_match` + `_process_triggered_stops` |
| Cancel Order (3) | `matching_engine/web.py:cancel_order` + `order_book.py:cancel` |
| Stop Lifecycle (4) | `order_book.py` stop-handling branches |
| WS Handler (5) | `matching_engine/web.py:websocket_handler` |
| Broadcast (6) | `matching_engine/web.py:EngineHub.broadcast` |
| Reset Book (7) | `matching_engine/web.py:reset_book` |
| Order Type Matrix (8) | `matching_engine/web.py:place_order` validation block |
