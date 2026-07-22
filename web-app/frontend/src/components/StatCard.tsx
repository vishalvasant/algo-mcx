import type { ReactNode } from "react";

export function StatCard({
  title,
  value,
  sub,
  tone,
}: {
  title: string;
  value: ReactNode;
  sub?: string;
  tone?: "positive" | "negative" | "neutral";
}) {
  const cls = tone ? `stat-value ${tone}` : "stat-value";
  return (
    <div className="card">
      <h3>{title}</h3>
      <div className={cls}>{value}</div>
      {sub && (
        <p style={{ marginTop: "0.35rem", fontSize: "0.75rem", color: "var(--text-muted)" }}>
          {sub}
        </p>
      )}
    </div>
  );
}
