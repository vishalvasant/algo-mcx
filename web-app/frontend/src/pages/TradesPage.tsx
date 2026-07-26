import { useEffect, useMemo, useState } from "react";
import { LineChart, RefreshCw } from "lucide-react";
import { fetchMarketSummary, fetchTradesToday } from "../api/client";
import type { MarketSummary, Trade } from "../types";
import { AppPageShell } from "../components/AppPageShell";

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

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatInr(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function pnlClass(pnl: number) {
  if (pnl > 0) return "positive";
  if (pnl < 0) return "negative";
  return "";
}

function holdLabel(seconds: number | null | undefined) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

export function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setBusy(true);
    try {
      const [t, sum]: [Trade[], MarketSummary] = await Promise.all([
        fetchTradesToday(200),
        fetchMarketSummary(),
      ]);
      setTrades(t);
      setSummary(sum);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const stats = useMemo(() => {
    const pnls = trades.map((t) => Number(t.pnl));
    const wins = pnls.filter((p) => p > 0);
    const losses = pnls.filter((p) => p <= 0);
    const total = pnls.reduce((s, p) => s + p, 0);
    const unrealized = Number(summary?.unrealized_pnl ?? 0);
    const starting = Number(summary?.starting_capital ?? 50000);
    const todayPnl = Number(summary?.today_pnl ?? total);
    return {
      total: todayPnl,
      wins: wins.length,
      losses: losses.length,
      winRate: pnls.length ? (wins.length / pnls.length) * 100 : 0,
      unrealized,
      starting,
      equity: Number(summary?.equity ?? starting + todayPnl + unrealized),
    };
  }, [trades, summary]);

  return (
    <AppPageShell
      title="P&L / Today"
      icon={LineChart}
      description="Today's closed trades · live equity (capital carries forward) · older days are on Order Book · auto-refresh 10s"
    >
      <div className="logs-page-full trades-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <LineChart size={14} strokeWidth={2} />
            <h3>Session overview</h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={load}
              disabled={busy}
            >
              <RefreshCw size={13} />
              {busy ? "Loading…" : "Refresh"}
            </button>
          </header>

          <div className="cockpit-command-metrics logs-command-metrics">
            <div className="cmd-metric">
              <span>Today P&amp;L</span>
              <strong className={pnlClass(stats.total)}>{formatInr(stats.total)}</strong>
            </div>
            <div className="cmd-metric">
              <span>Unrealized</span>
              <strong className={pnlClass(stats.unrealized)}>{formatInr(stats.unrealized)}</strong>
            </div>
            <div className="cmd-metric">
              <span>Trades</span>
              <strong>
                {trades.length}{" "}
                <span className="muted orderbook-metric-sub">
                  ({stats.wins}W / {stats.losses}L)
                </span>
              </strong>
            </div>
            <div className="cmd-metric">
              <span>Win rate</span>
              <strong>{trades.length ? `${stats.winRate.toFixed(0)}%` : "—"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Capital → Equity</span>
              <strong className="trades-equity-metric">
                ₹{stats.starting.toLocaleString("en-IN")} → {formatInr(stats.equity).replace("+", "")}
              </strong>
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        <section className="cockpit-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <h3>Closed trades today</h3>
            <span className="logs-range-pill mono muted">{trades.length} closed</span>
          </header>

          {trades.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy
                ? "Loading trades…"
                : "No closed trades today — they will appear here after exits"}
            </p>
          ) : (
            <div className="cockpit-table-scroll cockpit-table-scroll--journal orderbook-trades-scroll">
              <table className="trades-table cockpit-table pro">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Hold</th>
                    <th>Contract</th>
                    <th>Side</th>
                    <th>Setup</th>
                    <th className="num">Lots</th>
                    <th className="num">Entry ₹</th>
                    <th className="num">Exit ₹</th>
                    <th className="num">P&amp;L</th>
                    <th>Exit reason</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => {
                    const pnl = Number(t.pnl);
                    return (
                      <tr key={t.id}>
                        <td className="muted">{trades.length - i}</td>
                        <td className="mono">{formatTs(t.entry_ts)}</td>
                        <td className="mono">{formatTs(t.exit_ts)}</td>
                        <td>{holdLabel(t.hold_seconds)}</td>
                        <td className="mono">{t.tsym ?? "—"}</td>
                        <td>{t.side ?? "—"}</td>
                        <td>{t.setup_type}</td>
                        <td className="num">
                          {t.lots ?? "—"}
                          {t.lot_size ? ` × ${t.lot_size}` : ""}
                        </td>
                        <td className="num mono">{formatPrice(t.entry_price)}</td>
                        <td className="num mono">{formatPrice(t.exit_price)}</td>
                        <td className={`num mono ${pnlClass(pnl)}`}>{formatInr(pnl)}</td>
                        <td className="reason-cell">{t.exit_reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={10} className="orderbook-tfoot-label">
                      Session realized P&amp;L
                    </td>
                    <td className={`num mono ${pnlClass(stats.total)}`}>
                      {formatInr(stats.total)}
                    </td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppPageShell>
  );
}
