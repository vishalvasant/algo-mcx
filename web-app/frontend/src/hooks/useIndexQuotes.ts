import { useEffect, useMemo, useRef, useState } from "react";
import { fetchChartCandles } from "../api/client";
import type { IndexQuote } from "../components/GlobalTopHeader";
import type { CommoditySnapshot } from "../types";
import { sessionOpenFromBars } from "../utils/chartIndicators";

function buildQuote(c: CommoditySnapshot, sessionOpen: number | null): IndexQuote {
  const spot = c.spot_ltp ?? null;

  if (spot != null && c.change != null && c.change_pct != null) {
    return {
      ...c,
      spot_ltp: spot,
      change: c.change,
      changePct: c.change_pct,
    };
  }

  const open = c.session_open ?? sessionOpen;
  if (open != null && spot != null) {
    const change = spot - open;
    const changePct = open !== 0 ? (change / open) * 100 : null;
    return { ...c, spot_ltp: spot, change, changePct };
  }

  return { ...c, spot_ltp: spot, change: null, changePct: null };
}

export function useIndexQuotes(commodities: CommoditySnapshot[]): IndexQuote[] {
  const [sessionOpens, setSessionOpens] = useState<Record<string, number>>({});
  const fetchedRef = useRef<Set<string>>(new Set());

  // Fallback session open when engine has not yet published session_open on the stream.
  useEffect(() => {
    let cancelled = false;
    for (const c of commodities) {
      if (c.session_open != null || fetchedRef.current.has(c.underlying)) continue;
      fetchedRef.current.add(c.underlying);

      (async () => {
        try {
          const chartUnderlying =
            c.underlying === "GOLD_FUT" ? "GOLD" : c.underlying;
          const res = await fetchChartCandles(chartUnderlying, "1m");
          const open = sessionOpenFromBars(res.bars ?? []);
          if (!cancelled && open != null) {
            setSessionOpens((prev) =>
              prev[c.underlying] === open ? prev : { ...prev, [c.underlying]: open },
            );
          }
        } catch {
          /* wait for SSE to publish session_open */
        }
      })();
    }
    return () => {
      cancelled = true;
    };
  }, [commodities]);

  return useMemo(
    () => commodities.map((c) => buildQuote(c, sessionOpens[c.underlying] ?? null)),
    [commodities, sessionOpens],
  );
}
