import { useCallback, useEffect, useState } from "react";
import { Filter, RefreshCw, ScrollText } from "lucide-react";
import { fetchDecisionLogs } from "../api/client";
import type { DecisionLogEvent } from "../types";
import { StatusBadge } from "../components/StatusBadge";

function formatTs(ts: string) {
  try {
    return new Date(ts).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      day: "2-digit",
      month: "short",
    });
  } catch {
    return ts;
  }
}

function metaText(ev: DecisionLogEvent): string {
  const m = ev.metadata || {};
  if (ev.event_type === "strategy_decision") {
    const strat = String(m.selected_strategy ?? "—");
    const conf = m.confidence != null ? `conf ${m.confidence}` : "";
    const regime =
      m.regime && typeof m.regime === "object"
        ? `regime ${(m.regime as { primary?: string }).primary ?? "—"}`
        : "";
    const side = m.position_side ? `side ${m.position_side}` : "";
    const reason = ev.message && !ev.message.startsWith("selected")
      ? ev.message.slice(0, 120)
      : "";
    return [strat, conf, regime, side, reason].filter(Boolean).join(" · ");
  }
  if (ev.event_type === "entry_skipped") {
    return `${m.setup ?? ""} ${m.side ?? ""} ${m.tsym ?? ""} · ${m.block_reason ?? "blocked"}`;
  }
  if (ev.event_type === "manual_sync") {
    const u = m.universe as { action?: string; after?: number } | undefined;
    return `universe ${u?.action ?? "—"} (${u?.after ?? "?"} ctr)`;
  }
  return ev.message;
}

export function DecisionLogsPage() {
  const [events, setEvents] = useState<DecisionLogEvent[]>([]);
  const [intervalSec, setIntervalSec] = useState(10);
  const [todayCount, setTodayCount] = useState(0);
  const [filter, setFilter] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    const type = filter === "all" ? undefined : filter;
    fetchDecisionLogs(150, type)
      .then((res) => {
        setEvents(res.events);
        setIntervalSec(res.scan_interval_seconds);
        setTodayCount(res.decisions_today);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, [filter]);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="logs-page">
      <div className="card panel logs-hero">
        <div className="panel-head">
          <h3>
            <ScrollText size={16} />
            Decision Logs
          </h3>
          <button className="btn btn-ghost btn-sm" onClick={load} disabled={busy}>
            <RefreshCw size={14} />
            {busy ? "Loading…" : "Refresh"}
          </button>
        </div>
        <p className="panel-copy">
          While the market session is open, the engine scans for entries every{" "}
          <strong>{intervalSec}s</strong>. Each scan writes a decision here (NO_TRADE or
          selected strategy + confidence). Entry skips and manual syncs are included.
        </p>
        <div className="metric-grid logs-metrics">
          <div className="metric">
            <span>Scan interval</span>
            <strong>{intervalSec}s</strong>
          </div>
          <div className="metric">
            <span>Decisions today</span>
            <strong>{todayCount}</strong>
          </div>
          <div className="metric">
            <span>Showing</span>
            <strong>{events.length}</strong>
          </div>
        </div>
        <div className="logs-filter">
          <Filter size={14} />
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">All events</option>
            <option value="strategy_decision">Strategy decisions</option>
            <option value="entry_skipped">Entry skipped</option>
            <option value="manual_sync">Manual sync</option>
          </select>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card panel">
        {events.length === 0 ? (
          <p className="panel-copy muted">
            No logs yet. They appear once the market is open and the scanner runs (every{" "}
            {intervalSec}s), or after you run a manual Flattrade sync.
          </p>
        ) : (
          <div className="watchlist-scroll logs-table-wrap">
            <table className="watchlist-table pro blotter-table">
              <thead>
                <tr>
                  <th>Time (IST)</th>
                  <th>Type</th>
                  <th>Summary</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id}>
                    <td className="muted ts">{formatTs(ev.ts)}</td>
                    <td>
                      <StatusBadge
                        severity={
                          ev.event_type === "entry_skipped"
                            ? "warning"
                            : ev.metadata?.trade_allowed
                              ? "success"
                              : "neutral"
                        }
                        label={ev.event_type.replace(/_/g, " ").toUpperCase()}
                      />
                    </td>
                    <td className="mono">{metaText(ev)}</td>
                    <td className="muted logs-detail" title={ev.message}>
                      {ev.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
