import type { EngineHealth, MarketSummary } from "../types";

function formatInr(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

interface AppPageFooterProps {
  summary: MarketSummary | null;
  health: EngineHealth | null;
}

/** Slim status bar — same metrics strip as dashboard cockpit footer. */
export function AppPageFooter({ summary, health }: AppPageFooterProps) {
  const totalPnl = summary?.today_pnl ?? 0;
  const margin = summary?.used_margin ?? summary?.deployed_capital ?? 0;
  const mtm = summary?.unrealized_pnl ?? 0;
  const tradeCount = summary?.trade_count ?? 0;

  return (
    <footer className="app-page-footer cockpit-footer-summary">
      <div className="cockpit-footer-stat">
        <span>Total P&amp;L</span>
        <strong className={totalPnl >= 0 ? "positive" : "negative"}>{formatInr(totalPnl)}</strong>
      </div>
      <div className="cockpit-footer-stat">
        <span>Tot. Margin</span>
        <strong className="mono">{formatInr(margin)}</strong>
      </div>
      <div className="cockpit-footer-stat">
        <span>MTM</span>
        <strong className={mtm >= 0 ? "positive" : "negative"}>{formatInr(mtm)}</strong>
      </div>
      <div className="cockpit-footer-stat">
        <span>Engine</span>
        <strong>{health?.status ?? "—"}</strong>
      </div>
      <div className="cockpit-footer-stat">
        <span>Total Trades</span>
        <strong>{tradeCount}</strong>
      </div>
    </footer>
  );
}
