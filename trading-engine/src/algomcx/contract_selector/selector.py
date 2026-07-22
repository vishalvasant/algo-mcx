from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.config import AppConfig
from algomcx.contract_selector.expiry import parse_expiry_tag
from algomcx.contract_selector.mcx_options import (
    chain_anchor_for,
    instruments_from_search,
    option_expiry_candidates,
)
from algomcx.contract_selector.scripmaster import load_weekly_band_from_scripmaster
from algomcx.models.events import Instrument
from algomcx.symbols_util import strike_band_points

logger = structlog.get_logger(__name__)


@dataclass
class ContractUniverse:
    spot: Decimal
    atm_strike: Decimal
    expiry_symbol: str | None = None
    instruments: list[Instrument] = field(default_factory=list)
    atm_ce: Instrument | None = None
    atm_pe: Instrument | None = None
    subscription_keys: list[str] = field(default_factory=list)


class ContractSelector:
    def __init__(self, config: AppConfig, broker: BrokerAdapter) -> None:
        self._config = config
        self._broker = broker

    def _sym(self) -> dict[str, Any]:
        return self._config.symbols

    def atm_strike_for_spot(self, spot: Decimal) -> Decimal:
        step = Decimal(str(self._sym()["strike_step"]))
        return (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

    def retarget_atm(self, universe: ContractUniverse, spot: Decimal) -> ContractUniverse:
        atm = self.atm_strike_for_spot(spot)
        if not universe.instruments:
            return ContractUniverse(
                spot=spot,
                atm_strike=atm,
                expiry_symbol=universe.expiry_symbol,
                instruments=[],
                atm_ce=None,
                atm_pe=None,
                subscription_keys=universe.subscription_keys,
            )
        atm_ce = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "CE"),
            None,
        )
        atm_pe = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "PE"),
            None,
        )
        if atm_ce is None:
            ces = [i for i in universe.instruments if i.option_type == "CE"]
            atm_ce = min(ces, key=lambda i: abs(i.strike - atm), default=None)
        if atm_pe is None:
            pes = [i for i in universe.instruments if i.option_type == "PE"]
            atm_pe = min(pes, key=lambda i: abs(i.strike - atm), default=None)
        return ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=universe.expiry_symbol,
            instruments=universe.instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=universe.subscription_keys,
        )

    def strike_in_band(self, strike: Decimal, atm: Decimal) -> bool:
        sym = self._sym()
        band = Decimal(str(strike_band_points(sym)))
        step = Decimal(str(sym["strike_step"]))
        if abs(strike - atm) > band:
            return False
        diff = abs(strike - atm)
        return step == 0 or (diff % step) == 0

    async def build_universe(self, spot: Decimal) -> ContractUniverse:
        sym = self._sym()
        atm = self.atm_strike_for_spot(spot)
        exchange = sym["exchange_options"]
        underlying = sym["underlying"]
        option_prefix = str(sym.get("option_prefix") or underlying).upper()
        option_search = str(sym.get("option_search_text") or underlying)
        step = Decimal(str(sym["strike_step"]))
        band_points = Decimal(str(strike_band_points(sym)))
        band_steps = int(sym.get("atm_strike_steps", 10))

        search_rows = await self._broker.search_scrip(exchange, option_search)
        candidates = option_expiry_candidates(
            search_rows,
            option_prefix=option_prefix,
            limit=4,
        )

        if not candidates:
            logger.warning(
                "option_expiry_not_found",
                underlying=underlying,
                option_search=option_search,
                option_prefix=option_prefix,
            )
            return ContractUniverse(spot=spot, atm_strike=atm)

        logger.info(
            "option_expiry_candidates",
            underlying=underlying,
            candidates=candidates,
            option_search=option_search,
        )

        for expiry_tag in candidates:
            universe = await self._build_for_expiry(
                spot=spot,
                atm=atm,
                exchange=exchange,
                underlying=underlying,
                option_prefix=option_prefix,
                search_rows=search_rows,
                band_steps=band_steps,
                band_points=band_points,
                step=step,
                expiry_tag=expiry_tag,
            )
            if universe.instruments:
                return universe
            logger.warning(
                "option_expiry_chain_empty",
                underlying=underlying,
                expiry=expiry_tag,
                anchor=chain_anchor_for(option_prefix, expiry_tag, int(atm), "CE"),
            )

        # Search-based fallback (no get_option_chain).
        for expiry_tag in candidates:
            instruments = instruments_from_search(
                search_rows,
                option_prefix=option_prefix,
                expiry_tag=expiry_tag,
                atm=atm,
                band_points=band_points,
                step=step,
                exchange=exchange,
                underlying=underlying,
            )
            if not instruments:
                continue
            return self._finalize_universe(
                spot=spot,
                atm=atm,
                expiry_tag=expiry_tag,
                instruments=instruments,
                exchange=exchange,
            )

        # NFO scripmaster fallback (no-op for MCX).
        if exchange.upper() != "MCX":
            for expiry_tag in candidates:
                try:
                    instruments = load_weekly_band_from_scripmaster(
                        underlying=underlying,
                        expiry_tag=expiry_tag,
                        atm=atm,
                        band_points=band_points,
                        exchange=exchange,
                    )
                except Exception:
                    logger.exception("scripmaster_fallback_failed", expiry=expiry_tag)
                    continue
                if instruments:
                    return self._finalize_universe(
                        spot=spot,
                        atm=atm,
                        expiry_tag=expiry_tag,
                        instruments=instruments,
                        exchange=exchange,
                    )

        logger.warning(
            "option_expiry_all_chains_empty",
            underlying=underlying,
            candidates=candidates,
        )
        return ContractUniverse(spot=spot, atm_strike=atm, expiry_symbol=candidates[0])

    def _finalize_universe(
        self,
        *,
        spot: Decimal,
        atm: Decimal,
        expiry_tag: str,
        instruments: list[Instrument],
        exchange: str,
    ) -> ContractUniverse:
        atm_ce = next((i for i in instruments if i.is_atm and i.option_type == "CE"), None)
        atm_pe = next((i for i in instruments if i.is_atm and i.option_type == "PE"), None)
        if atm_ce is None:
            atm_ce = next((i for i in instruments if i.option_type == "CE"), None)
        if atm_pe is None:
            atm_pe = next((i for i in instruments if i.option_type == "PE"), None)
        spot_exchange = self._sym()["exchange_spot"]
        spot_token = self._sym()["spot_token"]
        keys = [BrokerAdapter.format_instrument(spot_exchange, spot_token)]
        keys.extend(BrokerAdapter.format_instrument(i.exchange, i.token) for i in instruments)
        universe = ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=expiry_tag,
            instruments=instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=keys,
        )
        logger.info(
            "contract_universe_built",
            underlying=self._sym()["underlying"],
            expiry=expiry_tag,
            instrument_count=len(instruments),
            has_atm_ce=atm_ce is not None,
            has_atm_pe=atm_pe is not None,
        )
        return universe

    async def _build_for_expiry(
        self,
        *,
        spot: Decimal,
        atm: Decimal,
        exchange: str,
        underlying: str,
        option_prefix: str,
        search_rows: list[dict[str, Any]],
        band_steps: int,
        band_points: Decimal,
        step: Decimal,
        expiry_tag: str,
    ) -> ContractUniverse:
        anchor = chain_anchor_for(option_prefix, expiry_tag, int(atm), "CE")
        # Flattrade count=N returns ~2N strikes; request +1 so ATM±band_steps is covered.
        chain = await self._broker.get_option_chain(
            exchange=exchange,
            tradingsymbol=anchor,
            strikeprice=float(atm),
            count=band_steps + 1,
        )

        expiry_date = parse_expiry_tag(expiry_tag)
        instruments: list[Instrument] = []
        atm_ce: Instrument | None = None
        atm_pe: Instrument | None = None

        for row in chain:
            strike = Decimal(str(row.get("strprc", 0)))
            if not self.strike_in_band(strike, atm):
                continue
            optt = str(row.get("optt", "")).upper()
            if optt in ("CE", "C", "CALL"):
                option_type = "CE"
            elif optt in ("PE", "P", "PUT"):
                option_type = "PE"
            else:
                continue
            inst = Instrument(
                exchange=exchange,
                token=str(row["token"]),
                tsym=str(row["tsym"]),
                underlying=underlying,
                expiry_date=datetime.combine(expiry_date, datetime.min.time()).replace(
                    tzinfo=timezone.utc
                )
                if expiry_date
                else None,
                strike=strike,
                option_type=option_type,
                lot_size=int(row.get("ls", 1)),
                tick_size=Decimal(str(row["ti"])) if row.get("ti") else None,
                is_atm=strike == atm,
                in_band=True,
            )
            instruments.append(inst)
            if strike == atm and option_type == "CE":
                atm_ce = inst
            if strike == atm and option_type == "PE":
                atm_pe = inst

        if instruments:
            return self._finalize_universe(
                spot=spot,
                atm=atm,
                expiry_tag=expiry_tag,
                instruments=instruments,
                exchange=exchange,
            )

        # Chain API empty — build from cached search rows.
        instruments = instruments_from_search(
            search_rows,
            option_prefix=option_prefix,
            expiry_tag=expiry_tag,
            atm=atm,
            band_points=band_points,
            step=step,
            exchange=exchange,
            underlying=underlying,
        )
        if instruments:
            return self._finalize_universe(
                spot=spot,
                atm=atm,
                expiry_tag=expiry_tag,
                instruments=instruments,
                exchange=exchange,
            )

        return ContractUniverse(spot=spot, atm_strike=atm, expiry_symbol=expiry_tag)

    async def persist_instruments(self, pool: Any, universe: ContractUniverse) -> None:
        underlying = self._sym()["underlying"]
        expiry_date = None
        if universe.expiry_symbol:
            expiry_date = parse_expiry_tag(universe.expiry_symbol)

        if expiry_date is not None:
            await pool.execute(
                """
                UPDATE instruments
                SET in_band = FALSE, is_atm = FALSE
                WHERE underlying = $1
                  AND (expiry_date IS DISTINCT FROM $2 OR expiry_date IS NULL)
                """,
                underlying,
                expiry_date,
            )
            await pool.execute(
                """
                UPDATE instruments
                SET in_band = FALSE, is_atm = FALSE
                WHERE underlying = $1 AND expiry_date = $2
                """,
                underlying,
                expiry_date,
            )

        for inst in universe.instruments:
            await pool.execute(
                """
                INSERT INTO instruments (
                    exchange, token, tsym, underlying, expiry_date,
                    strike, option_type, lot_size, tick_size, is_atm, in_band
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (exchange, token) DO UPDATE SET
                    tsym = EXCLUDED.tsym,
                    expiry_date = EXCLUDED.expiry_date,
                    strike = EXCLUDED.strike,
                    option_type = EXCLUDED.option_type,
                    lot_size = EXCLUDED.lot_size,
                    is_atm = EXCLUDED.is_atm,
                    in_band = EXCLUDED.in_band
                """,
                inst.exchange,
                inst.token,
                inst.tsym,
                inst.underlying,
                inst.expiry_date.date() if inst.expiry_date else date.today(),
                inst.strike,
                inst.option_type,
                inst.lot_size,
                inst.tick_size,
                inst.is_atm,
                inst.in_band,
            )
