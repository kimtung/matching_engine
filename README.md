# Mock Matching Engine

Mini project de ban luyen truoc hackathon.

## Muc tieu

- Doc nhanh cau truc project
- Tim va fix bug
- Mo ta luong matching
- Them feature nho trong thoi gian ngan

## Cau truc

- `matching_engine/models.py`: domain models
- `matching_engine/order_book.py`: logic luu lenh va match lenh
- `matching_engine/service.py`: facade de nap lenh va xem snapshot
- `matching_engine/cli.py`: chay demo bang terminal
- `tests/`: bo test de xac nhan hanh vi
- `PRACTICE.md`: de bai luyen tap

## Chay nhanh

```bash
python -m matching_engine.cli
```

## Kien truc

- `matching_engine/`: backend API + WebSocket + matching engine core
- `frontend/`: frontend app rieng

## Chay backend

```bash
python -m matching_engine.web
```

Backend mac dinh chay tai `http://127.0.0.1:8000`.

## Chay frontend

```bash
python -m frontend.server
```

Frontend mac dinh chay tai `http://127.0.0.1:3000`.

UI ho tro:

- Dat limit order
- Dat market order
- Huy lenh dang resting
- Xem buy book, sell book, active orders, trade history
- Dong bo realtime giua nhieu client qua WebSocket

## Backend API

- `GET /api/state`: lay snapshot hien tai
- `POST /api/orders`: dat lenh moi
- `POST /api/cancel`: huy lenh theo `order_id`
- `POST /api/reset`: reset order book
- `GET /ws`: WebSocket broadcast state moi den moi client khi co thay doi

## Ghi chu frontend

- Frontend mac dinh goi backend `http://127.0.0.1:8000`
- Neu doi backend URL, set `localStorage["matching-engine-backend"]` truoc khi reload trang

## Chay test

```bash
python -m pytest
```
