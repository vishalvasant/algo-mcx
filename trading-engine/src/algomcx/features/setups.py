"""Detect institutional strategy setups from enriched features."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from algomcx.models.events import Bias, Candle


def detect_all_setups(
  *,
  bias: Bias,
  spot: Decimal | None,
  vwap: Decimal | None,
  m1: list[Candle],
  m3: list[Candle],
  m5: list[Candle],
  m15: list[Candle],
  existing: dict[str, Any],
  ind: dict[str, Any],
  chain: dict[str, Any] | None = None,
  is_expiry: bool = False,
) -> dict[str, str | None]:
  """Return map strategy_name → setup label (bull/bear) or None."""
  chain = chain or {}
  return {
    "vwap_reclaim": _side_from_reclaim(existing.get("setup_3m")),
    "vwap_bounce": _map_pullback(existing.get("setup_vwap_pullback")),
    "vwap_pullback": _map_pullback(existing.get("setup_vwap_pullback")),
    "vwap_trend": _map_trend(existing.get("setup_vwap_trend")),
    "trend_continuation": _map_trend(existing.get("setup_vwap_trend")),
    "vwap_rejection": _vwap_rejection(m1, vwap, bias),
    "ema_pullback": _ema_pullback(ind, bias),
    "opening_range_breakout": _orb(spot, ind, bias),
    "momentum_continuation": _momentum(m1, bias, vwap),
    "reversal": _reversal(m1, vwap, ind),
    "cpr_breakout": _cpr(spot, ind, bias),
    "pdh_pdl_break": _pdh_pdl(spot, ind, bias),
    "oi_breakout": _oi_breakout(bias, chain),
    "delta_momentum": _delta_momentum(bias, ind),
    "gamma_expansion": _gamma_expansion(bias, ind, is_expiry),
    "iv_expansion": _iv_expansion(bias, ind),
    "gap_and_go": _gap_and_go(m1, ind, bias),
    "trend_day": _trend_day(m15, vwap, bias, ind),
    "mean_reversion": _mean_reversion(spot, vwap),
    "liquidity_sweep": _liquidity_sweep(m1, ind),
    "expiry_scalping": _expiry_scalp(m1, bias, is_expiry, ind),
  }


def _side_from_reclaim(label: str | None) -> str | None:
  if label == "vwap_reclaim_bull":
    return "bull"
  if label == "vwap_reclaim_bear":
    return "bear"
  return None


def _map_pullback(label: str | None) -> str | None:
  if label == "vwap_pullback_bull":
    return "bull"
  if label == "vwap_pullback_bear":
    return "bear"
  return None


def _map_trend(label: str | None) -> str | None:
  if label == "vwap_trend_bull":
    return "bull"
  if label == "vwap_trend_bear":
    return "bear"
  return None


def _vwap_rejection(m1: list[Candle], vwap: Decimal | None, bias: Bias) -> str | None:
  if not m1 or vwap is None or len(m1) < 3:
    return None
  a, c = m1[-3], m1[-1]
  if bias == Bias.BEARISH and a.high > vwap and c.close < vwap and c.close < c.open:
    return "bear"
  if bias == Bias.BULLISH and a.low < vwap and c.close > vwap and c.close > c.open:
    return "bull"
  return None


def _ema_pullback(ind: dict, bias: Bias) -> str | None:
  e9, e21 = ind.get("ema9"), ind.get("ema21")
  spot = ind.get("spot")
  if e9 is None or e21 is None or spot is None or bias == Bias.NEUTRAL:
    return None
  if bias == Bias.BULLISH and e9 > e21 and e21 <= spot <= e9 + Decimal("15"):
    return "bull"
  if bias == Bias.BEARISH and e9 < e21 and e9 - Decimal("15") <= spot <= e21:
    return "bear"
  return None


def _orb(spot: Decimal | None, ind: dict, bias: Bias) -> str | None:
  or_h, or_l = ind.get("or_high"), ind.get("or_low")
  if spot is None or or_h is None or or_l is None:
    return None
  if spot > or_h and bias == Bias.BULLISH:
    return "bull"
  if spot < or_l and bias == Bias.BEARISH:
    return "bear"
  return None


def _momentum(m1: list[Candle], bias: Bias, vwap: Decimal | None) -> str | None:
  if len(m1) < 4 or bias == Bias.NEUTRAL or vwap is None:
    return None
  closes = [c.close for c in m1[-4:]]
  if bias == Bias.BULLISH and closes[-1] > closes[0] and closes[-1] > vwap:
    if all(closes[i] >= closes[i - 1] for i in range(1, len(closes))):
      return "bull"
  if bias == Bias.BEARISH and closes[-1] < closes[0] and closes[-1] < vwap:
    if all(closes[i] <= closes[i - 1] for i in range(1, len(closes))):
      return "bear"
  return None


def _reversal(m1: list[Candle], vwap: Decimal | None, ind: dict) -> str | None:
  if len(m1) < 5 or vwap is None:
    return None
  dist = ind.get("abs_distance_to_vwap_points")
  if dist is None or float(dist) < 40:
    return None
  c0, c1 = m1[-2].close, m1[-1].close
  if c0 < vwap <= c1:
    return "bull"
  if c0 > vwap >= c1:
    return "bear"
  return None


def _cpr(spot: Decimal | None, ind: dict, bias: Bias) -> str | None:
  tc, bc = ind.get("cpr_tc"), ind.get("cpr_bc")
  if spot is None or tc is None or bc is None:
    return None
  if spot > tc and bias == Bias.BULLISH:
    return "bull"
  if spot < bc and bias == Bias.BEARISH:
    return "bear"
  return None


def _pdh_pdl(spot: Decimal | None, ind: dict, bias: Bias) -> str | None:
  pdh, pdl = ind.get("pdh"), ind.get("pdl")
  if spot is None:
    return None
  if pdh is not None and spot > pdh and bias == Bias.BULLISH:
    return "bull"
  if pdl is not None and spot < pdl and bias == Bias.BEARISH:
    return "bear"
  return None


def _oi_breakout(bias: Bias, chain: dict) -> str | None:
  if bias == Bias.BULLISH and chain.get("oi_confirms_ce") and chain.get("long_build_up"):
    return "bull"
  if bias == Bias.BEARISH and chain.get("oi_confirms_pe") and chain.get("long_build_up"):
    return "bear"
  return None


def _delta_momentum(bias: Bias, ind: dict) -> str | None:
  d = ind.get("option_delta")
  if d is None or bias == Bias.NEUTRAL:
    return None
  if bias == Bias.BULLISH and float(d) >= 0.45:
    return "bull"
  if bias == Bias.BEARISH and float(d) <= -0.45:
    return "bear"
  return None


def _gamma_expansion(bias: Bias, ind: dict, is_expiry: bool) -> str | None:
  g = ind.get("option_gamma")
  if g is None or bias == Bias.NEUTRAL:
    return None
  thr = 0.0008 if is_expiry else 0.0012
  if float(g) >= thr:
    return "bull" if bias == Bias.BULLISH else "bear"
  return None


def _iv_expansion(bias: Bias, ind: dict) -> str | None:
  iv = ind.get("option_iv")
  iv_chg = ind.get("option_iv_change")
  if iv is None or bias == Bias.NEUTRAL:
    return None
  if iv_chg is not None and float(iv_chg) > 0.01 and 0.12 <= float(iv) <= 0.35:
    return "bull" if bias == Bias.BULLISH else "bear"
  return None


def _gap_and_go(m1: list[Candle], ind: dict, bias: Bias) -> str | None:
  if len(m1) < 10:
    return None
  gap = ind.get("gap_points")
  if gap is None:
    return None
  if float(gap) >= 40 and bias == Bias.BULLISH and m1[-1].close > m1[0].open:
    return "bull"
  if float(gap) <= -40 and bias == Bias.BEARISH and m1[-1].close < m1[0].open:
    return "bear"
  return None


def _trend_day(m15: list[Candle], vwap: Decimal | None, bias: Bias, ind: dict) -> str | None:
  if len(m15) < 3 or vwap is None or bias == Bias.NEUTRAL:
    return None
  e50 = ind.get("ema50")
  if e50 is None:
    return None
  last = m15[-1].close
  if bias == Bias.BULLISH and last > vwap and last > e50 and ind.get("structure_5m") == "hhhl":
    return "bull"
  if bias == Bias.BEARISH and last < vwap and last < e50 and ind.get("structure_5m") == "lllh":
    return "bear"
  return None


def _mean_reversion(spot: Decimal | None, vwap: Decimal | None) -> str | None:
  if spot is None or vwap is None:
    return None
  dist = abs(spot - vwap)
  if dist >= Decimal("60"):
    return "bear" if spot > vwap else "bull"
  return None


def _liquidity_sweep(m1: list[Candle], ind: dict) -> str | None:
  if len(m1) < 6:
    return None
  or_h, or_l = ind.get("or_high"), ind.get("or_low")
  if or_h is None or or_l is None:
    return None
  w = m1[-5:]
  if any(c.high > or_h for c in w[:-1]) and w[-1].close < or_h:
    return "bear"
  if any(c.low < or_l for c in w[:-1]) and w[-1].close > or_l:
    return "bull"
  return None


def _expiry_scalp(m1: list[Candle], bias: Bias, is_expiry: bool, ind: dict) -> str | None:
  if not is_expiry or bias == Bias.NEUTRAL or len(m1) < 3:
    return None
  if ind.get("option_gamma") is None or float(ind["option_gamma"]) < 0.001:
    return None
  c0, c1 = m1[-2].close, m1[-1].close
  if bias == Bias.BULLISH and c1 > c0:
    return "bull"
  if bias == Bias.BEARISH and c1 < c0:
    return "bear"
  return None
