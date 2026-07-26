import { useMemo } from "react";
import type { ClosedBlotterTrade, MarketSummary, WatchlistOpenPosition } from "../../types";

function formatInr(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function formatPrice(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatTs(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

function sortClosedTrades(trades: ClosedBlotterTrade[]) {
  return [...trades].sort(
    (a, b) => new Date(b.exit_ts).getTime() - new Date(a.exit_ts).getTime(),
  );
}

interface CockpitBottomBarProps {
  openPositions: WatchlistOpenPosition[];
  closedTrades: ClosedBlotterTrade[];
  summary?: MarketSummary | null;
}

export function CockpitBottomBar({ openPositions, closedTrades, summary }: CockpitBottomBarProps) {
  const journal = useMemo(() => sortClosedTrades(closedTrades), [closedTrades]);

  const realizedFromTrades = journal.reduce((sum, t) => sum + Number(t.pnl ?? 0), 0);
  const mtmFromOpen = openPositions.reduce((sum, p) => sum + Number(p.unrealized_pnl ?? 0), 0);

  const totalPnl = summary?.today_pnl ?? realizedFromTrades;
  const margin = summary?.used_margin ?? summary?.deployed_capital ?? 0;
  const mtm = summary?.unrealized_pnl ?? mtmFromOpen;
  const tradeCount = summary?.trade_count ?? journal.length + openPositions.length;

  return (
    <div className="cockpit-bottom-stack">
      <section className="cockpit-bottom">
        <article className="cockpit-bottom-panel">
          <header className="cockpit-bottom-head">
            <h3>Open Positions</h3>
            <span className="muted">{openPositions.length} active</span>
          </header>
          <div className="cockpit-table-scroll">
            {openPositions.length === 0 ? (
              <p className="blotter-empty">No open positions</p>
            ) : (
              <table className="watchlist-table pro cockpit-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th className="num">Entry</th>
                    <th className="num">SL</th>
                    <th className="num">Target</th>
                    <th className="num">LTP</th>
                    <th className="num">P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((p) => (
                    <tr key={p.position_id ?? `${p.tsym}-${p.entry_ts}`}>
                      <td className="mono symbol">{p.tsym}</td>
                      <td>{p.side ?? "—"}</td>
                      <td className="mono num">{formatPrice(p.entry_price)}</td>
                      <td className="mono num">{formatPrice(p.stop_loss)}</td>
                      <td className="mono num">{formatPrice(p.target_price)}</td>
                      <td className="mono num">{formatPrice(p.current_ltp)}</td>
                      <td className={`mono num ${(p.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}`}>
                        {formatInr(p.unrealized_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </article>

        <article className="cockpit-bottom-panel">
          <header className="cockpit-bottom-head">
            <h3>Trade Journal</h3>
            <span className="muted">{journal.length} today</span>
          </header>
          <div className="cockpit-table-scroll cockpit-table-scroll--journal">
            {journal.length === 0 ? (
              <p className="blotter-empty">No closed trades today</p>
            ) : (
              <table className="watchlist-table pro cockpit-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Type</th>
                    <th className="num">Entry</th>
                    <th className="num">Exit</th>
                    <th className="num">P&amp;L</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {journal.map((t) => (
                    <tr key={t.id}>
                      <td className="muted ts">{formatTs(t.exit_ts ?? t.entry_ts)}</td>
                      <td className="mono symbol">{t.tsym}</td>
                      <td>{t.side ?? "—"}</td>
                      <td className="mono num">{formatPrice(t.entry_price)}</td>
                      <td className="mono num">{formatPrice(t.exit_price)}</td>
                      <td className={`mono num ${t.pnl >= 0 ? "positive" : "negative"}`}>
                        {formatInr(t.pnl)}
                      </td>
                      <td className="muted reason-cell">{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </article>
      </section>

      <footer className="cockpit-footer-summary">
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
          <span>Total Trades</span>
          <strong>{tradeCount}</strong>
        </div>
      </footer>
    </div>
  );
}
