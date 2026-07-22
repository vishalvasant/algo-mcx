from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from algomcx.config import AppConfig
from algomcx.models.events import Bias, Candle, FeatureSnapshot, MarketRegime

IST = ZoneInfo("Asia/Kolkata")

_REGIME_KEYS = (
  "trending_up",
  "trending_down",
  "sideways",
  "high_volatility",
  "low_volatility",
  "breakout",
  "opening_range",
  "expiry_behaviour",
)


class RegimeClassifier:
  """Rule-based market regime — deterministic, no LLM."""

  def __init__(self, config: AppConfig) -> None:
    self._cfg = config.strategy.get("regime", {})

  def classify(
    self,
    features: FeatureSnapshot,
    m1: list[Candle],
    m5: list[Candle],
    *,
    is_expiry_day: bool = False,
    now: datetime | None = None,
  ) -> MarketRegime:
    now_ist = (now or datetime.now(tz=IST)).astimezone(IST)
    scores = {k: 0.0 for k in _REGIME_KEYS}
    reasons: list[str] = []
    health: dict = {}

    spot = features.nifty_spot
    vwap = features.session_vwap
    extra = features.extra or {}

    atr = _approx_atr(m1, int(self._cfg.get("atr_lookback_bars", 14)))
    avg_atr = _approx_atr(m1[:-14], int(self._cfg.get("atr_lookback_bars", 14))) if len(m1) > 28 else atr
    health["atr_1m"] = float(atr) if atr is not None else None
    health["avg_atr_1m"] = float(avg_atr) if avg_atr is not None else None

    # Trend vs VWAP / bias
    if features.bias_5m == Bias.BULLISH:
      scores["trending_up"] += 30
      reasons.append("spot_above_vwap")
    elif features.bias_5m == Bias.BEARISH:
      scores["trending_down"] += 30
      reasons.append("spot_below_vwap")
    else:
      scores["sideways"] += 20
      reasons.append("spot_at_vwap")

    structure = str(extra.get("structure_5m") or _structure_5m(
      m5, int(self._cfg.get("structure_lookback_5m", 6))
    ))
    health["structure_5m"] = structure
    if structure == "hhhl":
      scores["trending_up"] += 20
    elif structure == "lllh":
      scores["trending_down"] += 20
    elif structure == "mixed":
      scores["sideways"] += 25
      reasons.append("mixed_5m_structure")

    # Range / volatility
    high_mult = float(self._cfg.get("high_vol_atr_mult", 1.4))
    low_mult = float(self._cfg.get("low_vol_atr_mult", 0.7))
    if atr is not None and avg_atr is not None and avg_atr > 0:
      if atr >= avg_atr * Decimal(str(high_mult)):
        scores["high_volatility"] += 35
        reasons.append("atr_elevated")
      elif atr <= avg_atr * Decimal(str(low_mult)):
        scores["low_volatility"] += 30
        reasons.append("atr_compressed")
      else:
        scores["low_volatility"] += 8
        scores["high_volatility"] += 8

    sideways_pts = Decimal(str(self._cfg.get("sideways_range_points", 25)))
    if len(m5) >= 4:
      window = m5[-6:]
      hi = max(c.high for c in window)
      lo = min(c.low for c in window)
      rng = hi - lo
      health["range_5m_points"] = float(rng)
      if rng <= sideways_pts and structure == "mixed":
        scores["sideways"] += 30
        reasons.append("tight_5m_range")

    # Session phase
    open_t = time(9, 15)
    minutes = int((now_ist.hour * 60 + now_ist.minute) - (open_t.hour * 60 + open_t.minute))
    minutes = max(0, minutes)
    health["minutes_since_open"] = minutes
    opening_m = int(self._cfg.get("opening_phase_minutes", 30))
    late_m = int(self._cfg.get("late_phase_after_minutes", 330))
    if minutes < opening_m:
      scores["opening_range"] += 25
      reasons.append("opening_phase")
      health["session_phase"] = "open"
    elif minutes >= late_m:
      scores["sideways"] += 10
      health["session_phase"] = "late"
      reasons.append("late_session")
    else:
      health["session_phase"] = "mid"

    # Breakout heuristic: stretch from VWAP + rising ATR
    dist = extra.get("distance_to_vwap_points")
    if dist is not None and abs(float(dist)) >= 20 and features.bias_5m != Bias.NEUTRAL:
      scores["breakout"] += 20
      reasons.append("extended_from_vwap")

    if is_expiry_day:
      scores["expiry_behaviour"] += 30
      reasons.append("expiry_day")

    probs = _normalize_100(scores)
    primary = max(probs, key=probs.get)

    risk = 20
    if probs.get("high_volatility", 0) >= 25:
      risk += 25
    if probs.get("sideways", 0) >= 30:
      risk += 20
    if probs.get("expiry_behaviour", 0) >= 20:
      risk += 15
    if health.get("session_phase") == "late":
      risk += 10
    if health.get("session_phase") == "open":
      risk += 10
    risk = min(100, risk)
    health["risk_score"] = risk

    max_risk = int(self._cfg.get("max_risk_score_to_trade", 75))
    trade_allowed = risk <= max_risk
    if bool(self._cfg.get("block_sideways", True)) and primary == "sideways":
      trade_allowed = False
      reasons.append("primary_sideways_blocks_trade")
    if bool(self._cfg.get("block_high_volatility", False)) and primary == "high_volatility":
      trade_allowed = False
      reasons.append("primary_high_vol_blocks_trade")
    if spot is None or vwap is None:
      trade_allowed = False
      reasons.append("missing_spot_or_vwap")

    return MarketRegime(
      ts=datetime.now(tz=timezone.utc),
      primary=primary,
      probabilities=probs,
      trade_allowed=trade_allowed,
      risk_score=risk,
      reasons=reasons,
      health=health,
    )


def _approx_atr(candles: list[Candle], lookback: int) -> Decimal | None:
  if len(candles) < 2:
    return None
  window = candles[-lookback:] if len(candles) >= lookback else candles
  if not window:
    return None
  ranges = [c.high - c.low for c in window]
  return sum(ranges, Decimal("0")) / Decimal(len(ranges))


def _structure_5m(m5: list[Candle], lookback: int) -> str:
  if len(m5) < max(3, lookback // 2):
    return "mixed"
  window = m5[-lookback:] if len(m5) >= lookback else m5
  highs = [c.high for c in window]
  lows = [c.low for c in window]
  hh = highs[-1] >= max(highs[:-1])
  hl = lows[-1] >= min(lows[:-1])
  ll = lows[-1] <= min(lows[:-1])
  lh = highs[-1] <= max(highs[:-1])
  if hh and hl:
    return "hhhl"
  if ll and lh:
    return "lllh"
  return "mixed"


def _normalize_100(scores: dict[str, float]) -> dict[str, float]:
  total = sum(scores.values())
  if total <= 0:
    n = len(scores)
    return {k: round(100.0 / n, 2) for k in scores}
  raw = {k: (v / total) * 100.0 for k, v in scores.items()}
  # Round and fix drift so sum == 100
  rounded = {k: round(v, 2) for k, v in raw.items()}
  drift = round(100.0 - sum(rounded.values()), 2)
  top = max(rounded, key=rounded.get)
  rounded[top] = round(rounded[top] + drift, 2)
  return rounded
