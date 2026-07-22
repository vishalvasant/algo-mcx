from decimal import Decimal

from algomcx.contract_selector.selector import ContractSelector
from algomcx.market_data.vwap import session_vwap
from algomcx.models.events import Candle, CandleInterval
from datetime import datetime, timezone


class DummyBroker:
    pass


class DummyConfig:
    symbols = {
        "strike_step": 50,
        "strike_band_points": 300,
        "underlying": "NIFTY",
        "exchange_options": "NFO",
        "exchange_spot": "NSE",
        "spot_token": "26000",
    }


def test_atm_strike_rounding():
    selector = ContractSelector(DummyConfig(), DummyBroker())  # type: ignore[arg-type]
    assert selector.atm_strike_for_spot(Decimal("24512")) == Decimal("24500")
    assert selector.atm_strike_for_spot(Decimal("24526")) == Decimal("24550")


def test_strike_in_band():
    selector = ContractSelector(DummyConfig(), DummyBroker())  # type: ignore[arg-type]
    atm = Decimal("24500")
    assert selector.strike_in_band(Decimal("24200"), atm)
    assert selector.strike_in_band(Decimal("24800"), atm)
    assert not selector.strike_in_band(Decimal("24150"), atm)


def test_session_vwap():
    candles = [
        Candle(
            instrument_token="26000",
            ts=datetime.now(tz=timezone.utc),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=1000,
            interval=CandleInterval.M1,
        ),
        Candle(
            instrument_token="26000",
            ts=datetime.now(tz=timezone.utc),
            open=Decimal("105"),
            high=Decimal("115"),
            low=Decimal("100"),
            close=Decimal("110"),
            volume=2000,
            interval=CandleInterval.M1,
        ),
    ]
    vwap = session_vwap(candles)
    assert vwap is not None
    assert vwap > Decimal("100")
