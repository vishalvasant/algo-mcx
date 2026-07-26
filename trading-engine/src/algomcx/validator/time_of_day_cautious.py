"""Time-of-day cautious entry gates (open / mid / late session)."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from algomcx.models.events import CandidateSignal

IST = ZoneInfo("Asia/Kolkata")


def _parse_time(raw: str) -> time:
    return time.fromisoformat(str(raw))


def active_phase(cfg: dict, clock: time) -> tuple[str, dict] | tuple[None, None]:
    phases = cfg.get("phases") or {}
    for name, phase in phases.items():
        if not isinstance(phase, dict):
            continue
        start = _parse_time(phase.get("start", "00:00"))
        end = _parse_time(phase.get("end", "23:59"))
        if start <= clock <= end:
            return str(name), phase
    return None, None


def time_of_day_lot_cap(cfg: dict, now: datetime | None = None) -> int | None:
    if not cfg.get("enabled", False):
        return None
    clock = (now or datetime.now(IST)).astimezone(IST).time()
    _, phase = active_phase(cfg, clock)
    if not phase:
        return None
    raw = phase.get("max_lots")
    if raw is None:
        return None
    return max(1, int(raw))


def time_of_day_cautious_rejection_reasons(
    signal: CandidateSignal,
    cautious_cfg: dict,
    *,
    now: datetime | None = None,
) -> list[str]:
    if not cautious_cfg.get("enabled", False):
        return []

    clock = (now or datetime.now(IST)).astimezone(IST).time()
    phase_name, phase = active_phase(cautious_cfg, clock)
    if not phase:
        return []

    reasons: list[str] = []
    prefix = f"tod_{phase_name}"

    min_conf = phase.get("min_confidence")
    if min_conf is not None:
        conf = int(signal.confidence or 0)
        if conf < int(min_conf):
            reasons.append(f"{prefix}_confidence_too_low")

    allowed = phase.get("allowed_setups")
    if isinstance(allowed, list) and len(allowed) > 0:
        if signal.setup_type not in allowed:
            reasons.append(f"{prefix}_setup_not_allowed")

    blocked = phase.get("blocked_setups") or []
    if signal.setup_type in blocked:
        reasons.append(f"{prefix}_setup_blocked")

    min_mtf = phase.get("min_mtf_score")
    if min_mtf is not None:
        extra = signal.feature_snapshot.extra or {}
        mtf_raw = extra.get("mtf_score_ce" if signal.side == "CE" else "mtf_score_pe")
        if mtf_raw is not None and int(mtf_raw) < int(min_mtf):
            reasons.append(f"{prefix}_mtf_too_low")

    return reasons
