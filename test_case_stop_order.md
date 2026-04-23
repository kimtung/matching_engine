# Test Cases — Stop Orders

## Tổng quan tính năng

Stop order là lệnh có **điều kiện kích hoạt**: lệnh được "ngủ" (pending) cho đến khi giá thị trường chạm đến `stop_price`. Sau khi kích hoạt, lệnh tự động chuyển thành lệnh MARKET hoặc LIMIT và vào sổ bình thường.

### Hai biến thể

| Loại | Sau khi kích hoạt | Tham số required | Ghi chú |
|------|-------------------|------------------|---------|
| `STOP_MARKET` | Trở thành `MARKET` order | `stop_price` | Lấy bất kỳ giá có sẵn ở sổ đối diện |
| `STOP_LIMIT` | Trở thành `LIMIT` order tại `price` | `stop_price`, `price` | Rest nếu không match được tại `price` |

### Điều kiện kích hoạt

Hệ thống theo dõi **`last_price`** — giá của trade gần nhất.

| Side | Trigger khi |
|------|-------------|
| BUY stop | `last_price >= stop_price` (giá tăng chạm stop) |
| SELL stop | `last_price <= stop_price` (giá giảm chạm stop) |

### Use cases

- **Stop-Loss** (SELL stop dưới giá hiện tại): tự động thoát lệnh khi giá giảm quá mức → giới hạn lỗ.
- **Take-Profit / Breakout BUY** (BUY stop trên giá hiện tại): tự động mua khi thị trường bứt phá lên.

---

## API

### Request payload

```json
POST /api/orders
{
  "side": "BUY",
  "quantity": 10,
  "order_type": "STOP_MARKET",
  "stop_price": 105.0,
  "order_id": "SM-001"
}
```

```json
POST /api/orders
{
  "side": "SELL",
  "quantity": 10,
  "order_type": "STOP_LIMIT",
  "stop_price": 95.0,
  "price": 94.0,
  "order_id": "SL-001"
}
```

### Snapshot additions

```json
GET /api/state
{
  "book": {
    "buys": [...],
    "sells": [...],
    "stops": [ { "order_id": "SM-001", "side": "BUY", "stop_price": 105.0, ... } ],
    "trades": [...],
    "last_price": 100.0
  }
}
```

---

## Bảng tổng hợp Test Cases

| ID | Nhóm | Tên | Mục tiêu |
|----|------|-----|----------|
| TC-SO-01 | Trigger semantics | BUY stop-market trigger khi giá tăng | Fire khi `last_price >= stop_price` |
| TC-SO-02 | Trigger semantics | SELL stop-market trigger khi giá giảm | Fire khi `last_price <= stop_price` |
| TC-SO-03 | Trigger semantics | Stop không trigger khi giá chưa chạm | Đảm bảo pending |
| TC-SO-04 | Trigger semantics | Trigger khi `last_price == stop_price` | Boundary (inclusive) |
| TC-SO-05 | Post-trigger | STOP_LIMIT rest nếu không match | Rest ở giá `limit_price` |
| TC-SO-06 | Post-trigger | STOP_MARKET không rest khi sổ rỗng | Đúng MARKET semantics |
| TC-SO-07 | Post-trigger | STOP_LIMIT partial fill + rest remainder | Hybrid behavior |
| TC-SO-08 | Immediate | BUY stop fire ngay khi last_price đã vượt stop | Sanity at submit |
| TC-SO-09 | Immediate | SELL stop fire ngay khi last_price đã dưới stop | Sanity at submit |
| TC-SO-10 | Immediate | Không fire khi `last_price` là None | Pre-trading state |
| TC-SO-11 | Cascade | Một stop fire → trigger stop kế tiếp | Multi-level cascade |
| TC-SO-12 | Cascade | Stop ngược phía không trigger khi giá một chiều | Side-discriminated |
| TC-SO-13 | Cancel | Cancel stop đang pending | Basic cancel |
| TC-SO-14 | Cancel | Stop đã cancel không fire về sau | Verify removal |
| TC-SO-15 | Cancel | Cancel stop không tồn tại → False | Negative case |
| TC-SO-16 | FIFO | Hai stops cùng `stop_price` fire theo timestamp | Ordering fairness |
| TC-SO-17 | Validation | Reject `stop_price < 0` | Input validation |
| TC-SO-18 | Validation | Reject `stop_price == 0` | Input validation |
| TC-SO-19 | Validation | Reject `limit_price < 0` cho STOP_LIMIT | Input validation |
| TC-SO-20 | Validation | Reject `quantity == 0` | Input validation |
| TC-SO-21 | Service E2E | Place STOP_MARKET qua Service rồi trigger | End-to-end |
| TC-SO-22 | Service E2E | Place STOP_LIMIT rồi cancel qua Service | End-to-end cancel |
| TC-SO-23 | Edge | Duplicate stop_id → dedup | Same-ID protection |
| TC-SO-24 | Edge | Snapshot chứa `stops` và `last_price` | API contract |
| TC-SO-25 | Edge | `active_orders()` bao gồm stops | UI listing |
| TC-SO-26 | Edge | STOP_LIMIT giữ nguyên `limit_price` sau trigger | No mutation bug |

---

## Chi tiết từng Test Case

---

### TC-SO-01 — BUY Stop-Market Trigger Khi Giá Tăng

**Điều kiện kết hợp:**
- `last_price` hiện tại = 100
- BUY stop-market tại `stop_price=105` pending
- Sau đó có trade tại giá ≥ 105

**Bước tái hiện:**
```
Bước 1: Establish last_price:
  SELL S0 qty=1 price=100 ts=1
  BUY B0 qty=1 price=100 ts=2
  → Trade 1 @ 100, last_price = 100

Bước 2: Submit stop:
  STOP_MARKET BUY SM1 qty=5 stop_price=105 ts=3
  → last_price (100) < stop_price (105) → SM1 pending

Bước 3: Liquidity for stop to sweep:
  SELL S1 qty=5 price=106 ts=4
  SELL S2 qty=5 price=110 ts=5

Bước 4: Trigger event:
  BUY B1 qty=5 price=106 ts=6
  → Trade: B1 × S1 @ 106 → last_price = 106
  → SM1 fires (106 >= 105), converts to MARKET BUY
  → SM1 matches S2 @ 110, qty=5

Kết quả mong đợi:
  snapshot.stops = []
  trades có entry: {buy_order_id: "SM1", price: 110, qty: 5}
  last_price = 110
```

**Severity nếu fail:** CRITICAL — chức năng stop không hoạt động.

---

### TC-SO-02 — SELL Stop-Market Trigger Khi Giá Giảm

**Điều kiện kết hợp:**
- `last_price = 100`
- SELL stop-market `stop_price=95` pending
- Sau đó có trade tại giá ≤ 95

**Bước tái hiện:**
```
Bước 1: last_price = 100 (qua trade SELL/BUY như TC-SO-01)
Bước 2: STOP_MARKET SELL SM1 qty=5 stop_price=95 ts=3 → pending
Bước 3: BUY B1 qty=5 price=94 ts=4  (resting)
        BUY B2 qty=10 price=90 ts=5 (resting)
Bước 4: SELL S1 qty=5 price=94 ts=6
        → S1 × B1 @ 94 → last_price = 94
        → SM1 fires (94 <= 95) → MARKET SELL sweep B2 @ 90

Kết quả: SM1 filled 5 @ 90
```

**Severity:** CRITICAL.

---

### TC-SO-03 — Stop Không Trigger Khi Giá Chưa Chạm

**Điều kiện:**
- BUY stop tại `stop_price=110`
- Mọi trade sau đó đều ở giá < 110

**Bước:**
```
Bước 1: last_price = 100
Bước 2: STOP_MARKET BUY SM1 stop_price=110 → pending
Bước 3: Thêm trade @ 105
        → last_price = 105 < 110
Bước 4: Verify: SM1 vẫn trong stop_orders
```

**Kỳ vọng:** `snapshot.stops` vẫn chứa SM1.

---

### TC-SO-04 — Trigger Tại Đúng Stop Price (Boundary)

**Điều kiện:** `last_price == stop_price` — trigger phải là **inclusive** (`>=` / `<=`).

**Bước:**
```
Bước 1: last_price = 100
Bước 2: STOP_MARKET BUY SM1 stop_price=105
Bước 3: Trade ĐÚNG tại 105 → last_price = 105 (== stop_price)
Bước 4: Verify SM1 đã fire
```

**Rationale:** Nếu dùng `>` thay `>=`, stop sẽ bỏ lỡ một tick — boundary quan trọng với HFT.

---

### TC-SO-05 — STOP_LIMIT Rest Nếu Không Match Được

**Điều kiện kết hợp:**
- Stop-limit BUY trigger nhưng `limit_price` quá thấp so với sổ sell còn lại

**Bước:**
```
Bước 1: last_price = 100
Bước 2: STOP_LIMIT BUY SL1 qty=5 stop_price=105 limit_price=104
Bước 3: SELL S1 qty=1 price=105 (triggering sell)
        SELL S2 qty=10 price=110 (sẽ không match vì 104 < 110)
Bước 4: BUY B1 qty=1 price=105 → S1 consumed, last_price=105
        → SL1 triggers, becomes LIMIT BUY @ 104
        → S2 @ 110, 104 < 110 → không match → SL1 rest

Kết quả:
  SL1 trong snapshot.buys với price=104, remaining=5, order_type="LIMIT"
```

**Severity:** HIGH — misbehavior gây silent drop.

---

### TC-SO-06 — STOP_MARKET Không Rest Khi Không Có Liquidity

**Điều kiện:** STOP_MARKET triggered nhưng sổ đối diện rỗng sau khi consume.

**Bước:**
```
Bước 1: last_price = 100
Bước 2: STOP_MARKET SELL SM1 qty=5 stop_price=95
Bước 3: BUY B1 qty=1 price=94 (lone resting)
Bước 4: SELL S1 qty=1 price=94 → S1 × B1 → last_price=94, buys=[]
        → SM1 fires → MARKET SELL, buys empty → no trade
        → SM1 không rest (market orders không rest)

Kết quả: snapshot.buys=[], snapshot.sells=[], snapshot.stops=[]
```

**Severity:** HIGH — không được giả market mà rest.

---

### TC-SO-07 — STOP_LIMIT Partial Fill + Rest Remainder

**Điều kiện:** Sau trigger, STOP_LIMIT match được một phần, phần còn lại phải rest tại `limit_price`.

**Bước:**
```
Bước 1: last_price = 100
Bước 2: STOP_LIMIT BUY SL1 qty=10 stop_price=105 limit_price=106
Bước 3: Sells: S1@105 qty=1, S2@106 qty=3, S3@107 qty=100
Bước 4: BUY B1 qty=1 price=105 → trigger SL1
        → SL1 matches S2 @ 106 qty=3
        → Remaining 7 > 0, limit=106 < 107 → không sweep S3 → REST
Kết quả: SL1 rest tại @106 với remaining=7
```

---

### TC-SO-08 — BUY Stop Fire Ngay Khi Submit (last_price đã vượt)

**Điều kiện:** Stop submitted khi `last_price` đã >= `stop_price`.

**Bước:**
```
Bước 1: Drive last_price lên 110 (SELL+BUY @ 110)
Bước 2: SELL S1 qty=5 price=111 (resting)
Bước 3: Submit STOP_MARKET BUY SM1 qty=5 stop_price=105
        → 110 >= 105 → fire ngay khi submit
        → match S1 @ 111
Kết quả: trades ngay trong response của submit; stop_orders=[]
```

**Severity:** HIGH — nếu không check trigger at submit, user sẽ phải đợi trade tiếp theo.

---

### TC-SO-09 — SELL Stop Fire Ngay Khi Submit

**Đối xứng TC-SO-08** cho SELL side.

---

### TC-SO-10 — Không Fire Khi `last_price` Là None

**Điều kiện:** Server vừa khởi động, chưa có trade nào.

**Bước:**
```
Bước 1: OrderBook() — last_price=None
Bước 2: Submit STOP_MARKET BUY SM1 stop_price=50.0
        → last_price=None → stop condition undefined → không trigger
Kết quả: SM1 pending trong stop_orders
```

**Rationale:** Không có reference price để so sánh → phải pending. Guard chống NPE.

---

### TC-SO-11 — Cascade: Stop Fire Kéo Theo Stop Khác

**Điều kiện kết hợp (compound):**
- Hai BUY stops với `stop_price` khác nhau (105, 110)
- Liquidity sắp xếp để stop A trigger tạo trade đẩy giá lên, trigger stop B

**Bước:**
```
Bước 1: last_price = 100
Bước 2: Pending stops:
        STOP_MARKET BUY SM_A qty=5 stop_price=105
        STOP_MARKET BUY SM_B qty=5 stop_price=110
Bước 3: Sells: S1@105 qty=5, S2@110 qty=5, S3@115 qty=100
Bước 4: BUY B1 qty=5 price=105
        → B1 × S1 @ 105 → last_price=105 → SM_A trigger
        → SM_A (MARKET BUY 5) × S2 @ 110 → last_price=110 → SM_B trigger
        → SM_B (MARKET BUY 5) × S3 @ 115 → last_price=115

Kết quả:
  snapshot.stops = []
  3 trades cascaded trong 1 lần submit
  last_price = 115
```

**Severity:** HIGH — nếu cascade không chạy, user nhận execution không đầy đủ.

**Risk nếu bug:** Stop B vẫn pending dù điều kiện đã thoả → delayed execution → loss.

---

### TC-SO-12 — Cascade Không Fire Stop Ngược Phía

**Điều kiện:** Có cả BUY stop và SELL stop pending. Giá chỉ chạy một chiều.

**Bước:**
```
Bước 1: last_price=100
Bước 2: STOP_MARKET BUY SM_BUY stop_price=105
        STOP_MARKET SELL SM_SELL stop_price=95
Bước 3: Trade driving price UP to 106
        → SM_BUY fires (106 >= 105)
        → SM_SELL vẫn pending (106 !<= 95)
Kết quả: Chỉ SM_BUY fired; SM_SELL còn trong stop_orders
```

**Rationale:** Trigger logic phải side-discriminated đúng.

---

### TC-SO-13 — Cancel Stop Đang Pending

**Bước:**
```
Bước 1: last_price=100
Bước 2: Submit STOP_MARKET BUY SM1 stop_price=200 (unreachable)
Bước 3: cancel("SM1")
Kết quả: stop_orders không còn SM1; return True
```

---

### TC-SO-14 — Stop Đã Cancel Không Fire Về Sau

**Điều kiện:** Giá vượt stop_price SAU khi cancel.

**Bước:**
```
Bước 1: last_price=100
Bước 2: STOP_MARKET BUY SM1 stop_price=105
Bước 3: cancel("SM1")
Bước 4: Trade @ 110 → last_price=110
Kết quả: SM1 không có trade trong trades log
```

**Severity:** HIGH — nếu fail → ghost order execute sau cancel (zombie stop).

---

### TC-SO-15 — Cancel Stop Không Tồn Tại

**Bước:** `cancel("GHOST-STOP")` → return False, không raise, không side-effect.

---

### TC-SO-16 — FIFO Giữa Stops Cùng Trigger Price

**Điều kiện:**
- Hai BUY stops cùng `stop_price=105`, timestamps 3 và 4
- Khi fire cùng lúc, stop có timestamp nhỏ hơn phải nhận liquidity tốt hơn

**Bước:**
```
Bước 1: last_price=100
Bước 2: STOP_MARKET BUY SM_EARLY stop=105 ts=3
        STOP_MARKET BUY SM_LATE  stop=105 ts=4
Bước 3: Sells: S1@106 qty=1 (trigger), S2@107 qty=5 (best), S3@108 qty=5 (worse)
Bước 4: BUY B1 qty=1 @106 → trigger cascade
        → Cả hai stops cross trigger đồng thời
        → FIFO: SM_EARLY fire trước → lấy S2 @ 107
                SM_LATE fire sau   → lấy S3 @ 108

Kết quả:
  SM_EARLY.price = 107
  SM_LATE.price = 108
```

**Severity:** HIGH — vi phạm queue fairness, trader submit sớm bị thiệt.

---

### TC-SO-17 — Reject `stop_price < 0`

```python
svc.place_stop_order("SM1", "BUY", 5, stop_price=-1.0, timestamp=1)
# → ValueError("stop_price must be positive")
```

---

### TC-SO-18 — Reject `stop_price == 0`

```python
svc.place_stop_order("SM1", "BUY", 5, stop_price=0.0, timestamp=1)
# → ValueError("stop_price must be positive")
```

Rationale: `stop_price=0` vô nghĩa với BUY (last_price luôn >= 0), và fire ngay không kiểm soát.

---

### TC-SO-19 — Reject `limit_price < 0` Cho STOP_LIMIT

```python
svc.place_stop_order("SL1", "BUY", 5, stop_price=100.0, timestamp=1, limit_price=-50.0)
# → ValueError("limit price must be positive")
```

---

### TC-SO-20 — Reject `quantity == 0`

```python
svc.place_stop_order("SM1", "BUY", 0, stop_price=100.0, timestamp=1)
# → ValueError("quantity must be positive")
```

---

### TC-SO-21 — Service E2E: Place STOP_MARKET Rồi Trigger

Xác nhận flow đầy đủ qua `MatchingEngineService.place_stop_order()`:
- place → pending
- kích hoạt qua trade tiếp theo → trade xuất hiện trong `get_order_book()["trades"]`

---

### TC-SO-22 — Service E2E: Place STOP_LIMIT Rồi Cancel

- `place_stop_order(..., limit_price=150.0)` → pending trong `book["stops"]`
- `cancel_order(order_id)` → trả True
- `book["stops"]` sau đó rỗng

---

### TC-SO-23 — Duplicate Stop ID → Dedup

**Điều kiện:** Submit hai stops với cùng `order_id`.

**Bước:**
```
Bước 1: submit STOP_MARKET BUY SM1 qty=5 stop=105
Bước 2: submit STOP_MARKET BUY SM1 qty=99 stop=200  (duplicate ID)
Kết quả: chỉ có 1 entry "SM1" trong stop_orders;
         giữ lại entry đầu tiên (qty=5, stop=105)
```

**Rationale:** Tương tự BUG-03 fix cho LIMIT — ID collision gây zombie nếu không dedup.

---

### TC-SO-24 — Snapshot Chứa `stops` Và `last_price`

API contract test:
```python
snap = book.snapshot()
assert "stops" in snap
assert "last_price" in snap
assert snap["stops"][0] chứa stop_price, price, side, quantity, remaining, timestamp, order_id
```

---

### TC-SO-25 — `active_orders()` Bao Gồm Stops

UI query phải trả về cả LIMIT resting, SELL resting, và stops pending.

---

### TC-SO-26 — STOP_LIMIT Giữ Nguyên `limit_price` Sau Trigger

**Điều kiện:** STOP_LIMIT SELL qty=5 stop=95 limit=93. Sau trigger, nếu không match, phải rest tại giá 93 (không phải 95, không phải giá trigger).

**Bước:**
```
Bước 1: last_price=100
Bước 2: STOP_LIMIT SELL SL1 qty=5 stop=95 limit=93
Bước 3: Drive last_price down to 94 (SELL @ 94 × BUY @ 94)
Bước 4: Sau trigger, buy side empty → SL1 rest at @93 in sells
Kết quả: snapshot.sells chứa SL1 với price=93, remaining=5
```

**Severity:** HIGH — nếu price bị ghi đè, trader sẽ rest ở giá sai → risk tài chính.

---

## Ma trận Điều Kiện Kết Hợp (Cross-Condition Matrix)

```
Điều kiện A × B → Test case chịu trách nhiệm

Trigger side (BUY/SELL) × Trigger direction:
  BUY stop × price rising      → TC-SO-01
  SELL stop × price falling    → TC-SO-02
  BUY stop × price stable      → TC-SO-03
  Trigger at exact boundary    → TC-SO-04

Post-trigger type × Liquidity:
  STOP_LIMIT × insufficient liquidity → TC-SO-05 (rest)
  STOP_MARKET × no liquidity          → TC-SO-06 (drop)
  STOP_LIMIT × partial liquidity      → TC-SO-07 (partial+rest)

Submission time × Existing state:
  Submit × last_price already past stop → TC-SO-08, TC-SO-09
  Submit × no prior trades              → TC-SO-10

Cascade × Multiple stops:
  Single stop firing × next stop    → TC-SO-11
  Mixed-direction stops pending     → TC-SO-12
  Multiple same-trigger stops       → TC-SO-16 (FIFO)

Lifecycle × Timing:
  Cancel × pending            → TC-SO-13
  Cancel × trigger-after      → TC-SO-14
  Cancel × non-existent       → TC-SO-15
```

---

## Automation Strategy

Tất cả test case đã được automated trong `tests/test_stop_orders.py` (26 tests). Chạy:

```bash
pytest tests/test_stop_orders.py -v
# Expected: 26 passed
```

Chạy cùng toàn bộ regression:
```bash
pytest tests/ -v
# Expected: 104 passed (78 existing + 26 new stop-order tests)
```

### Priority Execution Order

1. **TC-SO-17 → TC-SO-20** — Input validation (nhanh, bảo vệ các test sau khỏi noise).
2. **TC-SO-01, TC-SO-02** — Trigger cơ bản (blocking regression).
3. **TC-SO-08, TC-SO-09, TC-SO-10** — Immediate trigger / null state.
4. **TC-SO-05, TC-SO-06, TC-SO-07** — Post-trigger behavior (STOP_MARKET vs STOP_LIMIT).
5. **TC-SO-11, TC-SO-12, TC-SO-16** — Cascade và FIFO (most subtle).
6. **TC-SO-13, TC-SO-14, TC-SO-15** — Cancel lifecycle.
7. **TC-SO-21 đến TC-SO-26** — Integration & contract tests.
