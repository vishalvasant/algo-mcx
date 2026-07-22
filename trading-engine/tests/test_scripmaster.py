from __future__ import annotations

from decimal import Decimal

from algomcx.contract_selector.scripmaster import instruments_from_scripmaster


def test_instruments_from_scripmaster_band():
  rows = [
    {
      "Exchange": "NFO",
      "Token": "1",
      "Lotsize": "65",
      "Symbol": "NIFTY",
      "Tradingsymbol": "NIFTY21JUL26C24100",
      "Instrument": "OPTIDX",
      "Expiry": "21-JUL-2026",
      "Strike": "24100.00",
      "Optiontype": "CE",
    },
    {
      "Exchange": "NFO",
      "Token": "2",
      "Lotsize": "65",
      "Symbol": "NIFTY",
      "Tradingsymbol": "NIFTY21JUL26P24100",
      "Instrument": "OPTIDX",
      "Expiry": "21-JUL-2026",
      "Strike": "24100.00",
      "Optiontype": "PE",
    },
    {
      "Exchange": "NFO",
      "Token": "3",
      "Lotsize": "65",
      "Symbol": "NIFTY",
      "Tradingsymbol": "NIFTY21JUL26C25000",
      "Instrument": "OPTIDX",
      "Expiry": "21-JUL-2026",
      "Strike": "25000.00",
      "Optiontype": "CE",
    },
    {
      "Exchange": "NFO",
      "Token": "4",
      "Lotsize": "65",
      "Symbol": "NIFTY",
      "Tradingsymbol": "NIFTY28JUL26C24100",
      "Instrument": "OPTIDX",
      "Expiry": "28-JUL-2026",
      "Strike": "24100.00",
      "Optiontype": "CE",
    },
  ]
  out = instruments_from_scripmaster(
    rows,
    underlying="NIFTY",
    expiry_tag="21JUL26",
    atm=Decimal("24100"),
    band_points=Decimal("300"),
  )
  assert len(out) == 2
  assert {i.option_type for i in out} == {"CE", "PE"}
  assert all(i.is_atm for i in out)
  assert all(i.token in {"1", "2"} for i in out)
