import { useEffect, useMemo, useRef, useState } from "react";
import type { CommoditySnapshot, MarketSummary, Watchlist, WatchlistItem } from "../types";

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

function formatIv(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return `${Number(value).toFixed(1)}%`;
}

function formatDelta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(3);
}

function formatTheta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(2);
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

interface StrikeRow {
  strike: number;
  isAtm: boolean;
  lotSize: number;
  ce?: WatchlistItem;
  pe?: WatchlistItem;
}

interface WatchlistPanelProps {
  watchlist: Watchlist | null;
  summary?: MarketSummary | null;
  activeCommodity: string;
}

export function WatchlistPanel({ watchlist, summary, activeCommodity }: WatchlistPanelProps) {
  const commodities = watchlist?.commodities ?? summary?.commodities ?? [];
  const selected: CommoditySnapshot | undefined =
    commodities.find((c) => c.underlying === activeCommodity) ?? commodities[0];

  const items = selected?.items ?? (selected?.underlying === watchlist?.underlying ? watchlist?.items : []) ?? [];
  const spot = selected?.spot_ltp ?? null;
  const atm = selected?.atm_strike ?? null;
  const expiry = selected?.expiry_symbol ?? null;
  const feedMode = watchlist?.feed_mode ?? summary?.feed_mode ?? "offline";
  const atmSteps = selected?.atm_strike_steps ?? watchlist?.atm_strike_steps ?? 5;
  const step = selected?.strike_step ?? watchlist?.strike_step ?? 100;
  const displayName = selected?.display_name ?? selected?.underlying ?? activeCommodity;

  const feedLabel =
    feedMode === "websocket"
      ? "WebSocket LIVE"
      : feedMode === "rest"
        ? "REST live"
        : "Offline";

  const strikeRows = useMemo(() => {
    const byStrike = new Map<number, StrikeRow>();
    for (const item of items) {
      let row = byStrike.get(item.strike);
      if (!row) {
        row = {
          strike: item.strike,
          isAtm: item.is_atm,
          lotSize: item.lot_size ?? 1,
          ce: undefined,
          pe: undefined,
        };
        byStrike.set(item.strike, row);
      }
      row.lotSize = item.lot_size ?? row.lotSize;
      row.isAtm = row.isAtm || item.is_atm;
      if (item.option_type === "CE") row.ce = item;
      if (item.option_type === "PE") row.pe = item;
    }
    return [...byStrike.values()].sort((a, b) => a.strike - b.strike);
  }, [items]);

  const prevLtp = useRef<Record<string, number | null>>({});
  const [flashTokens, setFlashTokens] = useState<Record<string, number>>({});

  useEffect(() => {
    const nextFlash: Record<string, number> = {};
    for (const item of items) {
      const prev = prevLtp.current[item.token];
      if (prev != null && item.ltp != null && prev !== item.ltp) {
        nextFlash[item.token] = Date.now();
      }
      prevLtp.current[item.token] = item.ltp;
    }
    if (Object.keys(nextFlash).length > 0) {
      setFlashTokens((cur) => ({ ...cur, ...nextFlash }));
      const timer = window.setTimeout(() => {
        setFlashTokens((cur) => {
          const cut = Date.now() - 600;
          const kept: Record<string, number> = {};
          for (const [k, v] of Object.entries(cur)) {
            if (v >= cut) kept[k] = v;
          }
          return kept;
        });
      }, 700);
      return () => window.clearTimeout(timer);
    }
  }, [items]);

  return (
    <div className="card watchlist-card pro">
      <div className="watchlist-header">
        <div>
          <h3>
            {displayName} Options Chain · {feedLabel}
            {feedMode === "websocket" || feedMode === "rest" ? (
              <span className="live-dot" aria-label="live" />
            ) : null}
          </h3>
          <p className="watchlist-sub">
            Monthly {expiry ?? "—"} · ATM ±{atmSteps} ({step} pt step) · {strikeRows.length} strikes ·{" "}
            {items.length} contracts · lot {strikeRows[0]?.lotSize ?? 1}
            {" · "}
            <span title="Implied vol & Greeks derived from LTP (Black–Scholes)">
              Greeks: BS model
            </span>
          </p>
        </div>
        <div className="watchlist-meta">
          <div>
            <span className="meta-label">Spot</span>
            <span className="meta-value accent">
              {spot != null ? spot.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}
            </span>
          </div>
          <div>
            <span className="meta-label">ATM</span>
            <span className="meta-value">
              {atm != null ? atm.toLocaleString("en-IN") : "—"}
            </span>
          </div>
          <div>
            <span className="meta-label">Strikes</span>
            <span className="meta-value">{strikeRows.length}</span>
          </div>
          <div>
            <span className="meta-label">Last tick</span>
            <span className="meta-value sm">
              {formatTs(watchlist?.last_quote_ts ?? null)}
            </span>
          </div>
        </div>
      </div>

      {strikeRows.length === 0 ? (
        <div className="empty-state pro">
          <p>Loading {displayName} option chain…</p>
          <span>ATM ±{atmSteps} strikes · live feed during MCX session</span>
        </div>
      ) : (
        <div className="watchlist-scroll">
          <table className="watchlist-table pro chain-compact chain-greeks">
            <thead>
              <tr className="chain-group-row">
                <th colSpan={5} className="ce-group">
                  CALLS (CE)
                </th>
                <th className="strike-group">Strike</th>
                <th colSpan={5} className="pe-group">
                  PUTS (PE)
                </th>
              </tr>
              <tr>
                <th className="num">OI</th>
                <th className="num">IV</th>
                <th className="num">Δ</th>
                <th className="num">θ</th>
                <th className="num">LTP</th>
                <th className="num strike-head">Strike</th>
                <th className="num">LTP</th>
                <th className="num">Δ</th>
                <th className="num">θ</th>
                <th className="num">IV</th>
                <th className="num">OI</th>
              </tr>
            </thead>
            <tbody>
              {strikeRows.map((row) => {
                const ceFlash = row.ce && flashTokens[row.ce.token] != null;
                const peFlash = row.pe && flashTokens[row.pe.token] != null;
                return (
                  <tr
                    key={row.strike}
                    className={row.isAtm ? "watchlist-row tradable atm-row" : "watchlist-row"}
                  >
                    <td className="mono num muted">{formatOi(row.ce?.oi)}</td>
                    <td className="mono num muted">{formatIv(row.ce?.iv)}</td>
                    <td className="mono num muted">{formatDelta(row.ce?.delta)}</td>
                    <td className="mono num muted">{formatTheta(row.ce?.theta)}</td>
                    <td
                      className={`mono num ${hasPrice(row.ce?.ltp) ? "ltp" : "muted empty-ltp"}${
                        ceFlash ? " ltp-tick" : ""
                      }`}
                    >
                      {formatPrice(row.ce?.ltp)}
                    </td>
                    <td className="mono num strike-cell">
                      <strong>{row.strike.toLocaleString("en-IN")}</strong>
                      {row.isAtm ? <span className="atm-pill">ATM</span> : null}
                    </td>
                    <td
                      className={`mono num ${hasPrice(row.pe?.ltp) ? "ltp" : "muted empty-ltp"}${
                        peFlash ? " ltp-tick" : ""
                      }`}
                    >
                      {formatPrice(row.pe?.ltp)}
                    </td>
                    <td className="mono num muted">{formatDelta(row.pe?.delta)}</td>
                    <td className="mono num muted">{formatTheta(row.pe?.theta)}</td>
                    <td className="mono num muted">{formatIv(row.pe?.iv)}</td>
                    <td className="mono num muted">{formatOi(row.pe?.oi)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
