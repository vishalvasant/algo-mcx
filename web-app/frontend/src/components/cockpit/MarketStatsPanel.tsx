import { useMemo } from "react";
import { BarChart3 } from "lucide-react";
import type { MarketSummary, Watchlist } from "../../types";
import { COCKPIT_DUMMY } from "../../utils/cockpitDummyData";
import { useCommodityChain } from "../../hooks/useCommodityChain";

function formatPrice(n: number | null | undefined) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

interface MarketStatsPanelProps {
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
  activeCommodity: string;
}

export function MarketStatsPanel({ watchlist, summary, activeCommodity }: MarketStatsPanelProps) {
  const { items } = useCommodityChain(watchlist, summary, activeCommodity);

  const livePcr = useMemo(() => {
    let ce = 0;
    let pe = 0;
    for (const item of items) {
      const oi = item.oi ?? 0;
      if (item.option_type === "CE") ce += oi;
      if (item.option_type === "PE") pe += oi;
    }
    return ce > 0 ? pe / ce : null;
  }, [items]);

  const b = COCKPIT_DUMMY.breadth;
  const totalBreadth = b.advancing + b.declining + b.neutral;
  const advPct = Math.round((b.advancing / totalBreadth) * 100);
  const neuStart = advPct + Math.round((b.declining / totalBreadth) * 100);

  const pcr = livePcr ?? b.pcr;
  const vwap = summary?.session_vwap ?? b.vwap;

  return (
    <div className="cockpit-panel market-stats-panel">
      <div className="cockpit-panel-head">
        <BarChart3 size={14} />
        <h3>Market Breadth</h3>
      </div>

      <div className="oi-donut-wrap breadth-donut-wrap">
        <div
          className="oi-donut breadth-donut"
          style={{
            background: `conic-gradient(
              var(--success) 0 ${advPct}%,
              var(--danger) ${advPct}% ${neuStart}%,
              var(--text-muted) ${neuStart}% 100%
            )`,
          }}
        >
          <div className="oi-donut-hole">
            <span className="oi-donut-label">Breadth</span>
            <strong className="mono">{b.advancing}</strong>
            <span className="muted sm">Adv</span>
          </div>
        </div>
        <div className="oi-legend breadth-legend">
          <span>
            <i className="dot ce" /> Adv {b.advancing}
          </span>
          <span>
            <i className="dot pe" /> Dec {b.declining}
          </span>
          <span>
            <i className="dot neutral" /> Neu {b.neutral}
          </span>
        </div>
      </div>

      <div className="stat-rows">
        <div className="stat-row">
          <span>PCR ({activeCommodity})</span>
          <strong className="mono">{pcr.toFixed(2)}</strong>
        </div>
        <div className="stat-row">
          <span>VWAP ({activeCommodity})</span>
          <strong className="mono accent">{formatPrice(vwap)}</strong>
        </div>
        <div className="stat-row">
          <span>OI PCR</span>
          <strong className="mono">{b.oiPcr.toFixed(2)}</strong>
        </div>
        <div className="stat-row">
          <span>INDIA VIX</span>
          <strong className="mono negative">
            {b.vix.toFixed(2)}{" "}
            <small>
              ({b.vixChangePct > 0 ? "+" : ""}
              {b.vixChangePct.toFixed(2)}%)
            </small>
          </strong>
        </div>
      </div>
    </div>
  );
}
