import { useCallback, useEffect, useState } from "react";
import { Bell, RefreshCw } from "lucide-react";
import { fetchNotifications, markNotificationRead } from "../api/client";
import type { Notification } from "../types";
import { StatusBadge } from "../components/StatusBadge";
import { AppPageShell } from "../components/AppPageShell";

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

function severityClass(severity: string): string {
  if (severity === "critical" || severity === "error") return "sev-critical";
  if (severity === "warning") return "sev-warning";
  if (severity === "success") return "sev-success";
  return "";
}

export function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchNotifications(50)
      .then(setItems)
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
  }, [load]);

  const markRead = async (id: string) => {
    await markNotificationRead(id);
    load();
  };

  const unread = items.filter((n) => !n.read).length;

  return (
    <AppPageShell
      title="Alerts"
      icon={Bell}
      description={
        unread > 0
          ? `Important trading alerts — entries, exits, and critical system events · ${unread} unread`
          : "Important trading alerts — entries, exits, and critical system events"
      }
    >
      <div className="logs-page-full notifications-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Bell size={14} strokeWidth={2} />
            <h3>Overview</h3>
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
              <span>Unread</span>
              <strong>{unread}</strong>
            </div>
            <div className="cmd-metric">
              <span>Total loaded</span>
              <strong>{items.length}</strong>
            </div>
            <div className="cmd-metric">
              <span>Auto-refresh</span>
              <strong>8s</strong>
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        <section className="cockpit-panel logs-feed-panel notifications-feed-panel">
          <header className="cockpit-panel-head">
            <Bell size={14} strokeWidth={2} />
            <h3>Alert stream</h3>
            <span className="logs-range-pill mono muted">
              {unread ? `${unread} unread` : "All read"}
            </span>
          </header>

          {items.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy ? "Loading alerts…" : "No notifications yet"}
            </p>
          ) : (
            <ul className="notification-list notification-list--page">
              {items.map((n) => (
                <li
                  key={n.id}
                  className={`notification-item notification-feed-item ${n.read ? "" : "unread"} ${severityClass(n.severity)}`.trim()}
                  onClick={() => !n.read && markRead(n.id)}
                  onKeyDown={(e) => {
                    if (!n.read && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      markRead(n.id);
                    }
                  }}
                  role={n.read ? undefined : "button"}
                  tabIndex={n.read ? undefined : 0}
                >
                  <StatusBadge severity={n.severity} />
                  <div className="body">
                    <div className="title">{n.title}</div>
                    <div className="message">{n.message}</div>
                  </div>
                  <div className="time mono">{formatTs(n.ts)}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </AppPageShell>
  );
}
