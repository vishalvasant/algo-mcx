from datetime import datetime, timezone
from decimal import Decimal

from algomcx.models.events import Bias, CandidateSignal, FeatureSnapshot
from algomcx.validator.trap_avoidance import trap_rejection_reasons


def _signal(
    *,
    side: str = "CE",
    setup: str = "vwap_bounce",
    spot: str = "24100",
    vwap: str = "24150",
    bias: Bias = Bias.BULLISH,
    ema9: str | None = None,
    ema21: str | None = None,
    structure_5m: str | None = None,
    bias_1m: str | None = None,
    trigger: str | None = None,
    bars_with: int = 3,
    bars_against: int = 0,
) -> CandidateSignal:
    extra: dict = {
        "bars_with_vwap_3m": bars_with,
        "bars_against_vwap_3m": bars_against,
    }
    if ema9 is not None:
        extra["ema9"] = ema9
    if ema21 is not None:
        extra["ema21"] = ema21
    if structure_5m is not None:
        extra["structure_5m"] = structure_5m
    if bias_1m is not None:
        extra["bias_1m"] = bias_1m
    if trigger is not None:
        extra["trigger_vwap_pullback"] = trigger
    return CandidateSignal(
        ts=datetime.now(tz=timezone.utc),
        setup_type=setup,
        side=side,
        instrument_token="1",
        tsym="NIFTY21JUL26C24200",
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal(spot),
            session_vwap=Decimal(vwap),
            bias_5m=bias,
            extra=extra,
        ),
    )


TRAP_CFG = {
    "enabled": True,
    "require_spot_vwap_alignment": True,
    "spot_vwap_buffer_points": 10,
    "blocked_setups": ["vwap_bounce", "ema_pullback"],
    "require_ema_alignment_for": ["vwap_pullback"],
    "block_reversal_flips": True,
    "require_bias_side_match": True,
    "require_1m_5m_bias_agree": True,
    "require_5m_structure_align": True,
    "require_3m_bars_with_bias": True,
    "min_3m_bars_with_bias": 2,
    "require_pullback_trigger": True,
    "require_mtf_alignment": True,
    "min_mtf_score": 55,
}


def test_blocks_vwap_bounce_setup() -> None:
    sig = _signal(setup="vwap_bounce", spot="24200", vwap="24100")
    reasons = trap_rejection_reasons(sig, TRAP_CFG)
    assert "setup_blocked" in reasons


def test_blocks_ce_below_vwap_trap() -> None:
    sig = _signal(setup="vwap_pullback", spot="24100", vwap="24150", trigger="vwap_pullback_bounce_up")
    reasons = trap_rejection_reasons(sig, TRAP_CFG)
    assert "ce_below_vwap_trap" in reasons


def test_blocks_pullback_without_trigger() -> None:
    sig = _signal(
        setup="vwap_pullback",
        spot="24200",
        vwap="24150",
        ema9="24180",
        ema21="24160",
        structure_5m="hhhl",
        bias_1m="bullish",
    )
    reasons = trap_rejection_reasons(sig, TRAP_CFG)
    assert "pullback_trigger_missing" in reasons


def test_blocks_1m_5m_conflict() -> None:
    sig = _signal(
        setup="vwap_pullback",
        spot="24200",
        vwap="24150",
        trigger="vwap_pullback_bounce_up",
        ema9="24180",
        ema21="24160",
        structure_5m="hhhl",
        bias_1m="bearish",
    )
    reasons = trap_rejection_reasons(sig, TRAP_CFG)
    assert "ce_1m_5m_bias_conflict" in reasons


def test_allows_aligned_pe_pullback() -> None:
    sig = _signal(
        side="PE",
        setup="vwap_pullback",
        spot="24100",
        vwap="24150",
        bias=Bias.BEARISH,
        ema9="24120",
        ema21="24140",
        structure_5m="lllh",
        bias_1m="bearish",
        trigger="vwap_pullback_bounce_down",
    )
    # Inject passing MTF score
    sig.feature_snapshot.extra["mtf_score_pe"] = 70
    reasons = trap_rejection_reasons(sig, TRAP_CFG)
    assert reasons == []
