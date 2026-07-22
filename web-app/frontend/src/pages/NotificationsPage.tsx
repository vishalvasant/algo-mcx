import { useCallback, useEffect, useState } from "react";
import { BellOff } from "lucide-react";
import { fetchNotifications, markNotificationRead } from "../api/client";
import type { Notification } from "../types";
import { StatusBadge } from "../components/StatusBadge";

function formatTs(ts: string) {
  return new Date(ts).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
}

export function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchNotifications(50)
      .then(setItems)
      .catch((e) => setError(String(e)));
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
    <>
      <div className="page-header">
        <h2>Alerts</h2>
        <p>
          Important trading alerts only — entries, exits, and critical system events
          {unread > 0 && ` · ${unread} unread`}
        </p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {items.length === 0 ? (
          <div className="empty-state">
            <BellOff size={32} strokeWidth={1.5} />
            <p>No notifications yet</p>
          </div>
        ) : (
          <ul className="notification-list">
            {items.map((n) => (
              <li
                key={n.id}
                className={`notification-item ${n.read ? "" : "unread"}`}
                onClick={() => !n.read && markRead(n.id)}
                style={{ cursor: n.read ? "default" : "pointer" }}
              >
                <StatusBadge severity={n.severity} />
                <div className="body">
                  <div className="title">{n.title}</div>
                  <div className="message">{n.message}</div>
                </div>
                <div className="time">{formatTs(n.ts)}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
