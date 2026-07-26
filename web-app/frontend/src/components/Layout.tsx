import { Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { fetchHealth, fetchMarketSummary, fetchWatchlist, openWatchlistStream } from "../api/client";
import type { EngineHealth, MarketSummary, Watchlist } from "../types";
import { MarketStatsPanel } from "./cockpit/MarketStatsPanel";
import { GlobalTopHeader } from "./GlobalTopHeader";
import { CockpitEngineControls } from "./CockpitEngineControls";
import { RefSidebarFooter, RefSidebarNav } from "./RefSidebarNav";
import { AppPageFooter } from "./AppPageFooter";
import { useIndexQuotes } from "../hooks/useIndexQuotes";
import { mergeHeaderCommodities } from "../utils/headerCommodities";

export interface DashboardOutletContext {
  activeCommodity: string;
  setActiveCommodity: (v: string) => void;
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
}

export function useDashboardOutlet() {
  return useOutletContext<DashboardOutletContext>();
}

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
          hour12: true,
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
  const [watchlist, setWatchlist] = useState<Watchlist | null>(null);
  const [activeCommodity, setActiveCommodity] = useState("GOLD");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const isDashboard = location.pathname === "/";

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    const stop = openWatchlistStream(
      (wl) => setWatchlist(wl),
      () => {
        fetchWatchlist().then(setWatchlist).catch(() => undefined);
      },
    );
    const id = setInterval(() => {
      fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    }, 5000);
    return () => {
      stop();
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!watchlist?.commodities?.length) return;
    if (!watchlist.commodities.some((c) => c.underlying === activeCommodity)) {
      setActiveCommodity(watchlist.commodities[0].underlying);
    }
  }, [watchlist?.commodities, activeCommodity]);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [sidebarOpen]);

  const commodities = mergeHeaderCommodities(
    watchlist?.commodities ??
      summary?.commodities ?? [
        { underlying: "GOLD", display_name: "Gold", spot_ltp: null, atm_strike: null },
      ],
  );

  const indexQuotes = useIndexQuotes(commodities);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const refreshDashboardData = useCallback(async () => {
    load();
    try {
      const wl = await fetchWatchlist();
      setWatchlist(wl);
    } catch {
      setWatchlist(null);
    }
  }, [load]);

  const outletContext: DashboardOutletContext = {
    activeCommodity,
    setActiveCommodity,
    watchlist,
    summary,
  };

  return (
    <div className={`app-shell app-shell-dashboard ref-layout${sidebarOpen ? " ref-sidebar-open" : ""}`}>
      <GlobalTopHeader
        commodities={indexQuotes}
        active={activeCommodity}
        onChange={setActiveCommodity}
        brokerConnected={health?.broker_connected}
        clock={ist}
        onMenuToggle={() => setSidebarOpen((open) => !open)}
        hideIndexCards={watchlist?.watchlist_mode === "futures"}
        engineControls={
          <CockpitEngineControls onDataRefresh={refreshDashboardData} onLogout={handleLogout} />
        }
      />

      <div className="ref-body">
        <button
          type="button"
          className="ref-sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
        <aside className={`sidebar ref-sidebar${sidebarOpen ? " is-open" : ""}`}>
          <RefSidebarNav alertCount={summary?.unread_notifications ?? 0} />
          <div className="sidebar-widgets">
            <MarketStatsPanel watchlist={watchlist} summary={summary} activeCommodity={activeCommodity} />
          </div>
          <RefSidebarFooter
            status={health?.status}
            username={username ?? undefined}
            brokerOn={health?.broker_connected}
            onLogout={handleLogout}
          />
        </aside>

        <div className="main ref-main">
          <main className={isDashboard ? "content content-cockpit" : "content content-page"}>
            <Outlet context={outletContext} />
          </main>
          {!isDashboard ? <AppPageFooter summary={summary} health={health} /> : null}
        </div>
      </div>
    </div>
  );
}
