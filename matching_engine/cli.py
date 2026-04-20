from __future__ import annotations

from pprint import pprint

from matching_engine.service import MatchingEngineService


def main() -> None:
    service = MatchingEngineService()
    service.place_limit_order("S1", "SELL", 5, 101.0, 1)
    service.place_limit_order("S2", "SELL", 3, 102.0, 2)
    service.place_limit_order("B1", "BUY", 4, 100.0, 3)

    print("BOOK BEFORE:")
    pprint(service.get_order_book())

    print("\nPLACE MARKET BUY:")
    trades = service.place_market_order("B2", "BUY", 6, 4)
    pprint(trades)

    print("\nBOOK AFTER:")
    pprint(service.get_order_book())


if __name__ == "__main__":
    main()
