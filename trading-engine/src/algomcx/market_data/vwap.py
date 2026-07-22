from __future__ import annotations

from decimal import Decimal

from algomcx.models.events import Candle


def session_vwap(candles: list[Candle]) -> Decimal | None:
    if not candles:
        return None

    total_pv = Decimal("0")
    total_volume = Decimal("0")
    for candle in candles:
        if candle.volume is None or candle.volume <= 0:
            continue
        typical = (candle.high + candle.low + candle.close) / Decimal("3")
        volume = Decimal(candle.volume)
        total_pv += typical * volume
        total_volume += volume

    if total_volume == 0:
        closes = [c.close for c in candles]
        return sum(closes) / Decimal(len(closes))
    return total_pv / total_volume
