"""Config-driven entry traps filter — blocks setups that lead to adverse_momentum."""
from __future__ import annotations

from decimal import Decimal

from algomcx.models.events import Bias, CandidateSignal


def trap_rejection_reasons(
    signal: CandidateSignal,
    trap_cfg: dict,
) -> list[str]:
    if not trap_cfg.get("enabled", False):
        return []

    reasons: list[str] = []
    setup = signal.setup_type

    blocked = set(trap_cfg.get("blocked_setups") or [])
    if setup in blocked:
        reasons.append("setup_blocked")

    if trap_cfg.get("block_reversal_flips", False) and setup == "trend_reversal_flip":
        reasons.append("reversal_flip_blocked")

    feat = signal.feature_snapshot
    spot = feat.nifty_spot
    vwap = feat.session_vwap
    extra = feat.extra or {}

    if trap_cfg.get("require_bias_side_match", True):
        if signal.side == "CE" and feat.bias_5m == Bias.BEARISH:
            reasons.append("ce_against_bearish_bias")
        if signal.side == "PE" and feat.bias_5m == Bias.BULLISH:
            reasons.append("pe_against_bullish_bias")

    # Block when 1m bias fights 5m bias (chop / fake reclaim).
    if trap_cfg.get("require_1m_5m_bias_agree", True):
        bias_1m = str(extra.get("bias_1m") or "")
        if signal.side == "CE" and bias_1m == "bearish":
            reasons.append("ce_1m_5m_bias_conflict")
        if signal.side == "PE" and bias_1m == "bullish":
            reasons.append("pe_1m_5m_bias_conflict")

    buffer = Decimal(str(trap_cfg.get("spot_vwap_buffer_points", 8)))
    if trap_cfg.get("require_spot_vwap_alignment", True) and spot is not None and vwap is not None:
        if signal.side == "CE" and spot < vwap + buffer:
            reasons.append("ce_below_vwap_trap")
        if signal.side == "PE" and spot > vwap - buffer:
            reasons.append("pe_above_vwap_trap")

    # Multi-TF structure: 5m HHHL for CE, LLLH for PE (or at least not opposing).
    if trap_cfg.get("require_5m_structure_align", True):
        structure = str(extra.get("structure_5m") or "")
        if signal.side == "CE" and structure == "lllh":
            reasons.append("ce_against_5m_structure")
        if signal.side == "PE" and structure == "hhhl":
            reasons.append("pe_against_5m_structure")

    # Prefer trend continuation only when 3m bars are with VWAP side.
    if trap_cfg.get("require_3m_bars_with_bias", True):
        bars_with = int(extra.get("bars_with_vwap_3m") or 0)
        bars_against = int(extra.get("bars_against_vwap_3m") or 0)
        min_with = int(trap_cfg.get("min_3m_bars_with_bias", 2))
        if setup in ("trend_continuation", "vwap_trend", "momentum_continuation", "ema_pullback"):
            if bars_with < min_with or bars_against > bars_with:
                reasons.append("3m_structure_weak")

    # Pullback-family must have 1m bounce trigger (FeatureSetupScanner used to skip this).
    if trap_cfg.get("require_pullback_trigger", True):
        if setup in ("vwap_pullback", "vwap_bounce"):
            trigger = extra.get("trigger_vwap_pullback")
            if not trigger:
                reasons.append("pullback_trigger_missing")

    # Multi-TF candle alignment (Phase B/C) — block weak structure entries.
    if trap_cfg.get("require_mtf_alignment", True):
        min_mtf = int(trap_cfg.get("min_mtf_score", 55))
        if signal.side == "CE":
            mtf = int(extra.get("mtf_score_ce") or 0)
        else:
            mtf = int(extra.get("mtf_score_pe") or 0)
        if mtf < min_mtf:
            reasons.append("mtf_score_too_low")

    ema_setups = set(trap_cfg.get("require_ema_alignment_for") or [])
    if setup in ema_setups and spot is not None:
        e9 = extra.get("ema9")
        e21 = extra.get("ema21")
        if e9 is not None and e21 is not None:
            e9d, e21d = Decimal(str(e9)), Decimal(str(e21))
            if signal.side == "CE" and not (e9d > e21d and spot >= e21d):
                reasons.append("ema_structure_not_bull")
            if signal.side == "PE" and not (e9d < e21d and spot <= e21d):
                reasons.append("ema_structure_not_bear")

    return reasons
