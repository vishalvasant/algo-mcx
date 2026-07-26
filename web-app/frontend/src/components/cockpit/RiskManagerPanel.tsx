import type { CSSProperties } from "react";
import { Shield } from "lucide-react";
import type { MarketSummary } from "../../types";
import { StatusBadge } from "../StatusBadge";

function formatInr(value: number | undefined, digits = 0) {
  return `₹${(value ?? 0).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
}

function riskLabelFor(pct: number) {
  if (pct > 60) return "HIGH RISK";
  if (pct > 30) return "MEDIUM RISK";
  return "LOW RISK";
}

function riskSeverityFor(pct: number): "success" | "warning" | "critical" {
  if (pct > 60) return "critical";
  if (pct > 30) return "warning";
  return "success";
}

interface RiskManagerPanelProps {
  summary: MarketSummary | null;
}

export function RiskManagerPanel({ summary }: RiskManagerPanelProps) {
  const hasData = summary != null;

  const starting = summary?.starting_capital ?? 0;
  const realized = summary?.today_pnl ?? 0;
  const unrealized = summary?.unrealized_pnl ?? 0;
  const dailyPnl = hasData ? realized + unrealized : null;

  const usedMargin = summary?.used_margin ?? summary?.deployed_capital ?? 0;
  const available = summary?.available_capital ?? 0;
  const equity = summary?.equity ?? starting + realized + unrealized;
  const maxLoss = summary?.max_daily_loss ?? 0;
  const deployCapPct = summary?.max_deployed_pct_of_equity ?? 90;

  const marginRiskPct =
    equity > 0 ? Math.min(100, Math.round((usedMargin / equity) * 100)) : 0;
  const deployCap = equity * (deployCapPct / 100);
  const deployUtilPct =
    deployCap > 0 ? Math.min(100, Math.round((usedMargin / deployCap) * 100)) : marginRiskPct;
  const lossRiskPct =
    maxLoss > 0 && dailyPnl != null && dailyPnl < 0
      ? Math.min(100, Math.round((Math.abs(dailyPnl) / maxLoss) * 100))
      : 0;

  const riskPct = hasData ? Math.max(deployUtilPct, lossRiskPct) : 0;
  const riskLabel = riskLabelFor(riskPct);
  const riskSeverity = riskSeverityFor(riskPct);

  return (
    <section className="cockpit-panel risk-panel">
      <header className="cockpit-panel-head">
        <Shield size={14} />
        <h3>Risk Manager</h3>
        <StatusBadge severity={riskSeverity} label={riskLabel} />
      </header>

      <figure className="risk-gauge-semicircle" style={{ "--pct": riskPct } as CSSProperties}>
        <div className="risk-arc">
          <div className="risk-arc-fill" />
          <div className="risk-arc-hole">
            <span className="risk-arc-value">{hasData ? `${riskPct}%` : "—"}</span>
            <span className="risk-arc-label">{hasData ? riskLabel : "LOADING"}</span>
          </div>
        </div>
      </figure>

      <dl className="stat-rows">
        <div className="stat-row">
          <dt>Daily P&amp;L</dt>
          <dd
            className={`mono ${
              dailyPnl == null ? "" : dailyPnl >= 0 ? "positive" : "negative"
            }`}
          >
            {dailyPnl == null ? "—" : formatInr(dailyPnl, 2)}
          </dd>
        </div>
        <div className="stat-row">
          <dt>Max loss</dt>
          <dd className="mono negative">
            {hasData ? (maxLoss > 0 ? formatInr(maxLoss) : "No limit") : "—"}
          </dd>
        </div>
        <div className="stat-row">
          <dt>Used margin</dt>
          <dd className="mono">{hasData ? formatInr(usedMargin) : "—"}</dd>
        </div>
        <div className="stat-row">
          <dt>Available</dt>
          <dd className="mono accent">{hasData ? formatInr(available) : "—"}</dd>
        </div>
      </dl>
    </section>
  );
}
