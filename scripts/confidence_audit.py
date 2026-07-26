#!/usr/bin/env python3
"""Pull live Flattrade candles and audit confidence / regime / setups (today).

Usage (repo root):
  CONFIG_DIR=config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/confidence_audit.py
  ... --underlying NIFTY --minutes 90
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import get_config
from algomcx.contract_selector.selector import ContractSelector
from algomcx.features.chain_intel import build_chain_snapshot
from algomcx.features.engine import FeatureEngine
from algomcx.market_data.engine import MarketDataEngine, session_start_utc
from algomcx.models.events import CandleInterval
from algomcx.quality.gate import QualityGate
from algomcx.regime.classifier import RegimeClassifier
from algomcx.scanner.library import build_strategy_scanners
from algomcx.strategy.router import StrategyRouter
from algomcx.symbols_util import (
    apply_active_underlying,
    price_context,
    resolve_all_spot_tokens,
    symbols_for,
)
from algomcx.validator.trap_avoidance import trap_rejection_reasons

IST = ZoneInfo("Asia/Kolkata")


def _band_states(universe, quotes: dict) -> dict:
    from algomcx.contract_selector.strike_picker import atm_band_instruments

    states = {}
    for side in ("CE", "PE"):
        for inst in atm_band_instruments(universe, side, band_steps=1):
            q = quotes.get(inst.token) or {}
            ltp = q.get("ltp")
            if ltp is None:
                continue
            from algomcx.models.events import OptionState

            states[inst.token] = OptionState(
                instrument_token=inst.token,
                tsym=inst.tsym,
                ltp=Decimal(str(ltp)),
                bid=Decimal(str(q["bid"])) if q.get("bid") else None,
                ask=Decimal(str(q["ask"])) if q.get("ask") else None,
                volume=int(q["volume"]) if q.get("volume") else None,
                oi=int(q["oi"]) if q.get("oi") else None,
            )
    return states


async def run_audit(*, underlying: str, minutes: int) -> None:
    config = get_config()
    apply_active_underlying(config, underlying.upper())
    sym_cfg = symbols_for(config, underlying.upper())
    exchange, spot_token = price_context(sym_cfg)

    flattrade = FlattradeAdapter(config)
    await flattrade.connect()
    await resolve_all_spot_tokens(config, flattrade)
    sym_cfg = symbols_for(config, underlying.upper())
    exchange, spot_token = price_context(sym_cfg)
    bus = EventBus()
    market_data = MarketDataEngine(config, flattrade, bus)
    market_data.set_spot_context(exchange=exchange, spot_token=spot_token)

    now = datetime.now(tz=timezone.utc)
    start = session_start_utc(now)
    for interval in CandleInterval:
        rows = await flattrade.get_candles(exchange, spot_token, interval, start, now)
        market_data._candles[interval] = sorted(rows, key=lambda c: c.ts)

    selector = ContractSelector(config, flattrade)
    spot_quote = await flattrade.get_quotes(exchange, spot_token)
    spot = None
    if spot_quote:
        lp = spot_quote.get("lp") or spot_quote.get("ltp")
        if lp is not None:
            spot = Decimal(str(lp))
            market_data._spot_ltp = spot
    if spot is None and market_data.candles(CandleInterval.M1):
        spot = market_data.candles(CandleInterval.M1)[-1].close

    universe = await selector.build_universe(spot or Decimal("24000"))
    universe = selector.retarget_atm(universe, spot or universe.spot)

    tokens = []
    from algomcx.contract_selector.strike_picker import atm_band_instruments

    for side in ("CE", "PE"):
        tokens.extend(i.token for i in atm_band_instruments(universe, side, band_steps=1))
    opt_exchange = universe.instruments[0].exchange if universe.instruments else exchange
    quotes: dict[str, dict] = {}
    for tok in tokens:
        raw = await flattrade.get_quotes(opt_exchange, tok)
        if not raw:
            continue
        ltp = raw.get("lp") or raw.get("ltp")
        quotes[tok] = {
            "ltp": Decimal(str(ltp)) if ltp is not None else None,
            "bid": raw.get("bp") or raw.get("bid"),
            "ask": raw.get("sp") or raw.get("ask"),
            "volume": raw.get("v") or raw.get("volume"),
            "oi": raw.get("oi"),
        }
    band_states = _band_states(universe, quotes)
    chain = build_chain_snapshot(universe, band_states)

    features_engine = FeatureEngine(config, market_data)
    features_engine.set_chain_snapshot(chain)
    features_engine.set_option_context(
        {
            "ltp": float(list(band_states.values())[0].ltp)
            if band_states
            else None,
            "volume": list(band_states.values())[0].volume if band_states else None,
            "oi": list(band_states.values())[0].oi if band_states else None,
        }
    )

    features = features_engine.compute()
    m1 = market_data.candles(CandleInterval.M1)
    m5 = market_data.candles(CandleInterval.M5)
    regime = RegimeClassifier(config).classify(features, m1, m5)

    quality = QualityGate(config)
    router = StrategyRouter(config, build_strategy_scanners(config), quality)
    options_by_strategy = {s.name: band_states for s in router._scanners}
    decision, signal = router.route(features, regime, universe, options_by_strategy)

    extra = features.extra or {}
    print(f"\n=== CONFIDENCE AUDIT · {underlying.upper()} · {datetime.now(IST):%Y-%m-%d %H:%M IST} ===\n")
    print(f"spot={features.nifty_spot}  vwap={features.session_vwap}")
    print(f"bias_5m={features.bias_5m.value}  bias_1m={extra.get('bias_1m')}")
    print(f"structure_5m={extra.get('structure_5m')}  distance_vwap={extra.get('distance_to_vwap_points')}")
    print(f"mtf_ce={extra.get('mtf_score_ce')}  mtf_pe={extra.get('mtf_score_pe')}")
    print(f"regime={regime.primary}  trade_allowed={regime.trade_allowed}  risk={regime.risk_score}")
    print(f"active_setups={extra.get('active_setups')}")
    print(f"skip_reasons={extra.get('skip_reasons')}")
    print(f"\nRouter: {decision.selected_strategy}  conf={decision.confidence}")
    print(f"Reason: {decision.selected_reason}")

    print("\n--- Per-strategy scores ---")
    for row in decision.strategy_scores or []:
        if row.get("compatible"):
            print(
                f"  {row['strategy']}: conf={row.get('confidence')} "
                f"pass={row.get('passes_gate')} side={row.get('side')}"
            )
        else:
            print(f"  {row['strategy']}: — ({row.get('reason')})")

    if signal is not None:
        trap = trap_rejection_reasons(signal, config.validator.get("trap_avoidance") or {})
        comps = (signal.scanner_metadata or {}).get("confidence_components") or {}
        print(f"\nBest signal: {signal.setup_type} {signal.side} {signal.tsym}")
        print(f"Confidence components: {comps}")
        print(f"Trap filter: {trap or 'PASS'}")

    # Recent window: last N minutes of 1m bars — CE momentum check
    window = m1[-minutes:] if minutes < len(m1) else m1
    if len(window) >= 2:
        chg = float(window[-1].close - window[0].close)
        print(f"\nIndex move last {len(window)}m: {chg:+.1f} pts")
        print("(CE premium can rise on IV/delta even when index is flat — engine uses index candles.)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit confidence vs Flattrade live data")
    parser.add_argument("--underlying", default="NIFTY", choices=["NIFTY"])
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(run_audit(underlying=args.underlying, minutes=args.minutes))


if __name__ == "__main__":
    main()
