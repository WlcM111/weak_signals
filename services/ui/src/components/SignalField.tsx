/** Поле сигналов: шум у основания, порог модели, пики над порогом и синхронная развёртка. */

import { useId, useMemo, useRef } from "react";
import { cssVars } from "../lib/style";
import { useSize } from "../lib/useSize";

const SWEEP_TAIL = 96;
const CYCLE_SECONDS = { live: 6, scan: 3.4 } as const;
const PEAKS: { at: number; height: number }[] = [
  { at: 0.13, height: 0.74 },
  { at: 0.34, height: 0.6 },
  { at: 0.52, height: 0.93 },
  { at: 0.7, height: 0.68 },
  { at: 0.87, height: 0.82 },
];

function seeded(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

export function SignalField({ mode = "live", label }: { mode?: "live" | "scan"; label?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const { width, height } = useSize(ref);
  const uid = useId().replace(/:/g, "");
  const sweepId = `sweep-${uid}`;
  const peakId = `peak-${uid}`;
  const cycle = CYCLE_SECONDS[mode];

  const geometry = useMemo(() => {
    if (width < 60 || height < 60) return null;
    const random = seeded(11);
    const top = height * 0.12;
    const base = height * 0.86;
    const span = base - top;
    const threshold = base - span * 0.5;
    const step = Math.min(12, Math.max(6, width / 84));
    const peaks = PEAKS.map((peak) => ({ x: peak.at * width, top: base - span * peak.height }));
    const ticks: { x: number; y: number; amp: number; period: number; phase: number }[] = [];
    for (let x = step / 2; x < width; x += step) {
      const nearest = Math.min(...peaks.map((peak) => Math.abs(peak.x - x)));
      if (nearest < step * 0.9) continue;
      const skirt = 1 + 2.2 * Math.exp(-(nearest * nearest) / (2 * (step * 2.2) ** 2));
      const level = Math.min(0.36, (0.05 + random() * 0.12) * skirt);
      ticks.push({
        x,
        y: base - span * level,
        amp: 0.35 + random() * 0.95,
        period: 1.3 + random() * 2.1,
        phase: random() * 3,
      });
    }
    const columns = Array.from({ length: 9 }, (_, index) => ((index + 1) * width) / 10);
    return { top, base, threshold, peaks, ticks, columns };
  }, [width, height]);

  return (
    <div ref={ref} className={`sigfield sigfield--${mode}`} aria-hidden="true">
      {geometry ? (
        <svg
          key={`${width}x${height}`}
          className="sigfield__svg"
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          style={cssVars({ "--sweep-to": `${width + SWEEP_TAIL}px`, "--cycle": `${cycle}s` })}
        >
          <defs>
            <linearGradient id={sweepId} x1="0" x2="1" y1="0" y2="0">
              <stop offset="0" stopColor="#3fd6ea" stopOpacity="0" />
              <stop offset="1" stopColor="#3fd6ea" stopOpacity="0.24" />
            </linearGradient>
            <linearGradient
              id={peakId}
              gradientUnits="userSpaceOnUse"
              x1="0"
              x2="0"
              y1={geometry.base}
              y2={geometry.top}
            >
              <stop offset="0" stopColor="#b78bff" />
              <stop offset="0.42" stopColor="#b78bff" />
              <stop offset="0.62" stopColor="#3fd6ea" />
              <stop offset="1" stopColor="#8ff0fb" />
            </linearGradient>
          </defs>

          <g className="sigfield__grid">
            {geometry.columns.map((x) => (
              <line key={x} x1={x} x2={x} y1={0} y2={height} />
            ))}
          </g>
          <line className="sigfield__base" x1={0} x2={width} y1={geometry.base} y2={geometry.base} />
          <line
            className="sigfield__threshold"
            x1={0}
            x2={width}
            y1={geometry.threshold}
            y2={geometry.threshold}
          />

          <g className="sigfield__noise">
            {geometry.ticks.map((tick) => (
              <line
                key={tick.x}
                className="sigfield__tick"
                x1={tick.x}
                x2={tick.x}
                y1={geometry.base}
                y2={tick.y}
                style={cssVars({
                  "--amp": tick.amp.toFixed(2),
                  "--period": `${tick.period.toFixed(2)}s`,
                  "--phase": `${(-tick.phase).toFixed(2)}s`,
                })}
              />
            ))}
          </g>

          <g className="sigfield__sweep">
            <rect x={-SWEEP_TAIL} y={0} width={SWEEP_TAIL} height={height} fill={`url(#${sweepId})`} />
            <line x1={0} x2={0} y1={0} y2={height} />
          </g>

          {geometry.peaks.map((peak, index) => {
            const hit = (peak.x / (width + SWEEP_TAIL)) * cycle;
            return (
              <g
                key={peak.x}
                className="sigfield__peak"
                style={cssVars({ "--order": index, "--sync": `${(hit - cycle).toFixed(3)}s` })}
              >
                <line className="sigfield__glow" x1={peak.x} x2={peak.x} y1={geometry.base} y2={peak.top} />
                <line
                  className="sigfield__line"
                  x1={peak.x}
                  x2={peak.x}
                  y1={geometry.base}
                  y2={peak.top}
                  stroke={`url(#${peakId})`}
                />
                <circle className="sigfield__ping" cx={peak.x} cy={peak.top} r={5} />
                <circle className="sigfield__dot" cx={peak.x} cy={peak.top} r={3.4} />
              </g>
            );
          })}
        </svg>
      ) : null}
      {geometry && label ? (
        <span className="sigfield__label" style={{ top: geometry.threshold }}>
          {label}
        </span>
      ) : null}
    </div>
  );
}
