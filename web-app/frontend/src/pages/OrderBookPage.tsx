import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, CalendarRange, Download, FileText, RefreshCw } from "lucide-react";
import { fetchTradeDates, fetchTradesReport } from "../api/client";
import type { Trade, TradesReport } from "../types";
import { AppPageShell } from "../components/AppPageShell";

function todayIst(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function yesterdayIst(): string {
  const ist = new Date(
    new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }),
  );
  ist.setDate(ist.getDate() - 1);
  const y = ist.getFullYear();
  const m = String(ist.getMonth() + 1).padStart(2, "0");
  const day = String(ist.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function formatTs(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

function formatDate(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleDateString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return ts;
  }
}

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatInr(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function pnlClass(pnl: number) {
  if (pnl > 0) return "positive";
  if (pnl < 0) return "negative";
  return "";
}

function holdLabel(seconds: number | null | undefined) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

function downloadBlob(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function csvEscape(v: unknown): string {
  const s = v == null ? "" : String(v);
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function tradesToCsv(trades: Trade[]): string {
  const headers = [
    "date_ist",
    "entry_ts",
    "exit_ts",
    "hold_seconds",
    "tsym",
    "side",
    "setup_type",
    "lots",
    "lot_size",
    "quantity",
    "entry_price",
    "exit_price",
    "pnl",
    "pnl_pct",
    "mfe",
    "mae",
    "exit_reason",
    "mode",
    "id",
  ];
  const lines = [headers.join(",")];
  for (const t of trades) {
    const dateIst = (() => {
      try {
        return new Date(t.exit_ts).toLocaleDateString("en-CA", {
          timeZone: "Asia/Kolkata",
        });
      } catch {
        return "";
      }
    })();
    lines.push(
      [
        dateIst,
        t.entry_ts,
        t.exit_ts,
        t.hold_seconds ?? "",
        t.tsym ?? "",
        t.side ?? "",
        t.setup_type,
        t.lots ?? "",
        t.lot_size ?? "",
        t.quantity ?? "",
        t.entry_price ?? "",
        t.exit_price ?? "",
        t.pnl,
        t.pnl_pct ?? "",
        t.mfe ?? "",
        t.mae ?? "",
        t.exit_reason,
        t.mode,
        t.id,
      ]
        .map(csvEscape)
        .join(","),
    );
  }
  return lines.join("\n");
}

function buildMarkdownReport(report: TradesReport): string {
  const s = report.summary;
  const range =
    report.from_date || report.to_date
      ? `${report.from_date ?? "…"} → ${report.to_date ?? "…"}`
      : "All dates";
  const lines: string[] = [
    `# Algo-MCX P&L Report`,
    ``,
    `- Range (IST): **${range}**`,
    `- Generated: ${report.generated_at}`,
    `- Trades: **${s.trades}** (${s.wins}W / ${s.losses}L)`,
    `- Win rate: **${s.win_rate_pct.toFixed(1)}%**`,
    `- Total P&L: **₹${s.total_pnl.toFixed(2)}**`,
    `- Avg / Best / Worst: ₹${s.avg_pnl.toFixed(2)} / ₹${s.best_trade.toFixed(2)} / ₹${s.worst_trade.toFixed(2)}`,
    `- Gross profit / loss: ₹${s.gross_profit.toFixed(2)} / ₹${s.gross_loss.toFixed(2)}`,
    ``,
    `## By day`,
    ``,
    `| Date | Trades | P&L |`,
    `|---|---:|---:|`,
  ];
  for (const [day, row] of Object.entries(report.by_day)) {
    lines.push(`| ${day} | ${row.count} | ₹${Number(row.pnl).toFixed(2)} |`);
  }
  lines.push(``, `## By setup`, ``, `| Setup | Trades | P&L |`, `|---|---:|---:|`);
  for (const [k, row] of Object.entries(report.by_setup).sort(
    (a, b) => Number(b[1].pnl) - Number(a[1].pnl),
  )) {
    lines.push(`| ${k} | ${row.count} | ₹${Number(row.pnl).toFixed(2)} |`);
  }
  lines.push(
    ``,
    `## By exit reason`,
    ``,
    `| Reason | Trades | P&L |`,
    `|---|---:|---:|`,
  );
  for (const [k, row] of Object.entries(report.by_exit_reason).sort(
    (a, b) => Number(b[1].pnl) - Number(a[1].pnl),
  )) {
    lines.push(`| ${k} | ${row.count} | ₹${Number(row.pnl).toFixed(2)} |`);
  }
  lines.push(
    ``,
    `## Trades`,
    ``,
    `| # | Date | Entry | Exit | Hold | Contract | Side | Setup | Lots | Entry ₹ | Exit ₹ | P&L | Reason |`,
    `|---:|---|---|---|---|---|---|---|---:|---:|---:|---:|---|`,
  );
  const trades = [...report.trades].reverse();
  trades.forEach((t, i) => {
    lines.push(
      `| ${i + 1} | ${formatDate(t.exit_ts)} | ${formatTs(t.entry_ts)} | ${formatTs(t.exit_ts)} | ${holdLabel(t.hold_seconds)} | ${t.tsym ?? ""} | ${t.side ?? ""} | ${t.setup_type} | ${t.lots ?? ""} | ${formatPrice(t.entry_price)} | ${formatPrice(t.exit_price)} | ${Number(t.pnl).toFixed(2)} | ${t.exit_reason} |`,
    );
  });
  lines.push(``, `_Algo-MCX Order Book export_`, ``);
  return lines.join("\n");
}

const DATE_PRESETS = [
  { id: "yesterday", label: "Yesterday" },
  { id: "today", label: "Today" },
  { id: "week", label: "7 days" },
  { id: "all", label: "All" },
] as const;

type DatePreset = (typeof DATE_PRESETS)[number]["id"];

export function OrderBookPage() {
  const [fromDate, setFromDate] = useState(yesterdayIst);
  const [toDate, setToDate] = useState(yesterdayIst);
  const [activePreset, setActivePreset] = useState<DatePreset | "custom">("yesterday");
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  const [report, setReport] = useState<TradesReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchTradeDates()
      .then((dates) => {
        setAvailableDates(dates);
        if (dates.length && !dates.includes(fromDate)) {
          setFromDate(dates[0]);
          setToDate(dates[0]);
        }
      })
      .catch(() => setAvailableDates([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetchTradesReport({
        fromDate: fromDate || undefined,
        toDate: toDate || undefined,
        limit: 2000,
      });
      setReport(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [fromDate, toDate]);

  useEffect(() => {
    load();
  }, [load]);

  const summary = report?.summary;
  const trades = report?.trades ?? [];

  const rangeLabel = useMemo(() => {
    if (fromDate && toDate && fromDate === toDate) return fromDate;
    if (fromDate || toDate) return `${fromDate || "…"} → ${toDate || "…"}`;
    return "All dates";
  }, [fromDate, toDate]);

  const setPreset = (preset: DatePreset) => {
    setActivePreset(preset);
    const t = todayIst();
    if (preset === "today") {
      setFromDate(t);
      setToDate(t);
    } else if (preset === "yesterday") {
      const y = yesterdayIst();
      setFromDate(y);
      setToDate(y);
    } else if (preset === "all") {
      if (availableDates.length) {
        setFromDate(availableDates[availableDates.length - 1]);
        setToDate(availableDates[0]);
      } else {
        setFromDate("");
        setToDate("");
      }
    } else {
      const end = new Date(
        new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }),
      );
      const start = new Date(end);
      start.setDate(start.getDate() - 6);
      const fmt = (d: Date) =>
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      setFromDate(fmt(start));
      setToDate(fmt(end));
    }
  };

  const exportCsv = () => {
    const stamp = `${fromDate || "all"}_${toDate || "all"}`;
    downloadBlob(
      `algomcx_orderbook_${stamp}.csv`,
      tradesToCsv(trades),
      "text/csv;charset=utf-8",
    );
  };

  const exportJson = () => {
    if (!report) return;
    const stamp = `${fromDate || "all"}_${toDate || "all"}`;
    downloadBlob(
      `algomcx_pnl_report_${stamp}.json`,
      JSON.stringify(report, null, 2),
      "application/json",
    );
  };

  const exportMarkdown = () => {
    if (!report) return;
    const stamp = `${fromDate || "all"}_${toDate || "all"}`;
    downloadBlob(
      `algomcx_pnl_report_${stamp}.md`,
      buildMarkdownReport(report),
      "text/markdown;charset=utf-8",
    );
  };

  return (
    <AppPageShell
      title="Order Book"
      icon={BookOpen}
      description="Historical closed trades · filter by IST date · export CSV / JSON / full P&L report"
    >
      <div className="logs-page-full orderbook-page">
        <section className="cockpit-panel logs-stats-panel orderbook-toolbar">
          <header className="cockpit-panel-head logs-stats-head">
            <CalendarRange size={14} strokeWidth={2} />
            <h3>Date range</h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={load}
              disabled={loading}
            >
              <RefreshCw size={13} />
              {loading ? "Loading…" : "Refresh"}
            </button>
          </header>

          <div className="orderbook-date-row">
            <label className="orderbook-date-field">
              <span>From</span>
              <input
                type="date"
                value={fromDate}
                onChange={(e) => {
                  setFromDate(e.target.value);
                  setActivePreset("custom");
                }}
                list="trade-dates"
              />
            </label>
            <label className="orderbook-date-field">
              <span>To</span>
              <input
                type="date"
                value={toDate}
                onChange={(e) => {
                  setToDate(e.target.value);
                  setActivePreset("custom");
                }}
                list="trade-dates"
              />
            </label>
            <datalist id="trade-dates">
              {availableDates.map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
          </div>

          <div className="logs-filter-row">
            <span className="logs-filter-label">Preset</span>
            <div className="chart-interval-tabs logs-filter-tabs" role="tablist" aria-label="Date presets">
              {DATE_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  role="tab"
                  aria-selected={activePreset === p.id}
                  className={`chart-interval ${activePreset === p.id ? "active" : ""}`}
                  onClick={() => setPreset(p.id)}
                  disabled={loading}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          <div className="orderbook-export-row">
            <span className="logs-range-pill mono muted">{rangeLabel}</span>
            <div className="orderbook-export-btns">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={exportCsv}
                disabled={!trades.length}
              >
                <Download size={13} />
                CSV
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={exportJson}
                disabled={!report}
              >
                <Download size={13} />
                JSON
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={exportMarkdown}
                disabled={!report}
              >
                <FileText size={13} />
                Report
              </button>
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        {summary ? (
          <section className="cockpit-panel logs-stats-panel">
            <header className="cockpit-panel-head">
              <BookOpen size={14} strokeWidth={2} />
              <h3>Period summary</h3>
            </header>
            <div className="cockpit-command-metrics logs-command-metrics orderbook-metrics">
              <div className="cmd-metric">
                <span>Period P&amp;L</span>
                <strong className={pnlClass(summary.total_pnl)}>{formatInr(summary.total_pnl)}</strong>
              </div>
              <div className="cmd-metric">
                <span>Trades</span>
                <strong>
                  {summary.trades}{" "}
                  <span className="muted orderbook-metric-sub">
                    ({summary.wins}W / {summary.losses}L)
                  </span>
                </strong>
              </div>
              <div className="cmd-metric">
                <span>Win rate</span>
                <strong>{summary.trades ? `${summary.win_rate_pct.toFixed(0)}%` : "—"}</strong>
              </div>
              <div className="cmd-metric">
                <span>Avg trade</span>
                <strong className={pnlClass(summary.avg_pnl)}>{formatInr(summary.avg_pnl)}</strong>
              </div>
              <div className="cmd-metric">
                <span>Best / Worst</span>
                <strong className="orderbook-best-worst">
                  <span className={pnlClass(summary.best_trade)}>{formatInr(summary.best_trade)}</span>
                  <span className="muted"> / </span>
                  <span className={pnlClass(summary.worst_trade)}>{formatInr(summary.worst_trade)}</span>
                </strong>
              </div>
            </div>
          </section>
        ) : null}

        {report && Object.keys(report.by_day).length > 0 ? (
          <section className="cockpit-panel orderbook-table-panel">
            <header className="cockpit-panel-head">
              <h3>Day breakdown</h3>
            </header>
            <div className="cockpit-table-scroll cockpit-table-scroll--journal">
              <table className="trades-table cockpit-table pro">
                <thead>
                  <tr>
                    <th>Date (IST)</th>
                    <th className="num">Trades</th>
                    <th className="num">P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.by_day).map(([day, row]) => (
                    <tr key={day}>
                      <td>{day}</td>
                      <td className="num">{row.count}</td>
                      <td className={`num mono ${pnlClass(Number(row.pnl))}`}>
                        {formatInr(Number(row.pnl))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}

        <section className="cockpit-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <h3>Closed trades</h3>
            <span className="logs-range-pill mono muted">{trades.length} closed</span>
          </header>

          {trades.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {loading
                ? "Loading trades…"
                : "No closed trades in this date range"}
            </p>
          ) : (
            <div className="cockpit-table-scroll cockpit-table-scroll--journal orderbook-trades-scroll">
              <table className="trades-table cockpit-table pro">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Date</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Hold</th>
                    <th>Contract</th>
                    <th>Side</th>
                    <th>Setup</th>
                    <th className="num">Lots</th>
                    <th className="num">Entry ₹</th>
                    <th className="num">Exit ₹</th>
                    <th className="num">P&amp;L</th>
                    <th>Exit reason</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => {
                    const pnl = Number(t.pnl);
                    return (
                      <tr key={t.id}>
                        <td className="muted">{trades.length - i}</td>
                        <td>{formatDate(t.exit_ts)}</td>
                        <td className="mono">{formatTs(t.entry_ts)}</td>
                        <td className="mono">{formatTs(t.exit_ts)}</td>
                        <td>{holdLabel(t.hold_seconds)}</td>
                        <td className="mono">{t.tsym ?? "—"}</td>
                        <td>{t.side ?? "—"}</td>
                        <td>{t.setup_type}</td>
                        <td className="num">
                          {t.lots ?? "—"}
                          {t.lot_size ? ` × ${t.lot_size}` : ""}
                        </td>
                        <td className="num mono">{formatPrice(t.entry_price)}</td>
                        <td className="num mono">{formatPrice(t.exit_price)}</td>
                        <td className={`num mono ${pnlClass(pnl)}`}>{formatInr(pnl)}</td>
                        <td className="reason-cell">{t.exit_reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={11} className="orderbook-tfoot-label">
                      Period realized P&amp;L
                    </td>
                    <td className={`num mono ${pnlClass(summary?.total_pnl ?? 0)}`}>
                      {formatInr(summary?.total_pnl ?? 0)}
                    </td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppPageShell>
  );
}
