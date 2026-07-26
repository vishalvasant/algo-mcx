import type { MarketSummary } from "../types";

function formatPrice(n: number | null | undefined) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

interface MarketTickerProps {
  summary: MarketSummary | null;
  spotLtp?: string | null;
}

interface TickerItem {
  label: string;
  value: string;
  accent?: boolean;
  pnl?: number | null;
}

export function MarketTicker({ summary, spotLtp }: MarketTickerProps) {
  const commodities = summary?.commodities ?? [];
  const items: TickerItem[] =
    commodities.length > 0
      ? commodities.map((c) => ({
          label: c.display_name ?? c.underlying,
          value:
            c.spot_ltp != null
              ? c.spot_ltp.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
              : "—",
          accent: c.underlying === (summary?.active_underlying ?? summary?.underlying),
        }))
      : [
          {
            label: "GOLD",
            value: formatPrice(summary?.spot_ltp ?? (spotLtp ? Number(spotLtp) : null)),
            accent: true,
          },
        ];

  const tapeExtras: TickerItem[] = [
    { label: "VWAP", value: formatPrice(summary?.session_vwap ?? null) },
    { label: "Bias", value: summary?.bias_5m ?? "NEUTRAL" },
    {
      label: "Today P&L",
      value: summary?.today_pnl != null ? `₹${summary.today_pnl.toFixed(2)}` : "—",
      pnl: summary?.today_pnl,
    },
    { label: "Session", value: summary?.market_session ?? "—" },
  ];

  const tape: TickerItem[] = [...items, ...tapeExtras, ...items, ...tapeExtras];

  return (
    <div className="market-ticker" aria-label="Live market tape">
      <div className="ticker-viewport">
        <div className="ticker-track">
          {tape.map((item, i) => (
            <div key={`${item.label}-${i}`} className="ticker-item">
              <span className="ticker-label">{item.label}</span>
              <span
                className={`ticker-value ${item.accent ? "accent" : ""} ${
                  item.pnl != null ? (item.pnl >= 0 ? "up" : "down") : ""
                }`}
              >
                {item.value}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
