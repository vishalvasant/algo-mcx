import { Brain, ChevronLeft, ChevronRight, Target } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { CommoditySnapshot, MarketSummary } from "../../types";
import {
  AI_PANEL_INDICES,
  AI_PANEL_LABELS,
  type AiPanelIndex,
  type IndexStrategyInsight,
  levelsFromSpot,
  strategyByIndex,
} from "../../utils/cockpitDummyData";

interface StrategyInsightPanelProps {
  summary: MarketSummary | null;
  activeUnderlying: string;
  commodities?: CommoditySnapshot[];
}

function formatLevel(value: number) {
  return value.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function normalizeBias(raw: string | undefined): IndexStrategyInsight["bias"] {
  const v = (raw ?? "NEUTRAL").toUpperCase();
  if (v === "BULLISH" || v === "BEARISH") return v;
  return "NEUTRAL";
}

function buildInsight(
  underlying: AiPanelIndex,
  summary: MarketSummary | null,
  spot: number | null,
): IndexStrategyInsight {
  const base = strategyByIndex[underlying];
  const scanUl = (summary?.active_underlying ?? summary?.underlying ?? "GOLD").toUpperCase();
  const isLiveScan = underlying === scanUl && summary != null;

  if (!isLiveScan) {
    if (spot != null && spot > 0) {
      const levels = levelsFromSpot(spot, underlying);
      return { ...base, ...levels };
    }
    return base;
  }

  const strategy = (summary.strategy ?? "router").replace(/_/g, " ").toUpperCase();
  const bias = normalizeBias(summary.bias_5m);
  const confidence =
    summary.confidence != null
      ? Math.min(100, Math.max(0, Math.round(summary.confidence)))
      : base.confidence;

  const reasons: string[] = [];
  if (summary.spot_vs_vwap) {
    reasons.push(`Price ${summary.spot_vs_vwap.toLowerCase()} session VWAP`);
  }
  if (summary.bias_5m) {
    reasons.push(`5m structure is ${summary.bias_5m.toLowerCase()}`);
  }
  if (summary.regime) {
    reasons.push(`Regime: ${summary.regime.replace(/_/g, " ")}`);
  }
  if (summary.block_reason) {
    reasons.push(`Block: ${summary.block_reason.replace(/_/g, " ")}`);
  }
  if (summary.kill_switch) {
    reasons.push("Kill switch is ON");
  }
  if (summary.recent_rejections?.length) {
    const last = summary.recent_rejections[0];
    reasons.push(`Last skip: ${last.tsym} — ${last.reasons.join(", ")}`);
  }
  if (!reasons.length) {
    reasons.push(...base.reasons);
  }

  const liveSpot = spot ?? summary.spot_ltp ?? null;
  const levels =
    liveSpot != null && liveSpot > 0
      ? levelsFromSpot(liveSpot, underlying)
      : {
          entryLow: base.entryLow,
          entryHigh: base.entryHigh,
          targets: base.targets,
          stopLoss: base.stopLoss,
        };

  return {
    name: strategy,
    bias,
    confidence,
    reasons: reasons.slice(0, 4),
    ...levels,
  };
}

function StrategySlide({ insight }: { insight: IndexStrategyInsight }) {
  const gaugeStyle = { "--pct": insight.confidence } as CSSProperties;
  const biasClass = `strategy-v bias-${insight.bias.toLowerCase()}`;

  return (
    <div className="strategy-slide-inner">
      <div className="strategy-meta-grid">
        <div className="strategy-meta-cell">
          <span className="strategy-k">Strategy</span>
          <strong className="strategy-v">{insight.name}</strong>
        </div>
        <div className="strategy-meta-cell align-end">
          <span className="strategy-k">Direction</span>
          <strong className={biasClass}>{insight.bias}</strong>
        </div>
      </div>

      <section className="strategy-hero">
        <p className="strategy-name">
          <Target size={15} strokeWidth={2.25} />
          <span>Confidence</span>
        </p>
        <figure className="confidence-gauge" style={gaugeStyle}>
          <p className="confidence-ring">
            <span className="confidence-value">{insight.confidence}%</span>
          </p>
          <figcaption className="confidence-label">Confidence</figcaption>
        </figure>
      </section>

      <ul className="strategy-reasons">
        {insight.reasons.map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>

      <div className="strategy-levels" aria-label="Trade levels">
        <div className="strategy-level-col entry">
          <span className="strategy-level-k">Entry</span>
          <span className="strategy-level-v mono">
            <span className="strategy-level-line">{formatLevel(insight.entryLow)}</span>
            <span className="strategy-level-line muted">to {formatLevel(insight.entryHigh)}</span>
          </span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">T1</span>
          <span className="strategy-level-v mono">{formatLevel(insight.targets[0])}</span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">T2</span>
          <span className="strategy-level-v mono">{formatLevel(insight.targets[1])}</span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">SL</span>
          <span className="strategy-level-v mono sl">{formatLevel(insight.stopLoss)}</span>
        </div>
      </div>
    </div>
  );
}

export function StrategyInsightPanel({
  summary,
  activeUnderlying,
  commodities = [],
}: StrategyInsightPanelProps) {
  const indices = AI_PANEL_INDICES;
  const carouselEnabled = indices.length > 1;
  const [slideIdx, setSlideIdx] = useState(0);
  const [paused, setPaused] = useState(false);
  const touchStart = useRef<number | null>(null);

  const spotByUnderlying = useMemo(() => {
    const map: Record<string, number | null> = {};
    for (const c of commodities) {
      map[c.underlying] = c.spot_ltp ?? null;
    }
    return map;
  }, [commodities]);

  const slides = useMemo(
    () =>
      indices.map((ul) => ({
        underlying: ul,
        label: AI_PANEL_LABELS[ul],
        insight: buildInsight(ul, summary, spotByUnderlying[ul] ?? null),
      })),
    [indices, summary, spotByUnderlying],
  );

  const goTo = useCallback((idx: number) => {
    const next = ((idx % indices.length) + indices.length) % indices.length;
    setSlideIdx(next);
  }, [indices]);

  // Top-bar index click updates carousel slide; carousel does not change chart/chain.
  useEffect(() => {
    const idx = indices.indexOf(activeUnderlying as AiPanelIndex);
    if (idx >= 0) setSlideIdx(idx);
  }, [activeUnderlying, indices]);

  useEffect(() => {
    if (!carouselEnabled || paused) return;
    const id = window.setInterval(() => {
      setSlideIdx((i) => (i + 1) % indices.length);
    }, 9000);
    return () => window.clearInterval(id);
  }, [carouselEnabled, paused, indices]);

  const onTouchStart = (x: number) => {
    touchStart.current = x;
  };

  const onTouchEnd = (x: number) => {
    if (touchStart.current == null) return;
    const delta = x - touchStart.current;
    touchStart.current = null;
    if (Math.abs(delta) < 40) return;
    goTo(slideIdx + (delta < 0 ? 1 : -1));
  };

  const current = slides[slideIdx];

  return (
    <section
      className="cockpit-panel strategy-panel ref-style"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <header className="cockpit-panel-head strategy-panel-head">
        <div className="strategy-panel-title">
          <Brain size={14} />
          <h3>Trade Panel</h3>
        </div>
        <div className="strategy-panel-head-right">
          <span className="strategy-index-pill">{current.label}</span>
          {carouselEnabled ? (
            <div className="strategy-carousel-dots" role="tablist" aria-label="Index strategy slides">
              {slides.map((slide, i) => (
                <button
                  key={slide.underlying}
                  type="button"
                  role="tab"
                  aria-selected={i === slideIdx}
                  aria-label={slide.label}
                  className={i === slideIdx ? "strategy-dot active" : "strategy-dot"}
                  onClick={() => goTo(i)}
                />
              ))}
            </div>
          ) : null}
        </div>
      </header>

      <div
        className="strategy-carousel-body"
        onTouchStart={(e) => onTouchStart(e.touches[0]?.clientX ?? 0)}
        onTouchEnd={(e) => onTouchEnd(e.changedTouches[0]?.clientX ?? 0)}
      >
        {carouselEnabled ? (
          <button
            type="button"
            className="strategy-carousel-nav prev"
            onClick={() => goTo(slideIdx - 1)}
            aria-label="Previous index"
          >
            <ChevronLeft size={14} />
          </button>
        ) : null}

        <div className="strategy-carousel-track">
          {slides.map((slide, i) => (
            <article
              key={slide.underlying}
              className={i === slideIdx ? "strategy-slide active" : "strategy-slide"}
              aria-hidden={i !== slideIdx}
            >
              <StrategySlide insight={slide.insight} />
            </article>
          ))}
        </div>

        {carouselEnabled ? (
          <button
            type="button"
            className="strategy-carousel-nav next"
            onClick={() => goTo(slideIdx + 1)}
            aria-label="Next index"
          >
            <ChevronRight size={14} />
          </button>
        ) : null}
      </div>
    </section>
  );
}
