from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.config import get_config
from algomcx.contract_selector.expiry import (
  include_expiry_day,
  nearest_weekly_expiry_tag,
  weekly_expiry_candidates,
)
from algomcx.contract_selector.selector import ContractSelector


async def main() -> None:
    print("include_today_now:", include_expiry_day())
    print(
        "calendar tomorrow:",
        nearest_weekly_expiry_tag([], underlying="NIFTY", as_of=date(2026, 7, 15)),
    )
    print(
        "candidates after close today:",
        weekly_expiry_candidates([], underlying="NIFTY", as_of=date(2026, 7, 14), include_today=False),
    )

    cfg = get_config()
    broker = FlattradeAdapter(cfg)
    await broker.connect()
    selector = ContractSelector(cfg, broker)
    uni = await selector.build_universe(Decimal("24100"))
    print(
        "build_universe:",
        "expiry=", uni.expiry_symbol,
        "n=", len(uni.instruments),
        "atm=", uni.atm_strike,
    )
    await broker.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
