import { Bot, Database, KeyRound, LogOut, Power, RefreshCw, Radio } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  fetchHealth,
  fetchMarketSummary,
  reauthenticate,
  setAutoTrade,
  setKillSwitch,
  setTradingMode,
  syncMissingData,
} from "../api/client";
import type { EngineHealth, MarketSummary } from "../types";

export const COCKPIT_REFRESH_EVENT = "algomcx:cockpit-refresh";

interface CockpitEngineControlsProps {
  onDataRefresh?: () => void | Promise<void>;
  onLogout?: () => void | Promise<void>;
}

export function CockpitEngineControls({ onDataRefresh, onLogout }: CockpitEngineControlsProps) {
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [autoBusy, setAutoBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);

  const executionMode = summary?.trading_mode ?? health?.trading_mode ?? "paper";
  const isLiveMode = executionMode === "live";

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  const toggleKill = async () => {
    if (!health) return;
    const enabling = !health.kill_switch;
    if (enabling && !confirm("Enable kill switch? This blocks all new trades.")) return;
    setKillBusy(true);
    try {
      await setKillSwitch(enabling);
      load();
    } finally {
      setKillBusy(false);
    }
  };

  const toggleAutoTrade = async () => {
    const currentlyOn = summary?.auto_trade_enabled !== false && !summary?.entries_blocked;
    const enabling = !currentlyOn;
    if (!enabling && !confirm("Turn OFF auto trading?")) return;
    setAutoBusy(true);
    try {
      await setAutoTrade(enabling);
      load();
    } finally {
      setAutoBusy(false);
    }
  };

  const handleSync = async () => {
    setSyncBusy(true);
    try {
      const report = await syncMissingData();
      if (!report.ok) {
        const msg =
          "message" in report && typeof report.message === "string"
            ? report.message
            : "Sync failed — check broker connection.";
        alert(msg);
        return;
      }
      const uni = report.universe as { action?: string; after?: number };
      const candles = report.candles as { action?: string; m1_added?: number };
      const quotes = report.quotes as {
        action?: string;
        polled?: number;
        missing_ltp_after?: number;
      };
      const parts = [
        `Universe: ${uni.action ?? "—"} (${uni.after ?? 0} instruments)`,
        `Candles: ${candles.action ?? "—"}${candles.m1_added ? ` (+${candles.m1_added} bars)` : ""}`,
        `Quotes: ${quotes.action ?? "—"}${quotes.polled ? ` (${quotes.polled} polled)` : ""}`,
      ];
      if ((quotes.missing_ltp_after ?? 0) > 0) {
        parts.push(`${quotes.missing_ltp_after} symbols still missing LTP`);
      }
      alert(`Sync complete\n\n${parts.join("\n")}`);
      await onDataRefresh?.();
      load();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncBusy(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshBusy(true);
    try {
      await onDataRefresh?.();
      window.dispatchEvent(new CustomEvent(COCKPIT_REFRESH_EVENT));
      load();
    } finally {
      setRefreshBusy(false);
    }
  };

  const handleReauth = async () => {
    if (
      !confirm(
        "Re-authenticate with Flattrade using saved credentials?\n\nNo need to re-enter details unless you changed them in Settings → Flattrade.",
      )
    ) {
      return;
    }
    setReauthBusy(true);
    try {
      const result = await reauthenticate(true);
      await onDataRefresh?.();
      window.dispatchEvent(new CustomEvent(COCKPIT_REFRESH_EVENT));
      load();
      const expiry = result.expires_at
        ? new Date(result.expires_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })
        : "—";
      alert(
        `Flattrade connected\n\nUser: ${result.user_id}\nSession valid until: ${expiry}`,
      );
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setReauthBusy(false);
    }
  };

  const toggleExecutionMode = async () => {
    const nextMode = isLiveMode ? "paper" : "live";
    if (nextMode === "live") {
      const ok = window.confirm(
        "Switch to LIVE trading?\n\nReal broker orders will be placed for new entries and exits. Ensure Flattrade session and margin are ready.",
      );
      if (!ok) return;
    } else if (
      !window.confirm("Switch to PAPER trading?\n\nOrders will be simulated using the paper ledger.")
    ) {
      return;
    }
    setModeBusy(true);
    try {
      await setTradingMode(nextMode);
      load();
      await onDataRefresh?.();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setModeBusy(false);
    }
  };

  const handleLogout = async () => {
    if (!onLogout) return;
    setLogoutBusy(true);
    try {
      await onLogout();
    } finally {
      setLogoutBusy(false);
    }
  };

  return (
    <div className="global-top-engine">
      <button
        type="button"
        className={`header-ctl header-ctl-mode ${isLiveMode ? "live" : "paper"}`}
        onClick={toggleExecutionMode}
        disabled={modeBusy}
        title={isLiveMode ? "Live trading — click for paper" : "Paper trading — click for live"}
      >
        <Radio size={14} />
        <span>{isLiveMode ? "LIVE" : "PAPER"}</span>
      </button>
      <button
        type="button"
        className={`header-ctl ${summary?.auto_trade_enabled ? "on" : ""}`}
        onClick={toggleAutoTrade}
        disabled={autoBusy || !summary}
        title="Auto trade"
      >
        <Bot size={14} />
      </button>
      <button
        type="button"
        className={`header-ctl danger ${health?.kill_switch ? "on" : ""}`}
        onClick={toggleKill}
        disabled={killBusy || !health}
        title="Kill switch"
      >
        <Power size={14} />
      </button>
      <button
        type="button"
        className="header-ctl"
        onClick={handleSync}
        disabled={syncBusy}
        title="Sync missing data"
      >
        <Database size={14} />
      </button>
      <button
        type="button"
        className="header-ctl"
        onClick={handleRefresh}
        disabled={refreshBusy}
        title="Refresh"
      >
        <RefreshCw size={14} />
      </button>
      <button
        type="button"
        className="header-ctl"
        onClick={handleReauth}
        disabled={reauthBusy}
        title="Re-authenticate broker"
      >
        <KeyRound size={14} />
      </button>
      <button
        type="button"
        className="header-ctl"
        onClick={handleLogout}
        disabled={logoutBusy || !onLogout}
        title="Logout"
      >
        <LogOut size={14} />
      </button>
    </div>
  );
}
