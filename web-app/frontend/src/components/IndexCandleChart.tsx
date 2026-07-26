import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Crosshair, Expand, LineChart, Minus, Paintbrush, Ruler, Save, Search, TrendingUp, Type } from "lucide-react";
import {
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { fetchChartCandles, openWatchlistStream } from "../api/client";
import type { ChartCandlesResponse, OhlcBar, Watchlist } from "../types";
import { emaSeries, toLineData, vwapSeries } from "../utils/chartIndicators";

export const CHART_INTERVALS = ["1m", "3m", "5m", "15m"] as const;
export type ChartInterval = (typeof CHART_INTERVALS)[number];

const DRAWING_TOOLS = [
  { id: "crosshair", icon: Crosshair, title: "Crosshair" },
  { id: "trend", icon: TrendingUp, title: "Trend line (soon)" },
  { id: "hline", icon: Minus, title: "Horizontal line" },
  { id: "brush", icon: Paintbrush, title: "Brush (soon)" },
  { id: "text", icon: Type, title: "Text (soon)" },
  { id: "measure", icon: Ruler, title: "Measure (soon)" },
] as const;

type DrawingToolId = (typeof DRAWING_TOOLS)[number]["id"];

const DISPLAY_INTERVALS = [
  { label: "1m", value: "1m" as ChartInterval },
  { label: "3m", value: "3m" as ChartInterval },
  { label: "5m", value: "5m" as ChartInterval },
  { label: "15m", value: "15m" as ChartInterval },
  { label: "1h", value: "15m" as ChartInterval, disabled: true },
  { label: "D", value: "15m" as ChartInterval, disabled: true },
];

const CHART_HISTORY_DAYS = 30;

function intervalToMs(interval: string): number {
  if (interval === "1m") return 60_000;
  if (interval === "3m") return 3 * 60_000;
  if (interval === "5m") return 5 * 60_000;
  return 15 * 60_000;
}

interface IndexCandleChartProps {
  underlying: string;
  displayName?: string;
  liveSpot?: number | null;
  watchlist?: Watchlist | null;
  feedMode?: string;
  height?: number;
  compact?: boolean;
  interval?: ChartInterval;
  onIntervalChange?: (interval: ChartInterval) => void;
  contractToken?: string | null;
  contractExchange?: string;
  contractTsym?: string | null;
  historyDays?: number;
}

const DEFAULT_CHART_HEIGHT = 300;
const HISTORY_POLL_MS = 30_000;
const LIVE_HISTORY_POLL_MS = 8_000;
const IST = "Asia/Kolkata";
const BAR_SPACING = 16;
const MIN_VISIBLE_BARS = 40;
const MAX_VISIBLE_BARS = 120;
const RIGHT_OFFSET = 8;

const CHART_OPTIONS = {
  layout: {
    background: { color: "transparent" },
    textColor: "#9a8f7e",
    fontFamily: '"JetBrains Mono", ui-monospace, monospace',
    fontSize: 11,
  },
  grid: {
    vertLines: { color: "rgba(42, 36, 28, 0.65)" },
    horzLines: { color: "rgba(42, 36, 28, 0.65)" },
  },
  rightPriceScale: {
    borderColor: "#2a241c",
    scaleMargins: { top: 0.08, bottom: 0.22 },
  },
  timeScale: {
    borderColor: "#2a241c",
    timeVisible: true,
    secondsVisible: false,
    fixLeftEdge: false,
    fixRightEdge: false,
    barSpacing: BAR_SPACING,
    minBarSpacing: 4,
    rightOffset: RIGHT_OFFSET,
  },
  crosshair: {
    vertLine: { color: "rgba(232, 185, 35, 0.45)", width: 1, style: 2, labelBackgroundColor: "#1c1814" },
    horzLine: { color: "rgba(232, 185, 35, 0.45)", width: 1, style: 2, labelBackgroundColor: "#1c1814" },
  },
  handleScroll: { mouseWheel: true, pressedMouseMove: true },
  handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
} as const;

function chartTimeToMs(time: Time): number {
  if (typeof time === "number") return time * 1000;
  if (typeof time === "string") return new Date(time).getTime();
  return Date.UTC(time.year, time.month - 1, time.day);
}

function formatIstChartTime(time: Time): string {
  return new Date(chartTimeToMs(time)).toLocaleString("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

function formatIstTick(time: Time): string {
  return new Date(chartTimeToMs(time)).toLocaleString("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function toChartBars(bars: OhlcBar[]): CandlestickData[] {
  const seen = new Map<number, CandlestickData>();
  for (const bar of bars) {
    const time = Math.floor(new Date(bar.ts).getTime() / 1000) as UTCTimestamp;
    seen.set(time, {
      time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    });
  }
  return [...seen.values()].sort((a, b) => (a.time as number) - (b.time as number));
}

function formatIstTime(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: IST,
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return "—";
  }
}

function patchLastBar(bars: OhlcBar[], price: number): OhlcBar[] {
  if (!bars.length) return bars;
  const next = bars.slice();
  const last = next[next.length - 1];
  next[next.length - 1] = {
    ...last,
    high: Math.max(last.high, price),
    low: Math.min(last.low, price),
    close: price,
  };
  return next;
}

function barOpenMs(ts: string, barMs: number): number {
  const ms = new Date(ts).getTime();
  return Math.floor(ms / barMs) * barMs;
}

function isTodayIstBar(ts: string): boolean {
  const fmt = (d: Date) => d.toLocaleDateString("en-CA", { timeZone: IST });
  return fmt(new Date(ts)) === fmt(new Date());
}

function applyLiveSpot(bars: OhlcBar[], price: number, barMs: number): OhlcBar[] {
  if (!bars.length) return bars;
  const nowOpen = Math.floor(Date.now() / barMs) * barMs;
  const last = bars[bars.length - 1];
  const lastOpen = barOpenMs(last.ts, barMs);
  if (nowOpen > lastOpen) {
    return [
      ...bars,
      {
        ts: new Date(nowOpen).toISOString(),
        open: price,
        high: price,
        low: price,
        close: price,
        volume: 0,
      },
    ];
  }
  return patchLastBar(bars, price);
}

function barToCandle(bar: OhlcBar): CandlestickData {
  return {
    time: Math.floor(new Date(bar.ts).getTime() / 1000) as UTCTimestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
  };
}

function applyChartViewport(chart: IChartApi, barCount: number, resetView: boolean) {
  if (barCount <= 0) return;
  const ts = chart.timeScale();
  if (!resetView) return;
  const pad = 2;
  const window = Math.min(MAX_VISIBLE_BARS, Math.max(MIN_VISIBLE_BARS, barCount + pad));
  const from = Math.max(0, barCount - window + pad);
  const to = barCount + pad;
  ts.setVisibleLogicalRange({ from, to });
}

export function IndexCandleChart({
  underlying,
  displayName,
  liveSpot = null,
  watchlist = null,
  feedMode = "offline",
  height = DEFAULT_CHART_HEIGHT,
  compact = false,
  interval: intervalProp,
  onIntervalChange,
  contractToken = null,
  contractExchange = "MCX",
  contractTsym = null,
  historyDays,
}: IndexCandleChartProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema9Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema21Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const barsRef = useRef<OhlcBar[]>([]);
  const loadCandlesRef = useRef<(() => void) | null>(null);
  const resetViewRef = useRef(true);
  const activeToolRef = useRef<DrawingToolId>("crosshair");

  const streamSpot = useMemo(() => {
    if (contractToken) {
      const row = watchlist?.items?.find((item) => item.token === contractToken);
      return row?.ltp ?? null;
    }
    const c = watchlist?.commodities?.find((row) => row.underlying === underlying);
    return c?.spot_ltp ?? null;
  }, [watchlist, underlying, contractToken]);

  const [sseSpot, setSseSpot] = useState<number | null>(null);
  const [sseFeed, setSseFeed] = useState<string | null>(null);
  const effectiveSpot = sseSpot ?? streamSpot ?? liveSpot;
  const effectiveFeed = sseFeed ?? watchlist?.feed_mode ?? feedMode;
  const effectiveSpotRef = useRef<number | null>(null);
  effectiveSpotRef.current = effectiveSpot != null ? Number(effectiveSpot) : null;
  const exchangeLabel = contractToken ? "MCX" : "NSE";
  const chartHistoryDays = historyDays ?? CHART_HISTORY_DAYS;

  const [bars, setBars] = useState<OhlcBar[]>([]);
  const [barInterval, setBarInterval] = useState<ChartInterval>("15m");
  const [chartMeta, setChartMeta] = useState<{
    fut_tsym?: string | null;
    price_source?: string | null;
  }>({});
  const [internalInterval, setInternalInterval] = useState<ChartInterval>("15m");
  const selectedInterval = intervalProp ?? internalInterval;
  const barMs = intervalToMs(selectedInterval);
  const [loading, setLoading] = useState(true);
  const [hover, setHover] = useState<OhlcBar | null>(null);
  const [indicatorVals, setIndicatorVals] = useState({ vwap: null as number | null, ema9: null as number | null, ema21: null as number | null });
  const [showIndicators, setShowIndicators] = useState(true);
  const [activeTool, setActiveTool] = useState<DrawingToolId>("crosshair");
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [symbolQuery, setSymbolQuery] = useState("");
  const [showSymbolSearch, setShowSymbolSearch] = useState(false);

  const applyOverlayData = (nextBars: OhlcBar[]) => {
    if (!nextBars.length) return;
    const closes = nextBars.map((b) => b.close);
    const vwap = vwapSeries(nextBars);
    const ema9 = emaSeries(closes, 9);
    const ema21 = emaSeries(closes, 21);
    vwapRef.current?.setData(toLineData(nextBars, vwap));
    ema9Ref.current?.setData(toLineData(nextBars, ema9));
    ema21Ref.current?.setData(toLineData(nextBars, ema21));
    const volData = nextBars
      .filter((b) => (b.volume ?? 0) > 0)
      .map((b) => ({
        time: Math.floor(new Date(b.ts).getTime() / 1000) as UTCTimestamp,
        value: b.volume ?? 0,
        color: b.close >= b.open ? "rgba(232, 185, 35, 0.45)" : "rgba(239, 68, 68, 0.45)",
      }));
    volRef.current?.setData(volData);
    const i = nextBars.length - 1;
    setIndicatorVals({
      vwap: vwap[i] ?? null,
      ema9: ema9[i] ?? null,
      ema21: ema21[i] ?? null,
    });
  };

  const setIntervalChoice = (iv: ChartInterval) => {
    if (onIntervalChange) onIntervalChange(iv);
    else setInternalInterval(iv);
    resetViewRef.current = true;
  };

  const isLiveFeed =
    effectiveFeed === "websocket" ||
    effectiveFeed === "ws" ||
    effectiveFeed === "rest";

  const marketOpen = watchlist?.market_open ?? false;

  const applyIndicatorVisibility = useCallback((visible: boolean) => {
    vwapRef.current?.applyOptions({ visible });
    ema9Ref.current?.applyOptions({ visible });
    ema21Ref.current?.applyOptions({ visible });
    volRef.current?.applyOptions({ visible });
  }, []);

  useEffect(() => {
    applyIndicatorVisibility(showIndicators);
  }, [showIndicators, applyIndicatorVisibility]);

  useEffect(() => {
    activeToolRef.current = activeTool;
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      crosshair: {
        ...CHART_OPTIONS.crosshair,
        mode: activeTool === "crosshair" ? 1 : 0,
      },
    });
  }, [activeTool]);

  useEffect(() => {
    const onFs = () => setIsFullscreen(document.fullscreenElement === cardRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  const toggleFullscreen = async () => {
    const el = cardRef.current;
    if (!el) return;
    if (document.fullscreenElement === el) {
      await document.exitFullscreen();
    } else {
      await el.requestFullscreen();
    }
  };

  const clearPriceLines = () => {
    const series = seriesRef.current;
    if (!series) return;
    for (const line of priceLinesRef.current) {
      series.removePriceLine(line);
    }
    priceLinesRef.current = [];
  };

  const selectTool = (tool: DrawingToolId) => {
    if (tool === "trend" || tool === "brush" || tool === "text" || tool === "measure") return;
    setActiveTool(tool);
  };

  useEffect(() => {
    resetViewRef.current = true;
  }, [underlying, selectedInterval, contractToken]);

  // Direct SSE hook so chart ticks even if parent re-render is delayed.
  useEffect(() => {
    setSseSpot(null);
    const stop = openWatchlistStream((wl) => {
      if (wl.feed_mode) setSseFeed(wl.feed_mode);
      if (contractToken) {
        const row = wl.items?.find((item) => item.token === contractToken);
        if (row?.ltp != null) setSseSpot(Number(row.ltp));
        return;
      }
      const row = wl.commodities?.find((c) => c.underlying === underlying);
      if (row?.spot_ltp != null) setSseSpot(Number(row.spot_ltp));
    });
    return stop;
  }, [underlying, contractToken]);

  const applyLiveToBars = useCallback(
    (nextBars: OhlcBar[]): OhlcBar[] => {
      if (!marketOpen) return nextBars;
      const price = effectiveSpotRef.current;
      if (price == null || !Number.isFinite(price) || price <= 0 || !nextBars.length) {
        return nextBars;
      }
      const last = nextBars[nextBars.length - 1];
      if (!isTodayIstBar(last.ts)) return nextBars;
      return applyLiveSpot(nextBars, price, barMs);
    },
    [barMs, marketOpen],
  );

  const pushLiveCandle = useCallback(
    (patched: OhlcBar[]) => {
      barsRef.current = patched;
      setBars(patched);
      const series = seriesRef.current;
      if (!series || !patched.length) return;
      series.update(barToCandle(patched[patched.length - 1]));
      applyOverlayData(patched);
    },
    [],
  );

  useEffect(() => {
    const price = effectiveSpot != null ? Number(effectiveSpot) : NaN;
    if (!marketOpen || !Number.isFinite(price) || price <= 0) return;
    const current = barsRef.current;
    if (!current.length) return;

    const patched = applyLiveSpot(current, price, barMs);
    const grew = patched.length > current.length;
    pushLiveCandle(patched);
    if (grew) loadCandlesRef.current?.();
  }, [effectiveSpot, barMs, bars.length, pushLiveCandle, marketOpen]);

  useEffect(() => {
    let cancelled = false;

    const applyBars = (nextBars: OhlcBar[], interval: string, meta?: ChartCandlesResponse) => {
      const patched = applyLiveToBars(nextBars);
      barsRef.current = patched;
      setBars(patched);
      setBarInterval((interval as ChartInterval) ?? selectedInterval);
      if (meta) {
        setChartMeta({
          fut_tsym: meta.fut_tsym,
          price_source: meta.price_source,
        });
      }
      setLoading(false);

      const series = seriesRef.current;
      const chart = chartRef.current;
      if (series) {
        const data = toChartBars(patched);
        if (data.length) {
          series.setData(data);
          applyOverlayData(patched);
          if (chart) {
            applyChartViewport(chart, data.length, resetViewRef.current);
            resetViewRef.current = false;
          }
        } else {
          series.setData([]);
        }
      }
    };

    const load = () => {
      fetchChartCandles(underlying, selectedInterval, chartHistoryDays, {
        token: contractToken ?? undefined,
        exchange: contractToken ? contractExchange : undefined,
        tsym: contractTsym ?? undefined,
      })
        .then((res) => {
          if (cancelled) return;
          applyBars(res.bars ?? [], res.interval ?? selectedInterval, res);
        })
        .catch(() => {
          if (!cancelled) {
            barsRef.current = [];
            setBars([]);
            setLoading(false);
          }
        });
    };

    loadCandlesRef.current = load;
    setLoading(true);
    load();
    const pollMs = isLiveFeed ? LIVE_HISTORY_POLL_MS : HISTORY_POLL_MS;
    const timerId = window.setInterval(load, pollMs);

    return () => {
      cancelled = true;
      loadCandlesRef.current = null;
      window.clearInterval(timerId);
    };
  }, [underlying, selectedInterval, isLiveFeed, applyLiveToBars, contractToken, contractExchange, contractTsym, chartHistoryDays]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      ...CHART_OPTIONS,
      width: el.clientWidth,
      height,
      localization: {
        locale: "en-IN",
        dateFormat: "dd MMM 'yy",
        timeFormatter: formatIstChartTime,
      },
      timeScale: {
        ...CHART_OPTIONS.timeScale,
        tickMarkFormatter: (time: Time) => formatIstTick(time),
      },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#e8b923",
      downColor: "#ef4444",
      borderUpColor: "#e8b923",
      borderDownColor: "#ef4444",
      wickUpColor: "#e8b923",
      wickDownColor: "#ef4444",
    });
    const vwapLine = chart.addLineSeries({ color: "#c77d2e", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const ema9Line = chart.addLineSeries({ color: "#fbbf24", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const ema21Line = chart.addLineSeries({ color: "#a855f7", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const volSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      visible: false,
    });

    chartRef.current = chart;
    seriesRef.current = series;
    vwapRef.current = vwapLine;
    ema9Ref.current = ema9Line;
    ema21Ref.current = ema21Line;
    volRef.current = volSeries;

    if (barsRef.current.length) {
      const data = toChartBars(barsRef.current);
      series.setData(data);
      applyOverlayData(barsRef.current);
      applyChartViewport(chart, data.length, true);
      resetViewRef.current = false;
    }

    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width } = entry.contentRect;
      chart.applyOptions({ width: Math.max(280, Math.floor(width)) });
    });
    ro.observe(el);

    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData.size) {
        setHover(null);
        return;
      }
      const candle = param.seriesData.get(series) as CandlestickData | undefined;
      if (!candle) {
        setHover(null);
        return;
      }
      const tSec = candle.time as number;
      const match =
        barsRef.current.find(
          (b) => Math.floor(new Date(b.ts).getTime() / 1000) === tSec,
        ) ?? null;
      setHover(
        match ?? {
          ts: new Date(tSec * 1000).toISOString(),
          open: candle.open,
          high: candle.high,
          low: candle.low,
          close: candle.close,
        },
      );
    });

    chart.subscribeClick((param) => {
      if (activeToolRef.current !== "hline" || !param.point) return;
      const price = series.coordinateToPrice(param.point.y);
      if (price == null) return;
      const line = series.createPriceLine({
        price,
        color: "#fbbf24",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: price.toFixed(2),
      });
      priceLinesRef.current.push(line);
    });

    return () => {
      ro.disconnect();
      clearPriceLines();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      vwapRef.current = null;
      ema9Ref.current = null;
      ema21Ref.current = null;
      volRef.current = null;
    };
  }, [height]);

  const label = displayName ?? underlying;
  const last = bars[bars.length - 1];
  const tip = hover ?? last;
  const contractLabel = chartMeta.fut_tsym ? ` · ${chartMeta.fut_tsym}` : "";
  const sourceLabel = chartMeta.price_source === "futures" ? "FUT" : exchangeLabel;

  return (
    <div ref={cardRef} className={`card index-chart-card${compact ? " index-chart-card--compact" : ""}${isFullscreen ? " index-chart-card--fullscreen" : ""}`}>
      <div className="chart-ref-toolbar">
        <span className="chart-ref-label">
          {label} · {selectedInterval.toUpperCase()} · {sourceLabel}{contractLabel}
          {isLiveFeed ? <span className="live-dot" title="Live WebSocket" /> : null}
          {effectiveSpot != null ? (
            <span className="chart-live-spot mono">
              {Number(effectiveSpot).toLocaleString("en-IN", { maximumFractionDigits: 2 })}
            </span>
          ) : null}
        </span>
        <div className="chart-ref-tools">
          <div className="chart-interval-tabs">
            {DISPLAY_INTERVALS.map((tab) => (
              <button
                key={tab.label}
                type="button"
                className={
                  !tab.disabled && tab.value === selectedInterval
                    ? "chart-interval active"
                    : "chart-interval"
                }
                onClick={() => !tab.disabled && setIntervalChoice(tab.value)}
                disabled={tab.disabled}
                title={tab.disabled ? "Coming soon" : undefined}
              >
                {tab.label}
              </button>
            ))}
          </div>
          {showSymbolSearch ? (
            <input
              className="chart-symbol-search"
              value={symbolQuery}
              onChange={(e) => setSymbolQuery(e.target.value)}
              placeholder="Symbol"
              aria-label="Symbol search"
            />
          ) : null}
          <button
            type="button"
            className={`chart-tool-btn${showSymbolSearch ? " active" : ""}`}
            title="Search symbol"
            onClick={() => setShowSymbolSearch((v) => !v)}
          >
            <Search size={14} />
          </button>
          <button
            type="button"
            className={`chart-tool-btn${showIndicators ? " active" : ""}`}
            title="Toggle indicators"
            onClick={() => setShowIndicators((v) => !v)}
          >
            <LineChart size={14} />
          </button>
          <button type="button" className="chart-tool-btn" title="Clear drawings" onClick={clearPriceLines}>
            <Save size={14} />
          </button>
          <button type="button" className="chart-tool-btn" title="Fullscreen" onClick={() => void toggleFullscreen()}>
            <Expand size={14} />
          </button>
        </div>
      </div>

      <div className="chart-ref-body">
        <div className="chart-drawing-toolbar" aria-label="Drawing tools">
          {DRAWING_TOOLS.map(({ id, icon: Icon, title }) => (
            <button
              key={id}
              type="button"
              className={activeTool === id ? "chart-draw-btn active" : "chart-draw-btn"}
              title={title}
              onClick={() => selectTool(id)}
              disabled={id === "trend" || id === "brush" || id === "text" || id === "measure"}
            >
              <Icon size={14} />
            </button>
          ))}
        </div>

        <div className="chart-ref-main">
      {showIndicators && indicatorVals.vwap != null ? (
        <div className="chart-indicator-pills">
          <span className="vwap">VWAP {indicatorVals.vwap.toFixed(2)}</span>
          {indicatorVals.ema9 != null ? <span className="ema9">EMA 9 {indicatorVals.ema9.toFixed(2)}</span> : null}
          {indicatorVals.ema21 != null ? <span className="ema21">EMA 21 {indicatorVals.ema21.toFixed(2)}</span> : null}
        </div>
      ) : null}

      {tip && !compact ? (
        <div className="index-chart-ohlc">
          <span className="muted">{formatIstTime(tip.ts)} IST</span>
          <span>
            O <strong className="mono">{tip.open.toFixed(2)}</strong>
          </span>
          <span>
            H <strong className="mono">{tip.high.toFixed(2)}</strong>
          </span>
          <span>
            L <strong className="mono">{tip.low.toFixed(2)}</strong>
          </span>
          <span>
            C{" "}
            <strong className={`mono ${tip.close >= tip.open ? "positive" : "negative"}`}>
              {tip.close.toFixed(2)}
            </strong>
          </span>
        </div>
      ) : null}

      <div className="index-chart-canvas">
        <div className="index-chart-wrap" ref={containerRef} />
        {loading && bars.length === 0 ? (
          <div className="index-chart-empty index-chart-empty--overlay">
            Loading {barInterval} candles…
          </div>
        ) : bars.length === 0 ? (
          <div className="index-chart-empty index-chart-empty--overlay">
            No candle data for {label} yet
          </div>
        ) : null}
      </div>
        </div>
      </div>
    </div>
  );
}
