import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, Download, FileText, Filter } from "lucide-react";
import { fetchTradeDates, fetchTradesReport } from "../api/client";
import type { Trade, TradesReport } from "../types";

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

export function OrderBookPage() {
  const [fromDate, setFromDate] = useState(yesterdayIst);
  const [toDate, setToDate] = useState(yesterdayIst);
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

  const setPreset = (preset: "yesterday" | "today" | "all" | "week") => {
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
      // last 7 calendar days ending today
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
    <>
      <div className="page-header">
        <h2>Order Book</h2>
        <p>
          Historical closed trades · filter by IST date · export CSV / JSON /
          full P&amp;L report
        </p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card orderbook-filters">
        <div className="orderbook-filter-row">
          <label>
            <span>From</span>
            <input
              type="date"
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
              list="trade-dates"
            />
          </label>
          <label>
            <span>To</span>
            <input
              type="date"
              value={toDate}
              onChange={(e) => setToDate(e.target.value)}
              list="trade-dates"
            />
          </label>
          <datalist id="trade-dates">
            {availableDates.map((d) => (
              <option key={d} value={d} />
            ))}
          </datalist>
          <div className="orderbook-presets">
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPreset("yesterday")}>
              Yesterday
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPreset("today")}>
              Today
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPreset("week")}>
              7 days
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPreset("all")}>
              All
            </button>
          </div>
          <button type="button" className="btn btn-primary btn-sm" onClick={load} disabled={loading}>
            <Filter size={14} />
            {loading ? "Loading…" : "Apply"}
          </button>
        </div>
        <div className="orderbook-export-row">
          <span className="muted">Range: {rangeLabel}</span>
          <div className="orderbook-export-btns">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={exportCsv}
              disabled={!trades.length}
            >
              <Download size={14} />
              CSV
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={exportJson}
              disabled={!report}
            >
              <Download size={14} />
              JSON
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={exportMarkdown}
              disabled={!report}
            >
              <FileText size={14} />
              P&amp;L report
            </button>
          </div>
        </div>
      </div>

      {summary && (
        <div className="pnl-stats-grid">
          <div className="card pnl-stat">
            <span className="pnl-stat-label">Period P&amp;L</span>
            <span className={`pnl-stat-value ${pnlClass(summary.total_pnl)}`}>
              {formatInr(summary.total_pnl)}
            </span>
          </div>
          <div className="card pnl-stat">
            <span className="pnl-stat-label">Trades</span>
            <span className="pnl-stat-value">
              {summary.trades}{" "}
              <span className="muted" style={{ fontSize: "0.85rem", fontWeight: 500 }}>
                ({summary.wins}W / {summary.losses}L)
              </span>
            </span>
          </div>
          <div className="card pnl-stat">
            <span className="pnl-stat-label">Win rate</span>
            <span className="pnl-stat-value">
              {summary.trades ? `${summary.win_rate_pct.toFixed(0)}%` : "—"}
            </span>
          </div>
          <div className="card pnl-stat">
            <span className="pnl-stat-label">Best / Worst</span>
            <span className="pnl-stat-value" style={{ fontSize: "1rem" }}>
              <span className={pnlClass(summary.best_trade)}>
                {formatInr(summary.best_trade)}
              </span>
              {" / "}
              <span className={pnlClass(summary.worst_trade)}>
                {formatInr(summary.worst_trade)}
              </span>
            </span>
          </div>
          <div className="card pnl-stat">
            <span className="pnl-stat-label">Avg trade</span>
            <span className={`pnl-stat-value ${pnlClass(summary.avg_pnl)}`}>
              {formatInr(summary.avg_pnl)}
            </span>
          </div>
        </div>
      )}

      {report && Object.keys(report.by_day).length > 0 && (
        <div className="card" style={{ marginBottom: "1rem", overflowX: "auto" }}>
          <div className="panel-head" style={{ marginBottom: "0.75rem" }}>
            <h3>Day breakdown</h3>
          </div>
          <table className="trades-table">
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
      )}

      <div className="card" style={{ overflowX: "auto" }}>
        <div className="panel-head" style={{ marginBottom: "0.75rem" }}>
          <h3>Closed trades</h3>
          <span className="muted">{trades.length} closed</span>
        </div>
        {trades.length === 0 ? (
          <div className="empty-state">
            <BookOpen size={32} strokeWidth={1.5} />
            <p>No closed trades in this date range</p>
          </div>
        ) : (
          <table className="trades-table">
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
                    <td>{formatTs(t.entry_ts)}</td>
                    <td>{formatTs(t.exit_ts)}</td>
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
                    <td>{t.exit_reason}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={11} style={{ textAlign: "right", fontWeight: 600 }}>
                  Period realized P&amp;L
                </td>
                <td
                  className={`num mono ${pnlClass(summary?.total_pnl ?? 0)}`}
                  style={{ fontWeight: 700 }}
                >
                  {formatInr(summary?.total_pnl ?? 0)}
                </td>
                <td />
              </tr>
            </tfoot>
          </table>
        )}
      </div>
    </>
  );
}
