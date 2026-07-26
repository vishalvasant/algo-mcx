import type { DecisionLogEvent } from "../types";

function humanizeToken(raw: string): string {
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtPct(n: unknown): string | null {
  if (n == null || Number.isNaN(Number(n))) return null;
  return `${Math.round(Number(n))}%`;
}

function regimeLabel(m: Record<string, unknown>): string | null {
  const regime = m.regime;
  if (!regime || typeof regime !== "object") return null;
  const primary = (regime as { primary?: string }).primary;
  return primary ? humanizeToken(primary) : null;
}

export interface HumanDecision {
  title: string;
  summary: string;
  detail?: string;
}

export function formatHumanDecision(ev: DecisionLogEvent): HumanDecision {
  const m = ev.metadata ?? {};
  const ul = m.scan_underlying ? String(m.scan_underlying) : null;

  if (ev.event_type === "strategy_decision") {
    const strategy = String(m.selected_strategy ?? "NO_TRADE");
    const conf = fmtPct(m.confidence);
    const side = m.position_side && m.position_side !== "NONE" ? String(m.position_side) : null;
    const regime = regimeLabel(m);
    const allowed = m.trade_allowed === true;
    const reason = String(m.selected_reason || ev.message || "").trim();
    const prefix = ul ? `${ul} scan` : "Market scan";

    if (strategy === "NO_TRADE" || !allowed) {
      const why =
        reason && reason !== "NO_TRADE"
          ? humanizeToken(reason)
          : conf
            ? `Confidence ${conf} below entry threshold`
            : "No qualifying setup";
      return {
        title: `${prefix} · No trade`,
        summary: why,
        detail: regime ? `Regime: ${regime}` : undefined,
      };
    }

    const parts = [
      humanizeToken(strategy),
      conf ? `confidence ${conf}` : null,
      side ? `${side} side` : null,
      regime ? `regime ${regime}` : null,
    ].filter(Boolean);

    return {
      title: `${prefix} · Trade signal`,
      summary: parts.join(" · "),
      detail: reason ? humanizeToken(reason) : undefined,
    };
  }

  if (ev.event_type === "entry_skipped") {
    const tsym = m.tsym ? String(m.tsym) : "contract";
    const setup = m.setup ? humanizeToken(String(m.setup)) : "Entry";
    const side = m.side ? String(m.side) : "";
    const block = m.block_reason
      ? humanizeToken(String(m.block_reason))
      : ev.message.replace(/^Signal | skipped.*$/gi, "").trim() || "Blocked by risk rules";
    const conf = fmtPct(m.confidence);

    return {
      title: `${setup} ${side} skipped`.trim(),
      summary: `${tsym} — ${block}`,
      detail: conf ? `Signal confidence was ${conf}` : undefined,
    };
  }

  if (ev.event_type === "manual_sync") {
    const u = m.universe as { action?: string; after?: number; expected?: number } | undefined;
    const c = m.candles as { m1_added?: number } | undefined;
    const q = m.quotes as { polled?: number } | undefined;
    return {
      title: "Manual data sync",
      summary: [
        u?.action ? `Universe ${u.action}` : null,
        u?.after != null ? `${u.after} contracts` : null,
        c?.m1_added ? `+${c.m1_added} candles` : null,
        q?.polled != null ? `${q.polled} quotes polled` : null,
      ]
        .filter(Boolean)
        .join(" · "),
      detail: ev.message || undefined,
    };
  }

  return {
    title: humanizeToken(ev.event_type),
    summary: ev.message || "—",
  };
}
