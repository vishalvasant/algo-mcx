"""Multi-timeframe candle pattern detection for index (NIFTY) bars.

Phase A: detect patterns per TF (1m / 3m / 5m).
Phase B: build alignment score (0–100) for CE/PE.
Phase C: expose score + details for Decision Logs / entry gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from algomcx.models.events import Bias, Candle


@dataclass
class TfPatterns:
    timeframe: str
    bullish_engulf: bool = False
    bearish_engulf: bool = False
    bullish_pin: bool = False
    bearish_pin: bool = False
    inside_bar: bool = False
    momentum_up: bool = False  # 3 rising closes
    momentum_down: bool = False
    structure: str | None = None  # hhhl | lllh | mixed
    close_vs_vwap: str | None = None  # above | below | at
    reclaim_up: bool = False
    reclaim_down: bool = False
    labels: list[str] = field(default_factory=list)


def _body(c: Candle) -> Decimal:
    return abs(c.close - c.open)


def _range(c: Candle) -> Decimal:
    return max(c.high - c.low, Decimal("0.01"))


def detect_tf_patterns(
    bars: list[Candle],
    *,
    timeframe: str,
    vwap: Decimal | None = None,
) -> TfPatterns:
    out = TfPatterns(timeframe=timeframe)
    if len(bars) < 2:
        return out

    a, b = bars[-2], bars[-1]
    rng = _range(b)
    body = _body(b)
    upper = b.high - max(b.open, b.close)
    lower = min(b.open, b.close) - b.low

    # Engulfing
    if b.close > b.open and a.close < a.open:
        if b.close >= a.open and b.open <= a.close and body > _body(a):
            out.bullish_engulf = True
            out.labels.append("bullish_engulf")
    if b.close < b.open and a.close > a.open:
        if b.open >= a.close and b.close <= a.open and body > _body(a):
            out.bearish_engulf = True
            out.labels.append("bearish_engulf")

    # Pin bar (wick ≥ 2× body, body in opposite third)
    if body > 0:
        if lower >= body * Decimal("2") and upper <= body:
            out.bullish_pin = True
            out.labels.append("bullish_pin")
        if upper >= body * Decimal("2") and lower <= body:
            out.bearish_pin = True
            out.labels.append("bearish_pin")

    # Inside bar
    if b.high <= a.high and b.low >= a.low:
        out.inside_bar = True
        out.labels.append("inside_bar")

    # 3-bar momentum
    if len(bars) >= 3:
        c0, c1, c2 = bars[-3].close, bars[-2].close, bars[-1].close
        if c2 > c1 > c0:
            out.momentum_up = True
            out.labels.append("momentum_up")
        if c2 < c1 < c0:
            out.momentum_down = True
            out.labels.append("momentum_down")

    # Structure over last 4 bars
    window = bars[-4:] if len(bars) >= 4 else bars
    if len(window) >= 3:
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        hh = highs[-1] >= max(highs[:-1])
        hl = lows[-1] >= min(lows[:-1])
        ll = lows[-1] <= min(lows[:-1])
        lh = highs[-1] <= max(highs[:-1])
        if hh and hl:
            out.structure = "hhhl"
            out.labels.append("hhhl")
        elif ll and lh:
            out.structure = "lllh"
            out.labels.append("lllh")
        else:
            out.structure = "mixed"

    if vwap is not None:
        if b.close > vwap:
            out.close_vs_vwap = "above"
        elif b.close < vwap:
            out.close_vs_vwap = "below"
        else:
            out.close_vs_vwap = "at"
        if a.close < vwap <= b.close:
            out.reclaim_up = True
            out.labels.append("vwap_reclaim_up")
        if a.close > vwap >= b.close:
            out.reclaim_down = True
            out.labels.append("vwap_reclaim_down")

    return out


def _side_score(patterns: TfPatterns, side: str) -> int:
    """0–40 contribution from one TF."""
    score = 0
    if side == "CE":
        if patterns.bullish_engulf:
            score += 10
        if patterns.bullish_pin:
            score += 8
        if patterns.momentum_up:
            score += 8
        if patterns.structure == "hhhl":
            score += 8
        if patterns.close_vs_vwap == "above":
            score += 6
        if patterns.reclaim_up:
            score += 6
        if patterns.bearish_engulf or patterns.momentum_down or patterns.structure == "lllh":
            score -= 12
        if patterns.close_vs_vwap == "below":
            score -= 8
    else:
        if patterns.bearish_engulf:
            score += 10
        if patterns.bearish_pin:
            score += 8
        if patterns.momentum_down:
            score += 8
        if patterns.structure == "lllh":
            score += 8
        if patterns.close_vs_vwap == "below":
            score += 6
        if patterns.reclaim_down:
            score += 6
        if patterns.bullish_engulf or patterns.momentum_up or patterns.structure == "hhhl":
            score -= 12
        if patterns.close_vs_vwap == "above":
            score -= 8
    return max(0, min(40, score))


@dataclass
class MtfAlignment:
    score_ce: int
    score_pe: int
    m1: TfPatterns
    m3: TfPatterns
    m5: TfPatterns
    details: dict[str, Any]

    def score_for_side(self, side: str) -> int:
        return self.score_ce if side == "CE" else self.score_pe

    def passes(self, side: str, min_score: int) -> bool:
        return self.score_for_side(side) >= min_score


def build_mtf_alignment(
    *,
    m1: list[Candle],
    m3: list[Candle],
    m5: list[Candle],
    vwap: Decimal | None,
    bias: Bias | None = None,
    structure_5m: str | None = None,
) -> MtfAlignment:
    p1 = detect_tf_patterns(m1, timeframe="1m", vwap=vwap)
    p3 = detect_tf_patterns(m3, timeframe="3m", vwap=vwap)
    p5 = detect_tf_patterns(m5, timeframe="5m", vwap=vwap)

    # Weighted: 5m 40%, 3m 35%, 1m 25% of raw TF scores (each 0–40) → normalize to 0–100
    def _combine(side: str) -> int:
        raw = (
            _side_score(p5, side) * Decimal("0.40")
            + _side_score(p3, side) * Decimal("0.35")
            + _side_score(p1, side) * Decimal("0.25")
        )
        # Scale: max raw ≈ 40 → map to 100
        scaled = int(round(float(raw) / 40.0 * 100.0))
        # Bias bonus/penalty
        if bias == Bias.BULLISH and side == "CE":
            scaled += 5
        elif bias == Bias.BEARISH and side == "PE":
            scaled += 5
        elif bias == Bias.BULLISH and side == "PE":
            scaled -= 10
        elif bias == Bias.BEARISH and side == "CE":
            scaled -= 10
        return max(0, min(100, scaled))

    score_ce = _combine("CE")
    score_pe = _combine("PE")
    # When 5m structure already confirms trend, don't let 1m noise drag MTF below entry gates.
    if structure_5m == "hhhl" and bias == Bias.BULLISH:
        score_ce = min(100, score_ce + 18)
    elif structure_5m == "lllh" and bias == Bias.BEARISH:
        score_pe = min(100, score_pe + 18)
    details = {
        "score_ce": score_ce,
        "score_pe": score_pe,
        "m1_labels": p1.labels,
        "m3_labels": p3.labels,
        "m5_labels": p5.labels,
        "m1_structure": p1.structure,
        "m3_structure": p3.structure,
        "m5_structure": p5.structure,
        "m1_vs_vwap": p1.close_vs_vwap,
        "m5_vs_vwap": p5.close_vs_vwap,
    }
    return MtfAlignment(
        score_ce=score_ce,
        score_pe=score_pe,
        m1=p1,
        m3=p3,
        m5=p5,
        details=details,
    )
