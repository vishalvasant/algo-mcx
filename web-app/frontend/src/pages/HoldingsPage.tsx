import { useEffect, useState } from "react";
import { Briefcase, LogOut, RefreshCw } from "lucide-react";
import { exitPosition, fetchTradeBlotter } from "../api/client";
import type { WatchlistOpenPosition } from "../types";
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

export function HoldingsPage() {
  const [open, setOpen] = useState<WatchlistOpenPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [exitingId, setExitingId] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setBusy(true);
    try {
      const blotter = await fetchTradeBlotter(50);
      setOpen(blotter.open_positions ?? []);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
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
    <AppPageShell
      title="Positions"
      icon={Briefcase}
      description="Open positions with live LTP · manual exit squares off at current premium · auto-refresh 3s"
    >
      <div className="logs-page-full holdings-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Briefcase size={14} strokeWidth={2} />
            <h3>Book overview</h3>
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

          <div className="cockpit-command-metrics logs-command-metrics holdings-metrics">
            <div className="cmd-metric">
              <span>Open</span>
              <strong>{open.length}</strong>
            </div>
            <div className="cmd-metric">
              <span>Premium deployed</span>
              <strong>
                ₹{totalDeployed.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
              </strong>
            </div>
            <div className="cmd-metric">
              <span>Unrealized P&amp;L</span>
              <strong className={pnlClass(totalUnrealized)}>{formatInr(totalUnrealized)}</strong>
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}
        {flash ? <div className="cockpit-flash-banner">{flash}</div> : null}

        <section className="cockpit-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <h3>Open positions</h3>
            <span className="logs-range-pill mono muted">
              {open.length ? `${open.length} open` : "Flat"}
            </span>
          </header>

          {open.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy
                ? "Loading positions…"
                : "No open holdings — new entries will show here"}
            </p>
          ) : (
            <div className="cockpit-table-scroll cockpit-table-scroll--journal orderbook-trades-scroll">
              <table className="trades-table cockpit-table pro">
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
                    const busyExit = exitingId === p.position_id;
                    return (
                      <tr key={p.position_id ?? `${p.tsym}-${p.entry_ts}`}>
                        <td className="mono">{formatTs(p.entry_ts)}</td>
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
                            disabled={busyExit || !p.position_id}
                            onClick={() => handleExit(p)}
                            title="Exit at current LTP"
                          >
                            <LogOut size={14} />
                            {busyExit ? "Exiting…" : "Exit"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppPageShell>
  );
}
