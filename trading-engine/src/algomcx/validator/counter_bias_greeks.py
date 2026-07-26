"""Greek + chain validation for counter-bias (fade) entries."""
from __future__ import annotations

from algomcx.models.events import CandidateSignal

COUNTER_BIAS_SETUPS = frozenset({"peak_reversal_fade"})


def counter_bias_greek_reasons(
    signal: CandidateSignal,
    trap_cfg: dict,
) -> list[str]:
    cb = trap_cfg.get("counter_bias") or {}
    if not cb.get("require_greeks", True):
        return []
    if signal.setup_type not in COUNTER_BIAS_SETUPS:
        return []

    reasons: list[str] = []
    pick = (signal.scanner_metadata or {}).get("strike_pick") or {}
    extra = signal.feature_snapshot.extra or {}
    chain = extra.get("chain") or {}

    delta = pick.get("delta")
    gamma = pick.get("gamma")
    iv = pick.get("iv")

    min_d = float(cb.get("min_abs_delta", 0.30))
    max_d = float(cb.get("max_abs_delta", 0.55))
    min_gamma = float(cb.get("min_gamma", 0.0004))
    max_iv = float(cb.get("max_iv", 0.85))

    if delta is None:
        reasons.append("counter_greek_delta_missing")
    else:
        ad = abs(float(delta))
        if ad < min_d or ad > max_d:
            reasons.append("counter_greek_delta_out_of_band")
        if signal.side == "PE" and float(delta) > -0.22:
            reasons.append("counter_pe_delta_weak")
        if signal.side == "CE" and float(delta) < 0.22:
            reasons.append("counter_ce_delta_weak")

    if gamma is not None and float(gamma) < min_gamma:
        reasons.append("counter_gamma_too_low")

    if iv is not None:
        ivf = float(iv)
        if ivf > 1.5:
            ivf = ivf / 100.0
        if ivf > max_iv:
            reasons.append("counter_iv_too_high")

    if cb.get("require_oi_confirm", True):
        if signal.side == "PE" and not chain.get("oi_confirms_pe"):
            reasons.append("counter_oi_not_confirming_pe")
        if signal.side == "CE" and not chain.get("oi_confirms_ce"):
            reasons.append("counter_oi_not_confirming_ce")

    return reasons
