import type { Severity } from "../types";

const labels: Record<Severity, string> = {
  info: "Info",
  warning: "Warning",
  critical: "Critical",
};

export function StatusBadge({
  severity,
  label,
}: {
  severity: Severity | "success" | "neutral";
  label?: string;
}) {
  return (
    <span className={`badge ${severity}`}>
      {label ?? (severity in labels ? labels[severity as Severity] : severity)}
    </span>
  );
}
