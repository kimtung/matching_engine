"""
Stop order tests.

Covers:
  A) Trigger semantics — BUY stop fires on price rise; SELL stop on price fall
  B) STOP_MARKET vs STOP_LIMIT behavior after trigger
  C) Immediate trigger on submission (last_price already crossed)
  D) Cascade triggers (one stop firing crosses another's trigger)
  E) Cancel pending stops
  F) FIFO among simultaneously-triggered stops
  G) Input validation via Service layer
  H) Edge cases — no last_price, dedup, empty opposite book
"""

import pytest

from matching_engine.models import Order, OrderType, Side
from matching_engine.order_book import OrderBook
from matching_engine.service import MatchingEngineService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def lim(order_id, side, qty, price, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=price, timestamp=ts,
        order_type=OrderType.LIMIT,
    )


def mkt(order_id, side, qty, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=None, timestamp=ts,
        order_type=OrderType.MARKET,
    )


def stop_mkt(order_id, side, qty, stop_price, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=None, timestamp=ts,
        order_type=OrderType.STOP_MARKET,
        stop_price=stop_price,
    )


def stop_lim(order_id, side, qty, stop_price, limit_price, ts):
    return Order(
        order_id=order_id, side=Side(side),
        quantity=qty, price=limit_price, timestamp=ts,
        order_type=OrderType.STOP_LIMIT,
        stop_price=stop_price,
    )


# ===========================================================================
# A. Basic trigger semantics
# ===========================================================================

class TestStopTriggerSemantics:

    def test_buy_stop_market_triggers_when_price_rises_through_stop(self):
        """
        BUY stop-market @105: đặt khi last_price=100.
        Khi có trade tại 106 → stop trigger, becomes MARKET BUY.
        """
        book = OrderBook()
        # Establish last_price = 100
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))
        assert book.last_price == 100.0

        # Stop-market BUY qty=5 triggered at 105
        book.submit(stop_mkt("SM1", "BUY", 5, 105.0, 3))
        # Not triggered yet — 100 < 105
        assert any(s.order_id == "SM1" for s in book.stop_orders)
        assert book.snapshot()["trades"].__len__() == 1

        # Resting sells to absorb the triggered stop
        book.submit(lim("S1", "SELL", 5, 106.0, 4))   # @106
        book.submit(lim("S2", "SELL", 5, 110.0, 5))   # @110 (untouched)

        # A trade at 106 bumps last_price; this should fire SM1 which then
        # matches as MARKET against the remaining sells.
        book.submit(lim("B1", "BUY", 5, 106.0, 6))    # triggers SM1 cascade

        # SM1 fired and swept 5 units from S2 (S1 consumed by B1)
        snap = book.snapshot()
        assert not any(s["order_id"] == "SM1" for s in snap["stops"])
        # Two cascaded legs executed on S2
        assert book.last_price == 110.0
        # SM1 took price 110 (best remaining sell)
        sm1_trades = [t for t in snap["trades"] if t["buy_order_id"] == "SM1"]
        assert len(sm1_trades) == 1
        assert sm1_trades[0]["quantity"] == 5
        assert sm1_trades[0]["price"] == 110.0

    def test_sell_stop_market_triggers_when_price_falls_through_stop(self):
        """
        SELL stop-market @95: đặt khi last_price=100.
        Khi có trade tại 94 → stop trigger, becomes MARKET SELL.
        """
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))
        assert book.last_price == 100.0

        # Stop-market SELL qty=5 with trigger 95
        book.submit(stop_mkt("SM1", "SELL", 5, 95.0, 3))
        assert any(s.order_id == "SM1" for s in book.stop_orders)

        # Resting buys for the triggered SELL to match against
        book.submit(lim("B1", "BUY", 5, 94.0, 4))
        book.submit(lim("B2", "BUY", 10, 90.0, 5))

        # SELL at 94 → drives last_price down to 94 → SM1 triggers → matches B1/B2
        book.submit(lim("S1", "SELL", 5, 94.0, 6))

        snap = book.snapshot()
        assert snap["stops"] == []
        sm1_trades = [t for t in snap["trades"] if t["sell_order_id"] == "SM1"]
        assert len(sm1_trades) == 1
        assert sm1_trades[0]["quantity"] == 5
        assert sm1_trades[0]["price"] == 90.0  # swept B2

    def test_buy_stop_does_not_trigger_when_price_stays_below(self):
        """Giá chưa chạm stop → stop vẫn pending."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "BUY", 5, 110.0, 3))
        # Trades below 110 — SM1 phải vẫn pending
        book.submit(lim("S1", "SELL", 1, 105.0, 4))
        book.submit(lim("B1", "BUY", 1, 105.0, 5))

        assert book.last_price == 105.0
        assert any(s.order_id == "SM1" for s in book.stop_orders)

    def test_trigger_on_exact_stop_price(self):
        """last_price == stop_price → trigger (>= / <= inclusive)."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "BUY", 1, 105.0, 3))
        # Sells to match both the triggering trade and the triggered stop.
        book.submit(lim("S1", "SELL", 1, 105.0, 4))
        book.submit(lim("S2", "SELL", 1, 105.0, 5))

        # Trade at exactly 105 → SM1 must fire
        book.submit(lim("B1", "BUY", 1, 105.0, 6))

        snap = book.snapshot()
        assert snap["stops"] == []
        sm1_trades = [t for t in snap["trades"] if t["buy_order_id"] == "SM1"]
        assert len(sm1_trades) == 1


# ===========================================================================
# B. STOP_MARKET vs STOP_LIMIT post-trigger behavior
# ===========================================================================

class TestStopPostTriggerBehavior:

    def test_stop_limit_rests_if_cannot_match(self):
        """Stop-limit trigger nhưng limit price không đủ → rest trong sổ."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        # Stop-limit BUY: triggers at 105, limit 104 (too low to sweep anything at 110)
        book.submit(stop_lim("SL1", "BUY", 5, 105.0, 104.0, 3))
        book.submit(lim("S1", "SELL", 1, 105.0, 4))     # one sell at 105
        book.submit(lim("S2", "SELL", 10, 110.0, 5))    # rest at 110

        # Trade at 105 triggers SL1 → LIMIT BUY@104. S1 already consumed.
        # Only S2@110 is left; 104 < 110 → no match → SL1 rests.
        book.submit(lim("B1", "BUY", 1, 105.0, 6))

        snap = book.snapshot()
        assert not any(s["order_id"] == "SL1" for s in snap["stops"])
        sl1_in_buys = [b for b in snap["buys"] if b["order_id"] == "SL1"]
        assert len(sl1_in_buys) == 1
        assert sl1_in_buys[0]["price"] == 104.0
        assert sl1_in_buys[0]["remaining"] == 5
        # SL1's order_type got rewritten to LIMIT on trigger
        assert sl1_in_buys[0]["order_type"] == OrderType.LIMIT.value

    def test_stop_market_does_not_rest_if_no_liquidity(self):
        """Stop-market triggered vào sổ rỗng → không rest (đúng MARKET semantics)."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "SELL", 5, 95.0, 3))

        # Only one resting buy — gets consumed by our triggering trade.
        book.submit(lim("B1", "BUY", 1, 94.0, 4))
        book.submit(lim("S1", "SELL", 1, 94.0, 5))  # triggers SM1; buys empty after

        snap = book.snapshot()
        assert snap["buys"] == []
        assert snap["sells"] == []
        assert snap["stops"] == []
        # SM1 produced no trade (nothing to sweep)
        sm1_trades = [t for t in snap["trades"] if t["sell_order_id"] == "SM1"]
        assert sm1_trades == []

    def test_stop_limit_partial_fill_rests_remainder(self):
        """Stop-limit trigger, fill một phần tại limit price, rest remainder."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_lim("SL1", "BUY", 10, 105.0, 106.0, 3))
        book.submit(lim("S1", "SELL", 1, 105.0, 4))     # triggering sell
        book.submit(lim("S2", "SELL", 3, 106.0, 5))     # 3 @ 106
        book.submit(lim("S3", "SELL", 100, 107.0, 6))   # rest higher

        book.submit(lim("B1", "BUY", 1, 105.0, 7))      # triggers SL1

        snap = book.snapshot()
        # SL1 fills 3 @106 then rests with remaining=7 at price=106
        sl1_trades = [t for t in snap["trades"] if t["buy_order_id"] == "SL1"]
        assert len(sl1_trades) == 1
        assert sl1_trades[0]["quantity"] == 3
        sl1_resting = [b for b in snap["buys"] if b["order_id"] == "SL1"]
        assert len(sl1_resting) == 1
        assert sl1_resting[0]["remaining"] == 7
        assert sl1_resting[0]["price"] == 106.0


# ===========================================================================
# C. Immediate trigger on submission
# ===========================================================================

class TestImmediateTrigger:

    def test_buy_stop_submitted_when_last_price_already_above_stop(self):
        """Nếu last_price đã vượt stop_price khi submit → trigger ngay lập tức."""
        book = OrderBook()
        # Drive last_price to 110 first
        book.submit(lim("S0", "SELL", 1, 110.0, 1))
        book.submit(lim("B0", "BUY", 1, 110.0, 2))
        assert book.last_price == 110.0

        # Resting sells to absorb the immediate-fire stop
        book.submit(lim("S1", "SELL", 5, 111.0, 3))

        # Submit BUY stop-market @105 — last_price=110 already >= 105 → fires now
        trades = book.submit(stop_mkt("SM1", "BUY", 5, 105.0, 4))

        assert len(trades) == 1
        assert trades[0].buy_order_id == "SM1"
        assert trades[0].quantity == 5
        assert trades[0].price == 111.0
        assert book.stop_orders == []

    def test_sell_stop_submitted_when_last_price_already_below_stop(self):
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 90.0, 1))
        book.submit(lim("B0", "BUY", 1, 90.0, 2))

        book.submit(lim("B1", "BUY", 5, 89.0, 3))

        # SELL stop @95, last_price=90 already <= 95 → immediate fire
        trades = book.submit(stop_mkt("SM1", "SELL", 5, 95.0, 4))

        assert len(trades) == 1
        assert trades[0].sell_order_id == "SM1"
        assert trades[0].price == 89.0

    def test_stop_not_triggered_when_no_last_price_yet(self):
        """Chưa có trade nào → last_price=None → stop không trigger bất kể stop_price."""
        book = OrderBook()
        book.submit(stop_mkt("SM1", "BUY", 5, 50.0, 1))
        # Aggressively low stop for a BUY → would trigger if last_price=0,
        # but last_price is None → must NOT trigger.
        assert book.last_price is None
        assert any(s.order_id == "SM1" for s in book.stop_orders)


# ===========================================================================
# D. Cascade triggers
# ===========================================================================

class TestCascadeTriggers:

    def test_one_triggered_stop_fires_another(self):
        """
        Stop A trigger → trade đẩy last_price tiếp → Stop B cũng trigger.

        Timeline:
          last_price = 100
          SM_A: BUY stop @ 105, qty=5   (pending)
          SM_B: BUY stop @ 110, qty=5   (pending)

          SELL wall: 5@105, 5@110, 100@115

          Aggressor BUY 5 @ 105:
            - fills S@105 → last_price=105 → SM_A triggers
            - SM_A (MARKET BUY 5) sweeps S@110 → last_price=110 → SM_B triggers
            - SM_B (MARKET BUY 5) sweeps S@115 → last_price=115
        """
        book = OrderBook()
        book.submit(lim("S_init", "SELL", 1, 100.0, 1))
        book.submit(lim("B_init", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM_A", "BUY", 5, 105.0, 3))
        book.submit(stop_mkt("SM_B", "BUY", 5, 110.0, 4))

        book.submit(lim("S1", "SELL", 5, 105.0, 5))
        book.submit(lim("S2", "SELL", 5, 110.0, 6))
        book.submit(lim("S3", "SELL", 100, 115.0, 7))

        book.submit(lim("B1", "BUY", 5, 105.0, 8))  # kicks off the cascade

        snap = book.snapshot()
        assert snap["stops"] == []
        sm_a = [t for t in snap["trades"] if t["buy_order_id"] == "SM_A"]
        sm_b = [t for t in snap["trades"] if t["buy_order_id"] == "SM_B"]
        assert len(sm_a) == 1 and sm_a[0]["price"] == 110.0
        assert len(sm_b) == 1 and sm_b[0]["price"] == 115.0
        assert book.last_price == 115.0

    def test_cascade_does_not_fire_stops_on_wrong_side(self):
        """
        Giá tăng → BUY stops trigger — SELL stops ngược phía KHÔNG được trigger
        dù có trong stop_orders.
        """
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        # Đặt cả hai loại: BUY stop @ 105 (should fire), SELL stop @ 95 (should NOT)
        book.submit(stop_mkt("SM_BUY", "BUY", 1, 105.0, 3))
        book.submit(stop_mkt("SM_SELL", "SELL", 1, 95.0, 4))

        # Trade tại 106 → last_price rises. SELL stop needs last_price <= 95, stays pending.
        book.submit(lim("S1", "SELL", 1, 106.0, 5))
        book.submit(lim("S2", "SELL", 5, 106.0, 6))
        book.submit(lim("B1", "BUY", 1, 106.0, 7))

        pending_ids = {s.order_id for s in book.stop_orders}
        assert "SM_BUY" not in pending_ids
        assert "SM_SELL" in pending_ids


# ===========================================================================
# E. Cancel pending stops
# ===========================================================================

class TestCancelStop:

    def test_cancel_pending_stop(self):
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "BUY", 5, 200.0, 3))
        assert any(s.order_id == "SM1" for s in book.stop_orders)

        assert book.cancel("SM1") is True
        assert book.stop_orders == []

    def test_cancel_stop_prevents_future_trigger(self):
        """Cancelled stop không fire khi giá vượt stop sau đó."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "BUY", 5, 105.0, 3))
        book.cancel("SM1")

        # Now trade at 110 — SM1 must not execute
        book.submit(lim("S1", "SELL", 1, 110.0, 4))
        book.submit(lim("B1", "BUY", 1, 110.0, 5))

        sm1_trades = [t for t in book.snapshot()["trades"] if t["buy_order_id"] == "SM1"]
        assert sm1_trades == []

    def test_cancel_nonexistent_stop_returns_false(self):
        book = OrderBook()
        assert book.cancel("GHOST-STOP") is False


# ===========================================================================
# F. FIFO among simultaneously-triggered stops
# ===========================================================================

class TestSimultaneousTriggerFIFO:

    def test_two_stops_same_trigger_fire_in_timestamp_order(self):
        """
        Hai BUY stops cùng stop_price=105, ts khác nhau → stop ts nhỏ fires trước
        (nhận liquidity tốt hơn tại triggered moment).
        """
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        # SM_EARLY submitted before SM_LATE
        book.submit(stop_mkt("SM_EARLY", "BUY", 5, 105.0, 3))
        book.submit(stop_mkt("SM_LATE", "BUY", 5, 105.0, 4))

        book.submit(lim("S1", "SELL", 1, 106.0, 5))    # trigger sell
        book.submit(lim("S2", "SELL", 5, 107.0, 6))    # better price → EARLY should grab
        book.submit(lim("S3", "SELL", 5, 108.0, 7))    # LATE takes worse price

        book.submit(lim("B1", "BUY", 1, 106.0, 8))     # triggers cascade

        snap = book.snapshot()
        t_early = [t for t in snap["trades"] if t["buy_order_id"] == "SM_EARLY"]
        t_late = [t for t in snap["trades"] if t["buy_order_id"] == "SM_LATE"]
        assert t_early[0]["price"] == 107.0, \
            f"EARLY submitted first must get best price. Got {t_early[0]['price']}"
        assert t_late[0]["price"] == 108.0


# ===========================================================================
# G. Service layer input validation
# ===========================================================================

class TestServiceValidation:

    def test_negative_stop_price_rejected(self):
        svc = MatchingEngineService()
        with pytest.raises(ValueError, match="stop_price must be positive"):
            svc.place_stop_order("SM1", "BUY", 5, stop_price=-1.0, timestamp=1)

    def test_zero_stop_price_rejected(self):
        svc = MatchingEngineService()
        with pytest.raises(ValueError, match="stop_price must be positive"):
            svc.place_stop_order("SM1", "BUY", 5, stop_price=0.0, timestamp=1)

    def test_negative_limit_price_rejected_for_stop_limit(self):
        svc = MatchingEngineService()
        with pytest.raises(ValueError, match="limit price must be positive"):
            svc.place_stop_order(
                "SL1", "BUY", 5, stop_price=100.0, timestamp=1, limit_price=-50.0
            )

    def test_zero_quantity_rejected(self):
        svc = MatchingEngineService()
        with pytest.raises(ValueError, match="quantity must be positive"):
            svc.place_stop_order("SM1", "BUY", 0, stop_price=100.0, timestamp=1)

    def test_service_place_stop_market_and_trigger(self):
        """End-to-end through service: stop-market BUY triggers on price rise."""
        svc = MatchingEngineService()
        svc.place_limit_order("S0", "SELL", 1, 100.0, 1)
        svc.place_limit_order("B0", "BUY", 1, 100.0, 2)

        trades = svc.place_stop_order("SM1", "BUY", 5, stop_price=105.0, timestamp=3)
        assert trades == []  # pending

        svc.place_limit_order("S1", "SELL", 1, 105.0, 4)
        svc.place_limit_order("S2", "SELL", 10, 106.0, 5)
        trades = svc.place_limit_order("B1", "BUY", 1, 105.0, 6)
        # B1 itself trades with S1 at 105; SM1 cascades and buys 5 @ 106

        book = svc.get_order_book()
        sm1 = [t for t in book["trades"] if t["buy_order_id"] == "SM1"]
        assert len(sm1) == 1
        assert sm1[0]["quantity"] == 5
        assert sm1[0]["price"] == 106.0

    def test_service_place_stop_limit_rest_then_cancel(self):
        svc = MatchingEngineService()
        svc.place_limit_order("S0", "SELL", 1, 100.0, 1)
        svc.place_limit_order("B0", "BUY", 1, 100.0, 2)

        svc.place_stop_order("SL1", "BUY", 5, stop_price=200.0,
                             timestamp=3, limit_price=150.0)
        book = svc.get_order_book()
        assert any(s["order_id"] == "SL1" for s in book["stops"])

        assert svc.cancel_order("SL1") is True
        book = svc.get_order_book()
        assert book["stops"] == []


# ===========================================================================
# H. Edge cases & dedup
# ===========================================================================

class TestStopEdgeCases:

    def test_duplicate_stop_id_dedup(self):
        """Submit cùng stop_id 2 lần → chỉ 1 entry trong stop_orders."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_mkt("SM1", "BUY", 5, 105.0, 3))
        book.submit(stop_mkt("SM1", "BUY", 99, 200.0, 4))  # dup

        active = [s for s in book.stop_orders if s.order_id == "SM1"]
        assert len(active) == 1
        assert active[0].quantity == 5
        assert active[0].stop_price == 105.0

    def test_snapshot_includes_stops_and_last_price(self):
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))
        book.submit(stop_mkt("SM1", "BUY", 5, 110.0, 3))

        snap = book.snapshot()
        assert "stops" in snap
        assert "last_price" in snap
        assert snap["last_price"] == 100.0
        assert len(snap["stops"]) == 1
        assert snap["stops"][0]["order_id"] == "SM1"
        assert snap["stops"][0]["stop_price"] == 110.0

    def test_active_orders_includes_stops(self):
        book = OrderBook()
        book.submit(lim("B0", "BUY", 5, 99.0, 1))
        book.submit(stop_mkt("SM1", "BUY", 5, 110.0, 2))

        ids = {o["order_id"] for o in book.active_orders()}
        assert "B0" in ids
        assert "SM1" in ids

    def test_stop_limit_limit_price_preserved_through_trigger(self):
        """Sau khi trigger, stop_limit rest với đúng limit_price."""
        book = OrderBook()
        book.submit(lim("S0", "SELL", 1, 100.0, 1))
        book.submit(lim("B0", "BUY", 1, 100.0, 2))

        book.submit(stop_lim("SL1", "SELL", 5, stop_price=95.0, limit_price=93.0, ts=3))
        book.submit(lim("B1", "BUY", 1, 94.0, 4))
        book.submit(lim("S1", "SELL", 1, 94.0, 5))  # triggers SL1

        # Buys empty after trigger — SL1 rests as LIMIT SELL @93
        snap = book.snapshot()
        resting = [s for s in snap["sells"] if s["order_id"] == "SL1"]
        assert len(resting) == 1
        assert resting[0]["price"] == 93.0
        assert resting[0]["remaining"] == 5
