import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Filter,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import { fetchDecisionLogs } from "../api/client";
import type { DecisionLogEvent } from "../types";
import { AppPageShell } from "../components/AppPageShell";
import { formatHumanDecision } from "../utils/decisionLogFormat";

const PAGE_SIZE = 25;

const FILTERS = [
  { id: "all", label: "All" },
  { id: "strategy_decision", label: "Decisions" },
  { id: "entry_skipped", label: "Skipped" },
  { id: "manual_sync", label: "Sync" },
] as const;

function formatTs(ts: string) {
  try {
    const d = new Date(ts);
    const date = d.toLocaleDateString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
    });
    const time = d.toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    return `${date} · ${time}`;
  } catch {
    return ts;
  }
}

function feedSeverityClass(ev: DecisionLogEvent): string {
  if (ev.event_type === "entry_skipped") return "sev-warning";
  if (ev.metadata?.trade_allowed === true) return "sev-success";
  if (ev.severity === "critical") return "sev-critical";
  if (ev.severity === "warning") return "sev-warning";
  if (ev.severity === "success") return "sev-success";
  return "";
}

export function DecisionLogsPage() {
  const [events, setEvents] = useState<DecisionLogEvent[]>([]);
  const [intervalSec, setIntervalSec] = useState(10);
  const [todayCount, setTodayCount] = useState(0);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const offset = (page - 1) * PAGE_SIZE;

  const rangeLabel = useMemo(() => {
    if (total === 0) return "0 records";
    const from = offset + 1;
    const to = Math.min(offset + events.length, total);
    return `${from}–${to} of ${total}`;
  }, [events.length, offset, total]);

  const load = useCallback(() => {
    setBusy(true);
    const type = filter === "all" ? undefined : filter;
    fetchDecisionLogs({ limit: PAGE_SIZE, offset, eventType: type })
      .then((res) => {
        setEvents(res.events);
        setIntervalSec(res.scan_interval_seconds);
        setTodayCount(res.decisions_today);
        setTotal(res.total);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, [filter, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const goPrev = () => setPage((p) => Math.max(1, p - 1));
  const goNext = () => setPage((p) => Math.min(totalPages, p + 1));

  const setFilterAndReset = (id: string) => {
    setFilter(id);
    setPage(1);
  };

  return (
    <AppPageShell
      title="Decision Logs"
      icon={ScrollText}
      description="Human-readable audit of every scan, entry skip, and sync while the engine is running."
    >
      <div className="logs-page-full">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Filter size={14} strokeWidth={2} />
            <h3>Overview</h3>
            <button
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={load}
              disabled={busy}
              type="button"
            >
              <RefreshCw size={13} />
              {busy ? "Loading…" : "Refresh"}
            </button>
          </header>

          <div className="cockpit-command-metrics logs-command-metrics">
            <div className="cmd-metric">
              <span>Scan interval</span>
              <strong>{intervalSec}s</strong>
            </div>
            <div className="cmd-metric">
              <span>Decisions today</span>
              <strong>{todayCount}</strong>
            </div>
            <div className="cmd-metric">
              <span>Matching filter</span>
              <strong>{total}</strong>
            </div>
            <div className="cmd-metric">
              <span>Page</span>
              <strong>
                {page} / {totalPages}
              </strong>
            </div>
          </div>

          <div className="logs-filter-row">
            <span className="logs-filter-label">Filter</span>
            <div className="chart-interval-tabs logs-filter-tabs" role="tablist" aria-label="Log type">
              {FILTERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  role="tab"
                  aria-selected={filter === f.id}
                  className={`chart-interval ${filter === f.id ? "active" : ""}`}
                  onClick={() => setFilterAndReset(f.id)}
                  disabled={busy}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        <section className="cockpit-panel logs-feed-panel decision-feed-panel">
          <header className="cockpit-panel-head">
            <ScrollText size={14} strokeWidth={2} />
            <h3>Event stream</h3>
            <span className="logs-range-pill mono muted">{rangeLabel}</span>
          </header>

          {events.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy
                ? "Loading events…"
                : "No logs yet. They appear once the market is open and the scanner runs."}
            </p>
          ) : (
            <ul className="decision-feed-list decision-feed-list--page">
              {events.map((ev) => {
                const human = formatHumanDecision(ev);
                return (
                  <li
                    key={ev.id}
                    className={`decision-feed-item ${feedSeverityClass(ev)}`.trim()}
                  >
                    <span className="decision-feed-time mono">{formatTs(ev.ts)}</span>
                    <strong className="decision-feed-title">{human.title}</strong>
                    <span className="decision-feed-msg">{human.summary}</span>
                    {human.detail ? (
                      <span className="decision-feed-detail muted">{human.detail}</span>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}

          <nav className="logs-pagination logs-pagination--inline" aria-label="Decision log pages">
            <span className="logs-pagination-range muted">{rangeLabel}</span>
            <div className="logs-pagination-actions">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={goPrev}
                disabled={busy || page <= 1}
              >
                <ChevronLeft size={14} />
                Prev
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={goNext}
                disabled={busy || page >= totalPages}
              >
                Next
                <ChevronRight size={14} />
              </button>
            </div>
          </nav>
        </section>
      </div>
    </AppPageShell>
  );
}
