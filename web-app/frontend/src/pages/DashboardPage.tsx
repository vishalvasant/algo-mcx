import { useCallback, useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { fetchHealth, fetchMarketSummary, fetchTradeBlotter, fetchTradesToday } from "../api/client";
import type { ClosedBlotterTrade, WatchlistItem } from "../types";
import { IndexCandleChart, type ChartInterval } from "../components/IndexCandleChart";
import { useDashboardOutlet } from "../components/Layout";
import { COCKPIT_REFRESH_EVENT } from "../components/CockpitEngineControls";
import { WatchlistPanel } from "../components/WatchlistPanel";
import { CockpitBottomBar } from "../components/cockpit/CockpitBottomBar";
import { DecisionLogFeed } from "../components/cockpit/DecisionLogFeed";
import { PositionSummaryPanel } from "../components/cockpit/PositionSummaryPanel";
import { RiskManagerPanel } from "../components/cockpit/RiskManagerPanel";
import { StrategyInsightPanel } from "../components/cockpit/StrategyInsightPanel";
import { useStableOpenPositions } from "../hooks/useStableOpenPositions";
import { useResponsiveChartHeight } from "../hooks/useResponsiveChartHeight";
import { sortClosedTrades, tradeToClosedBlotter } from "../utils/tradeBlotter";

export function DashboardPage() {
  const { activeCommodity, watchlist, summary } = useDashboardOutlet();
  const [chartInterval, setChartInterval] = useState<ChartInterval>("5m");
  const [closedTrades, setClosedTrades] = useState<ClosedBlotterTrade[]>([]);
  const [error, setError] = useState<string | null>(null);

  const commodities =
    watchlist?.commodities ?? summary?.commodities ?? [
      { underlying: "GOLD", display_name: "Gold", spot_ltp: null, atm_strike: null },
    ];

  const selectedCommodity =
    commodities.find((c) => c.underlying === activeCommodity) ?? commodities[0];
  const commodityLabel = selectedCommodity?.display_name ?? selectedCommodity?.underlying ?? "Index";
  const commoditySpot = selectedCommodity?.spot_ltp ?? null;

  const openPositions = useStableOpenPositions(watchlist, summary);
  const chartHeight = useResponsiveChartHeight();
  const isFuturesWatchlist = watchlist?.watchlist_mode === "futures";
  const [selectedWatchItem, setSelectedWatchItem] = useState<WatchlistItem | null>(null);

  useEffect(() => {
    if (!isFuturesWatchlist || !watchlist?.items?.length) return;
    setSelectedWatchItem((prev) => {
      if (prev && watchlist.items.some((item) => item.token === prev.token)) {
        const updated = watchlist.items.find((item) => item.token === prev.token);
        return updated ?? prev;
      }
      return watchlist.items[0];
    });
  }, [isFuturesWatchlist, watchlist?.items]);

  const chartUnderlying =
    selectedWatchItem?.segment_key ?? activeCommodity;
  const chartLabel =
    selectedWatchItem?.segment_label ?? selectedWatchItem?.tsym ?? commodityLabel;
  const chartSpot =
    isFuturesWatchlist && selectedWatchItem
      ? selectedWatchItem.ltp ?? null
      : commoditySpot;

  const load = useCallback(() => {
    fetchHealth()
      .then((h) => setError(h.error ?? null))
      .catch((e) => setError(String(e)));
    fetchMarketSummary().catch(() => undefined);

    Promise.all([fetchTradesToday(500), fetchTradeBlotter(500).catch(() => null)])
      .then(([today, blotterRes]) => {
        const rows =
          today.length > 0
            ? today.map(tradeToClosedBlotter)
            : (blotterRes?.closed_trades ?? []);
        setClosedTrades(sortClosedTrades(rows));
      })
      .catch(() => setClosedTrades([]));
  }, []);

  useEffect(() => {
    load();
    const intervalMs = openPositions.length ? 2000 : 5000;
    const healthId = setInterval(load, intervalMs);
    const onRefresh = () => load();
    window.addEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    return () => {
      clearInterval(healthId);
      window.removeEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    };
  }, [load, openPositions.length]);

  const feedMode = summary?.feed_mode ?? watchlist?.feed_mode ?? "offline";

  return (
    <div className="terminal-cockpit">
      {error ? (
        <div className="error-banner">
          <AlertTriangle size={16} />
          {error}
        </div>
      ) : null}

      <section className="cockpit-main cockpit-main-ref">
        <main className="cockpit-col cockpit-col-center">
          <IndexCandleChart
            underlying={chartUnderlying}
            displayName={chartLabel}
            liveSpot={chartSpot}
            watchlist={watchlist}
            feedMode={watchlist?.feed_mode ?? feedMode}
            height={chartHeight}
            compact
            interval={chartInterval}
            onIntervalChange={setChartInterval}
            contractToken={isFuturesWatchlist ? selectedWatchItem?.token : null}
            contractExchange="MCX"
            contractTsym={selectedWatchItem?.tsym ?? null}
            historyDays={isFuturesWatchlist ? 7 : undefined}
          />
          <WatchlistPanel
            watchlist={watchlist}
            summary={summary}
            activeCommodity={activeCommodity}
            chainOnly
            selectedToken={selectedWatchItem?.token}
            onSelectItem={setSelectedWatchItem}
          />
        </main>

        <aside className="cockpit-col cockpit-col-right">
          <StrategyInsightPanel
            summary={summary}
            activeUnderlying={activeCommodity}
            commodities={commodities}
          />
          <PositionSummaryPanel openPositions={openPositions} onRefresh={load} />
          <RiskManagerPanel summary={summary} />
          <DecisionLogFeed />
        </aside>
      </section>

      <CockpitBottomBar
        openPositions={openPositions}
        closedTrades={closedTrades}
        summary={summary}
      />
    </div>
  );
}
