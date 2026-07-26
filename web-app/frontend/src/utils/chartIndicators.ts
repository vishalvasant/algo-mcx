import type { OhlcBar } from "../types";
import type { LineData, UTCTimestamp } from "lightweight-charts";

export function emaSeries(values: number[], period: number): (number | null)[] {
  if (period < 1) return values.map(() => null);
  const k = 2 / (period + 1);
  const out: (number | null)[] = [];
  let prev: number | null = null;
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      out.push(null);
      continue;
    }
    if (prev === null) {
      const seed = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
      prev = seed;
      out.push(seed);
    } else {
      prev = values[i] * k + prev * (1 - k);
      out.push(prev);
    }
  }
  return out;
}

export function vwapSeries(bars: OhlcBar[]): (number | null)[] {
  let cumPv = 0;
  let cumVol = 0;
  return bars.map((b) => {
    const vol = b.volume ?? 0;
    const typical = (b.high + b.low + b.close) / 3;
    if (vol > 0) {
      cumPv += typical * vol;
      cumVol += vol;
      return cumPv / cumVol;
    }
    if (cumVol > 0) return cumPv / cumVol;
    return typical;
  });
}

export function toLineData(bars: OhlcBar[], values: (number | null)[]): LineData[] {
  const out: LineData[] = [];
  for (let i = 0; i < bars.length; i++) {
    const v = values[i];
    if (v == null) continue;
    out.push({
      time: Math.floor(new Date(bars[i].ts).getTime() / 1000) as UTCTimestamp,
      value: v,
    });
  }
  return out;
}

export function sessionOpenFromBars(bars: OhlcBar[]): number | null {
  return bars.length ? bars[0].open : null;
}
