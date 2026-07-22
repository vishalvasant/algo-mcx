import { useEffect, useState } from "react";
import { Briefcase, LogOut } from "lucide-react";
import { exitPosition, fetchTradeBlotter } from "../api/client";
import type { WatchlistOpenPosition } from "../types";

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

export function HoldingsPage() {
  const [open, setOpen] = useState<WatchlistOpenPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [exitingId, setExitingId] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const load = async () => {
    try {
      const blotter = await fetchTradeBlotter(50);
      setOpen(blotter.open_positions ?? []);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const totalUnrealized = open.reduce((s, p) => s + Number(p.unrealized_pnl ?? 0), 0);
  const totalDeployed = open.reduce((s, p) => s + Number(p.premium_deployed ?? 0), 0);

  const handleExit = async (p: WatchlistOpenPosition) => {
    if (!p.position_id) {
      setError("Missing position id — refresh and try again");
      return;
    }
    const label = `${p.tsym} @ ₹${formatPrice(p.current_ltp)}`;
    if (!window.confirm(`Exit ${label} at current LTP?`)) return;

    setExitingId(p.position_id);
    setFlash(null);
    try {
      const res = await exitPosition(p.position_id);
      setFlash(
        `Exited ${res.tsym} @ ₹${formatPrice(res.exit_price)} · P&L ${formatInr(res.pnl)}`,
      );
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setExitingId(null);
    }
  };

  return (
    <>
      <div className="page-header">
        <h2>Holdings</h2>
        <p>
          Open positions with live LTP · manual exit squares off at current premium ·
          auto-refresh 3s
        </p>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {flash && (
        <div className="card" style={{ marginBottom: "1rem", padding: "0.85rem 1rem" }}>
          {flash}
        </div>
      )}

      <div className="pnl-stats-grid" style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Open</span>
          <span className="pnl-stat-value">{open.length}</span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Premium deployed</span>
          <span className="pnl-stat-value">
            ₹{totalDeployed.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
          </span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Unrealized P&amp;L</span>
          <span className={`pnl-stat-value ${pnlClass(totalUnrealized)}`}>
            {formatInr(totalUnrealized)}
          </span>
        </div>
      </div>

      <div className="card" style={{ overflowX: "auto" }}>
        <div className="panel-head" style={{ marginBottom: "0.75rem" }}>
          <h3>Open positions</h3>
          <span className="muted">{open.length ? `${open.length} open` : "Flat"}</span>
        </div>
        {open.length === 0 ? (
          <div className="empty-state">
            <Briefcase size={32} strokeWidth={1.5} />
            <p>No open holdings — new entries will show here</p>
          </div>
        ) : (
          <table className="trades-table">
            <thead>
              <tr>
                <th>Entry time</th>
                <th>Contract</th>
                <th>Side</th>
                <th>Setup</th>
                <th className="num">Lots</th>
                <th className="num">Entry ₹</th>
                <th className="num">LTP</th>
                <th className="num">Deployed</th>
                <th className="num">Unrealized</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {open.map((p) => {
                const upnl = Number(p.unrealized_pnl ?? 0);
                const busy = exitingId === p.position_id;
                return (
                  <tr key={p.position_id ?? `${p.tsym}-${p.entry_ts}`}>
                    <td>{formatTs(p.entry_ts)}</td>
                    <td className="mono">{p.tsym}</td>
                    <td>{p.side ?? "—"}</td>
                    <td>{p.setup_type ?? "—"}</td>
                    <td className="num">
                      {p.lots ?? "—"}
                      {p.lot_size ? ` × ${p.lot_size}` : ""}
                    </td>
                    <td className="num mono">{formatPrice(p.entry_price)}</td>
                    <td className="num mono">{formatPrice(p.current_ltp)}</td>
                    <td className="num mono">
                      ₹
                      {Number(p.premium_deployed ?? 0).toLocaleString("en-IN", {
                        maximumFractionDigits: 0,
                      })}
                    </td>
                    <td className={`num mono ${pnlClass(upnl)}`}>{formatInr(upnl)}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        disabled={busy || !p.position_id}
                        onClick={() => handleExit(p)}
                        title="Exit at current LTP"
                      >
                        <LogOut size={14} />
                        {busy ? "Exiting…" : "Exit"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
