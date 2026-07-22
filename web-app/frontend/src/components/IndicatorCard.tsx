import type { ReactNode } from "react";
import { Sparkline } from "./Sparkline";

interface IndicatorCardProps {
  label: string;
  value: ReactNode;
  sub?: string;
  tone?: "neutral" | "positive" | "negative" | "warning" | "accent";
  spark?: number[];
}

export function IndicatorCard({ label, value, sub, tone = "neutral", spark }: IndicatorCardProps) {
  return (
    <div className={`indicator-card tone-${tone}`}>
      <div className="indicator-top">
        <span className="indicator-label">{label}</span>
        {spark && spark.length > 1 && <Sparkline values={spark} width={72} height={28} />}
      </div>
      <div className={`indicator-value tone-${tone}`}>{value}</div>
      {sub && <div className="indicator-sub">{sub}</div>}
    </div>
  );
}
