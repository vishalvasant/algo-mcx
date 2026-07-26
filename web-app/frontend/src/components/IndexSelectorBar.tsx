import type { CommoditySnapshot } from "../types";

interface IndexSelectorBarProps {
  commodities: CommoditySnapshot[];
  active: string;
  onChange: (underlying: string) => void;
}

export function IndexSelectorBar({ commodities, active, onChange }: IndexSelectorBarProps) {
  if (!commodities.length) return null;

  return (
    <div className="index-selector-bar" role="tablist" aria-label="Select index">
      {commodities.map((c) => {
        const isActive = c.underlying === active;
        return (
          <button
            key={c.underlying}
            type="button"
            role="tab"
            aria-selected={isActive}
            className={isActive ? "index-selector-tab active" : "index-selector-tab"}
            onClick={() => onChange(c.underlying)}
          >
            <span className="index-selector-name">{c.display_name ?? c.underlying}</span>
            {c.spot_ltp != null ? (
              <span className="index-selector-spot mono">
                {Number(c.spot_ltp).toLocaleString("en-IN", { maximumFractionDigits: 2 })}
              </span>
            ) : (
              <span className="index-selector-spot muted">—</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
