import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  Bell,
  BookOpen,
  Briefcase,
  LayoutDashboard,
  LineChart,
  LogOut,
  Radio,
  ScrollText,
  User,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { fetchHealth, fetchMarketSummary } from "../api/client";
import type { EngineHealth, MarketSummary } from "../types";
import { MarketTicker } from "./MarketTicker";
import { StatusBadge } from "./StatusBadge";

const links = [
  { to: "/", label: "Terminal", icon: LayoutDashboard },
  { to: "/holdings", label: "Holdings", icon: Briefcase },
  { to: "/trades", label: "P&L", icon: LineChart },
  { to: "/order-book", label: "Order Book", icon: BookOpen },
  { to: "/logs", label: "Decision Logs", icon: ScrollText },
  { to: "/notifications", label: "Alerts", icon: Bell },
];

function useIstClock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const tick = () => {
      setNow(
        new Date().toLocaleString("en-IN", {
          timeZone: "Asia/Kolkata",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      );
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { username, logout } = useAuth();
  const ist = useIstClock();
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const pageTitle =
    links.find((l) => l.to === location.pathname)?.label ?? "Algo-MCX";

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const sessionTone =
    summary?.market_session === "OPEN"
      ? "success"
      : summary?.market_session === "PRE_MARKET"
        ? "warning"
        : "neutral";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">AM</div>
          <div>
            <h1>Algo-MCX</h1>
            <p>Gold · Silver · Gas</p>
          </div>
        </div>
        <nav className="nav">
          {links.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === "/"}>
              <Icon size={18} />
              <span>{label}</span>
              {to === "/notifications" && (summary?.unread_notifications ?? 0) > 0 && (
                <span className="nav-badge">{summary?.unread_notifications}</span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-status">
            <Activity size={14} />
            <span>{health?.status ?? "—"}</span>
          </div>
          <div className="sidebar-user">
            <User size={12} />
            <span>{username}</span>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            <div className="topbar-title">{pageTitle}</div>
            <div className="topbar-clock mono">{ist} IST</div>
          </div>
          <div className="topbar-meta">
            <StatusBadge severity={sessionTone} label={summary?.market_session ?? "—"} />
            {health?.broker_connected ? (
              <StatusBadge severity="success" label="Broker LIVE" />
            ) : (
              <StatusBadge severity="warning" label="Broker OFF" />
            )}
            <span className="live-pill">
              <Radio size={12} />
              LIVE
            </span>
            <button className="btn btn-ghost btn-sm" onClick={handleLogout} type="button">
              <LogOut size={14} />
              Logout
            </button>
          </div>
        </header>

        <MarketTicker summary={summary} spotLtp={health?.spot_ltp} />

        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
