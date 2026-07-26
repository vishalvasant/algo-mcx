#!/usr/bin/env python3
"""Replay today's session with current strategy router and estimate paper P&L.

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/day_backtest.py
  trading-engine/.venv/bin/python scripts/day_backtest.py --date 2026-07-16
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
# Quiet scanner/router logs — live-parity ticks are dense.
os.environ.setdefault("STRUCTLOG_LEVEL", "WARNING")

import logging

logging.basicConfig(level=logging.WARNING)
for _name in ("algomcx", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from algomcx.broker.auth import ensure_session
from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import AppConfig, EnvSettings, get_config
from algomcx.contract_selector.resolve import resolve_side_contract
from algomcx.contract_selector.expiry import format_expiry_tag, next_weekly_expiry_dates
from algomcx.contract_selector.selector import ContractSelector, ContractUniverse
from algomcx.contract_selector.strike_picker import atm_band_instruments
from algomcx.features.chain_intel import build_chain_snapshot
from algomcx.features.engine import FeatureEngine
from algomcx.journal.analytics import StrategyLearner
from algomcx.market_data.engine import MarketDataEngine
from algomcx.market_data.vwap import session_vwap
from algomcx.models.events import (
    Bias,
    CandidateSignal,
    Candle,
    CandleInterval,
    FeatureSnapshot,
    Instrument,
    OptionState,
)
from algomcx.option_data.greeks import DEFAULT_RATE, _bs_price, compute_greeks, years_to_expiry
from algomcx.position.exit_rules import evaluate_momentum_exit
from algomcx.quality.gate import QualityGate
from algomcx.regime.classifier import RegimeClassifier
from algomcx.risk.engine import fit_lots_to_capital, lots_for_confidence
from algomcx.risk.greeks_sizing import greeks_confirmation_multiplier
from algomcx.scanner.library import build_strategy_scanners
from algomcx.strategy.router import StrategyRouter
from algomcx.validator.engine import RuleValidator

IST = ZoneInfo("Asia/Kolkata")
ASSUMED_IV = 0.14  # ~14% for ATM weekly premium path when option candles missing
LOT_FALLBACK = 65
SNAP_ROOT = ROOT / "reports" / "snaps"


def _snap_dir(day: date, underlying: str = "NIFTY") -> Path:
    return SNAP_ROOT / day.isoformat() / underlying.upper()


def _snap_dirs_to_try(day: date, underlying: str = "NIFTY") -> list[Path]:
    """Per-underlying snap dir, then legacy flat layout (pre dual-index paths)."""
    ul = underlying.upper()
    legacy = SNAP_ROOT / day.isoformat()
    dirs = [_snap_dir(day, ul)]
    if legacy != dirs[0]:
        dirs.append(legacy)
    return dirs


def _candle_to_dict(c: Candle) -> dict[str, Any]:
    return {
        "instrument_token": c.instrument_token,
        "ts": c.ts.isoformat(),
        "open": str(c.open),
        "high": str(c.high),
        "low": str(c.low),
        "close": str(c.close),
        "volume": c.volume,
        "interval": c.interval.value,
        "vwap": str(c.vwap) if c.vwap is not None else None,
        "oi": c.oi,
    }


def _candle_from_dict(d: dict[str, Any]) -> Candle:
    return Candle(
        instrument_token=str(d["instrument_token"]),
        ts=datetime.fromisoformat(d["ts"]),
        open=Decimal(str(d["open"])),
        high=Decimal(str(d["high"])),
        low=Decimal(str(d["low"])),
        close=Decimal(str(d["close"])),
        volume=d.get("volume"),
        interval=CandleInterval(d["interval"]),
        vwap=Decimal(str(d["vwap"])) if d.get("vwap") not in (None, "") else None,
        oi=d.get("oi"),
    )


def _instrument_to_dict(i: Instrument) -> dict[str, Any]:
    return i.model_dump(mode="json")


def _instrument_from_dict(d: dict[str, Any]) -> Instrument:
    return Instrument.model_validate(d)


def save_day_snap(
    day: date,
    *,
    underlying: str,
    all_candles: dict[CandleInterval, list[Candle]],
    prior_day: date | None,
    prior_m5: list[Candle],
    universe: ContractUniverse,
    option_series: dict[str, list[Candle]],
) -> Path:
    """Persist full-day 1m/3m/5m spot + option series for reproducible replay.

    This is NOT tick data — Flattrade finest history is 1-minute TPSeries.
    """
    out = _snap_dir(day, underlying)
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "day": day.isoformat(),
        "underlying": underlying.upper(),
        "saved_at": datetime.now(tz=IST).isoformat(),
        "source": "flattrade_tpseries",
        "note": (
            "Finest available historical bars (1m OHLC + intvwap/oi when present). "
            "Not tick-perfect — broker does not expose historical ticks."
        ),
        "spot_bars": {k.value: len(v) for k, v in all_candles.items()},
        "option_tokens": len(option_series),
        "option_bars_total": sum(len(v) for v in option_series.values()),
        "prior_day": prior_day.isoformat() if prior_day else None,
        "universe_instruments": len(universe.instruments),
        "expiry_symbol": universe.expiry_symbol,
        "atm_strike": str(universe.atm_strike),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    spot_payload = {
        interval.value: [_candle_to_dict(c) for c in bars]
        for interval, bars in all_candles.items()
    }
    (out / "spot.json").write_text(json.dumps(spot_payload))
    (out / "prior_day.json").write_text(
        json.dumps(
            {
                "date": prior_day.isoformat() if prior_day else None,
                "m5": [_candle_to_dict(c) for c in prior_m5],
            }
        )
    )
    uni_payload = {
        "spot": str(universe.spot),
        "atm_strike": str(universe.atm_strike),
        "expiry_symbol": universe.expiry_symbol,
        "instruments": [_instrument_to_dict(i) for i in universe.instruments],
        "atm_ce_token": universe.atm_ce.token if universe.atm_ce else None,
        "atm_pe_token": universe.atm_pe.token if universe.atm_pe else None,
        "subscription_keys": list(universe.subscription_keys),
    }
    (out / "universe.json").write_text(json.dumps(uni_payload, indent=2))
    opt_payload = {
        token: [_candle_to_dict(c) for c in bars]
        for token, bars in option_series.items()
    }
    (out / "options.json").write_text(json.dumps(opt_payload))
    print(
        f"  Snap saved → {out}  "
        f"(spot 1m={meta['spot_bars'].get('1m', 0)}  "
        f"options={meta['option_tokens']} tokens / "
        f"{meta['option_bars_total']} bars)"
    )
    return out


def _prior_snap_universe(day: date, underlying: str, *, lookback: int = 8) -> ContractUniverse | None:
    """Reuse instruments from a recent snap when broker no longer lists expired weeklies."""
    d = day - timedelta(days=1)
    for _ in range(lookback):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        prior = load_day_snap(d, underlying)
        if prior and prior["universe"].instruments:
            return prior["universe"]
        d -= timedelta(days=1)
    return None


def load_day_snap(day: date, underlying: str = "NIFTY") -> dict[str, Any] | None:
    """Load a previously saved day snap. Returns None if incomplete."""
    required = ("meta.json", "spot.json", "prior_day.json", "universe.json", "options.json")
    out: Path | None = None
    for candidate in _snap_dirs_to_try(day, underlying):
        if all((candidate / name).exists() for name in required):
            out = candidate
            break
    if out is None:
        return None
    spot_raw = json.loads((out / "spot.json").read_text())
    all_candles: dict[CandleInterval, list[Candle]] = {}
    for key, rows in spot_raw.items():
        all_candles[CandleInterval(key)] = [_candle_from_dict(r) for r in rows]
    prior_raw = json.loads((out / "prior_day.json").read_text())
    prior_day = (
        date.fromisoformat(prior_raw["date"]) if prior_raw.get("date") else None
    )
    prior_m5 = [_candle_from_dict(r) for r in prior_raw.get("m5") or []]
    uni_raw = json.loads((out / "universe.json").read_text())
    instruments = [_instrument_from_dict(i) for i in uni_raw.get("instruments") or []]
    by_token = {i.token: i for i in instruments}
    atm_ce = by_token.get(uni_raw.get("atm_ce_token") or "")
    atm_pe = by_token.get(uni_raw.get("atm_pe_token") or "")
    universe = ContractUniverse(
        spot=Decimal(str(uni_raw["spot"])),
        atm_strike=Decimal(str(uni_raw["atm_strike"])),
        expiry_symbol=uni_raw.get("expiry_symbol"),
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
        subscription_keys=list(uni_raw.get("subscription_keys") or []),
    )
    opt_raw = json.loads((out / "options.json").read_text())
    option_series = {
        token: [_candle_from_dict(r) for r in rows] for token, rows in opt_raw.items()
    }
    meta = json.loads((out / "meta.json").read_text())
    print(
        f"  Snap loaded ← {out}  "
        f"(saved {meta.get('saved_at', '?')}; "
        f"1m={len(all_candles.get(CandleInterval.M1, []))}  "
        f"options={len(option_series)})"
    )
    return {
        "all_candles": all_candles,
        "prior_day": prior_day,
        "prior_m5": prior_m5,
        "universe": universe,
        "option_series": option_series,
        "meta": meta,
    }


@dataclass
class OpenTrade:
    strategy: str
    side: str
    strike: Decimal
    entry_ts: datetime
    entry_spot: Decimal
    entry_premium: Decimal
    lot_size: int
    lots: int
    confidence: int = 0
    mfe: Decimal = Decimal("0")
    premium_source: str = "bs"
    tsym: str = ""
    token: str = ""


@dataclass
class ClosedTrade:
    strategy: str
    side: str
    strike: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry_spot: Decimal
    exit_spot: Decimal
    entry_premium: Decimal
    exit_premium: Decimal
    lots: int
    lot_size: int
    pnl: Decimal
    exit_reason: str
    confidence: int
    premium_source: str
    tsym: str = ""


@dataclass
class DayStats:
    signals_seen: int = 0
    entries: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)


def _round_strike(spot: Decimal, step: Decimal) -> Decimal:
    return (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def _bs_premium(
    spot: Decimal,
    strike: Decimal,
    side: str,
    when: datetime,
    expiry: date,
    iv: float = ASSUMED_IV,
) -> Decimal:
    t = years_to_expiry(expiry, now=when)
    px = _bs_price(float(spot), float(strike), t, DEFAULT_RATE, iv, side)
    return Decimal(str(round(max(px, 0.05), 2)))


def _premium_at(
    *,
    when: datetime,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
) -> tuple[Decimal, str]:
    if token and token in option_series:
        bars = option_series[token]
        # last bar at or before when
        best: Candle | None = None
        for c in bars:
            if c.ts <= when:
                best = c
            else:
                break
        if best is not None and best.close > 0:
            return best.close, "option_candle"
    return _bs_premium(spot, strike, side, when, expiry), "bs"


def _exit_decision(
    *,
    side: str,
    entry_premium: Decimal,
    entry_ts: datetime,
    now: datetime,
    current_premium: Decimal,
    mfe: Decimal,
    spot: Decimal,
    vwap: Decimal | None,
    force_exit: bool,
    cfg: dict,
    market_data: MarketDataEngine,
    regime_primary: str | None = None,
) -> tuple[bool, str | None]:
    """Thin wrapper — same exit engine as live (`evaluate_momentum_exit`)."""
    # Ensure MD mirrors replay spot for VWAP-flip checks inside exit_rules.
    market_data._spot_ltp = spot  # type: ignore[attr-defined]
    decision = evaluate_momentum_exit(
        option_side=side,
        entry_price=entry_premium,
        entry_ts=entry_ts,
        current_ltp=current_premium,
        mfe_points=mfe,
        market_data=market_data,
        cfg=cfg,
        force_exit=force_exit,
        regime_primary=regime_primary,
        now=now,
    )
    return decision.should_exit, decision.reason


def _fit_lots(
    *,
    risk_cfg: dict,
    confidence: int,
    entry_ltp: Decimal,
    lot_size: int,
    available: Decimal,
    deployed: Decimal,
    equity: Decimal,
    strike_pick: dict | None = None,
) -> tuple[int, Decimal]:
    """Delegate to live RiskEngine sizing (backtest ≡ production)."""
    greeks_mult = Decimal("1")
    if strike_pick:
        stub = CandidateSignal(
            ts=datetime.now(tz=timezone.utc),
            setup_type="backtest",
            side="CE",
            instrument_token="0",
            tsym="BT",
            strategy_version="bt",
            feature_snapshot=FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
            scanner_metadata={"strike_pick": strike_pick},
        )
        greeks_mult = greeks_confirmation_multiplier(stub, None, risk_cfg)
    return fit_lots_to_capital(
        risk_cfg,
        confidence=confidence,
        entry_ltp=entry_ltp,
        lot_size=lot_size,
        available=available,
        deployed=deployed,
        equity=equity,
        greeks_mult=greeks_mult,
    )


def _slice_upto(bars: list[Candle], ts: datetime) -> list[Candle]:
    return [c for c in bars if c.ts <= ts]


def _slice_before(bars: list[Candle], ts: datetime) -> list[Candle]:
    return [c for c in bars if c.ts < ts]


def _slice_closed_period(
    bars: list[Candle], when: datetime, period_min: int
) -> list[Candle]:
    """Only bars whose period has fully ended before ``when`` (no forming HTF bar)."""
    out: list[Candle] = []
    for c in bars:
        if c.ts + timedelta(minutes=period_min) <= when:
            out.append(c)
    return out


def _honest_spot(
    bar: Candle, when: datetime, prior_m1: list[Candle]
) -> Decimal:
    """Spot LTP without peeking at the current minute's close/VWAP."""
    minute_end = bar.ts + timedelta(minutes=1)
    if when >= minute_end:
        return bar.close
    closed = [c for c in prior_m1 if c.ts < bar.ts]
    if closed:
        return closed[-1].close
    return bar.open


def _honest_premium_path(
    *,
    when: datetime,
    bar: Candle,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
) -> tuple[Decimal, str]:
    """Option mark using last closed 1m candle — no intra-minute close/VWAP peek."""
    minute_end = bar.ts + timedelta(minutes=1)
    mark_when = when if when >= minute_end else bar.ts - timedelta(seconds=1)
    prem, src = _premium_at(
        when=mark_when,
        spot=spot,
        side=side,
        strike=strike,
        expiry=expiry,
        option_series=option_series,
        token=token,
    )
    if when >= minute_end and token and token in option_series:
        for c in option_series[token]:
            cts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
            if cts == bar.ts and c.close > 0:
                return c.close, "option_candle_closed"
    return prem, src


def _scan_waypoints(bar: Candle, scan_sec: int) -> list[tuple[datetime, Decimal]]:
    """Approximate live scans inside a 1m bar (open → mid → … toward close).

    Live engine scans every ``scan_interval_seconds`` on ticking LTP; a 1m-only
    backtest under-counts entries. We synthesize intra-bar spots from OHLC.
    """
    return [
        (ts, spot)
        for ts, spot, _allow in _live_parity_waypoints(bar, exit_sec=scan_sec, scan_sec=scan_sec)
    ]


def _bar_path_price(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    bar_frac: Decimal,
    bar_vwap: Decimal | None = None,
) -> Decimal:
    """Intra-bar path without fake H/L visits.

    Flattrade has no historical ticks — finest data is 1m OHLC (+ intvwap).
    Use O → (bar VWAP or mid) → C so we don't invent adverse spikes from high/low.
    """
    frac = max(Decimal("0"), min(Decimal("1"), bar_frac))
    mid = bar_vwap if bar_vwap is not None else (high + low) / Decimal("2")
    if frac <= Decimal("0.5"):
        return open_ + (mid - open_) * (frac * 2)
    return mid + (close - mid) * ((frac - Decimal("0.5")) * 2)


def _ohlc_path_price(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    bar_frac: Decimal,
) -> Decimal:
    """Compat alias — prefers mid path (no extreme thrash)."""
    return _bar_path_price(open_, high, low, close, bar_frac, None)


def _live_parity_waypoints(
    bar: Candle,
    *,
    exit_sec: int,
    scan_sec: int,
    prior_m1: list[Candle] | None = None,
    honest: bool = False,
) -> list[tuple[datetime, Decimal, bool]]:
    """Fine exit ticks + coarser entry scans from 1m snap (not true ticks).

    Path: open → bar VWAP/mid → close. Entries every scan_interval_seconds.
    """
    exit_sec = max(1, int(exit_sec))
    scan_sec = max(exit_sec, int(scan_sec))
    points: list[tuple[datetime, Decimal, bool]] = []
    end = bar.ts + timedelta(minutes=1)
    t = bar.ts
    while t < end:
        frac = Decimal(str((t - bar.ts).total_seconds() / 60.0))
        if honest:
            spot = _honest_spot(bar, t, prior_m1 or [])
        else:
            spot = _bar_path_price(
                bar.open, bar.high, bar.low, bar.close, frac, bar.vwap
            )
        sec = int((t - bar.ts).total_seconds())
        allow_entry = (sec % scan_sec) == 0
        points.append((t, spot, allow_entry))
        t += timedelta(seconds=exit_sec)
    last_ts = bar.ts + timedelta(seconds=59)
    if not points or points[-1][0] < last_ts:
        points.append((last_ts, bar.close, True))
    return points


def _forming_m1(bar: Candle, when: datetime, spot: Decimal, *, honest: bool = False) -> Candle:
    """Synthetic forming 1m bar so bias/setup see current scan price as close."""
    if honest:
        return Candle(
            instrument_token=bar.instrument_token,
            ts=bar.ts,
            open=bar.open,
            high=max(bar.open, spot),
            low=min(bar.open, spot),
            close=spot,
            volume=bar.volume,
            interval=CandleInterval.M1,
        )
    return Candle(
        instrument_token=bar.instrument_token,
        ts=bar.ts,
        open=bar.open,
        high=max(bar.open, bar.high, spot),
        low=min(bar.open, bar.low, spot),
        close=spot,
        volume=bar.volume,
        interval=CandleInterval.M1,
    )


def _premium_path(
    *,
    when: datetime,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
    bar_frac: Decimal,
    bar: Candle | None = None,
    honest: bool = False,
) -> tuple[Decimal, str]:
    """Option premium at an intra-bar tick using that minute's option OHLC path."""
    if honest and bar is not None:
        return _honest_premium_path(
            when=when,
            bar=bar,
            spot=spot,
            side=side,
            strike=strike,
            expiry=expiry,
            option_series=option_series,
            token=token,
        )
    if token and token in option_series:
        series = option_series[token]
        # Candle ts is usually minute start; pick the bar covering ``when``.
        minute = when.replace(second=0, microsecond=0)
        best: Candle | None = None
        for c in series:
            cts = c.ts
            if cts.tzinfo is None:
                cts = cts.replace(tzinfo=timezone.utc)
            if cts <= minute:
                best = c
            else:
                break
        if best is not None and best.close > 0:
            px = _bar_path_price(
                best.open, best.high, best.low, best.close, bar_frac, best.vwap
            )
            return max(px, Decimal("0.05")), "option_candle"
    return _bs_premium(spot, strike, side, when, expiry), "bs"


def _fake_inst(
    side: str,
    strike: Decimal,
    lot: int,
    *,
    underlying: str = "NIFTY",
    exchange: str = "NFO",
    token: str = "",
    tsym: str = "",
) -> Instrument:
    ul = underlying.upper()
    return Instrument(
        exchange=exchange,
        token=token or f"SIM-{side}-{strike}",
        tsym=tsym or f"{ul}{strike}{side}",
        underlying=ul,
        expiry_date=None,
        strike=strike,
        option_type=side,
        lot_size=lot,
        is_atm=True,
        in_band=True,
    )


async def _fetch_session_candles(
    broker: FlattradeAdapter,
    config: AppConfig,
    day: date,
) -> dict[CandleInterval, list[Candle]]:
    start = (
        datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    )
    end = (
        datetime.combine(day, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    )
    now = datetime.now(tz=timezone.utc)
    if end > now:
        end = now
    exchange = config.symbols["exchange_spot"]
    token = config.symbols["spot_token"]
    out: dict[CandleInterval, list[Candle]] = {}
    for interval in CandleInterval:
        rows = await broker.get_candles(exchange, token, interval, start, end)
        rows = sorted(rows, key=lambda c: c.ts)
        out[interval] = rows
        print(f"  {interval.value}: {len(rows)} bars", end="")
        if rows:
            print(
                f"  [{rows[0].ts.astimezone(IST).strftime('%H:%M')} → "
                f"{rows[-1].ts.astimezone(IST).strftime('%H:%M')}] "
                f"close={rows[-1].close}"
            )
        else:
            print()
    return out


async def _load_option_series(
    broker: FlattradeAdapter,
    universe: ContractUniverse,
    strikes: list[Decimal],
    session_start: datetime,
    session_end: datetime,
) -> dict[str, list[Candle]]:
    """Fetch 1m candles for ATM± band CE/PE tokens present in universe."""
    series: dict[str, list[Candle]] = {}
    wanted = set(strikes)
    targets = [
        i
        for i in universe.instruments
        if i.strike in wanted and i.option_type in ("CE", "PE")
    ]
    print(f"  Fetching option 1m candles for {len(targets)} contracts...")
    for i, inst in enumerate(targets, 1):
        try:
            rows = await broker.get_candles(
                inst.exchange,
                inst.token,
                CandleInterval.M1,
                session_start,
                session_end,
            )
            rows = sorted(rows, key=lambda c: c.ts)
            if rows:
                series[inst.token] = rows
            if i % 5 == 0 or i == len(targets):
                print(f"    {i}/{len(targets)} done ({len(series)} with data)")
            await asyncio.sleep(0.15)  # gentle rate limit
        except Exception as exc:  # noqa: BLE001
            print(f"    skip {inst.tsym}: {exc}")
    return series


def _find_inst(universe: ContractUniverse, side: str, strike: Decimal) -> Instrument | None:
    for i in universe.instruments:
        if i.option_type == side and i.strike == strike:
            return i
    if side == "CE":
        return universe.atm_ce
    return universe.atm_pe


async def run_backtest(
    day: date | None = None,
    *,
    underlying: str = "NIFTY",
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    from_snap: bool = False,
    refresh_snap: bool = False,
    snap_only: bool = False,
    capital_pool: dict[str, Decimal] | None = None,
    honest_replay: bool = True,
) -> dict[str, Any] | None:
    day = day or datetime.now(tz=IST).date()
    config = get_config()
    ul = underlying.upper()
    from algomcx.symbols_util import (
        apply_active_underlying,
        capital_for,
        total_account_capital,
        uses_pooled_capital,
    )

    apply_active_underlying(config, ul)
    broker: FlattradeAdapter | None = None
    broker_connected = False

    snap = None if refresh_snap else load_day_snap(day, ul)
    if from_snap:
        if not snap:
            print(f"ERROR: --from-snap but no snap at {_snap_dir(day, ul)}")
            return None
        use_snap = True
    elif refresh_snap or snap is None:
        use_snap = False
    else:
        use_snap = True

    if use_snap and snap is not None:
        print(f"==> Using day snap for {day.isoformat()} (1m bars + option series)...")
        print(
            "  NOTE: Flattrade has no historical ticks — snap is finest available "
            "(1m OHLC + intvwap/oi). Replay uses O→VWAP/mid→C path, not fake H/L thrash."
        )
        all_candles = snap["all_candles"]
        universe = snap["universe"]
        option_series = snap["option_series"]
        prior_day = snap["prior_day"]
        prior_m5 = snap["prior_m5"]
        m1_all = all_candles[CandleInterval.M1]
        m3_all = all_candles[CandleInterval.M3]
        m5_all = all_candles[CandleInterval.M5]
        if len(m1_all) < 30:
            print(f"ERROR: snap has too few 1m candles for {day} — aborting")
            return None
    else:
        print("==> Ensuring Flattrade session...")
        await ensure_session(EnvSettings())
        broker = FlattradeAdapter(config)
        await broker.connect()
        broker_connected = True

        from algomcx.symbols_util import resolve_all_spot_tokens

        await resolve_all_spot_tokens(config, broker)

        fut_tsym = config.symbols.get("fut_tsym", "")
        print(
            f"==> Fetching {ul} FUT candles for {day.isoformat()} "
            f"({config.symbols.get('exchange_spot')} token={config.symbols.get('spot_token')} "
            f"{fut_tsym})..."
        )
        all_candles = await _fetch_session_candles(broker, config, day)
        m1_all = all_candles[CandleInterval.M1]
        m3_all = all_candles[CandleInterval.M3]
        m5_all = all_candles[CandleInterval.M5]
        if len(m1_all) < 30:
            print(f"ERROR: not enough 1m candles for {day} — aborting")
            await broker.disconnect()
            return None

        open_spot = m1_all[0].close
        close_spot = m1_all[-1].close
        step = Decimal(str(config.symbols["strike_step"]))
        print(f"\n==> Spot range: {open_spot} → {close_spot}  (Δ {close_spot - open_spot})")

        print("==> Building contract universe (weekly ATM band)...")
        selector = ContractSelector(config, broker)
        mid_spot = m1_all[len(m1_all) // 2].close
        universe = await selector.build_universe(mid_spot, as_of=day)
        if not universe.instruments:
            cal = next_weekly_expiry_dates(
                underlying=ul, as_of=day, count=1, include_today_if_expiry=True
            )
            primary_exp = format_expiry_tag(cal[0]) if cal else None
            prior_uni = _prior_snap_universe(day, ul)
            if prior_uni and (
                primary_exp is None or prior_uni.expiry_symbol == primary_exp
            ):
                universe = selector.retarget_atm(prior_uni, mid_spot)
                print(
                    f"  Reused prior snap universe expiry={universe.expiry_symbol} "
                    f"instruments={len(universe.instruments)} (expired series)"
                )
        print(
            f"  expiry={universe.expiry_symbol} atm={universe.atm_strike} "
            f"instruments={len(universe.instruments)}"
        )
        if not universe.instruments:
            print("  WARN: empty universe — will use synthetic tokens + BS premiums only")

        open_atm = _round_strike(open_spot, step)
        close_atm = _round_strike(close_spot, step)
        lo = min(open_atm, close_atm) - Decimal("150")
        hi = max(open_atm, close_atm) + Decimal("150")
        strikes: list[Decimal] = []
        s = lo
        while s <= hi:
            strikes.append(s)
            s += step

        session_start = m1_all[0].ts
        session_end = m1_all[-1].ts
        option_series = await _load_option_series(
            broker, universe, strikes, session_start, session_end
        )
        print(f"  Option series loaded: {len(option_series)}")

        # Prior day for CPR / PDH / PDL / gap
        prior_day = day - timedelta(days=1)
        while prior_day.weekday() >= 5:
            prior_day = prior_day - timedelta(days=1)
        prior_m5: list[Candle] = []
        try:
            prev_candles = await _fetch_session_candles(broker, config, prior_day)
            prior_m5 = prev_candles.get(CandleInterval.M5) or []
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: prior day fetch failed: {exc}")
            prior_day = None

        save_day_snap(
            day,
            underlying=ul,
            all_candles=all_candles,
            prior_day=prior_day,
            prior_m5=prior_m5,
            universe=universe,
            option_series=option_series,
        )
        if snap_only:
            print("==> --snap-only: fetch complete, skipping replay.")
            await broker.disconnect()
            return None

    open_spot = m1_all[0].close
    close_spot = m1_all[-1].close
    step = Decimal(str(config.symbols["strike_step"]))
    if use_snap:
        print(f"\n==> Spot range: {open_spot} → {close_spot}  (Δ {close_spot - open_spot})")
        print(
            f"  expiry={universe.expiry_symbol} atm={universe.atm_strike} "
            f"instruments={len(universe.instruments)}  options={len(option_series)}"
        )

    # Expiry date for BS fallback
    expiry_d = day
    if universe.atm_ce and universe.atm_ce.expiry_date:
        expiry_d = universe.atm_ce.expiry_date.date()
    elif universe.expiry_symbol:
        from algomcx.contract_selector.expiry import parse_expiry_tag

        parsed = parse_expiry_tag(universe.expiry_symbol)
        if parsed:
            expiry_d = parsed

    # Replay engines
    bus = EventBus(max_size=10)
    # Broker only needed for MarketDataEngine construction; snap replay never calls it.
    if broker is None:
        broker = FlattradeAdapter(config)
    md = MarketDataEngine(config, broker, bus)
    features_eng = FeatureEngine(config, md)
    if prior_m5:
        features_eng.set_prior_day(
            max(c.high for c in prior_m5),
            min(c.low for c in prior_m5),
            prior_m5[-1].close,
        )
        print(
            f"  Prior day {prior_day}: PDH={features_eng.prior_high} "
            f"PDL={features_eng.prior_low} close={features_eng.prior_close}"
        )
    else:
        print("  WARN: prior day levels unavailable")

    regime_clf = RegimeClassifier(config)
    quality = QualityGate(config)
    # Same learner file as live so demotion multipliers match.
    learner = StrategyLearner(ROOT / "reports" / "strategy_learner.json")
    quality.set_learner(learner)
    scanners = build_strategy_scanners(config)
    router = StrategyRouter(config, scanners, quality)
    validator = RuleValidator(config)
    print(f"  Strategies enabled: {len(scanners)} (same library as live)")
    print("  Layers: FeatureEngine → Regime → Router/Quality §9 → RuleValidator → sizing → exit_rules")
    exit_cfg = config.position_exit
    cooldown_min = int(config.risk.get("cooldown_after_exit_minutes", 3))
    entry_start = time.fromisoformat(str(config.validator.get("entry_start_time", "10:00")))
    entry_end = time.fromisoformat(str(config.validator.get("entry_end_time", "15:15")))
    force_exit_t = time.fromisoformat(str(config.risk.get("force_exit_time", "15:15")))
    max_concurrent = int(config.risk.get("max_concurrent_positions_per_index", 0))
    max_daily_loss = Decimal(str(config.risk.get("max_daily_loss", 0)))
    max_consec = int(config.risk.get("max_consecutive_losses", 0))
    max_trades_day = int(config.risk.get("max_trades_per_day", 0))
    scan_sec = int(
        scan_interval
        if scan_interval is not None
        else config.runtime.get("scan_interval_seconds", 30)
    )
    # Live marks exits on every quote (~2s REST poll / WS ticks). Coarse 30s-only
    # exits under-count re-entries vs a real session.
    exit_sec = int(
        exit_interval
        if exit_interval is not None
        else config.runtime.get(
            "backtest_exit_interval_seconds",
            config.runtime.get("rest_quote_poll_interval_market_seconds", 2),
        )
    )
    exit_sec = max(1, min(exit_sec, scan_sec))

    if capital_pool is not None:
        starting = capital_pool["starting"]
        available = capital_pool["available"]
        deployed = capital_pool["deployed"]
        realized = capital_pool["realized"]
    elif uses_pooled_capital(config):
        starting = total_account_capital(config)
        available = starting
        deployed = Decimal("0")
        realized = Decimal("0")
    else:
        starting = capital_for(config, ul)
        available = starting
        deployed = Decimal("0")
        realized = Decimal("0")
    pool_start_equity = starting + realized
    peak_deployed = Decimal("0")
    peak_util_pct = Decimal("0")

    stats = DayStats()
    open_trades: list[OpenTrade] = []
    cooldown_until: dict[str, datetime] = {}
    global_cooldown_until: datetime | None = None
    pending_flips: list[dict] = []
    consecutive_losses = 0
    prior_oi: dict[str, int] = {}
    option_vwap_num: dict[str, Decimal] = {}
    option_vwap_den: dict[str, Decimal] = {}
    strike_band = int(
        (config.strategy.get("strike_selection") or {}).get("atm_band_steps", 1)
    )
    flip_on_reversal = bool(exit_cfg.get("flip_on_trend_reversal", True))
    lot_size = LOT_FALLBACK
    if universe.atm_pe:
        lot_size = universe.atm_pe.lot_size
    elif universe.atm_ce:
        lot_size = universe.atm_ce.lot_size

    print(
        f"\n==> Replaying {ul} session "
        "(LIVE-PARITY layers · exit_rules · RuleValidator · §9 quality · library)..."
    )
    print(
        f"  capital=₹{starting}"
        + (" (shared pool)" if capital_pool is not None or uses_pooled_capital(config) else "")
        + f"  deploy_cap={config.risk.get('max_deployed_pct_of_equity')}%  "
        f"per_trade_cap={config.risk.get('max_premium_pct_of_available')}%  "
        f"window={entry_start.strftime('%H:%M')}–{entry_end.strftime('%H:%M')} IST  "
        f"force_exit={force_exit_t.strftime('%H:%M')}  "
        f"exit_tick={exit_sec}s  entry_scan={scan_sec}s  strike_band=ATM±{strike_band}  "
        f"flip_on_reversal={flip_on_reversal}  "
        f"concurrent={max_concurrent or 'unlimited'}  "
        f"max_trades/day={max_trades_day or '∞'}  "
        f"daily_loss_cap=₹{max_daily_loss}  consec_loss_cap={max_consec or '∞'}  "
        f"cooldown={cooldown_min}m global  "
        f"data={'snap' if use_snap else 'live-fetch→snap'}  "
        f"replay={'honest (no future bar peek)' if honest_replay else 'intra-bar synthetic'}"
    )

    def _mark_premium(
        *,
        when: datetime,
        spot: Decimal,
        side: str,
        strike: Decimal,
        token: str | None,
        bar_frac: Decimal,
        bar: Candle,
    ) -> tuple[Decimal, str]:
        return _premium_path(
            when=when,
            spot=spot,
            side=side,
            strike=strike,
            expiry=expiry_d,
            option_series=option_series,
            token=token,
            bar_frac=bar_frac,
            bar=bar,
            honest=honest_replay,
        )

    def _band_opt_states(
        uni: ContractUniverse,
        *,
        when: datetime,
        spot: Decimal,
        bar_frac: Decimal,
        bar: Candle,
    ) -> dict[str, OptionState | None]:
        states: dict[str, OptionState | None] = {}
        for side in ("CE", "PE"):
            for inst in atm_band_instruments(
                uni, side, band_steps=strike_band, step=step
            ):
                prem, _ = _mark_premium(
                    when=when,
                    spot=spot,
                    side=inst.option_type,
                    strike=inst.strike,
                    token=inst.token,
                    bar_frac=bar_frac,
                    bar=bar,
                )
                vol = None
                oi = None
                if inst.token in option_series:
                    for c in option_series[inst.token]:
                        if c.ts <= when:
                            vol = c.volume
                            if c.oi is not None:
                                oi = c.oi
                        else:
                            break
                states[inst.token] = OptionState(
                    instrument_token=inst.token,
                    tsym=inst.tsym,
                    ltp=prem,
                    bid=prem - Decimal("0.5"),
                    ask=prem + Decimal("0.5"),
                    spread_pct=Decimal("1"),
                    volume=vol,
                    oi=oi,
                )
                # Running option VWAP (equal-weight ticks like live OptionDataLayer)
                option_vwap_num[inst.token] = option_vwap_num.get(
                    inst.token, Decimal("0")
                ) + prem
                option_vwap_den[inst.token] = option_vwap_den.get(
                    inst.token, Decimal("0")
                ) + Decimal("1")
        return states

    def _try_enter(
        *,
        strategy: str,
        side: str,
        strike: Decimal,
        inst: Instrument | None,
        conf: int,
        when: datetime,
        spot: Decimal,
        bar_frac: Decimal,
        ts_ist: datetime,
        equity: Decimal,
        bypass_cooldown: bool = False,
        strike_pick: dict | None = None,
        bar: Candle | None = None,
    ) -> bool:
        nonlocal available, deployed
        if max_trades_day > 0 and (len(stats.closed) + len(open_trades)) >= max_trades_day:
            stats.skips["max_trades_per_day"] = stats.skips.get("max_trades_per_day", 0) + 1
            return False
        if max_consec > 0 and consecutive_losses >= max_consec:
            stats.skips["max_consecutive_losses"] = stats.skips.get(
                "max_consecutive_losses", 0
            ) + 1
            return False
        # Flips must fire immediately — global cooldown would kill opposite entry.
        if (
            not bypass_cooldown
            and global_cooldown_until is not None
            and when < global_cooldown_until
        ):
            stats.skips["global_cooldown"] = stats.skips.get("global_cooldown", 0) + 1
            return False
        token = inst.token if inst else ""
        if (
            not bypass_cooldown
            and token
            and token in cooldown_until
            and when < cooldown_until[token]
        ):
            stats.skips["instrument_cooldown"] = stats.skips.get("instrument_cooldown", 0) + 1
            return False
        if any(o.token == token and o.side == side for o in open_trades if token):
            stats.skips["already_open_same_token"] = stats.skips.get(
                "already_open_same_token", 0
            ) + 1
            return False
        if max_concurrent > 0 and len(open_trades) >= max_concurrent:
            stats.skips["max_concurrent"] = stats.skips.get("max_concurrent", 0) + 1
            return False

        prem, src = _mark_premium(
            when=when,
            spot=spot,
            side=side,
            strike=strike,
            token=inst.token if inst else None,
            bar_frac=bar_frac,
            bar=bar or m1_all[0],
        )
        ls = inst.lot_size if inst else lot_size
        lots, premium_cost = _fit_lots(
            risk_cfg=config.risk,
            confidence=conf,
            entry_ltp=prem,
            lot_size=ls,
            available=available,
            deployed=deployed,
            equity=equity,
            strike_pick=strike_pick,
        )
        if lots < 1:
            stats.skips["capital_or_deploy_cap"] = stats.skips.get(
                "capital_or_deploy_cap", 0
            ) + 1
            return False

        available -= premium_cost
        deployed += premium_cost
        open_trades.append(
            OpenTrade(
                strategy=strategy,
                side=side,
                strike=strike,
                entry_ts=when,
                entry_spot=spot,
                entry_premium=prem,
                lot_size=ls,
                lots=lots,
                confidence=conf,
                premium_source=src,
                tsym=inst.tsym if inst else "",
                token=token,
            )
        )
        stats.entries += 1
        util_now = deployed / equity * Decimal("100") if equity > 0 else Decimal("0")
        target = lots_for_confidence(
            config.risk,
            conf,
            entry_ltp=prem,
            lot_size=ls,
            available=available,
            deployed=deployed,
            equity=equity,
        )
        pool_equity = starting + realized
        print(
            f"  ENTRY {ts_ist.strftime('%H:%M:%S')} {strategy} {side} "
            f"@ {strike} prem={prem} lots={lots}/{target} "
            f"(conf={conf}) open={len(open_trades)} "
            f"pool=₹{pool_equity:.0f} deployed=₹{deployed:.0f} ({util_now:.0f}%) "
            f"avail=₹{available:.0f}"
        )
        return True

    total_ticks = 0
    total_entry_scans = 0
    for i, bar in enumerate(m1_all):
        prior_m1 = _slice_before(m1_all, bar.ts)
        waypoints = _live_parity_waypoints(
            bar,
            exit_sec=exit_sec,
            scan_sec=scan_sec,
            prior_m1=prior_m1,
            honest=honest_replay,
        )

        for wp_i, (ts, spot, allow_entry) in enumerate(waypoints):
            total_ticks += 1
            ts_ist = ts.astimezone(IST)
            t_clock = ts_ist.time()
            bar_frac = Decimal(str(min(1.0, (ts - bar.ts).total_seconds() / 60.0)))
            is_last_scan = i == len(m1_all) - 1 and wp_i == len(waypoints) - 1

            m1_closed = _slice_before(m1_all, bar.ts)
            m1 = m1_closed + [_forming_m1(bar, ts, spot, honest=honest_replay)]
            if honest_replay:
                m3 = _slice_closed_period(m3_all, ts, 3)
                m5 = _slice_closed_period(m5_all, ts, 5)
            else:
                m3 = _slice_upto(m3_all, ts)
                m5 = _slice_upto(m5_all, ts)
            md._candles[CandleInterval.M1] = m1  # type: ignore[attr-defined]
            md._candles[CandleInterval.M3] = m3  # type: ignore[attr-defined]
            md._candles[CandleInterval.M5] = m5  # type: ignore[attr-defined]
            md._spot_ltp = spot  # type: ignore[attr-defined]

            atm = _round_strike(spot, step)
            opt_ex = str(config.symbols.get("exchange_options", "NFO"))
            ce = _find_inst(universe, "CE", atm) or _fake_inst(
                "CE", atm, lot_size, underlying=ul, exchange=opt_ex
            )
            pe = _find_inst(universe, "PE", atm) or _fake_inst(
                "PE", atm, lot_size, underlying=ul, exchange=opt_ex
            )
            uni = ContractUniverse(
                spot=spot,
                atm_strike=atm,
                expiry_symbol=universe.expiry_symbol,
                instruments=universe.instruments,
                atm_ce=ce,
                atm_pe=pe,
            )
            band_states = _band_opt_states(
                uni, when=ts, spot=spot, bar_frac=bar_frac, bar=bar
            )
            chain = build_chain_snapshot(uni, band_states, prior_oi=prior_oi)
            features_eng.set_chain_snapshot(chain)
            for tok, st in band_states.items():
                if st is not None and st.oi is not None:
                    prior_oi[tok] = int(st.oi)
            features_eng.is_expiry_day = expiry_d == day

            # Option VWAP / Greeks context from ATM side vs VWAP (same idea as live)
            vwap_hint = session_vwap(m1)
            prefer_pe = vwap_hint is not None and spot < vwap_hint
            ctx_inst = pe if prefer_pe else ce
            ctx_st = band_states.get(ctx_inst.token) if ctx_inst else None
            opt_ctx: dict = {}
            if ctx_st and ctx_st.ltp is not None:
                opt_ctx["ltp"] = float(ctx_st.ltp)
                opt_ctx["spread_pct"] = float(ctx_st.spread_pct or 1)
                opt_ctx["volume"] = ctx_st.volume
                opt_ctx["oi"] = ctx_st.oi
                den = option_vwap_den.get(ctx_inst.token)
                num = option_vwap_num.get(ctx_inst.token)
                if den and num and den > 0:
                    opt_ctx["option_vwap"] = float(num / den)
                # else leave unset → quality gate scores +7 like live
                g = compute_greeks(
                    spot=float(spot),
                    strike=float(ctx_inst.strike),
                    premium=float(ctx_st.ltp),
                    option_type=ctx_inst.option_type,
                    expiry=expiry_d,
                    now=ts,
                )
                opt_ctx["delta"] = g.delta
                opt_ctx["gamma"] = g.gamma
                opt_ctx["iv"] = g.iv
            features_eng.set_option_context(opt_ctx)

            feat = features_eng.compute()
            feat = feat.model_copy(update={"ts": ts, "nifty_spot": spot})
            regime = regime_clf.classify(
                feat, m1, m5, is_expiry_day=(expiry_d == day), now=ts_ist
            )
            vwap = feat.session_vwap
            force = t_clock >= force_exit_t or is_last_scan

            still_open: list[OpenTrade] = []
            for ot in open_trades:
                prem, src = _mark_premium(
                    when=ts,
                    spot=spot,
                    side=ot.side,
                    strike=ot.strike,
                    token=ot.token or None,
                    bar_frac=bar_frac,
                    bar=bar,
                )
                ot.mfe = max(ot.mfe, prem - ot.entry_premium)
                should, reason = _exit_decision(
                    side=ot.side,
                    entry_premium=ot.entry_premium,
                    entry_ts=ot.entry_ts,
                    now=ts,
                    current_premium=prem,
                    mfe=ot.mfe,
                    spot=spot,
                    vwap=vwap,
                    force_exit=force,
                    cfg=exit_cfg,
                    market_data=md,
                    regime_primary=regime.primary,
                )
                if force and not reason:
                    should, reason = True, "session_end"
                if should and reason:
                    pnl = (prem - ot.entry_premium) * ot.lot_size * ot.lots
                    premium_freed = ot.entry_premium * ot.lot_size * ot.lots
                    deployed = max(Decimal("0"), deployed - premium_freed)
                    available = available + premium_freed + pnl
                    realized += pnl
                    stats.closed.append(
                        ClosedTrade(
                            strategy=ot.strategy,
                            side=ot.side,
                            strike=ot.strike,
                            entry_ts=ot.entry_ts,
                            exit_ts=ts,
                            entry_spot=ot.entry_spot,
                            exit_spot=spot,
                            entry_premium=ot.entry_premium,
                            exit_premium=prem,
                            lots=ot.lots,
                            lot_size=ot.lot_size,
                            pnl=pnl.quantize(Decimal("0.01")),
                            exit_reason=reason,
                            confidence=ot.confidence,
                            premium_source=f"{ot.premium_source}->{src}",
                            tsym=ot.tsym,
                        )
                    )
                    if ot.token:
                        cooldown_until[ot.token] = ts + timedelta(minutes=cooldown_min)
                    global_cooldown_until = ts + timedelta(minutes=cooldown_min)
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    learner.record_trade(
                        ot.strategy, pnl, confidence=ot.confidence, exit_reason=reason
                    )
                    if reason == "trend_reversal" and flip_on_reversal:
                        pending_flips.append(
                            {
                                "side": "PE" if ot.side == "CE" else "CE",
                                "from_side": ot.side,
                            }
                        )
                    # Live does not force an extra entry scan on the exit quote.
                    pool_equity = starting + realized
                    print(
                        f"  EXIT  {ts_ist.strftime('%H:%M:%S')} {ot.strategy} {ot.side} "
                        f"{ot.strike} lots={ot.lots} P&L=₹{pnl.quantize(Decimal('0.01'))} "
                        f"reason={reason}  open={len(open_trades)-1} "
                        f"pool=₹{pool_equity:.0f} avail=₹{available:.0f} deployed=₹{deployed:.0f}"
                    )
                else:
                    still_open.append(ot)
            open_trades = still_open

            equity = starting + realized
            if deployed > peak_deployed:
                peak_deployed = deployed
            util = (deployed / equity * Decimal("100")) if equity > 0 else Decimal("0")
            if util > peak_util_pct:
                peak_util_pct = util

            if t_clock < entry_start or t_clock > entry_end:
                pending_flips.clear()
                continue
            if len(m1) < 20 or len(m3) < 5:
                pending_flips.clear()
                continue
            if max_daily_loss > 0 and realized <= -max_daily_loss:
                stats.skips["max_daily_loss"] = stats.skips.get("max_daily_loss", 0) + 1
                pending_flips.clear()
                continue

            # Immediate CE↔PE flip after trend_reversal (before normal scan).
            flips = list(pending_flips)
            pending_flips.clear()
            for flip in flips:
                flip_side = flip["side"]
                resolved = resolve_side_contract(
                    config=config,
                    universe=uni,
                    side=flip_side,
                    spot=spot,
                    option_states=band_states,
                    expiry=expiry_d,
                    now=ts,
                )
                if resolved is None:
                    stats.skips["flip_no_contract"] = stats.skips.get("flip_no_contract", 0) + 1
                    continue
                flip_inst, flip_st, pick = resolved
                flip_bias = Bias.BULLISH if flip_side == "CE" else Bias.BEARISH
                flip_feat = feat.model_copy(update={"bias_5m": flip_bias, "ts": ts})
                flip_signal = CandidateSignal(
                    ts=ts,
                    setup_type="trend_reversal_flip",
                    side=flip_side,
                    instrument_token=flip_inst.token,
                    tsym=flip_inst.tsym,
                    strategy_version=str(config.strategy.get("strategy_version", "bt")),
                    feature_snapshot=flip_feat,
                    scanner_metadata={
                        "option_ltp": str(flip_st.ltp),
                        "lot_size": flip_inst.lot_size,
                        "exchange": flip_inst.exchange,
                        "strike_pick": pick,
                    },
                    confidence=80,
                )
                v = validator.validate(
                    flip_signal,
                    flip_st,
                    has_open_for_token=any(
                        o.token == flip_inst.token for o in open_trades if o.token
                    ),
                    in_cooldown=False,
                    kill_switch=False,
                    is_expiry_day=(expiry_d == day),
                    now=ts,
                )
                if not v.passed:
                    key = ",".join(v.rejection_reasons)[:60]
                    stats.skips[f"validator:{key}"] = stats.skips.get(f"validator:{key}", 0) + 1
                    continue
                _try_enter(
                    strategy="trend_reversal_flip",
                    side=flip_side,
                    strike=Decimal(str(pick.get("strike", flip_inst.strike))),
                    inst=flip_inst,
                    conf=80,
                    when=ts,
                    spot=spot,
                    bar_frac=bar_frac,
                    ts_ist=ts_ist,
                    equity=equity,
                    bypass_cooldown=True,
                    strike_pick=pick,
                    bar=bar,
                )

            if not allow_entry and not flips:
                continue

            total_entry_scans += 1

            if max_concurrent > 0 and len(open_trades) >= max_concurrent:
                stats.skips["max_concurrent"] = stats.skips.get("max_concurrent", 0) + 1
                continue

            options = {s.name: band_states for s in scanners}

            decision, signal = router.route(feat, regime, uni, options)
            if signal is None:
                key = decision.selected_reason.split(":")[0][:60]
                stats.skips[key] = stats.skips.get(key, 0) + 1
                continue

            stats.signals_seen += 1
            side = signal.side
            pick = signal.scanner_metadata.get("strike_pick") or {}
            strike = Decimal(str(pick.get("strike", atm)))
            inst = next(
                (i for i in uni.instruments if i.token == signal.instrument_token),
                None,
            )
            if inst is None:
                inst = _find_inst(uni, side, strike) or _fake_inst(
                    side, strike, lot_size, token=signal.instrument_token, tsym=signal.tsym
                )
            opt_st = band_states.get(inst.token) if inst else None
            if opt_st is None and inst is not None:
                prem0, _ = _mark_premium(
                    when=ts,
                    spot=spot,
                    side=side,
                    strike=strike,
                    token=inst.token,
                    bar_frac=bar_frac,
                    bar=bar,
                )
                opt_st = OptionState(
                    instrument_token=inst.token,
                    tsym=inst.tsym,
                    ltp=prem0,
                    bid=prem0 - Decimal("0.5"),
                    ask=prem0 + Decimal("0.5"),
                    spread_pct=Decimal("1"),
                )
            in_cd = (
                (global_cooldown_until is not None and ts < global_cooldown_until)
                or (
                    inst is not None
                    and inst.token in cooldown_until
                    and ts < cooldown_until[inst.token]
                )
            )
            v = validator.validate(
                signal,
                opt_st,
                has_open_for_token=any(
                    o.token == (inst.token if inst else "") for o in open_trades if o.token
                ),
                in_cooldown=in_cd,
                kill_switch=False,
                is_expiry_day=(expiry_d == day),
                now=ts,
            )
            if not v.passed:
                key = ",".join(v.rejection_reasons)[:60]
                stats.skips[f"validator:{key}"] = stats.skips.get(f"validator:{key}", 0) + 1
                continue

            _try_enter(
                strategy=signal.setup_type,
                side=side,
                strike=strike,
                inst=inst,
                conf=int(signal.confidence or 0),
                when=ts,
                spot=spot,
                bar_frac=bar_frac,
                ts_ist=ts_ist,
                equity=equity,
                strike_pick=pick,
                bar=bar,
            )

    print(
        f"\n  Exit ticks: {total_ticks} (@{exit_sec}s)  |  "
        f"Entry scans: {total_entry_scans} (@{scan_sec}s + post-exit)  |  "
        f"1m bars: {len(m1_all)}"
    )

    # Persist machine-readable export for reports
    export_path = (
        ROOT / "reports" / f"backtest_{m1_all[0].ts.astimezone(IST).date()}_{ul}.json"
    )
    export_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    payload = {
        "date": str(m1_all[0].ts.astimezone(IST).date()),
        "spot_open": float(open_spot),
        "spot_close": float(close_spot),
        "spot_change": float(close_spot - open_spot),
        "session_vwap": float(vwap_final) if (vwap_final := session_vwap(m1_all)) else None,
        "capital_start": float(starting),
        "capital_end": float(starting + realized),
        "total_pnl": float(realized),
        "peak_deployed": float(peak_deployed),
        "peak_util_pct": float(peak_util_pct),
        "rules": {
            "max_deployed_pct_of_equity": config.risk.get("max_deployed_pct_of_equity"),
            "max_premium_pct_of_available": config.risk.get("max_premium_pct_of_available"),
            "max_daily_loss": config.risk.get("max_daily_loss"),
            "confidence_lot_sizing": config.risk.get("confidence_lot_sizing"),
            "exits": "5% SL · progressive trail from 1.5% MFE · no time-stop when green",
            "strike_selection": "ATM±1 via BS delta/gamma",
            "flip_on_trend_reversal": flip_on_reversal,
            "exit_tick_seconds": exit_sec,
            "entry_scan_seconds": scan_sec,
            "cadence": "live_parity",
            "honest_replay": honest_replay,
        },
        "signals_seen": stats.signals_seen,
        "entries_filled": stats.entries,
        "trades": [
            {
                "n": n,
                "strategy": t.strategy,
                "side": t.side,
                "strike": float(t.strike),
                "tsym": t.tsym,
                "confidence": t.confidence,
                "lots": t.lots,
                "lot_size": t.lot_size,
                "qty": t.lots * t.lot_size,
                "entry_ist": t.entry_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "exit_ist": t.exit_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "hold_minutes": round((t.exit_ts - t.entry_ts).total_seconds() / 60, 1),
                "entry_spot": float(t.entry_spot),
                "exit_spot": float(t.exit_spot),
                "entry_premium": float(t.entry_premium),
                "exit_premium": float(t.exit_premium),
                "premium_change_pct": round(
                    float((t.exit_premium - t.entry_premium) / t.entry_premium * 100), 2
                )
                if t.entry_premium
                else 0,
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
                "premium_source": t.premium_source,
                "result": "WIN" if t.pnl > 0 else "LOSS",
            }
            for n, t in enumerate(stats.closed, 1)
        ],
        "skips": stats.skips,
    }
    export_path.write_text(json.dumps(payload, indent=2))
    print(f"\nJSON export: {export_path}")

    # Human-readable Markdown report
    md_path = ROOT / "reports" / f"backtest_{m1_all[0].ts.astimezone(IST).date()}_{ul}.md"
    wins = [t for t in stats.closed if t.pnl > 0]
    losses = [t for t in stats.closed if t.pnl <= 0]
    session_pnl = sum((t.pnl for t in stats.closed), Decimal("0"))
    pool_end_equity = starting + realized
    cap_label = (
        f"| Pool equity | ₹{pool_start_equity:.0f} → ₹{pool_end_equity:.2f} |"
        if capital_pool is not None
        else f"| Capital | ₹{starting:.0f} → ₹{pool_end_equity:.2f} |"
    )
    pnl_label = (
        f"| **Session P&L** | **₹{session_pnl:.2f}** |"
        if capital_pool is not None
        else f"| **Net P&L** | **₹{session_pnl:.2f}** |"
    )
    lines = [
        f"# Algo-MCX Day Backtest Report — {payload['date']} ({ul})",
        "",
        "## Session overview",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Spot | {open_spot} → {close_spot} (Δ {close_spot - open_spot}) |",
        f"| Session VWAP | {payload['session_vwap']:.2f} |" if payload["session_vwap"] else "| Session VWAP | n/a |",
        cap_label,
        pnl_label,
        f"| Trades | {len(stats.closed)} ({len(wins)} wins / {len(losses)} losses) |",
        f"| Win rate | {len(wins)/len(stats.closed)*100:.1f}% |" if stats.closed else "| Win rate | n/a |",
        f"| Peak deployed | ₹{peak_deployed:.0f} ({peak_util_pct:.1f}% of equity) |",
        f"| Deploy target | {config.risk.get('max_deployed_pct_of_equity')}% |",
        "",
        "## Rules used",
        "",
        "- Confidence-based lots (70→1, 80→2, 90→3), fill toward deploy room",
        "- Multiple concurrent positions (different strike/side)",
        "- Max ~85% equity deployed; per-trade premium ≤65% of available",
        "- **5% max stop** on losers; early cut if never +2% green",
        "- **Progressive trailing** from +1.5% MFE (tighter lock-in as profit grows)",
        "- **No time exit while in profit** — winners run on trailing SL",
        "- Trend reversal (VWAP bias flip) still exits immediately",
        "- Strike pick among **ATM / ATM±1** via Black-Scholes delta/gamma + spread",
        "- **Live-parity cadence**: exit ticks ~2s (quote pace); entry scans every scan_interval + post-exit",
        "- Spot/premium path visits OHLC extremes (tick-like adverse/trail)",
        "- No fixed profit target wait",
        "- Premium marked from **live option 1m candles** (intra-bar OHLC path)",
        "",
        "## Trade-by-trade detail",
        "",
    ]
    for row in payload["trades"]:
        lines += [
            f"### Trade #{row['n']} — {row['result']} ₹{row['pnl']:+.2f}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Strategy | `{row['strategy']}` |",
            f"| Contract | {row['side']} {row['strike']:.0f} (`{row['tsym'] or 'n/a'}`) |",
            f"| Confidence | {row['confidence']} |",
            f"| Size | {row['lots']} lot(s) × {row['lot_size']} = **{row['qty']} qty** |",
            f"| Entry | **{row['entry_ist']} IST** @ premium ₹{row['entry_premium']:.2f} (spot {row['entry_spot']:.2f}) |",
            f"| Exit | **{row['exit_ist']} IST** @ premium ₹{row['exit_premium']:.2f} (spot {row['exit_spot']:.2f}) |",
            f"| Hold | {row['hold_minutes']} min |",
            f"| Premium Δ | {row['premium_change_pct']:+.2f}% |",
            f"| Exit reason | `{row['exit_reason']}` |",
            f"| Marking | {row['premium_source']} |",
            f"| **P&L** | **₹{row['pnl']:+.2f}** |",
            "",
        ]
    lines += [
        "## P&L running balance",
        "",
        "| After trade | Cumulative P&L | Equity |",
        "|---|---|---|",
    ]
    cum = Decimal("0")
    for row in payload["trades"]:
        cum += Decimal(str(row["pnl"]))
        lines.append(
            f"| #{row['n']} {row['strategy']} {row['side']} | ₹{float(cum):+.2f} | ₹{float(pool_start_equity + cum):.2f} |"
        )
    lines += [
        "",
        "## Exit reason breakdown",
        "",
    ]
    by_reason: dict[str, list] = {}
    for t in stats.closed:
        by_reason.setdefault(t.exit_reason, []).append(t)
    for reason, items in sorted(by_reason.items()):
        rpnl = sum((x.pnl for x in items), Decimal("0"))
        lines.append(f"- `{reason}`: {len(items)} trades, P&L ₹{rpnl:+.2f}")
    lines += ["", f"_Generated by `scripts/day_backtest.py` · export `{export_path.name}`_", ""]
    md_path.write_text("\n".join(lines))
    print(f"Markdown report: {md_path}")

    print("\n" + "=" * 64)
    print(f"TODAY BACKTEST — {ul} · futures · live-parity exits · flip · ATM±1")
    print("=" * 64)
    print(f"Date (IST): {m1_all[0].ts.astimezone(IST).date()}")
    print(f"Underlying: {ul}  capital=₹{starting}")
    print(f"Spot: {open_spot} → {close_spot}  Δ={close_spot - open_spot}")
    print(f"1m bars: {len(m1_all)}  | 3m: {len(m3_all)}  | 5m: {len(m5_all)}")
    print(f"Signals seen: {stats.signals_seen}  | Entries filled: {stats.entries}")
    print(f"Session VWAP (full day): {vwap_final}")
    print(f"Pool equity start: ₹{pool_start_equity:.0f}  | End: ₹{pool_end_equity:.2f}  session P&L: ₹{session_pnl:.2f}")
    print(
        f"Peak deployed: ₹{peak_deployed:.0f}  "
        f"({peak_util_pct:.1f}% of equity)  target≈85%"
    )

    total = sum((t.pnl for t in stats.closed), Decimal("0"))
    print(f"\nTrades closed: {len(stats.closed)}  wins={len(wins)} losses={len(losses)}")
    print(f"Total P&L (₹): {total}")
    if stats.closed:
        by_reason2: dict[str, int] = {}
        for t in stats.closed:
            by_reason2[t.exit_reason] = by_reason2.get(t.exit_reason, 0) + 1
        print("Exit reasons:", ", ".join(f"{k}={v}" for k, v in sorted(by_reason2.items())))
        print("\n--- Trade list ---")
        for n, t in enumerate(stats.closed, 1):
            hold_m = (t.exit_ts - t.entry_ts).total_seconds() / 60
            print(
                f"{n}. {t.entry_ts.astimezone(IST).strftime('%H:%M')}→"
                f"{t.exit_ts.astimezone(IST).strftime('%H:%M')}  "
                f"{t.strategy} {t.side} {t.strike}  conf={t.confidence} "
                f"lots={t.lots}  "
                f"prem {t.entry_premium}→{t.exit_premium}  "
                f"P&L ₹{t.pnl}  exit={t.exit_reason}  "
                f"hold={hold_m:.0f}m"
            )

    top = sorted(stats.skips.items(), key=lambda x: -x[1])[:10]
    print("\n--- Skip / NO_TRADE reasons ---")
    for k, v in top:
        print(f"  {v:4d}  {k}")

    opt_entries = sum(1 for t in stats.closed if t.premium_source.startswith("option_candle"))
    print(
        f"\nPremium marking: {opt_entries}/{len(stats.closed)} trades used option candles"
    )
    print("=" * 64)

    if broker_connected and broker is not None:
        await broker.disconnect()

    if capital_pool is not None:
        capital_pool["realized"] = realized
        end_equity = capital_pool["starting"] + realized
        capital_pool["opening_equity"] = end_equity
        capital_pool["available"] = end_equity
        capital_pool["deployed"] = Decimal("0")

    return {
        "underlying": ul,
        "date": str(m1_all[0].ts.astimezone(IST).date()),
        "starting": starting,
        "pnl": total,
        "trades": len(stats.closed),
        "wins": len(wins),
        "losses": len(losses),
        "entries": stats.entries,
        "signals_seen": stats.signals_seen,
        "open_spot": open_spot,
        "close_spot": close_spot,
        "closed": stats.closed,
        "skips": dict(stats.skips),
    }


async def run_week_backtest(
    *,
    week_monday: date | None = None,
    underlying: str = "NIFTY",
    from_snap: bool = False,
    refresh_snap: bool = False,
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    honest_replay: bool = True,
    initial_capital: Decimal | None = None,
) -> dict[str, Any]:
    """Mon–Fri backtest with compounded capital (P&L carries day to day)."""
    from algomcx.symbols_util import total_account_capital, uses_pooled_capital

    config = get_config()
    ul = underlying.upper()
    today = datetime.now(tz=IST).date()
    if week_monday is None:
        # Last complete Mon–Fri week (if today is Sat/Sun, that's the week just ended).
        dow = today.weekday()  # Mon=0
        if dow >= 5:
            friday = today - timedelta(days=dow - 4)
        else:
            friday = today - timedelta(days=dow + 3)
        week_monday = friday - timedelta(days=4)
    else:
        friday = week_monday + timedelta(days=4)

    days = [week_monday + timedelta(days=i) for i in range(5)]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    initial = initial_capital
    if initial is None:
        initial = total_account_capital(config) if uses_pooled_capital(config) else Decimal(
            str(config.risk.get("account_capital_inr", 50000))
        )
    capital_pool: dict[str, Decimal] = {
        "starting": initial,
        "available": initial,
        "deployed": Decimal("0"),
        "realized": Decimal("0"),
    }

    print("\n" + "=" * 72)
    print(f"WEEKLY BACKTEST — {ul} · {days[0]} → {days[-1]} · carry-forward capital")
    print(f"Opening equity: ₹{initial}")
    print("Expiry note: NIFTY weekly = Tuesday — Mon/Tue use expiring series; Wed+ rolls next week")
    print(
        f"Replay mode: {'HONEST (no future bar/VWAP peek)' if honest_replay else 'synthetic intra-bar'}"
    )
    print("=" * 72)

    daily_results: list[dict[str, Any]] = []
    missing: list[date] = []

    for label, day in zip(day_names, days):
        print(f"\n{'#' * 72}\n#  {label} {day.isoformat()}\n{'#' * 72}")
        open_eq = capital_pool["starting"] + capital_pool["realized"]
        capital_pool["available"] = open_eq
        capital_pool["deployed"] = Decimal("0")
        print(f"  >>> Session open equity: ₹{open_eq:.2f}")

        snap = None if refresh_snap else load_day_snap(day, ul)
        if from_snap and snap is None:
            print(f"  WARN: no snap for {day} — skipping (fetch with refresh-snap)")
            missing.append(day)
            continue

        out = await run_backtest(
            day,
            underlying=ul,
            exit_interval=exit_interval,
            scan_interval=scan_interval,
            from_snap=from_snap,
            refresh_snap=refresh_snap,
            capital_pool=capital_pool,
            honest_replay=honest_replay,
        )
        if not out:
            missing.append(day)
            continue

        end_eq = float(open_eq) + float(out["pnl"])
        day_pnl = out["pnl"]
        snap_meta = snap.get("meta", {}) if snap else {}
        expiry_sym = snap_meta.get("expiry_symbol") if snap_meta else "?"
        if not expiry_sym or expiry_sym == "?":
            loaded = load_day_snap(day, ul)
            if loaded:
                expiry_sym = loaded.get("meta", {}).get("expiry_symbol", "?")

        daily_results.append(
            {
                "underlying": out["underlying"],
                "date": out["date"],
                "weekday": label,
                "open_equity": float(open_eq),
                "close_equity": end_eq,
                "day_pnl": float(day_pnl),
                "pnl": float(out["pnl"]),
                "trades": out["trades"],
                "wins": out["wins"],
                "losses": out["losses"],
                "entries": out["entries"],
                "signals_seen": out["signals_seen"],
                "open_spot": float(out["open_spot"]),
                "close_spot": float(out["close_spot"]),
                "expiry_symbol": expiry_sym,
            }
        )

        # Compound: next day starts from today's closing equity.
        capital_pool["starting"] = Decimal(str(end_eq))
        capital_pool["realized"] = Decimal("0")
        capital_pool["available"] = Decimal(str(end_eq))
        capital_pool["deployed"] = Decimal("0")
        print(f"  >>> Session close equity: ₹{end_eq:.2f}  (day P&L ₹{float(day_pnl):.2f})")

    final_equity = capital_pool["starting"]
    total_pnl = final_equity - initial
    total_trades = sum(r["trades"] for r in daily_results)
    total_wins = sum(r["wins"] for r in daily_results)

    def _json_num(v: Any) -> Any:
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dict):
            return {k: _json_num(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_json_num(x) for x in v]
        return v

    week_payload = _json_num(
        {
        "underlying": ul,
        "week_start": days[0].isoformat(),
        "week_end": days[-1].isoformat(),
        "capital_start": float(initial),
        "capital_end": float(final_equity),
        "total_pnl": float(total_pnl),
        "return_pct": float(total_pnl / initial * 100) if initial else 0.0,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "days_run": len(daily_results),
        "days_missing": [d.isoformat() for d in missing],
        "daily": daily_results,
        }
    )

    report_stem = f"backtest_week_{'honest_' if honest_replay else ''}{days[0]}_{days[-1]}_{ul}"
    config_capital = total_account_capital(config) if uses_pooled_capital(config) else Decimal(
        str(config.risk.get("account_capital_inr", 50000))
    )
    if initial != config_capital:
        report_stem = f"backtest_week_{'honest_' if honest_replay else ''}{int(initial)}_{days[0]}_{days[-1]}_{ul}"
    json_path = ROOT / "reports" / f"{report_stem}.json"
    md_path = ROOT / "reports" / f"{report_stem}.md"
    json_path.write_text(json.dumps(week_payload, indent=2))

    md_lines = [
        f"# Weekly Backtest — {ul} ({days[0]} → {days[-1]})",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Starting capital (Mon) | ₹{initial:,.2f} |",
        f"| Ending capital (Fri) | **₹{final_equity:,.2f}** |",
        f"| **Week P&L** | **₹{total_pnl:+,.2f}** ({week_payload['return_pct']:+.1f}%) |",
        f"| Trading days replayed | {len(daily_results)} / 5 |",
        f"| Total trades | {total_trades} ({total_wins} wins) |",
        "",
        "## Daily carry-forward (compounded)",
        "",
        "| Day | Date | Expiry series | Open ₹ | Close ₹ | Day P&L | Trades | W/L |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in daily_results:
        md_lines.append(
            f"| {r['weekday']} | {r['date']} | {r.get('expiry_symbol', '?')} | "
            f"₹{r['open_equity']:,.0f} | ₹{r['close_equity']:,.0f} | "
            f"₹{r['day_pnl']:+,.2f} | {r['trades']} | {r['wins']}/{r['losses']} |"
        )
    if missing:
        md_lines += ["", f"**Missing snaps:** {', '.join(d.isoformat() for d in missing)}"]
    md_lines += [
        "",
        "_Capital compounds daily — each session opens with prior close equity._",
        f"_Generated by `scripts/day_backtest.py --week` · `{json_path.name}`_",
        "",
    ]
    md_path.write_text("\n".join(md_lines))

    ledger_stem = f"ledger_week_{week_start}_{week_end}_{ul}"
    if initial != config_capital:
        ledger_stem = f"ledger_week_{int(initial)}_{week_start}_{week_end}_{ul}"
    ledger_md, ledger_json = write_week_ledger(
        week_start=days[0],
        week_end=days[-1],
        underlying=ul,
        capital_start=initial,
        stem=ledger_stem,
    )

    print("\n" + "=" * 72)
    print("WEEKLY SUMMARY")
    print("=" * 72)
    for r in daily_results:
        print(
            f"  {r['weekday']} {r['date']}  exp={r.get('expiry_symbol','?')}  "
            f"₹{r['open_equity']:,.0f} → ₹{r['close_equity']:,.0f}  "
            f"P&L ₹{r['day_pnl']:+,.2f}  trades={r['trades']}"
        )
    print("-" * 72)
    print(
        f"  WEEK TOTAL  ₹{initial:,.0f} → ₹{final_equity:,.2f}  "
        f"P&L ₹{total_pnl:+,.2f} ({week_payload['return_pct']:+.1f}%)  "
        f"trades={total_trades}"
    )
    if missing:
        print(f"  MISSING DAYS: {', '.join(d.isoformat() for d in missing)}")
    print(f"\nJSON: {json_path}")
    print(f"Markdown: {md_path}")
    print(f"Ledger: {ledger_md}")
    print(f"Ledger JSON: {ledger_json}")
    print("=" * 72)

    return week_payload


def write_week_ledger(
    *,
    week_start: date,
    week_end: date,
    underlying: str,
    capital_start: Decimal,
    stem: str | None = None,
) -> tuple[Path, Path]:
    """Merge per-day backtest JSONs into one full trade ledger."""
    ul = underlying.upper()
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    days = [week_start + timedelta(days=i) for i in range(5)]
    all_trades: list[dict[str, Any]] = []
    for label, day in zip(day_names, days):
        p = ROOT / "reports" / f"backtest_{day.isoformat()}_{ul}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text())
        for t in j.get("trades", []):
            all_trades.append({**t, "date": day.isoformat(), "weekday": label})
    all_trades.sort(key=lambda t: t["entry_ist"])

    capital = float(capital_start)
    rows: list[dict[str, Any]] = []
    for i, t in enumerate(all_trades, 1):
        capital += float(t["pnl"])
        rows.append({**t, "trade_num": i, "running_equity": round(capital, 2)})

    stem = stem or f"ledger_week_{week_start}_{week_end}_{ul}"
    json_path = ROOT / "reports" / f"{stem}.json"
    md_path = ROOT / "reports" / f"{stem}.md"
    json_path.write_text(
        json.dumps(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "underlying": ul,
                "capital_start": float(capital_start),
                "capital_end": capital,
                "total_trades": len(rows),
                "trades": rows,
            },
            indent=2,
        )
    )

    lines = [
        f"# Weekly Trade Ledger — {ul} ({week_start} → {week_end})",
        "",
        "Full trade log: CE/PE, entry/exit premium & time, lots, spot, P&L, running equity.",
        "",
        f"**Starting capital:** ₹{float(capital_start):,.0f}  |  "
        f"**Ending capital:** ₹{capital:,.2f}  |  **Trades:** {len(rows)}",
        "",
        "## Ledger table",
        "",
        "| # | Day | Date | Strategy | Side | Symbol | Strike | Lots | Qty | "
        "Entry (IST) | Entry ₹ | Exit (IST) | Exit ₹ | Hold | Spot In→Out | P&L | Balance | Result | Exit |",
        "|---:|---|---|---|---|---|---:|---:|---:|---|---:|---|---:|---:|---|---:|---:|---|---|",
    ]
    for r in rows:
        entry_t = r["entry_ist"].split(" ", 1)[1]
        exit_t = r["exit_ist"].split(" ", 1)[1]
        lines.append(
            f"| {r['trade_num']} | {r['weekday']} | {r['date']} | {r['strategy']} | {r['side']} | "
            f"`{r['tsym']}` | {r['strike']:.0f} | {r['lots']} | {r['qty']} | {entry_t} | "
            f"{r['entry_premium']:.2f} | {exit_t} | {r['exit_premium']:.2f} | "
            f"{r['hold_minutes']:.1f}m | {r['entry_spot']:.0f}→{r['exit_spot']:.0f} | "
            f"**{r['pnl']:+,.2f}** | ₹{r['running_equity']:,.0f} | {r['result']} | {r['exit_reason']} |"
        )
    lines += ["", "## Trade-by-trade detail", ""]
    for r in rows:
        lines += [
            f"### #{r['trade_num']} — {r['result']} ₹{r['pnl']:+,.2f} ({r['weekday']} {r['date']})",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Strategy | `{r['strategy']}` |",
            f"| Contract | **{r['side']}** {r['strike']:.0f} — `{r['tsym']}` |",
            f"| Confidence | {r['confidence']} |",
            f"| Size | {r['lots']} lot(s) × {r['lot_size']} = **{r['qty']} qty** |",
            f"| Entry | **{r['entry_ist']} IST** @ premium **₹{r['entry_premium']:.2f}** "
            f"(NIFTY spot {r['entry_spot']:.2f}) |",
            f"| Exit | **{r['exit_ist']} IST** @ premium **₹{r['exit_premium']:.2f}** "
            f"(NIFTY spot {r['exit_spot']:.2f}) |",
            f"| Premium change | {r['premium_change_pct']:+.2f}% |",
            f"| Hold | {r['hold_minutes']:.1f} min |",
            f"| **P&L** | **₹{r['pnl']:+,.2f}** |",
            f"| Running equity | ₹{r['running_equity']:,.2f} |",
            f"| Exit reason | `{r['exit_reason']}` |",
            "",
        ]
    lines += [
        f"_Generated by `scripts/day_backtest.py --week` · `{json_path.name}`_",
        "",
    ]
    md_path.write_text("\n".join(lines))
    return md_path, json_path


async def run_dual_backtest(
    day: date | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    from algomcx.symbols_util import list_underlyings, total_account_capital, uses_pooled_capital

    config = get_config()
    symbols = [
        str(row.get("symbol", "")).upper()
        for row in list_underlyings(config)
        if row.get("symbol")
    ]
    if not symbols:
        symbols = ["NIFTY"]
    capital_pool: dict[str, Decimal] | None = None
    if uses_pooled_capital(config):
        total = total_account_capital(config)
        capital_pool = {
            "starting": total,
            "available": total,
            "deployed": Decimal("0"),
            "realized": Decimal("0"),
        }
        print(f"\n>>> Shared capital pool: ₹{total}")
    results: list[dict[str, Any]] = []
    for sym in symbols:
        print("\n" + "#" * 72)
        print(f"#  BACKTEST: {sym}")
        print("#" * 72)
        out = await run_backtest(
            day, underlying=sym, capital_pool=capital_pool, **kwargs
        )
        if out:
            results.append(out)
    if len(results) > 1:
        combined_pnl = sum((r["pnl"] for r in results), Decimal("0"))
        combined_trades = sum(r["trades"] for r in results)
        combined_wins = sum(r["wins"] for r in results)
        print("\n" + "=" * 72)
        print("COMBINED DUAL-INDEX BACKTEST")
        print("=" * 72)
        for r in results:
            print(
                f"  {r['underlying']:6s}  P&L ₹{r['pnl']:>10}  "
                f"trades={r['trades']}  wins={r['wins']}  "
                f"spot {r['open_spot']}→{r['close_spot']}"
            )
        print("-" * 72)
        print(
            f"  TOTAL   P&L ₹{combined_pnl:>10}  "
            f"trades={combined_trades}  wins={combined_wins}"
        )
        if capital_pool is not None:
            end_equity = capital_pool["starting"] + capital_pool["realized"]
            print(
                f"  POOL    end equity ₹{end_equity:.2f}  "
                f"avail=₹{capital_pool['available']:.0f}  "
                f"deployed=₹{capital_pool['deployed']:.0f}"
            )
        print("=" * 72)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Algo-MCX day backtest")
    parser.add_argument(
        "--date",
        help="IST session date YYYY-MM-DD (default: today)",
        default=None,
    )
    parser.add_argument(
        "--underlying",
        help="Underlying symbol (default: NIFTY)",
        default="NIFTY",
    )
    parser.add_argument(
        "--exit-interval",
        type=int,
        default=None,
        help="Exit tick seconds (default: runtime backtest_exit_interval_seconds / 2)",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=None,
        help="Entry scan seconds (default: runtime scan_interval_seconds / 30)",
    )
    parser.add_argument(
        "--from-snap",
        action="store_true",
        help="Replay only from reports/snaps/YYYY-MM-DD (no broker fetch)",
    )
    parser.add_argument(
        "--refresh-snap",
        action="store_true",
        help="Re-fetch Flattrade 1m series and overwrite the day snap",
    )
    parser.add_argument(
        "--snap-only",
        action="store_true",
        help="Fetch and save day snap, then exit without replaying",
    )
    parser.add_argument(
        "--week",
        action="store_true",
        help="Run Mon–Fri week backtest with capital carry-forward (last complete week)",
    )
    parser.add_argument(
        "--week-start",
        help="Monday of week to backtest YYYY-MM-DD (with --week)",
        default=None,
    )
    parser.add_argument(
        "--optimistic-intra-bar",
        action="store_true",
        help="Allow intra-bar VWAP/close path (can peek at future within 1m bar; not recommended)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Override starting capital for backtest (week mode compounds daily from this)",
    )
    args = parser.parse_args()
    d = date.fromisoformat(args.date) if args.date else None
    honest_replay = not args.optimistic_intra_bar
    capital_override = Decimal(str(args.capital)) if args.capital is not None else None
    common = dict(
        exit_interval=args.exit_interval,
        scan_interval=args.scan_interval,
        from_snap=args.from_snap,
        refresh_snap=args.refresh_snap,
        snap_only=args.snap_only,
        honest_replay=honest_replay,
    )
    if args.week:
        week_mon = date.fromisoformat(args.week_start) if args.week_start else None
        asyncio.run(
            run_week_backtest(
                week_monday=week_mon,
                underlying=args.underlying.upper()
                if str(args.underlying).upper() != "ALL"
                else "NIFTY",
                from_snap=args.from_snap,
                refresh_snap=args.refresh_snap,
                exit_interval=args.exit_interval,
                scan_interval=args.scan_interval,
                honest_replay=honest_replay,
                initial_capital=capital_override,
            )
        )
    elif str(args.underlying).upper() == "ALL":
        asyncio.run(run_dual_backtest(d, **common))
    else:
        asyncio.run(run_backtest(d, underlying=args.underlying.upper(), **common))
