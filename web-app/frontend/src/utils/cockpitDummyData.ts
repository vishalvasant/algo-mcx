import type { ClosedBlotterTrade, DecisionLogEvent, WatchlistOpenPosition } from "../types";

/** Placeholder cockpit data — swap for live logic later. */
export const COCKPIT_PREVIEW_MODE = true;

export const AI_PANEL_INDICES = ["GOLD"] as const;
export type AiPanelIndex = (typeof AI_PANEL_INDICES)[number];

export const AI_PANEL_LABELS: Record<AiPanelIndex, string> = {
  GOLD: "Gold",
};

export interface IndexStrategyInsight {
  name: string;
  bias: "BULLISH" | "BEARISH" | "NEUTRAL";
  confidence: number;
  reasons: string[];
  entryLow: number;
  entryHigh: number;
  targets: [number, number];
  stopLoss: number;
}

export const strategyByIndex: Record<AiPanelIndex, IndexStrategyInsight> = {
  GOLD: {
    name: "VWAP RECLAIM",
    bias: "BULLISH",
    confidence: 78,
    reasons: [
      "Price reclaimed VWAP with volume confirmation",
      "PCR below 1.0 — bullish positioning",
      "5m structure turned bullish above EMA21",
      "OI buildup at ATM calls supports upside",
    ],
    entryLow: 145000,
    entryHigh: 145500,
    targets: [146500, 147500],
    stopLoss: 144000,
  },
};

export function indexStep(underlying: string): number {
  if (underlying === "SILVER") return 1000;
  return 500;
}

export function levelsFromSpot(spot: number, underlying: string) {
  const step = indexStep(underlying);
  const entryLow = Math.round((spot - step * 3) / step) * step;
  const entryHigh = Math.round((spot - step) / step) * step;
  const t1 = Math.round((spot + step * 4) / step) * step;
  const t2 = Math.round((spot + step * 7) / step) * step;
  const sl = Math.round((spot - step * 6) / step) * step;
  return { entryLow, entryHigh, targets: [t1, t2] as [number, number], stopLoss: sl };
}

export const COCKPIT_DUMMY = {
  indices: {
    GOLD: { spot: 145520, change: 420.5, changePct: 0.29 },
    GOLD_FUT: { spot: 145520, change: 420.5, changePct: 0.29 },
  },
  breadth: {
    advancing: 0,
    declining: 0,
    neutral: 0,
    pcr: 0.92,
    vwap: 145180,
    oiPcr: 1.08,
    vix: 0,
    vixChangePct: 0,
  },
  strategy: strategyByIndex.GOLD,
  position: {
    tsym: "GOLD 05 DEC 145500 CE",
    side: "BUY",
    optionType: "CE",
    lots: 1,
    quantity: 1,
    entryPrice: 1425.5,
    ltp: 1589.0,
    indexEntry: 145472.5,
    indexLtp: 145520.3,
    unrealizedPnl: 163.5,
    pnlPct: 11.5,
    setupType: "VWAP RECLAIM",
    stopLoss: 1180.0,
    targetPrice: 1750.0,
    entryTs: new Date().toISOString(),
  },
  risk: {
    riskPct: 28,
    label: "LOW RISK",
    dailyPnl: 6927,
    maxLoss: 10000,
    usedMargin: 238760,
    available: 261240,
  },
  footer: {
    totalPnl: 163.5,
    margin: 238760,
    mtm: 163.5,
    tradeCount: 1,
  },
  journal: [
    {
      id: "dummy-j1",
      tsym: "GOLD 05 DEC 145000 PE",
      side: "SELL",
      entryPrice: 982.2,
      exitPrice: 725.5,
      pnl: 256.7,
      exitReason: "Target Hit",
      time: "14:22",
    },
    {
      id: "dummy-j2",
      tsym: "GOLD 05 DEC 146000 CE",
      side: "BUY",
      entryPrice: 1150.0,
      exitPrice: 984.4,
      pnl: -165.6,
      exitReason: "SL Hit",
      time: "11:05",
    },
  ] as const,
  decisions: [
    { time: "10:27:18", title: "VWAP Reclaim detected", summary: "Gold reclaimed session VWAP on 5m close", severity: "success" },
    { time: "10:27:22", title: "Entry signal generated", summary: "145500 CE added to candidate queue", severity: "success" },
    { time: "10:26:55", title: "Volume spike", summary: "1.8× average volume on Gold futures", severity: "info" },
    { time: "10:25:10", title: "Risk check passed", summary: "Margin and daily loss limits within bounds", severity: "success" },
  ] as const,
  chainExpiries: ["05 DEC 25 (W)", "12 DEC 25 (W)", "05 JAN 26 (M)", "05 FEB 26 (M)"],
};

function todayIstIso(hour: number, minute: number): string {
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .formatToParts(now)
    .reduce<Record<string, string>>((acc, p) => {
      if (p.type !== "literal") acc[p.type] = p.value;
      return acc;
    }, {});
  const y = parts.year;
  const m = parts.month;
  const d = parts.day;
  const hh = String(hour).padStart(2, "0");
  const mm = String(minute).padStart(2, "0");
  return `${y}-${m}-${d}T${hh}:${mm}:00+05:30`;
}

export function dummyOpenPosition(): WatchlistOpenPosition {
  const p = COCKPIT_DUMMY.position;
  return {
    tsym: p.tsym,
    side: p.side,
    quantity: p.quantity,
    lots: p.lots,
    entry_price: p.entryPrice,
    entry_ts: p.entryTs,
    current_ltp: p.ltp,
    unrealized_pnl: p.unrealizedPnl,
    premium_deployed: p.entryPrice * p.quantity,
    setup_type: p.setupType,
    stop_loss: p.stopLoss,
    target_price: p.targetPrice,
  };
}

export function dummyClosedTrades(): ClosedBlotterTrade[] {
  const times = [todayIstIso(14, 22), todayIstIso(11, 5)];
  return COCKPIT_DUMMY.journal.map((j, i) => ({
    id: j.id,
    tsym: j.tsym,
    side: j.side,
    entry_ts: times[i] ?? todayIstIso(10, 0),
    exit_ts: times[i] ?? todayIstIso(10, 0),
    entry_price: j.entryPrice,
    exit_price: j.exitPrice,
    quantity: 1,
    lot_size: 1,
    lots: 1,
    pnl: j.pnl,
    exit_reason: j.exitReason,
    setup_type: "DEMO",
  }));
}

export function dummyDecisionEvents(): DecisionLogEvent[] {
  const now = Date.now();
  return COCKPIT_DUMMY.decisions.map((d, i) => ({
    id: `dummy-dec-${i}`,
    ts: new Date(now - i * 45_000).toISOString(),
    event_type: "scan",
    severity: d.severity,
    message: d.title,
    metadata: { summary: d.summary },
  }));
}

/** Deterministic dummy OI change from strike (visual only). */
export function dummyOiChange(strike: number, isCe: boolean): number {
  const sign = isCe ? 1 : -1;
  return sign * (((strike / 500) % 17) - 8) * 125;
}

export function dummyVolume(strike: number): number {
  return Math.round(800 + (strike % 11) * 340);
}

/** Deterministic OI change % for pill display (visual only). */
export function dummyOiChangePct(strike: number, isCe: boolean): number {
  const base = ((strike / 500) % 13) - 6;
  return Number((base * (isCe ? 1.1 : -0.9)).toFixed(1));
}
