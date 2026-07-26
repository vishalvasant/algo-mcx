import type { CommoditySnapshot } from "../types";
import { StatusBadge } from "./StatusBadge";

interface TopIndexHeaderProps {
  commodities: CommoditySnapshot[];
  active: string;
  onChange: (underlying: string) => void;
  brokerConnected?: boolean;
  clock?: string;
  session?: string;
}

export function TopIndexHeader({
  commodities,
  active,
  onChange,
  brokerConnected,
  clock,
  session,
}: TopIndexHeaderProps) {
  return (
    <header className="top-index-header">
      <div className="top-index-cards">
        {commodities.map((c) => {
          const isActive = c.underlying === active;
          return (
            <button
              key={c.underlying}
              type="button"
              className={isActive ? "top-index-card active" : "top-index-card"}
              onClick={() => onChange(c.underlying)}
            >
              <span className="top-index-name">{c.display_name ?? c.underlying}</span>
              <span className="top-index-price mono">
                {c.spot_ltp != null
                  ? Number(c.spot_ltp).toLocaleString("en-IN", { maximumFractionDigits: 2 })
                  : "—"}
              </span>
            </button>
          );
        })}
      </div>

      <div className="top-index-meta">
        {session ? (
          <StatusBadge severity={session === "OPEN" ? "success" : "neutral"} label={session} />
        ) : null}
        <StatusBadge
          severity={brokerConnected ? "success" : "warning"}
          label={brokerConnected ? "Broker LIVE" : "Broker OFF"}
        />
        {clock ? <span className="top-index-clock mono">{clock} IST</span> : null}
      </div>
    </header>
  );
}
