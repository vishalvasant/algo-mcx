import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface AppPageShellProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  actions?: ReactNode;
  children: ReactNode;
}

/** Secondary pages — matches dashboard cockpit panel typography. */
export function AppPageShell({
  title,
  description,
  icon: Icon,
  actions,
  children,
}: AppPageShellProps) {
  return (
    <div className="app-page">
      <section className="cockpit-panel app-page-hero">
        <header className="cockpit-panel-head app-page-head">
          <h3>
            {Icon ? <Icon size={14} strokeWidth={2} /> : null}
            <span>{title}</span>
          </h3>
          {actions}
        </header>
        {description ? <p className="app-page-desc">{description}</p> : null}
      </section>
      <div className="app-page-content">{children}</div>
    </div>
  );
}
