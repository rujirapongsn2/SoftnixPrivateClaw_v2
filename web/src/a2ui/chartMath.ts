/**
 * Pure helpers for the A2UI `Chart` component (no DOM, no React) so the layout
 * maths is unit-testable with `node --test` (test/a2ui-protocol.test.mjs).
 * The chart never invents numbers: every value it draws arrives in the payload,
 * computed server-side (chart_from_data / tools). The only arithmetic here is
 * presentation: axis scale, pie shares, stack totals, label thinning.
 */

export const CHART_TYPES = ["bar", "line", "area", "pie", "donut", "stackedBar", "hbar", "progress"] as const;
export type ChartType = (typeof CHART_TYPES)[number];

export const CHART_LIMITS = {
  maxCategories: 60,
  maxSeries: 8,
  maxPoints: 480,
  maxPieSlices: 12,
  maxLabelChars: 60,
  maxTitleChars: 200,
  maxUnitChars: 20,
};

export type Delta = { pct: number | null; direction: "up" | "down" | "flat" | "new" } | null;
export type ChartSeries = { label: string; values: (number | null)[]; deltas?: Delta[] };

/** A "nice" linear scale covering [lo, hi] with about `count` ticks. */
export function niceScale(lo: number, hi: number, count = 5): { min: number; max: number; step: number; ticks: number[] } {
  let min = Math.min(0, lo);
  let max = Math.max(0, hi);
  if (!Number.isFinite(min) || !Number.isFinite(max)) min = max = 0;
  if (max === min) max = min + 1;
  const raw = (max - min) / Math.max(1, count - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  min = Math.floor(min / step) * step;
  max = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let v = min; v <= max + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
  return { min, max, step, ticks };
}

/** Fixed-decimals number with thousands separators. */
export function formatValue(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "–";
  const d = Math.max(0, Math.min(6, Math.trunc(decimals)));
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/** Short axis label: 1.2K / 3.4M / 5B, plain under 1000. */
export function compactValue(n: number): string {
  const a = Math.abs(n);
  const f = (v: number, s: string) => `${Number(v.toFixed(a >= 1e10 || v >= 100 ? 0 : 1))}${s}`;
  if (a >= 1e9) return f(n / 1e9, "B");
  if (a >= 1e6) return f(n / 1e6, "M");
  if (a >= 1e3) return f(n / 1e3, "K");
  return Number(n.toPrecision(4)).toString();
}

/** Show every `step`-th category label so labels at ~`minPx` apart never collide. */
export function labelStep(count: number, plotWidth: number, minPx = 56): number {
  if (count <= 0) return 1;
  const fit = Math.max(1, Math.floor(plotWidth / minPx));
  return Math.max(1, Math.ceil(count / fit));
}

export function truncate(s: string, max: number): string {
  const chars = Array.from(s);
  return chars.length <= max ? s : `${chars.slice(0, Math.max(1, max - 1)).join("")}…`;
}

/** Pie/donut slices: share in % (1 decimal) and start/end angles (radians, 0 = 12 o'clock). */
export function pieSlices(values: (number | null)[]): { value: number; pct: number; start: number; end: number }[] {
  const vals = values.map((v) => (typeof v === "number" && v > 0 && Number.isFinite(v) ? v : 0));
  const total = vals.reduce((a, b) => a + b, 0);
  let acc = 0;
  return vals.map((v) => {
    const start = total ? (acc / total) * Math.PI * 2 : 0;
    acc += v;
    const end = total ? (acc / total) * Math.PI * 2 : 0;
    return { value: v, pct: total ? Math.round((v / total) * 1000) / 10 : 0, start, end };
  });
}

/** SVG path for one pie (inner = 0) or donut slice. */
export function arcPath(cx: number, cy: number, r: number, inner: number, start: number, end: number): string {
  const full = end - start >= Math.PI * 2 - 1e-9;
  if (full) end = start + Math.PI * 2 - 1e-4;
  const pt = (rad: number, ang: number) => [cx + rad * Math.sin(ang), cy - rad * Math.cos(ang)].map((n) => Number(n.toFixed(2)));
  const large = end - start > Math.PI ? 1 : 0;
  const [x1, y1] = pt(r, start);
  const [x2, y2] = pt(r, end);
  if (inner <= 0) return `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z`;
  const [x3, y3] = pt(inner, end);
  const [x4, y4] = pt(inner, start);
  return `M${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} L${x3},${y3} A${inner},${inner} 0 ${large} 0 ${x4},${y4} Z`;
}

/** Per-category stack totals (nulls count as 0). */
export function stackTotals(series: ChartSeries[], n: number): number[] {
  return Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + (typeof s.values[i] === "number" ? (s.values[i] as number) : 0), 0));
}

/** Value range the axis must cover for a chart type. */
export function valueRange(type: ChartType, series: ChartSeries[], n: number): [number, number] {
  if (type === "stackedBar") {
    const t = stackTotals(series, n);
    return [Math.min(0, ...t), Math.max(0, ...t)];
  }
  const all = series.flatMap((s) => s.values).filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  return all.length ? [Math.min(0, ...all), Math.max(0, ...all)] : [0, 1];
}

function okDelta(d: unknown): boolean {
  if (d === null) return true;
  if (typeof d !== "object" || Array.isArray(d)) return false;
  const o = d as Record<string, unknown>;
  return ["up", "down", "flat", "new"].includes(String(o.direction)) && (o.pct === null || o.pct === undefined || (typeof o.pct === "number" && Number.isFinite(o.pct)));
}

/** Structural check of Chart props (mirrors claw/a2ui/validate.py). Returns an error or null. */
export function validateChart(c: Record<string, unknown>): string | null {
  const where = `Chart "${String(c.id)}"`;
  if (!(CHART_TYPES as readonly string[]).includes(String(c.chartType))) return `${where}: chartType must be one of ${CHART_TYPES.join("|")}`;
  const cats = c.categories;
  if (!Array.isArray(cats) || cats.length < 1 || cats.length > CHART_LIMITS.maxCategories) return `${where}: categories must be 1-${CHART_LIMITS.maxCategories} labels`;
  if (!cats.every((x) => typeof x === "string" && x.length <= CHART_LIMITS.maxLabelChars)) return `${where}: categories must be strings (max ${CHART_LIMITS.maxLabelChars} chars)`;
  const series = c.series;
  if (!Array.isArray(series) || series.length < 1 || series.length > CHART_LIMITS.maxSeries) return `${where}: series must be 1-${CHART_LIMITS.maxSeries} items`;
  if (cats.length * series.length > CHART_LIMITS.maxPoints) return `${where}: too many points (max ${CHART_LIMITS.maxPoints})`;
  for (const [i, s] of series.entries()) {
    if (typeof s !== "object" || s === null || Array.isArray(s)) return `${where}: series[${i}] must be an object`;
    const so = s as Record<string, unknown>;
    if (typeof so.label !== "string" || so.label.length > CHART_LIMITS.maxLabelChars) return `${where}: series[${i}].label must be a string`;
    const vals = so.values;
    if (!Array.isArray(vals) || vals.length !== cats.length) return `${where}: series[${i}].values must have one value per category`;
    if (!vals.every((v) => v === null || (typeof v === "number" && Number.isFinite(v)))) return `${where}: series[${i}].values must be numbers or null`;
    if (so.deltas !== undefined && (!Array.isArray(so.deltas) || so.deltas.length !== cats.length)) return `${where}: series[${i}].deltas must have one entry per category`;
    if (Array.isArray(so.deltas) && !so.deltas.every(okDelta)) return `${where}: series[${i}].deltas entries must be {pct, direction up|down|flat|new} or null`;
  }
  const t = String(c.chartType);
  if (t === "pie" || t === "donut" || t === "progress") {
    if (t !== "progress" && series.length !== 1) return `${where}: ${t} takes exactly one series`;
    if (t !== "progress" && cats.length > CHART_LIMITS.maxPieSlices) return `${where}: ${t} takes at most ${CHART_LIMITS.maxPieSlices} slices`;
    const vals = (series[0] as { values: unknown[] }).values;
    if (vals.some((v) => typeof v === "number" && v < 0)) return `${where}: ${t} values must be >= 0`;
  }
  return null;
}
