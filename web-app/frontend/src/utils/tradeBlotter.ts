import type { ClosedBlotterTrade, Trade } from "../types";

export function tradeToClosedBlotter(t: Trade): ClosedBlotterTrade {
  const qty = Number(t.quantity ?? 0);
  const lotSize = Number(t.lot_size ?? 1);
  return {
    id: t.id,
    tsym: t.tsym ?? "—",
    side: t.side,
    entry_ts: t.entry_ts,
    exit_ts: t.exit_ts,
    entry_price: Number(t.entry_price ?? 0),
    exit_price: Number(t.exit_price ?? 0),
    quantity: qty,
    lot_size: lotSize,
    lots: t.lots ?? Math.floor(qty / Math.max(lotSize, 1)),
    pnl: Number(t.pnl),
    exit_reason: t.exit_reason,
    setup_type: t.setup_type,
    hold_seconds: t.hold_seconds ?? undefined,
  };
}

export function sortClosedTrades(trades: ClosedBlotterTrade[]) {
  return [...trades].sort(
    (a, b) => new Date(b.exit_ts).getTime() - new Date(a.exit_ts).getTime(),
  );
}
