"""Expiry-day cautious entry gates (replaces hard block-all)."""
from __future__ import annotations

from algomcx.models.events import CandidateSignal


def expiry_cautious_rejection_reasons(
    signal: CandidateSignal,
    cautious_cfg: dict,
) -> list[str]:
    if not cautious_cfg.get("enabled", False):
        return []

    reasons: list[str] = []
    min_conf = int(cautious_cfg.get("min_confidence", 88))
    conf = int(signal.confidence or 0)
    if conf < min_conf:
        reasons.append("expiry_confidence_too_low")

    allowed = cautious_cfg.get("allowed_setups")
    if isinstance(allowed, list) and len(allowed) > 0:
        if signal.setup_type not in allowed:
            reasons.append("expiry_setup_not_allowed")

    blocked = cautious_cfg.get("blocked_setups") or []
    if signal.setup_type in blocked:
        reasons.append("expiry_setup_blocked")

    feat = signal.feature_snapshot
    extra = feat.extra or {}
    min_mtf = int(cautious_cfg.get("min_mtf_score", 65))
    if signal.side == "CE":
        mtf_raw = extra.get("mtf_score_ce")
    else:
        mtf_raw = extra.get("mtf_score_pe")
    if mtf_raw is not None and int(mtf_raw) < min_mtf:
        reasons.append("expiry_mtf_too_low")

    return reasons
