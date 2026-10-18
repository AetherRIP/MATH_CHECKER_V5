// Chart math (chart module §10) — pure functions only, unit-tested.
//
// Everything here is deterministic geometry/labeling so the SVG components
// stay declarative and the risky bits (nice axis bounds, binning, arcs) are
// testable without a DOM. No dependency does this for us, and the bundle
// budget says it should not start.

/** Round up to a human axis bound: 1/2/2.5/5/10 x 10^n. */
export function niceCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const pow = Math.pow(10, exp);
  const frac = value / pow;
  const nice = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 2.5 ? 2.5 : frac <= 5 ? 5 : 10;
  return nice * pow;
}

/** Even tick values from 0 to a nice maximum (inclusive). */
export function niceTicks(max: number, count = 4): number[] {
  const top = niceCeil(max);
  const step = top / count;
  return Array.from({ length: count + 1 }, (_, i) => i * step);
}

/** Percentage formatter that never invents precision. */
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

/** Compact count formatter (1.2k, 3.4M) for axis labels. */
export function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
  if (abs >= 10_000) return `${(value / 1_000).toFixed(0)}k`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value * 100) / 100);
}

/** "Mar 4" style label for an ISO day key (YYYY-MM-DD). */
export function formatDayLabel(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[Number(m[2]) - 1]} ${Number(m[3])}`;
}

export interface Bin {
  from: number;
  to: number;
  count: number;
}

/**
 * Fixed-width histogram bins over [min, max]. Values outside the range land
 * in the first/last bin (clamped, never dropped silently); empty bins are
 * kept so the x-axis is continuous.
 */
export function histogramBins(values: number[], binCount: number,
                              min: number, max: number): Bin[] {
  const n = Math.max(1, Math.floor(binCount));
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    return Array.from({ length: n }, () => ({ from: min, to: min, count: 0 }));
  }
  const width = (max - min) / n;
  const bins: Bin[] = Array.from({ length: n }, (_, i) => ({
    from: min + i * width,
    to: min + (i + 1) * width,
    count: 0,
  }));
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    const idx = Math.min(n - 1, Math.max(0, Math.floor((v - min) / width)));
    bins[idx].count += 1;
  }
  return bins;
}

export interface Segment {
  /** Start angle in radians (0 = 12 o'clock, clockwise). */
  start: number;
  end: number;
  value: number;
}

/** Donut/pie segments in draw order; zero values get no arc. */
export function donutSegments(values: number[]): Segment[] {
  const total = values.reduce((n, v) => n + (Number.isFinite(v) && v > 0 ? v : 0), 0);
  if (total <= 0) return [];
  let angle = 0;
  const out: Segment[] = [];
  for (const v of values) {
    const val = Number.isFinite(v) && v > 0 ? v : 0;
    const sweep = (val / total) * Math.PI * 2;
    if (sweep > 0) out.push({ start: angle, end: angle + sweep, value: val });
    angle += sweep;
  }
  return out;
}

/** SVG polyline `d` for data points already in pixel space. */
export function linePath(points: Array<[number, number]>): string {
  return points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`)
    .join(" ");
}

/** Filled area under a line, closed at `baseline` (pixel space). */
export function areaPath(points: Array<[number, number]>, baseline: number): string {
  if (points.length === 0) return "";
  const [x0] = points[0];
  const [x1] = points[points.length - 1];
  return `${linePath(points)} L${x1.toFixed(2)},${baseline.toFixed(2)}`
    + ` L${x0.toFixed(2)},${baseline.toFixed(2)} Z`;
}

/** Point positions for one series inside a box (pixel space). */
export function scalePoints(values: number[], box: { x: number; y: number; w: number; h: number },
                            max: number): Array<[number, number]> {
  const top = max > 0 ? max : 1;
  const n = values.length;
  return values.map((v, i) => [
    box.x + (n <= 1 ? box.w / 2 : (i / (n - 1)) * box.w),
    box.y + box.h - (Math.max(0, v) / top) * box.h,
  ]);
}

/**
 * Horizontal stacked-bar segments: returns each segment's x/width in a bar
 * of total width `w`, clamped so a stack never draws past its track.
 */
export function stackSegments(values: number[], w: number): Array<{ x: number; w: number }> {
  const total = values.reduce((n, v) => n + (Number.isFinite(v) && v > 0 ? v : 0), 0);
  if (total <= 0 || w <= 0) return values.map(() => ({ x: 0, w: 0 }));
  let x = 0;
  return values.map((v) => {
    const seg = (Math.max(0, v) / total) * w;
    const out = { x, w: seg };
    x += seg;
    return out;
  });
}
