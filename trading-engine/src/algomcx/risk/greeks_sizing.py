"""Scale deployable capital by latest strike greeks confirmation."""
from __future__ import annotations

from decimal import Decimal

from algomcx.models.events import CandidateSignal, OptionState


def greeks_confirmation_multiplier(
    signal: CandidateSignal,
    option: OptionState | None,
    risk_cfg: dict,
) -> Decimal:
    """Return 0.55–1.0 multiplier applied to confidence-based capital budget."""
    del option  # strike_pick already computed at scan with BS greeks
    cfg = risk_cfg.get("greeks_lot_sizing") or {}
    if not cfg.get("enabled", True):
        return Decimal("1")

    pick = (signal.scanner_metadata or {}).get("strike_pick") or {}
    delta = pick.get("delta")
    gamma = pick.get("gamma")
    iv = pick.get("iv")

    ideal_min = float(cfg.get("ideal_delta_min", 0.35))
    ideal_max = float(cfg.get("ideal_delta_max", 0.65))
    near_min = float(cfg.get("near_delta_min", 0.25))
    near_max = float(cfg.get("near_delta_max", 0.75))
    min_mult = Decimal(str(cfg.get("min_multiplier", 0.55)))
    max_iv = float(cfg.get("max_iv", 0.85))
    min_gamma_ok = float(cfg.get("min_gamma_ok", 0.0004))
    min_gamma_strong = float(cfg.get("min_gamma_strong", 0.001))

    mult = Decimal("0.65")
    if delta is not None:
        ad = abs(float(delta))
        if ideal_min <= ad <= ideal_max:
            mult = Decimal("1")
        elif near_min <= ad <= near_max:
            mult = Decimal("0.88")
        else:
            mult = Decimal("0.7")
    else:
        mult = Decimal("0.75")

    if gamma is not None:
        g = float(gamma)
        if g >= min_gamma_strong:
            mult = min(Decimal("1"), mult + Decimal("0.03"))
        elif g < min_gamma_ok:
            mult = mult * Decimal("0.92")

    if iv is not None:
        ivf = float(iv)
        if ivf > 1.5:
            ivf /= 100.0
        if ivf > max_iv:
            mult = mult * Decimal("0.8")

    return max(min_mult, min(Decimal("1"), mult))
