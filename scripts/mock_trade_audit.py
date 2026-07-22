"""Mock end-to-end paper trade + historical VWAP-reclaim opportunity scan."""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.broker.paper import PaperBrokerAdapter
from algomcx.config import get_config
from algomcx.db.connection import close_pool, get_pool, init_pool
from algomcx.execution.engine import ExecutionEngine
from algomcx.features.engine import FeatureEngine
from algomcx.journal.writer import JournalWriter
from algomcx.market_data.engine import MarketDataEngine
from algomcx.market_data.vwap import session_vwap
from algomcx.bus.event_bus import EventBus
from algomcx.models.events import (
    Bias,
    CandleInterval,
    CandidateSignal,
    FeatureSnapshot,
    OptionState,
    QuoteUpdate,
    ValidationResult,
)
from algomcx.position.exit_rules import evaluate_momentum_exit
from algomcx.position.manager import PositionManager
from algomcx.risk.engine import RiskEngine
from algomcx.scanner.vwap_reclaim import VwapReclaimScanner
from algomcx.validator.engine import RuleValidator

IST = ZoneInfo("Asia/Kolkata")


def _print(title: str) -> None:
    print(f"\n=== {title} ===")


async def analyze_today_opportunities(market_data: MarketDataEngine) -> None:
    _print("ENTRY RULES (what must be true)")
    print(
        """
BULLISH CE entry (ALL required at the same scan):
  1. spot > session VWAP          → bias = bullish
  2. 3m: prev.close < VWAP <= curr.close  → setup_3m = vwap_reclaim_bull
  3. 1m: prev.close < VWAP <= curr.close  → trigger_1m = vwap_reclaim_cross_up
     (scanner only requires trigger_1m non-null today; direction not enforced)
  4. ATM CE LTP available
  5. Entry window 09:25–15:00 IST
  6. Risk: capital ok, kill switch off, not duplicate token, cooldown ok

BEARISH PE entry:
  1. spot < session VWAP          → bias = bearish
  2. 3m: prev.close > VWAP >= curr.close  → setup_3m = vwap_reclaim_bear
  3. 1m: prev.close > VWAP >= curr.close  → trigger_1m = vwap_reclaim_cross_down
  4. ATM PE LTP available
  + same window / risk gates

EXIT (momentum):
  - bias_flip (CE & spot<VWAP, or PE & spot>VWAP)
  - adverse premium drop ≥12% from entry
  - trail: after ≥4% profit, give back 40% of MFE
  - force_exit 15:20 IST
"""
    )

    m1 = market_data.candles(CandleInterval.M1)
    m3 = market_data.candles(CandleInterval.M3)
    features = FeatureEngine(get_config(), market_data).compute()
    _print("CURRENT FEATURE SNAPSHOT")
    print(
        f"spot={features.nifty_spot} vwap={features.session_vwap} "
        f"bias={features.bias_5m.value} setup={features.setup_3m} "
        f"trigger={features.trigger_1m}"
    )
    print(f"candle counts: m1={len(m1)} m3={len(m3)}")

    # Count simultaneous setup+trigger moments on today's candles
    bull_aligned = bear_aligned = 0
    bull_setup_only = bear_setup_only = 0
    bull_trig_only = bear_trig_only = 0
    for i in range(2, len(m1)):
        vwap = session_vwap(m1[: i + 1])
        if vwap is None:
            continue
        spot = m1[i].close
        bias_bull = spot > vwap
        bias_bear = spot < vwap
        p1, c1 = m1[i - 1].close, m1[i].close
        trig_up = p1 < vwap <= c1
        trig_dn = p1 > vwap >= c1
        # map to matching 3m bar
        j = min(len(m3) - 1, max(1, i // 3))
        if j < 1:
            continue
        p3, c3 = m3[j - 1].close, m3[j].close
        setup_bull = p3 < vwap <= c3
        setup_bear = p3 > vwap >= c3
        if setup_bull:
            bull_setup_only += 1
        if setup_bear:
            bear_setup_only += 1
        if trig_up:
            bull_trig_only += 1
        if trig_dn:
            bear_trig_only += 1
        if bias_bull and setup_bull and trig_up:
            bull_aligned += 1
        if bias_bear and setup_bear and trig_dn:
            bear_aligned += 1

    _print("TODAY HISTORICAL OPPORTUNITY COUNT (from cached candles)")
    print(f"3m bull setups seen (approx): {bull_setup_only}")
    print(f"3m bear setups seen (approx): {bear_setup_only}")
    print(f"1m cross-up triggers: {bull_trig_only}")
    print(f"1m cross-down triggers: {bear_trig_only}")
    print(f"ALIGNED bull (bias+setup+trigger): {bull_aligned}")
    print(f"ALIGNED bear (bias+setup+trigger): {bear_aligned}")
    print(
        "Note: reclaim uses N-bar lookback + aligned 1m trigger; "
        "router may still NO_TRADE if confidence < min_confidence."
    )


async def mock_trade_flow(
    config,
    broker,
    journal: JournalWriter,
    market_data: MarketDataEngine,
) -> None:
    _print("MOCK TRADE — inject bullish VWAP reclaim features")
    features = FeatureSnapshot(
        ts=datetime.now(tz=timezone.utc),
        nifty_spot=Decimal("24120"),
        session_vwap=Decimal("24100"),
        bias_5m=Bias.BULLISH,
        setup_3m="vwap_reclaim_bull",
        trigger_1m="vwap_reclaim_cross_up",
    )
    print(
        f"injected: spot={features.nifty_spot} vwap={features.session_vwap} "
        f"bias={features.bias_5m.value} setup={features.setup_3m} "
        f"trigger={features.trigger_1m}"
    )

    # Synthetic option with full quotes so validator passes
    option = OptionState(
        instrument_token="MOCK-CE",
        tsym="NIFTY14JUL26C24100",
        ltp=Decimal("45.50"),
        bid=Decimal("45.00"),
        ask=Decimal("45.80"),
        spread_pct=Decimal("1.75"),  # (ask-bid)/ask*100
        volume=250000,
        oi=800000,
        last_update_ts=datetime.now(tz=timezone.utc),
    )
    print(
        f"option state: ltp={option.ltp} bid={option.bid} ask={option.ask} "
        f"spread_pct={option.spread_pct} vol={option.volume} oi={option.oi}"
    )

    # Scanner with a tiny fake universe
    from algomcx.models.events import Instrument
    from algomcx.contract_selector.selector import ContractUniverse

    atm_ce = Instrument(
        exchange="NFO",
        token="MOCK-CE",
        tsym="NIFTY14JUL26C24100",
        underlying="NIFTY",
        expiry_date=datetime.now(tz=timezone.utc),
        strike=Decimal("24100"),
        option_type="CE",
        lot_size=65,
        is_atm=True,
        in_band=True,
    )
    universe = ContractUniverse(
        spot=Decimal("24120"),
        atm_strike=Decimal("24100"),
        expiry_symbol="14JUL26",
        instruments=[atm_ce],
        atm_ce=atm_ce,
        atm_pe=None,
        subscription_keys=["NFO|MOCK-CE"],
    )

    scanner = VwapReclaimScanner(config)
    signal = scanner.scan(features, universe, option)
    assert signal is not None, "SCANNER FAILED — should emit CE signal"
    print(f"scanner OK → {signal.side} {signal.tsym} setup={signal.setup_type}")
    await journal.write_candidate_signal(signal)

    validator = RuleValidator(config)
    # Bypass entry window for mock: temporarily validate without time block by
    # checking liquidity/risk fields manually if outside window.
    now_ist = datetime.now(IST).time()
    validation = validator.validate(
        signal,
        option,
        has_open_for_token=False,
        in_cooldown=False,
        kill_switch=False,
    )
    print(f"validator passed={validation.passed} reasons={validation.rejection_reasons}")
    if not validation.passed and validation.rejection_reasons == ["outside_entry_window"]:
        print("NOTE: market closed / outside 09:25–15:00 — forcing pass for mock only")
        validation = ValidationResult(
            candidate_signal_id=signal.id,
            ts=datetime.now(tz=timezone.utc),
            passed=True,
            rejection_reasons=[],
            validator_version=validation.validator_version,
        )
    await journal.write_validation(validation)
    assert validation.passed, f"validator blocked mock: {validation.rejection_reasons}"

    risk = RiskEngine(config)
    snap = await risk.ensure_daily_state()
    print(
        f"capital start={snap.starting_capital} avail={snap.available_capital} "
        f"deployed={snap.deployed_capital} pnl={snap.realized_pnl} "
        f"kill={snap.kill_switch}"
    )
    sizing = await risk.size_entry(signal, option, snap, open_position_count=0)
    print(
        f"sizing approved={sizing.approved} qty={sizing.quantity} "
        f"premium={sizing.premium_required} reason={sizing.rejection_reason}"
    )
    assert sizing.approved, f"risk blocked: {sizing.rejection_reason}"

    await risk.reserve_capital(sizing.premium_required)
    execution = ExecutionEngine(config, broker, journal)
    position_id, order_id, update = await execution.enter(signal, sizing)
    print(
        f"ENTRY FILLED position={position_id} order={order_id} "
        f"fill={update.fill_price} slippage={update.slippage}"
    )

    positions = PositionManager(config, broker, journal, risk, market_data)
    positions.register_open(
        position_id, order_id, signal.id, signal, sizing, update.fill_price or sizing.entry_ltp
    )

    # Seed market bias flip for exit: spot below VWAP with CE open
    market_data._spot_ltp = Decimal("24080")  # type: ignore[attr-defined]
    # Force VWAP via fake candles if needed — bias_flip uses session_vwap_value
    # Inject a quote that triggers adverse OR bias flip after min hold
    entry = update.fill_price or sizing.entry_ltp
    # Drop 15% to hit adverse_momentum (bypass min_hold by calling exit rule directly)
    exit_ltp = (entry * Decimal("0.85")).quantize(Decimal("0.05"))
    decision = evaluate_momentum_exit(
        option_side="CE",
        entry_price=entry,
        entry_ts=datetime.now(tz=timezone.utc),  # still within min hold
        current_ltp=exit_ltp,
        mfe_points=Decimal("0"),
        market_data=market_data,
        cfg=config.position_exit,
        force_exit=False,
    )
    print(f"exit_rule immediate (within min_hold): should_exit={decision.should_exit} reason={decision.reason}")

    # Force exit path through position manager (uses paper fill)
    await positions._close_position(position_id, exit_ltp, "mock_adverse_momentum")
    print(f"EXIT DONE @ {exit_ltp}")

    pool = get_pool()
    async with pool.acquire() as conn:
        cs = await conn.fetchval(
            "SELECT COUNT(*) FROM candidate_signals WHERE id=$1::uuid", signal.id
        )
        vr = await conn.fetchval(
            "SELECT COUNT(*) FROM validation_results WHERE candidate_signal_id=$1::uuid",
            signal.id,
        )
        od = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE candidate_signal_id=$1::uuid", signal.id
        )
        pos = await conn.fetchrow(
            "SELECT status, entry_price, mfe, mae FROM positions WHERE id=$1::uuid",
            position_id,
        )
        ct = await conn.fetchrow(
            """
            SELECT exit_price, pnl, exit_reason, hold_seconds, setup_type
            FROM closed_trades WHERE position_id=$1::uuid
            """,
            position_id,
        )
        risk_row = await conn.fetchrow(
            """
            SELECT available_capital, deployed_capital, realized_pnl, trade_count
            FROM daily_risk_state
            WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
            """
        )
        notif = await conn.fetchval(
            """
            SELECT COUNT(*) FROM notifications
            WHERE related_id=$1::uuid
            """,
            position_id,
        )

    _print("MOCK TRADE DB VERIFICATION")
    print(f"candidate_signals: {cs}")
    print(f"validation_results: {vr}")
    print(f"orders (entry+exit): {od}")
    print(f"position: {dict(pos) if pos else None}")
    print(f"closed_trade: {dict(ct) if ct else None}")
    print(f"daily_risk: {dict(risk_row) if risk_row else None}")
    print(f"notifications for position: {notif}")

    ok = (
        cs == 1
        and vr == 1
        and od >= 2
        and pos
        and pos["status"] == "CLOSED"
        and ct is not None
        and float(ct["pnl"]) != 0
    )
    print(f"\nMOCK_TRADE_RESULT: {'PASS' if ok else 'FAIL'}")


async def main() -> None:
    config = get_config()
    await init_pool()
    journal = JournalWriter()
    bus = EventBus(max_size=1000)
    flattrade = FlattradeAdapter(config)
    await flattrade.connect()
    broker = PaperBrokerAdapter(config, flattrade)
    await broker.connect()
    market_data = MarketDataEngine(config, broker, bus)
    await market_data.backfill_today()

    await analyze_today_opportunities(market_data)
    await mock_trade_flow(config, broker, journal, market_data)

    await broker.disconnect()
    await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
