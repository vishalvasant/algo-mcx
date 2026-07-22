from decimal import Decimal

from algomcx.features.mtf_patterns import build_mtf_alignment, detect_tf_patterns
from algomcx.models.events import Bias, Candle, CandleInterval
from datetime import datetime, timezone


def _c(o, h, l, c, i=0):
    return Candle(
        instrument_token="26000",
        ts=datetime(2026, 7, 20, 5, 0, i, tzinfo=timezone.utc),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        interval=CandleInterval.M1,
    )


def test_bullish_engulf_detected():
    bars = [
        _c(100, 101, 98, 99, 0),  # bearish
        _c(98, 103, 97, 102, 1),  # bullish engulf
    ]
    p = detect_tf_patterns(bars, timeframe="1m")
    assert p.bullish_engulf


def test_mtf_ce_score_higher_in_uptrend():
    # Rising closes above vwap
    m1 = [_c(100 + i, 101 + i, 99 + i, 100.5 + i, i) for i in range(6)]
    m3 = m1[:]
    m5 = m1[:]
    vwap = Decimal("100")
    align = build_mtf_alignment(m1=m1, m3=m3, m5=m5, vwap=vwap, bias=Bias.BULLISH)
    assert align.score_ce > align.score_pe
    assert align.score_ce >= 40
