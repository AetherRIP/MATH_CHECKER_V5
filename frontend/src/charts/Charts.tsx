// Zero-dependency SVG charts (chart inventory §10).
//
// Deliberate choice (§14): the app ships no chart library. These components
// draw the handful of chart forms the dashboard/analytics actually need,
// using the same design tokens as everything else. Every chart:
//   * answers one question (its title says which),
//   * carries `role="img"` + a written summary for screen readers,
//   * includes a visually hidden data table with the real numbers,
//   * shows a calm empty state instead of a misleading blank axis,
//   * animates entrance only (see styles.css), never continuously.

import { type ReactNode } from "react";
import {
  areaPath, compactNumber, donutSegments, formatDayLabel, linePath, scalePoints,
  stackSegments, type Bin,
} from "./format";

export type ChartTone = "accent" | "ok" | "warn" | "err" | "review" | "mute";

const TONE_VAR: Record<ChartTone, string> = {
  accent: "var(--accent)",
  ok: "var(--ok)",
  warn: "var(--warn)",
  err: "var(--err)",
  review: "var(--review)",
  mute: "var(--text-faint)",
};

export function ChartFrame({ title, hint, legend, summary, rows, children }: {
  title: string; hint?: ReactNode;
  legend?: { label: string; tone: ChartTone }[];
  summary: string;
  rows?: Array<[string, string]>;
  children: ReactNode;
}) {
  return (
    <div className="chart-card">
      <div className="chart-title">
        {title}
        {hint != null && <span className="hint">{hint}</span>}
      </div>
      <div role="img" aria-label={summary}>
        {children}
      </div>
      {legend && (
        <div className="chart-legend">
          {legend.map((k) => (
            <span className="key" key={k.label}>
              <span className="swatch" style={{ background: TONE_VAR[k.tone] }} />
              {k.label}
            </span>
          ))}
        </div>
      )}
      {rows && (
        <table className="visually-hidden">
          <caption>{title} — data</caption>
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}><th scope="row">{k}</th><td>{v}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function ChartEmpty({ children }: { children: ReactNode }) {
  return <div className="chart-empty">{children}</div>;
}

/* --------------------------------------------------------------- line */

export function LineChart({ points, tone = "accent", height = 160,
  valueFormat = compactNumber, summary, title, hint }: {
  points: { label: string; value: number }[];
  tone?: ChartTone;
  height?: number;
  valueFormat?: (v: number) => string;
  summary: string; title: string; hint?: ReactNode;
}) {
  const values = points.map((p) => p.value);
  const max = Math.max(1, ...values);
  const box = { x: 36, y: 8, w: 10, h: height - 30 };
  // viewBox width is fixed and scaled by CSS; 360 keeps label density sane.
  const W = 360;
  box.w = W - box.x - 8;
  const pts = scalePoints(values, box, max);

  return (
    <ChartFrame title={title} hint={hint} summary={summary}
      rows={points.map((p) => [p.label, valueFormat(p.value)])}>
      {points.length === 0 ? (
        <ChartEmpty>No data yet.</ChartEmpty>
      ) : (
        <svg className="chart-svg" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
          {[0, 0.5, 1].map((f) => (
            <line key={f} className="chart-grid-line"
              x1={box.x} x2={box.x + box.w}
              y1={box.y + box.h * f} y2={box.y + box.h * f} />
          ))}
          <text className="chart-axis" x={4} y={box.y + 4}>{valueFormat(max)}</text>
          <text className="chart-axis" x={4} y={box.y + box.h + 3}>{valueFormat(0)}</text>
          <path d={areaPath(pts, box.y + box.h)} fill={TONE_VAR[tone]} opacity={0.10} />
          <path d={linePath(pts)} fill="none" stroke={TONE_VAR[tone]}
            strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round"
            vectorEffect="non-scaling-stroke" />
          {points.map((p, i) => (
            <circle key={p.label} cx={pts[i][0]} cy={pts[i][1]} r={2.5}
              fill={TONE_VAR[tone]}>
              <title>{`${p.label}: ${valueFormat(p.value)}`}</title>
            </circle>
          ))}
          <text className="chart-axis" x={box.x} y={height - 6}>{points[0]?.label}</text>
          {points.length > 1 && (
            <text className="chart-axis" x={box.x + box.w} y={height - 6} textAnchor="end">
              {points[points.length - 1].label}
            </text>
          )}
        </svg>
      )}
    </ChartFrame>
  );
}

/* ---------------------------------------------------------------- bar */

export function BarChart({ bars, tone = "accent", height = 160,
  valueFormat = compactNumber, summary, title, hint }: {
  bars: { label: string; value: number; tone?: ChartTone }[];
  tone?: ChartTone;
  height?: number;
  valueFormat?: (v: number) => string;
  summary: string; title: string; hint?: ReactNode;
}) {
  const values = bars.map((b) => b.value);
  const max = Math.max(1, ...values);
  const W = 360;
  const box = { x: 36, y: 8, w: W - 44, h: height - 30 };
  const slot = bars.length ? box.w / bars.length : 0;
  const barW = Math.max(3, slot * 0.62);

  return (
    <ChartFrame title={title} hint={hint} summary={summary}
      rows={bars.map((b) => [b.label, valueFormat(b.value)])}>
      {bars.length === 0 ? (
        <ChartEmpty>No data yet.</ChartEmpty>
      ) : (
        <svg className="chart-svg" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
          <line className="chart-grid-line" x1={box.x} x2={box.x + box.w}
            y1={box.y + box.h} y2={box.y + box.h} />
          <text className="chart-axis" x={4} y={box.y + 4}>{valueFormat(max)}</text>
          {bars.map((b, i) => {
            const h = (Math.max(0, b.value) / max) * box.h;
            const x = box.x + i * slot + (slot - barW) / 2;
            return (
              <rect className="chart-bar" key={`${b.label}:${i}`}
                x={x} y={box.y + box.h - h} width={barW} height={Math.max(1, h)}
                rx={Math.min(3, barW / 2)} fill={TONE_VAR[b.tone ?? tone]}>
                <title>{`${b.label}: ${valueFormat(b.value)}`}</title>
              </rect>
            );
          })}
          <text className="chart-axis" x={box.x} y={height - 6}>{bars[0]?.label}</text>
          {bars.length > 1 && (
            <text className="chart-axis" x={box.x + box.w} y={height - 6} textAnchor="end">
              {bars[bars.length - 1].label}
            </text>
          )}
        </svg>
      )}
    </ChartFrame>
  );
}

/* ------------------------------------------------------ stacked track */

/** One horizontal 100% stacked bar (status distributions). */
export function StackedBar({ segments, summary, title, hint, height = 18 }: {
  segments: { label: string; value: number; tone: ChartTone }[];
  summary: string; title: string; hint?: ReactNode; height?: number;
}) {
  const total = segments.reduce((n, s) => n + s.value, 0);
  const parts = stackSegments(segments.map((s) => s.value), 100);
  return (
    <ChartFrame title={title} hint={hint} summary={summary} legend={
      segments.filter((s) => s.value > 0).map((s) => ({ label: `${s.label} (${s.value})`, tone: s.tone }))
    } rows={segments.map((s) => [s.label, String(s.value)])}>
      {total === 0 ? (
        <ChartEmpty>Nothing to show yet.</ChartEmpty>
      ) : (
        <svg className="chart-svg" viewBox={`0 0 100 ${height}`} preserveAspectRatio="none">
          {segments.map((s, i) => parts[i].w > 0 && (
            <rect key={s.label} x={parts[i].x} y={0} width={parts[i].w} height={height}
              fill={TONE_VAR[s.tone]}>
              <title>{`${s.label}: ${s.value}`}</title>
            </rect>
          ))}
        </svg>
      )}
    </ChartFrame>
  );
}

/* --------------------------------------------------------------- donut */

export function Donut({ slices, size = 120, thickness = 16, centerLabel,
  summary, title, hint }: {
  slices: { label: string; value: number; tone: ChartTone }[];
  size?: number; thickness?: number; centerLabel?: ReactNode;
  summary: string; title: string; hint?: ReactNode;
}) {
  const segs = donutSegments(slices.map((s) => s.value));
  const r = (size - thickness) / 2;
  const c = size / 2;
  const arc = (start: number, end: number) => {
    const x0 = c + r * Math.sin(start), y0 = c - r * Math.cos(start);
    const x1 = c + r * Math.sin(end), y1 = c - r * Math.cos(end);
    const large = end - start > Math.PI ? 1 : 0;
    return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
  };

  return (
    <ChartFrame title={title} hint={hint} summary={summary} legend={
      slices.filter((s) => s.value > 0).map((s) => ({ label: `${s.label} (${s.value})`, tone: s.tone }))
    } rows={slices.map((s) => [s.label, String(s.value)])}>
      {segs.length === 0 ? (
        <ChartEmpty>Nothing to show yet.</ChartEmpty>
      ) : (
        <svg className="chart-svg" width={size} height={size}
          viewBox={`0 0 ${size} ${size}`} style={{ width: size, height: size }}>
          {segs.map((seg, i) => (
            <path key={i} d={arc(seg.start, seg.end)} fill="none"
              stroke={TONE_VAR[slices.filter((s) => s.value > 0)[i].tone]}
              strokeWidth={thickness} strokeLinecap="butt">
              <title>{`${slices.filter((s) => s.value > 0)[i].label}: ${seg.value}`}</title>
            </path>
          ))}
          {centerLabel != null && (
            <text x={c} y={c + 4} textAnchor="middle"
              style={{ fontSize: thickness, fontWeight: 700, fill: "var(--text)" }}>
              {centerLabel}
            </text>
          )}
        </svg>
      )}
    </ChartFrame>
  );
}

/* ----------------------------------------------------------- histogram */

export function Histogram({ bins, tone = "accent", height = 160, binLabel,
  summary, title, hint }: {
  bins: Bin[];
  tone?: ChartTone;
  height?: number;
  binLabel?: (b: Bin) => string;
  summary: string; title: string; hint?: ReactNode;
}) {
  const max = Math.max(1, ...bins.map((b) => b.count));
  const W = 360;
  const box = { x: 36, y: 8, w: W - 44, h: height - 30 };
  const slot = bins.length ? box.w / bins.length : 0;
  const label = binLabel ?? ((b: Bin) => `${Math.round(b.from)}–${Math.round(b.to)}`);

  return (
    <ChartFrame title={title} hint={hint} summary={summary}
      rows={bins.map((b) => [label(b), String(b.count)])}>
      {bins.length === 0 ? (
        <ChartEmpty>No marked work yet.</ChartEmpty>
      ) : (
        <svg className="chart-svg" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
          <line className="chart-grid-line" x1={box.x} x2={box.x + box.w}
            y1={box.y + box.h} y2={box.y + box.h} />
          <text className="chart-axis" x={4} y={box.y + 4}>{max}</text>
          {bins.map((b, i) => {
            const h = (b.count / max) * box.h;
            return (
              <rect className="chart-bar" key={i}
                x={box.x + i * slot + 1} y={box.y + box.h - h}
                width={Math.max(1, slot - 2)} height={Math.max(1, h)}
                fill={TONE_VAR[tone]}>
                <title>{`${label(b)}: ${b.count}`}</title>
              </rect>
            );
          })}
          <text className="chart-axis" x={box.x} y={height - 6}>
            {bins[0] ? label(bins[0]) : ""}</text>
          {bins.length > 1 && (
            <text className="chart-axis" x={box.x + box.w} y={height - 6} textAnchor="end">
              {label(bins[bins.length - 1])}
            </text>
          )}
        </svg>
      )}
    </ChartFrame>
  );
}

/* ------------------------------------------------------------ sparkline */

export function Sparkline({ values, tone = "accent", width = 90, height = 28 }: {
  values: number[]; tone?: ChartTone; width?: number; height?: number;
}) {
  if (values.length === 0) return null;
  const max = Math.max(1, ...values);
  const pts = scalePoints(values, { x: 2, y: 3, w: width - 4, h: height - 6 }, max);
  return (
    <svg width={width} height={height} aria-hidden="true"
      style={{ display: "block" }}>
      <path d={linePath(pts)} fill="none" stroke={TONE_VAR[tone]}
        strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* --------------------------------------------------------------- gauge */

/** Usage-limit gauge: used vs cap, with the over-limit part in error tone. */
export function Gauge({ used, cap, label, detail }: {
  used: number; cap: number; label: ReactNode; detail?: ReactNode;
}) {
  const ratio = cap > 0 ? Math.min(1.25, used / cap) : 0;
  const over = cap > 0 && used >= cap;
  return (
    <div className="chart-card">
      <div className="row between small">
        <span>{label}</span>
        <span className="faint">{detail}</span>
      </div>
      <div className="progress" role="img"
        aria-label={`${label}: ${compactNumber(used)} of ${compactNumber(cap)}`}>
        <div className="bar" style={{
          width: `${Math.min(100, ratio * 100)}%`,
          background: over ? "var(--err)" : undefined,
          transition: "width var(--speed-slow) var(--ease-out)",
        }} />
      </div>
    </div>
  );
}

export { formatDayLabel, compactNumber };
