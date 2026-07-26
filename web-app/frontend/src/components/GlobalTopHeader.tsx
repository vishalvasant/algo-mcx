import { Menu, Zap } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { CommoditySnapshot } from "../types";
import { useAuth } from "../auth/AuthContext";

export interface IndexQuote extends CommoditySnapshot {
  change?: number | null;
  changePct?: number | null;
}

interface GlobalTopHeaderProps {
  commodities: IndexQuote[];
  active: string;
  onChange: (underlying: string) => void;
  brokerConnected?: boolean;
  brokerName?: string;
  clock?: string;
  onMenuToggle?: () => void;
  engineControls?: ReactNode;
  hideIndexCards?: boolean;
}

const TRADEABLE = new Set(["GOLD", "SILVER", "NATURALGAS", "CRUDEOIL"]);

function formatPrice(spot: number | null | undefined) {
  if (spot == null) return "—";
  return Number(spot).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatChange(change: number | null | undefined, pct: number | null | undefined) {
  if (change == null || pct == null) return { text: "—", up: true };
  const up = change >= 0;
  const sign = up ? "+" : "";
  return {
    text: `${sign}${change.toFixed(2)} / ${sign}${pct.toFixed(2)}%`,
    up,
  };
}

export function GlobalTopHeader({
  commodities,
  active,
  onChange,
  brokerConnected,
  brokerName = "Flattrade",
  clock,
  onMenuToggle,
  engineControls,
  hideIndexCards = false,
}: GlobalTopHeaderProps) {
  const { username } = useAuth();
  const initials = (username ?? "AF").slice(0, 2).toUpperCase();
  const prevPrices = useRef<Record<string, number>>({});
  const [priceFlash, setPriceFlash] = useState<Record<string, number>>({});

  useEffect(() => {
    const nextFlash: Record<string, number> = {};
    for (const c of commodities) {
      const spot = c.spot_ltp;
      if (spot == null) continue;
      const prev = prevPrices.current[c.underlying];
      if (prev != null && prev !== spot) {
        nextFlash[c.underlying] = Date.now();
      }
      prevPrices.current[c.underlying] = spot;
    }
    if (Object.keys(nextFlash).length === 0) return;
    setPriceFlash((prev) => ({ ...prev, ...nextFlash }));
    const id = window.setTimeout(() => {
      setPriceFlash((prev) => {
        const cleaned = { ...prev };
        for (const key of Object.keys(nextFlash)) delete cleaned[key];
        return cleaned;
      });
    }, 650);
    return () => window.clearTimeout(id);
  }, [commodities]);

  return (
    <header className="global-top-header">
      <div className="global-top-brand">
        {onMenuToggle ? (
          <button
            type="button"
            className="global-menu-btn"
            onClick={onMenuToggle}
            aria-label="Toggle navigation"
          >
            <Menu size={18} />
          </button>
        ) : null}
        <span className="global-top-logo">Algo-MCX</span>
        <span className="global-top-live">
          <Zap size={10} /> LIVE
        </span>
      </div>

      <div className="global-top-center">
        {!hideIndexCards ? (
        <div className="global-top-indices">
        {commodities.map((c) => {
          const isActive = c.underlying === active;
          const tradeable = TRADEABLE.has(c.underlying);
          const isFut = c.card_type === "fut" || c.underlying.endsWith("_FUT");
          const chg = formatChange(c.change, c.changePct);
          return (
            <button
              key={c.underlying}
              type="button"
              className={[
                "global-index-card",
                isActive ? "active" : "",
                tradeable ? "" : "display-only",
                isFut ? "fut-card" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              onClick={() => tradeable && onChange(c.underlying)}
              disabled={!tradeable}
              title={
                isFut && c.fut_tsym
                  ? c.fut_tsym
                  : tradeable
                    ? undefined
                    : "Display only"
              }
            >
              <span className="global-index-name">{c.display_name ?? c.underlying}</span>
              {c.expiry_label ? (
                <span className="global-index-expiry mono">{c.expiry_label}</span>
              ) : null}
              <span
                className={[
                  "global-index-price",
                  priceFlash[c.underlying] != null ? "price-tick" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                {formatPrice(c.spot_ltp)}
              </span>
              <span className={`global-index-chg ${chg.up ? "up" : "down"}`}>{chg.text}</span>
            </button>
          );
        })}
        </div>
        ) : null}
      </div>

      <div className="global-top-meta">
        <span className="global-meta-item">
          <span className="meta-k">Broker</span>
          <span className="meta-v">{brokerName}</span>
          <i className={`broker-dot ${brokerConnected ? "on" : "off"}`} />
        </span>
        <span className="global-meta-divider" />
        <span className="global-meta-item">
          <span className="meta-k">Latency</span>
          <span className="meta-v accent">23ms</span>
        </span>
        <span className="global-meta-divider" />
        {clock ? <span className="global-meta-clock mono">{clock}</span> : null}
        <span className="global-live-pill">Live</span>
        {engineControls}
        <span className="global-avatar" title={username ?? "User"}>
          {initials}
        </span>
      </div>
    </header>
  );
}
