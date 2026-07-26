import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketSummary, Watchlist, WatchlistItem } from "../types";
import { useCommodityChain } from "../hooks/useCommodityChain";
import { IndexCandleChart } from "./IndexCandleChart";

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function hasPrice(value: number | null | undefined): boolean {
  return value !== null && value !== undefined && !Number.isNaN(Number(value));
}

function formatOi(value: number | null | undefined) {
  if (value === null || value === undefined) return "–";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString("en-IN");
}

function formatVolume(value: number | null | undefined) {
  if (value === null || value === undefined) return "–";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString("en-IN");
}

function formatTs(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

interface WatchlistPanelProps {
  watchlist: Watchlist | null;
  summary?: MarketSummary | null;
  activeCommodity: string;
  chainOnly?: boolean;
  selectedToken?: string | null;
  onSelectItem?: (item: WatchlistItem) => void;
}

export function WatchlistPanel({
  watchlist,
  summary,
  activeCommodity,
  chainOnly = false,
  selectedToken = null,
  onSelectItem,
}: WatchlistPanelProps) {
  const { selected, items: optionItems, isRefreshing } = useCommodityChain(watchlist, summary, activeCommodity);
  const isFuturesWatchlist = watchlist?.watchlist_mode === "futures";
  const items = isFuturesWatchlist ? (watchlist?.items ?? []) : optionItems;

  const spot = selected?.trading_spot_ltp ?? selected?.spot_ltp ?? null;
  const feedMode = watchlist?.feed_mode ?? summary?.feed_mode ?? "offline";
  const displayName = selected?.display_name ?? selected?.underlying ?? activeCommodity;

  const feedLabel =
    feedMode === "websocket"
      ? "WebSocket LIVE"
      : feedMode === "rest"
        ? "REST live"
        : "Offline";

  const groupedFutures = useMemo(() => {
    const groups = new Map<string, WatchlistItem[]>();
    const order = new Map<string, number>();
    for (const row of items) {
      const key = row.segment_group ?? "MCX";
      const bucket = groups.get(key) ?? [];
      bucket.push(row);
      groups.set(key, bucket);
      const go = row.group_order;
      if (go != null && !order.has(key)) order.set(key, go);
    }
    return [...groups.entries()].sort(
      (a, b) => (order.get(a[0]) ?? 99) - (order.get(b[0]) ?? 99),
    );
  }, [items]);

  const prevLtp = useRef<Record<string, number>>({});
  const [ltpFlash, setLtpFlash] = useState<Record<string, { dir: "up" | "down"; at: number }>>({});

  useEffect(() => {
    prevLtp.current = {};
    setLtpFlash({});
  }, [activeCommodity, isFuturesWatchlist]);

  useEffect(() => {
    const next: Record<string, { dir: "up" | "down"; at: number }> = {};
    const now = Date.now();
    for (const item of items) {
      if (item.ltp == null || Number.isNaN(Number(item.ltp))) continue;
      const price = Number(item.ltp);
      const prev = prevLtp.current[item.token];
      if (prev != null && price !== prev) {
        next[item.token] = { dir: price > prev ? "up" : "down", at: now };
      }
      prevLtp.current[item.token] = price;
    }
    if (Object.keys(next).length === 0) return;
    setLtpFlash((cur) => ({ ...cur, ...next }));
    const timer = window.setTimeout(() => {
      setLtpFlash((cur) => {
        const cut = Date.now() - 650;
        const kept: Record<string, { dir: "up" | "down"; at: number }> = {};
        for (const [k, v] of Object.entries(cur)) {
          if (v.at >= cut) kept[k] = v;
        }
        return kept;
      });
    }, 700);
    return () => window.clearTimeout(timer);
  }, [items]);

  const ltpCellClass = (token: string | undefined, ltp: number | null | undefined) => {
    const base = hasPrice(ltp) ? "ltp" : "muted empty-ltp";
    if (!token) return base;
    const flash = ltpFlash[token];
    if (!flash) return base;
    return `${base} ltp-tick ltp-tick-${flash.dir}`;
  };

  return (
    <div className="card watchlist-card pro">
      <div className={`watchlist-header watchlist-header--slim${chainOnly ? " watchlist-header--ref" : ""}`}>
        <div>
          <h3>
            Watchlist
            <span className="watchlist-feed-tag">{feedLabel}</span>
            {feedMode === "websocket" || feedMode === "rest" ? (
              <span className="live-dot" aria-label="live" />
            ) : null}
          </h3>
          {!chainOnly ? (
            <p className="watchlist-sub">
              MCX futures · bullion · energy · base metals
            </p>
          ) : null}
        </div>
        {!chainOnly ? (
          <div className="watchlist-meta watchlist-meta--compact">
            <div>
              <span className="meta-label">{displayName}</span>
              <span className="meta-value accent">
                {spot != null ? spot.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}
              </span>
            </div>
            <div>
              <span className="meta-label">Tick</span>
              <span className="meta-value sm">{formatTs(watchlist?.last_quote_ts ?? null)}</span>
            </div>
          </div>
        ) : null}
      </div>

      {items.length === 0 ? (
        <div className="empty-state pro">
          <p>Loading watchlist…</p>
          <span>Resolving nearest MCX FUTCOM contracts via Flattrade</span>
        </div>
      ) : (
        <div className={`watchlist-scroll${isRefreshing ? " watchlist-scroll--stale" : ""}`}>
          <table className="watchlist-table pro mcx-futures-watchlist">
            <thead>
              <tr>
                <th>Group</th>
                <th>Segment</th>
                <th>Symbol</th>
                <th className="num">Expiry</th>
                <th className="num">LTP</th>
                <th className="num">OI</th>
                <th className="num">Vol</th>
                <th className="num">Lot</th>
              </tr>
            </thead>
            <tbody>
              {groupedFutures.map(([group, rows]) =>
                rows.map((row, idx) => {
                  const isSelected = selectedToken != null && row.token === selectedToken;
                  return (
                  <tr
                    key={row.token || row.tsym}
                    className={`watchlist-row${isSelected ? " is-selected" : ""}`}
                    onClick={() => onSelectItem?.(row)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelectItem?.(row);
                      }
                    }}
                  >
                    <td className="symbol">{idx === 0 ? group : ""}</td>
                    <td className="symbol">{row.segment_label ?? row.segment_key ?? "—"}</td>
                    <td className="mono symbol">{row.tsym}</td>
                    <td className="mono num muted">{row.expiry_label ?? "—"}</td>
                    <td className={`mono num ${ltpCellClass(row.token, row.ltp)}`}>{formatPrice(row.ltp)}</td>
                    <td className="mono num muted">{formatOi(row.oi)}</td>
                    <td className="mono num muted">{formatVolume(row.volume)}</td>
                    <td className="mono num muted">{row.lot_size ?? "—"}</td>
                  </tr>
                  );
                }),
              )}
            </tbody>
          </table>
        </div>
      )}

      {!chainOnly ? (
        <IndexCandleChart
          underlying={activeCommodity}
          displayName={displayName}
          liveSpot={spot}
          watchlist={watchlist ?? null}
          feedMode={feedMode}
        />
      ) : null}
    </div>
  );
}
