import {
  Activity,
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Briefcase,
  LayoutDashboard,
  LineChart,
  List,
  LogOut,
  ScrollText,
  Settings,
  Shield,
  Sparkles,
  User,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import type { LucideIcon } from "lucide-react";

type NavItem =
  | { to: string; label: string; icon: LucideIcon; end?: boolean; badge?: boolean }
  | { label: string; icon: LucideIcon; badge?: boolean };

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { label: "Market Scanner", icon: Activity },
  { label: "AI Strategies", icon: Sparkles },
  { label: "Watchlist", icon: List },
  { label: "Options Chain", icon: BarChart3 },
  { to: "/holdings", label: "Positions", icon: Briefcase },
  { to: "/order-book", label: "Orders", icon: BookOpen },
  { label: "Risk Manager", icon: Shield },
  { to: "/trades", label: "Analytics", icon: LineChart },
  { to: "/notifications", label: "Alerts", icon: Bell, badge: true },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/settings/flattrade", label: "Flattrade", icon: Settings },
];

interface RefSidebarNavProps {
  alertCount?: number;
}

export function RefSidebarNav({ alertCount = 0 }: RefSidebarNavProps) {
  return (
    <nav className="ref-sidebar-nav">
      {NAV_ITEMS.map((item) => {
        const { label, icon: Icon } = item;
        const showBadge = "badge" in item && item.badge && alertCount > 0;
        const content = (
          <>
            <Icon size={16} strokeWidth={1.75} />
            <span>{label}</span>
            {showBadge ? <span className="nav-badge">{alertCount}</span> : null}
          </>
        );
        if ("to" in item && item.to) {
          return (
            <NavLink key={label} to={item.to} end={"end" in item ? item.end : undefined} className="ref-nav-link">
              {content}
            </NavLink>
          );
        }
        return (
          <span key={label} className="ref-nav-link ref-nav-link--soon" title="Coming soon">
            {content}
          </span>
        );
      })}
    </nav>
  );
}

export function RefSidebarFooter({
  status,
  username,
  brokerOn,
  onLogout,
}: {
  status?: string;
  username?: string;
  brokerOn?: boolean;
  onLogout: () => void;
}) {
  return (
    <div className="ref-sidebar-footer">
      <div className="ref-sidebar-status">
        <Bot size={13} />
        <span>{status ?? "—"}</span>
        {brokerOn ? <span className="sidebar-live-dot" /> : null}
      </div>
      <div className="ref-sidebar-user">
        <User size={12} />
        <span>{username}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onLogout} title="Logout">
          <LogOut size={14} />
        </button>
      </div>
    </div>
  );
}
