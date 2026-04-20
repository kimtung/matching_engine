# Bug Map

Tai lieu nay ghi lai 3 bug da duoc inject de ban luyen tap.

## Bug 1: Market order khong match duoc

- File: `matching_engine/order_book.py`
- Khu vuc: ham `_is_match`
- Hien tuong: market order duoc dat lenh thanh cong qua API nhung khong khop voi resting order du dieu kien.
- Cach tai hien:
  - Dat 1 lenh `SELL LIMIT` gia `100`
  - Dat 1 lenh `BUY MARKET`
  - Khong co trade nao duoc tao
- Goc van de: logic match cho `MARKET` order dang kiem tra dieu kien sai.
- Muc tieu sua: market order phai co the match bat ky lenh doi ung nao dang ton tai trong book.

## Bug 2: Buy side sai price-time priority

- File: `matching_engine/order_book.py`
- Khu vuc: ham `_sort_books`
- Hien tuong: hai lenh BUY cung gia nhung lenh vao sau lai duoc uu tien hon lenh vao truoc.
- Cach tai hien:
  - Dat `B1 BUY LIMIT 100` voi timestamp nho hon
  - Dat `B2 BUY LIMIT 100` voi timestamp lon hon
  - Snapshot book cho thay `B2` dung truoc `B1`
- Goc van de: sort cua buy book dang sap xep sai o phan time priority.
- Muc tieu sua: cung gia thi lenh den truoc phai dung truoc.

## Bug 3: Huy lenh SELL khong dong bo realtime toi client khac

- File: `matching_engine/web.py`
- Khu vuc: ham `cancel_order`
- Hien tuong: huy BUY order thi cac client khac nhan duoc update qua WebSocket, nhung huy SELL order thi client khac khong duoc broadcast ngay.
- Cach tai hien:
  - Mo 2 client frontend
  - Dat 1 SELL order
  - Huy order tu client A
  - Client B khong cap nhat ngay neu chi nghe socket
- Goc van de: backend dang broadcast sai dieu kien sau khi cancel.
- Muc tieu sua: moi lenh cancel thanh cong deu phai broadcast state moi toi tat ca client.
