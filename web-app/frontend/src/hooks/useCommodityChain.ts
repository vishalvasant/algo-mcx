import { useEffect, useMemo, useRef } from "react";
import type { CommoditySnapshot, MarketSummary, Watchlist, WatchlistItem } from "../types";

const chainCache = new Map<string, WatchlistItem[]>();

function resolveRawItems(
  selected: CommoditySnapshot | undefined,
  watchlist: Watchlist | null,
): WatchlistItem[] {
  if (!selected) return [];
  const fromCommodity = selected.items ?? [];
  if (fromCommodity.length > 0) return fromCommodity;
  if (selected.underlying === watchlist?.underlying) {
    return watchlist?.items ?? [];
  }
  return [];
}

function strikeCount(items: WatchlistItem[]): number {
  return new Set(items.map((i) => i.strike)).size;
}

function mergeChainItems(base: WatchlistItem[], fresh: WatchlistItem[]): WatchlistItem[] {
  if (!base.length) return fresh;
  if (!fresh.length) return base;
  const byToken = new Map(base.map((row) => [row.token, { ...row }]));
  for (const row of fresh) {
    const prev = byToken.get(row.token);
    if (prev) {
      byToken.set(row.token, { ...prev, ...row });
    } else {
      byToken.set(row.token, row);
    }
  }
  return [...byToken.values()].sort(
    (a, b) => a.strike - b.strike || (a.option_type === "CE" ? -1 : 1),
  );
}

export function useCommodityChain(
  watchlist: Watchlist | null,
  summary: MarketSummary | null | undefined,
  activeCommodity: string,
) {
  const cacheRef = useRef(chainCache);

  const commodities = watchlist?.commodities ?? summary?.commodities ?? [];
  const selected = commodities.find((c) => c.underlying === activeCommodity);

  const rawItems = useMemo(
    () => resolveRawItems(selected, watchlist),
    [selected, watchlist],
  );

  const atmSteps = selected?.atm_strike_steps ?? watchlist?.atm_strike_steps ?? 5;
  const minStrikes = atmSteps * 2 + 1;

  const cached = cacheRef.current.get(activeCommodity) ?? [];
  const cachedStrikes = strikeCount(cached);
  const cachedUsable = cachedStrikes >= minStrikes;

  const freshStrikes = strikeCount(rawItems);

  const items = useMemo(() => {
    if (rawItems.length === 0) {
      return cachedUsable ? cached : [];
    }

    let structure = rawItems;
    if (freshStrikes < minStrikes && cachedUsable) {
      structure = mergeChainItems(cached, rawItems);
    } else if (cachedUsable && freshStrikes >= minStrikes) {
      structure = mergeChainItems(rawItems, cached);
    }

    // Always overlay the latest SSE quotes (LTP/OI/volume) on the stable strike band.
    return mergeChainItems(structure, rawItems);
  }, [rawItems, cached, cachedUsable, freshStrikes, minStrikes]);

  useEffect(() => {
    if (items.length === 0) return;
    const itemStrikes = strikeCount(items);
    const prev = cacheRef.current.get(activeCommodity) ?? [];
    const prevStrikes = strikeCount(prev);
    if (itemStrikes >= minStrikes || itemStrikes >= prevStrikes) {
      cacheRef.current.set(activeCommodity, items);
    }
  }, [activeCommodity, items, minStrikes]);

  const isRefreshing =
    rawItems.length > 0 && freshStrikes < minStrikes && cachedUsable && freshStrikes < cachedStrikes;

  return { selected, commodities, items, isRefreshing };
}
