"""Open holdings must stay on the WebSocket subscription set."""
from __future__ import annotations

from algomcx.broker.base import BrokerAdapter


def test_holding_keys_merge_with_universe() -> None:
    exchange = "NFO"
    universe_keys = [
        BrokerAdapter.format_instrument("NSE", "26000"),
        BrokerAdapter.format_instrument(exchange, "111"),
        BrokerAdapter.format_instrument(exchange, "222"),
    ]
    holding_tokens = ["222", "999"]  # 999 outside band

    keys: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    for k in universe_keys:
        add(k)
    for tok in holding_tokens:
        add(BrokerAdapter.format_instrument(exchange, tok))

    assert BrokerAdapter.format_instrument(exchange, "999") in keys
    assert keys.count(BrokerAdapter.format_instrument(exchange, "222")) == 1
    assert keys[0].startswith("NSE|")
