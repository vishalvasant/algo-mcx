import type { TradeBlotter } from "../types";
import { StatusBadge } from "./StatusBadge";

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatInr(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
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

interface TradeBlotterPanelProps {
  blotter: TradeBlotter | null;
}

export function TradeBlotterPanel({ blotter }: TradeBlotterPanelProps) {
  const openPositions = blotter?.open_positions ?? [];
  const closedTrades = blotter?.closed_trades ?? [];

  return (
    <div className="card panel trade-blotter-card">
      <div className="panel-head">
        <h3>Trade blotter</h3>
        <StatusBadge
          severity={openPositions.length ? "success" : "neutral"}
          label={openPositions.length ? `${openPositions.length} OPEN` : "FLAT"}
        />
      </div>

      <div className="blotter-grid">
        <section className="blotter-section">
          <p className="blotter-label">Open positions</p>
          {openPositions.length === 0 ? (
            <p className="blotter-empty">No open positions</p>
          ) : (
            <div className="blotter-scroll">
              <table className="watchlist-table pro blotter-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th className="num">Lots</th>
                    <th className="num">Qty</th>
                    <th className="num">Entry</th>
                    <th className="num">LTP</th>
                    <th className="num">Unrealized</th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((p) => (
                    <tr key={`${p.tsym}-${p.entry_ts}`}>
                      <td className="muted ts">{formatTs(p.entry_ts)}</td>
                      <td className="mono symbol">{p.tsym}</td>
                      <td>{p.side}</td>
                      <td className="num">{p.lots ?? "—"}</td>
                      <td className="num">{p.quantity}</td>
                      <td className="mono num">{formatPrice(p.entry_price)}</td>
                      <td className="mono num">{formatPrice(p.current_ltp)}</td>
                      <td
                        className={`mono num ${(p.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}`}
                      >
                        {formatInr(p.unrealized_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="blotter-section">
          <p className="blotter-label">Closed today</p>
          {closedTrades.length === 0 ? (
            <p className="blotter-empty">No closed trades today</p>
          ) : (
            <div className="blotter-scroll">
              <table className="watchlist-table pro blotter-table">
                <thead>
                  <tr>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Symbol</th>
                    <th className="num">Lots</th>
                    <th className="num">Entry</th>
                    <th className="num">Exit LTP</th>
                    <th className="num">P&L</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {closedTrades.map((t) => (
                    <tr key={t.id}>
                      <td className="muted ts">{formatTs(t.entry_ts)}</td>
                      <td className="muted ts">{formatTs(t.exit_ts)}</td>
                      <td className="mono symbol">{t.tsym}</td>
                      <td className="num">
                        {t.lots} × {t.lot_size}
                      </td>
                      <td className="mono num">{formatPrice(t.entry_price)}</td>
                      <td className="mono num">{formatPrice(t.exit_price)}</td>
                      <td className={`mono num ${t.pnl >= 0 ? "positive" : "negative"}`}>
                        {formatInr(t.pnl)}
                      </td>
                      <td className="muted">{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
