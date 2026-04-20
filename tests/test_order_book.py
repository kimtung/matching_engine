from matching_engine.service import MatchingEngineService


def test_limit_buy_matches_best_sell_price() -> None:
    service = MatchingEngineService()
    service.place_limit_order("S1", "SELL", 5, 100.0, 1)
    trades = service.place_limit_order("B1", "BUY", 3, 101.0, 2)

    assert trades == [
        {
            "buy_order_id": "B1",
            "sell_order_id": "S1",
            "price": 100.0,
            "quantity": 3,
            "aggressor_order_id": "B1",
            "timestamp": 2,
        }
    ]


def test_market_order_should_match_without_price() -> None:
    service = MatchingEngineService()
    service.place_limit_order("S1", "SELL", 5, 100.0, 1)
    trades = service.place_market_order("B1", "BUY", 2, 2)

    assert trades[0]["quantity"] == 2
    assert trades[0]["price"] == 100.0


def test_filled_orders_should_not_remain_in_snapshot() -> None:
    service = MatchingEngineService()
    service.place_limit_order("S1", "SELL", 2, 100.0, 1)
    service.place_limit_order("B1", "BUY", 2, 100.0, 2)

    book = service.get_order_book()
    assert book["sells"] == []
    assert book["buys"] == []


def test_buy_side_uses_price_time_priority() -> None:
    service = MatchingEngineService()
    service.place_limit_order("B1", "BUY", 2, 100.0, 1)
    service.place_limit_order("B2", "BUY", 2, 100.0, 2)

    book = service.get_order_book()
    assert [order["order_id"] for order in book["buys"]] == ["B1", "B2"]


def test_cancel_order_removes_it_from_book() -> None:
    service = MatchingEngineService()
    service.place_limit_order("B1", "BUY", 2, 100.0, 1)

    cancelled = service.cancel_order("B1")

    assert cancelled is True
    assert service.get_order_book()["buys"] == []
