import type { CommoditySnapshot } from "../types";

/** Top-bar cards — merged with live watchlist/SSE data. */
export const HEADER_COMMODITY_DEFAULTS: CommoditySnapshot[] = [
  { underlying: "GOLD", display_name: "Gold", spot_ltp: null, atm_strike: null },
  {
    underlying: "GOLD_FUT",
    display_name: "GOLD FUT",
    spot_ltp: null,
    atm_strike: null,
    card_type: "fut",
  },
  { underlying: "SILVER", display_name: "Silver", spot_ltp: null, atm_strike: null },
  { underlying: "NATURALGAS", display_name: "Nat Gas", spot_ltp: null, atm_strike: null },
  { underlying: "CRUDEOIL", display_name: "Crude", spot_ltp: null, atm_strike: null },
];

export function mergeHeaderCommodities(apiRows: CommoditySnapshot[]): CommoditySnapshot[] {
  const byKey = new Map(apiRows.map((row) => [row.underlying, row]));
  const merged = HEADER_COMMODITY_DEFAULTS.map((def) => ({
    ...def,
    ...byKey.get(def.underlying),
  }));
  for (const row of apiRows) {
    if (!HEADER_COMMODITY_DEFAULTS.some((d) => d.underlying === row.underlying)) {
      merged.push(row);
    }
  }
  return merged;
}
