"""Counter-bias peak reversal detection using 5m + 15m structure."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from algomcx.features.mtf_patterns import detect_tf_patterns
from algomcx.models.events import Bias, Candle


@dataclass
class CounterBiasResult:
    setup: str | None = None  # bull | bear
    score_ce: int = 0
    score_pe: int = 0
    signals: list[str] = field(default_factory=list)
    m5_labels: list[str] = field(default_factory=list)
    m15_labels: list[str] = field(default_factory=list)
    bias_confidence_mismatch: bool = False
    mtf_confidence_gap: int = 0
    bias_side_mtf_score: int | None = None
    counter_side_mtf_score: int | None = None
    trigger_reason: str | None = None  # peak_extension | bias_confidence_mismatch


def evaluate_bias_confidence_mismatch(
    bias: Bias,
    mtf_score_ce: int,
    mtf_score_pe: int,
    cfg: dict,
) -> dict:
    """Detect when counter-side MTF confidence leads the bias-aligned side."""
    min_gap = int(cfg.get("min_bias_confidence_gap", 15))
    max_bias_side = int(cfg.get("max_bias_side_mtf_score", 52))

    if bias == Bias.BULLISH:
        gap = mtf_score_pe - mtf_score_ce
        if mtf_score_ce <= max_bias_side and gap >= min_gap:
            return {
                "mismatch": True,
                "fade_setup": "bear",
                "gap": gap,
                "bias_side_score": mtf_score_ce,
                "counter_side_score": mtf_score_pe,
                "label": "bullish_bias_pe_confidence_leads",
            }
    elif bias == Bias.BEARISH:
        gap = mtf_score_ce - mtf_score_pe
        if mtf_score_pe <= max_bias_side and gap >= min_gap:
            return {
                "mismatch": True,
                "fade_setup": "bull",
                "gap": gap,
                "bias_side_score": mtf_score_pe,
                "counter_side_score": mtf_score_ce,
                "label": "bearish_bias_ce_confidence_leads",
            }
    return {"mismatch": False}


def build_counter_mtf_scores(
    *,
    m5: list[Candle],
    m15: list[Candle],
    vwap: Decimal | None,
) -> tuple[int, int, dict]:
    """MTF alignment for fading the primary trend (5m 55%, 15m 45%)."""
    from algomcx.features.mtf_patterns import _side_score

    p5 = detect_tf_patterns(m5, timeframe="5m", vwap=vwap)
    p15 = detect_tf_patterns(m15, timeframe="15m", vwap=vwap)
    raw_pe = float(_side_score(p5, "PE")) * 0.55 + float(_side_score(p15, "PE")) * 0.45
    raw_ce = float(_side_score(p5, "CE")) * 0.55 + float(_side_score(p15, "CE")) * 0.45
    score_pe = max(0, min(100, int(round(raw_pe / 40.0 * 100.0))))
    score_ce = max(0, min(100, int(round(raw_ce / 40.0 * 100.0))))
    details = {
        "m5_labels": p5.labels,
        "m15_labels": p15.labels,
        "m5_structure": p5.structure,
        "m15_structure": p15.structure,
    }
    return score_ce, score_pe, details


def _reversal_signals_m5(m5: list[Candle], vwap: Decimal | None) -> list[str]:
    if len(m5) < 3:
        return []
    p = detect_tf_patterns(m5, timeframe="5m", vwap=vwap)
    out: list[str] = []
    if p.bearish_engulf:
        out.append("m5_bearish_engulf")
    if p.bearish_pin:
        out.append("m5_bearish_pin")
    if p.momentum_down:
        out.append("m5_momentum_down")
    if p.reclaim_down:
        out.append("m5_vwap_reclaim_down")
    if p.structure in ("lllh", "mixed"):
        out.append(f"m5_structure_{p.structure}")
    if len(m5) >= 4:
        prior_high = max(c.high for c in m5[-4:-1])
        if m5[-1].high < prior_high:
            out.append("m5_lower_high")
    return out


def _reversal_signals_m15(m15: list[Candle], vwap: Decimal | None) -> list[str]:
    if len(m15) < 3:
        return []
    p = detect_tf_patterns(m15, timeframe="15m", vwap=vwap)
    out: list[str] = []
    if p.bearish_engulf:
        out.append("m15_bearish_engulf")
    if p.bearish_pin:
        out.append("m15_bearish_pin")
    if p.momentum_down:
        out.append("m15_momentum_down")
    if p.reclaim_down:
        out.append("m15_vwap_reclaim_down")
    if p.structure in ("lllh", "mixed"):
        out.append(f"m15_structure_{p.structure}")
    if len(m15) >= 3 and m15[-1].close < m15[-2].close:
        out.append("m15_close_roll_down")
    if vwap is not None and m15[-1].close < vwap and m15[-2].close >= vwap:
        out.append("m15_close_below_vwap")
    return out


def _bull_reversal_signals_m5(m5: list[Candle], vwap: Decimal | None) -> list[str]:
    if len(m5) < 3:
        return []
    p = detect_tf_patterns(m5, timeframe="5m", vwap=vwap)
    out: list[str] = []
    if p.bullish_engulf:
        out.append("m5_bullish_engulf")
    if p.bullish_pin:
        out.append("m5_bullish_pin")
    if p.momentum_up:
        out.append("m5_momentum_up")
    if p.reclaim_up:
        out.append("m5_vwap_reclaim_up")
    if p.structure in ("hhhl", "mixed"):
        out.append(f"m5_structure_{p.structure}")
    if len(m5) >= 4:
        prior_low = min(c.low for c in m5[-4:-1])
        if m5[-1].low > prior_low:
            out.append("m5_higher_low")
    return out


def _bull_reversal_signals_m15(m15: list[Candle], vwap: Decimal | None) -> list[str]:
    if len(m15) < 3:
        return []
    p = detect_tf_patterns(m15, timeframe="15m", vwap=vwap)
    out: list[str] = []
    if p.bullish_engulf:
        out.append("m15_bullish_engulf")
    if p.bullish_pin:
        out.append("m15_bullish_pin")
    if p.momentum_up:
        out.append("m15_momentum_up")
    if p.reclaim_up:
        out.append("m15_vwap_reclaim_up")
    if p.structure in ("hhhl", "mixed"):
        out.append(f"m15_structure_{p.structure}")
    if len(m15) >= 3 and m15[-1].close > m15[-2].close:
        out.append("m15_close_roll_up")
    if vwap is not None and m15[-1].close > vwap and m15[-2].close <= vwap:
        out.append("m15_close_above_vwap")
    return out


def detect_peak_reversal_fade(
    *,
    bias: Bias,
    spot: Decimal | None,
    vwap: Decimal | None,
    m5: list[Candle],
    m15: list[Candle],
    ind: dict,
    mtf_score_ce: int = 0,
    mtf_score_pe: int = 0,
    cfg: dict | None = None,
) -> CounterBiasResult:
    """Fade extended moves or bias-confidence divergences (5m/15m)."""
    cfg = cfg or {}
    min_ext = Decimal(str(cfg.get("min_extension_points", 20)))
    mismatch_min_ext = Decimal(str(cfg.get("mismatch_min_extension_points", 12)))
    min_signals = int(cfg.get("min_reversal_signals", 2))
    mismatch_min_signals = int(cfg.get("mismatch_min_reversal_signals", 1))
    min_counter_mtf = int(cfg.get("min_counter_mtf_score", 48))
    mismatch_mtf_floor = int(cfg.get("mismatch_min_counter_mtf_score", min_counter_mtf - 5))

    score_ce, score_pe, mtf_details = build_counter_mtf_scores(m5=m5, m15=m15, vwap=vwap)
    mismatch = evaluate_bias_confidence_mismatch(bias, mtf_score_ce, mtf_score_pe, cfg)
    result = CounterBiasResult(
        score_ce=score_ce,
        score_pe=score_pe,
        m5_labels=mtf_details.get("m5_labels", []),
        m15_labels=mtf_details.get("m15_labels", []),
        bias_confidence_mismatch=bool(mismatch.get("mismatch")),
        mtf_confidence_gap=int(mismatch.get("gap") or 0),
        bias_side_mtf_score=mismatch.get("bias_side_score"),
        counter_side_mtf_score=mismatch.get("counter_side_score"),
    )

    if spot is None or vwap is None or bias == Bias.NEUTRAL:
        return result

    extension = spot - vwap
    structure_5m = str(ind.get("structure_5m") or "")
    bias_1m = str(ind.get("bias_1m") or "")

    def _try_pe_fade(bear_sigs: list[str]) -> bool:
        peak_ok = (
            extension >= min_ext
            and len(bear_sigs) >= min_signals
            and score_pe >= min_counter_mtf
        )
        mismatch_ok = (
            mismatch.get("mismatch")
            and mismatch.get("fade_setup") == "bear"
            and extension >= mismatch_min_ext
            and len(bear_sigs) >= mismatch_min_signals
            and score_pe >= mismatch_mtf_floor
        )
        if peak_ok:
            result.trigger_reason = "peak_extension"
            return True
        if mismatch_ok:
            bear_sigs.append(str(mismatch.get("label")))
            bear_sigs.append("bias_side_confidence_mismatch")
            result.trigger_reason = "bias_confidence_mismatch"
            return True
        return False

    def _try_ce_fade(bull_sigs: list[str]) -> bool:
        peak_ok = (
            (-extension) >= min_ext
            and len(bull_sigs) >= min_signals
            and score_ce >= min_counter_mtf
        )
        mismatch_ok = (
            mismatch.get("mismatch")
            and mismatch.get("fade_setup") == "bull"
            and (-extension) >= mismatch_min_ext
            and len(bull_sigs) >= mismatch_min_signals
            and score_ce >= mismatch_mtf_floor
        )
        if peak_ok:
            result.trigger_reason = "peak_extension"
            return True
        if mismatch_ok:
            bull_sigs.append(str(mismatch.get("label")))
            bull_sigs.append("bias_side_confidence_mismatch")
            result.trigger_reason = "bias_confidence_mismatch"
            return True
        return False

    # PE fade: bullish trend extended above VWAP, 5m/15m show exhaustion.
    if bias == Bias.BULLISH:
        bear_sigs = _reversal_signals_m5(m5, vwap) + _reversal_signals_m15(m15, vwap)
        if structure_5m == "hhhl":
            bear_sigs.append("trend_extended_hhhl")
        if bias_1m == "bearish":
            bear_sigs.append("1m_turning_bearish")
        result.signals = bear_sigs
        if _try_pe_fade(bear_sigs):
            result.setup = "bear"
            result.signals = bear_sigs

    # CE fade: bearish trend extended below VWAP.
    elif bias == Bias.BEARISH:
        bull_sigs = _bull_reversal_signals_m5(m5, vwap) + _bull_reversal_signals_m15(m15, vwap)
        if structure_5m == "lllh":
            bull_sigs.append("trend_extended_lllh")
        if bias_1m == "bullish":
            bull_sigs.append("1m_turning_bullish")
        result.signals = bull_sigs
        if _try_ce_fade(bull_sigs):
            result.setup = "bull"
            result.signals = bull_sigs

    return result
