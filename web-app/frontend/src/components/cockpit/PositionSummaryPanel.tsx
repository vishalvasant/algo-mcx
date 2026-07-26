import { Briefcase } from "lucide-react";
import { useRef, useState } from "react";
import { exitPosition } from "../../api/client";
import type { WatchlistOpenPosition } from "../../types";
import { StatusBadge } from "../StatusBadge";

function formatInr(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function formatPrice(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function optionTypeFor(position: WatchlistOpenPosition) {
  if (position.side && position.side !== "BUY" && position.side !== "SELL") {
    return position.side;
  }
  if (position.tsym.includes("CE")) return "CE";
  if (position.tsym.includes("PE")) return "PE";
  return position.side ?? "—";
}

function useStablePnlTone(pnl: number) {
  const toneRef = useRef<"positive" | "negative" | "neutral">("neutral");
  if (pnl > 1) toneRef.current = "positive";
  else if (pnl < -1) toneRef.current = "negative";
  return toneRef.current;
}

interface PositionSummaryPanelProps {
  openPositions: WatchlistOpenPosition[];
  onRefresh?: () => void;
}

export function PositionSummaryPanel({ openPositions, onRefresh }: PositionSummaryPanelProps) {
  const [exiting, setExiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primary = openPositions[0] ?? null;
  const liveCount = openPositions.length;
  const hasPosition = primary != null;

  const handleExit = async () => {
    if (!primary?.position_id) {
      setError("Missing position id — refresh and try again");
      return;
    }
    const label = `${primary.tsym} @ ₹${formatPrice(primary.current_ltp ?? primary.entry_price)}`;
    if (!window.confirm(`Exit ${label} at current LTP?`)) return;

    setExiting(true);
    setError(null);
    try {
      await exitPosition(primary.position_id);
      onRefresh?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExiting(false);
    }
  };

  const pnl = primary?.unrealized_pnl ?? 0;
  const entry = primary?.entry_price ?? 0;
  const ltp = primary?.current_ltp ?? entry;
  const deployed = primary?.premium_deployed ?? entry * (primary?.quantity ?? 1);
  const pnlPct = deployed > 0 ? (pnl / deployed) * 100 : 0;
  const optionType = primary ? optionTypeFor(primary) : "—";
  const pnlTone = useStablePnlTone(pnl);

  return (
    <section
      className={`cockpit-panel position-summary-panel${hasPosition ? "" : " position-summary-flat"}`}
    >
      <header className="cockpit-panel-head">
        <Briefcase size={14} />
        <h3>Position Summary</h3>
        <StatusBadge
          severity={hasPosition ? "success" : "neutral"}
          label={hasPosition ? (liveCount > 1 ? `${liveCount} OPEN` : "OPEN") : "FLAT"}
        />
      </header>

      {error && hasPosition ? <p className="position-error">{error}</p> : null}

      {hasPosition && primary ? (
        <>
          <p className="position-symbol mono">{primary.tsym}</p>
          <p className="position-side">
            {optionType} · {primary.lots ?? "—"} lots / {primary.quantity ?? "—"} qty
          </p>
          <p className={`position-pnl ${pnlTone}`}>
            {formatInr(pnl)}
            <span className="position-pnl-pct">
              ({pnlPct >= 0 ? "+" : ""}
              {pnlPct.toFixed(2)}%)
            </span>
          </p>
          <dl className="position-details">
            <div>
              <dt>Entry</dt>
              <dd className="mono tabular-nums">{formatPrice(entry)}</dd>
            </div>
            <div>
              <dt>LTP</dt>
              <dd className="mono accent tabular-nums">{formatPrice(ltp)}</dd>
            </div>
            <div>
              <dt>Setup</dt>
              <dd>{primary.setup_type ?? "—"}</dd>
            </div>
          </dl>
          {liveCount > 1 ? (
            <p className="position-more muted">+{liveCount - 1} more in Open Positions below</p>
          ) : (
            <p className="position-more muted position-more--spacer" aria-hidden>
              &nbsp;
            </p>
          )}
        </>
      ) : (
        <>
          <p className="position-flat-msg">No open positions</p>
          <p className="position-flat-hint muted">
            Live trades appear here when the engine opens a position.
          </p>
          <p className="position-more muted position-more--spacer" aria-hidden>
            &nbsp;
          </p>
        </>
      )}

      <div className={`position-actions${hasPosition ? "" : " position-actions--placeholder"}`}>
        <button type="button" className="btn-modify" disabled={!hasPosition} title="Coming soon">
          Modify SL
        </button>
        <button type="button" className="btn-partial" disabled={!hasPosition} title="Coming soon">
          Book Partial
        </button>
        <button
          type="button"
          className="btn-exit"
          onClick={handleExit}
          disabled={!hasPosition || exiting}
        >
          {exiting ? "Exiting…" : "Exit Trade"}
        </button>
      </div>
    </section>
  );
}
