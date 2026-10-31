/**
 * Generic A2UI chart (Softnix extension `Chart`): bar, line, area, pie, donut,
 * stackedBar, hbar and progress, drawn in our own SVG — no chart library, no
 * script, no HTML from the payload (every string is a React text node).
 *
 * Numbers are drawn exactly as they arrive (computed server-side); this file
 * only does presentation maths (see chartMath.ts). Accessible: role="img" with
 * a summary, an HTML legend, <title> tooltips on every mark, value labels where
 * they fit, and a "show as table" toggle with the same numbers.
 */
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  type ChartSeries,
  type ChartType,
  type Delta,
  arcPath,
  compactValue,
  formatValue,
  labelStep,
  niceScale,
  pieSlices,
  stackTotals,
  truncate,
  valueRange,
} from "./chartMath";
import { CARD_UI, type CardLang, deltaGlyph } from "./protocol";

export type ChartViewProps = {
  chartType: ChartType;
  categories: string[];
  series: ChartSeries[];
  title?: string;
  unit?: string;
  decimals?: number;
  xLabel?: string;
  yLabel?: string;
  goodDirection?: "up" | "down";
  max?: number;
  showTable?: boolean;
  /** Numbers typed by the model (inline card), not computed by chart_from_data: say so. */
  unverified?: boolean;
  lang: CardLang;
};

const PALETTE = 8;
const color = (i: number) => `var(--a2ui-c${i % PALETTE})`;

function useWidth(fallback = 560) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const set = () => setW(Math.max(220, Math.floor(el.clientWidth || fallback)));
    set();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, [fallback]);
  return [ref, w] as const;
}

function deltaText(d: Delta | undefined, lang: CardLang): string {
  if (!d) return "";
  if (d.direction === "new") return CARD_UI[lang].isNew;
  const pct = typeof d.pct === "number" ? `${d.pct > 0 ? "+" : ""}${formatValue(d.pct, 1)}%` : "";
  return `${deltaGlyph(d.direction)} ${pct}`.trim();
}

function deltaTone(d: Delta | undefined, good: "up" | "down"): string {
  if (!d || d.direction === "flat" || d.direction === "new") return "a2ui-delta-neutral";
  return d.direction === good ? "a2ui-delta-good" : "a2ui-delta-bad";
}

export function ChartView(props: ChartViewProps) {
  const { chartType, categories, series, lang } = props;
  const ui = CARD_UI[lang];
  const [ref, width] = useWidth();
  const [table, setTable] = useState(Boolean(props.showTable));
  const decimals = props.decimals ?? 2;
  const unit = props.unit ? ` ${props.unit}` : "";
  const good = props.goodDirection ?? "up";
  const fmt = (v: number | null | undefined) => formatValue(v, decimals);
  const tip = (cat: string, s: ChartSeries, i: number) => {
    const d = deltaText(s.deltas?.[i], lang);
    return `${cat} · ${s.label}: ${fmt(s.values[i])}${unit}${d ? ` (${d})` : ""}`;
  };
  const summary = `${props.title ?? ui.chart}: ${series
    .map((s) => `${s.label} — ${categories.map((c, i) => `${c} ${fmt(s.values[i])}`).join(", ")}`)
    .join("; ")}${unit}`.slice(0, 900);

  let body: ReactNode;
  if (chartType === "pie" || chartType === "donut") body = <Pie {...props} width={width} fmt={fmt} tip={tip} />;
  else if (chartType === "hbar" || chartType === "progress") body = <HBars {...props} width={width} fmt={fmt} tip={tip} good={good} />;
  else body = <Cartesian {...props} width={width} fmt={fmt} tip={tip} good={good} />;

  const legend =
    chartType !== "pie" && chartType !== "donut" && series.length > 1 ? (
      <ul className="a2ui-legend">
        {series.map((s, i) => (
          <li key={i}>
            <span className="a2ui-swatch" style={{ background: color(i) }} aria-hidden="true" />
            {s.label}
          </li>
        ))}
      </ul>
    ) : null;

  return (
    <figure className="a2ui-chart" data-type={chartType}>
      {props.title ? <Text size="sm" weight="semibold">{props.title}</Text> : null}
      <div ref={ref} className="a2ui-chart-plot" role="img" aria-label={summary}>
        {body}
      </div>
      {legend}
      {props.unverified ? <Text size="sm" color="secondary">{ui.unverified}</Text> : null}
      <div className="a2ui-chart-actions">
        <Button label={table ? ui.hideTable : ui.showTable} variant="ghost" size="sm" clickAction={() => setTable((t) => !t)} />
      </div>
      {table ? <ChartTable {...props} fmt={fmt} /> : null}
    </figure>
  );
}

type Inner = ChartViewProps & {
  width: number;
  fmt: (v: number | null | undefined) => string;
  tip: (cat: string, s: ChartSeries, i: number) => string;
  good?: "up" | "down";
};

function Cartesian({ chartType, categories, series, width, fmt, tip, xLabel, yLabel, lang, good = "up" }: Inner) {
  const n = categories.length;
  const narrow = width < 440;
  const H = narrow ? 220 : 260;
  const padL = (narrow ? 40 : 48) + (yLabel ? 16 : 0);
  const padR = 12;
  const padT = 22;
  const padB = 30 + (xLabel ? 16 : 0);
  const plotW = Math.max(40, width - padL - padR);
  const plotH = H - padT - padB;
  const [lo, hi] = valueRange(chartType, series, n);
  const sc = niceScale(lo, hi, narrow ? 4 : 5);
  const y = (v: number) => padT + plotH - ((v - sc.min) / (sc.max - sc.min)) * plotH;
  const band = plotW / n;
  const cx = (i: number) => padL + band * (i + 0.5);
  const step = labelStep(n, plotW, narrow ? 48 : 60);
  const maxChars = Math.max(4, Math.floor((band * step) / 7));
  const single = series.length === 1;
  const marks: ReactNode[] = [];

  if (chartType === "bar") {
    const gw = band * 0.72;
    const bw = gw / series.length;
    series.forEach((s, j) =>
      s.values.forEach((v, i) => {
        if (typeof v !== "number") return;
        const x = padL + band * i + (band - gw) / 2 + j * bw;
        const top = y(Math.max(v, 0));
        const h = Math.max(1, Math.abs(y(v) - y(0)));
        marks.push(
          <rect key={`b${j}-${i}`} x={x + 1} y={top} width={Math.max(1, bw - 2)} height={h} rx={2} style={{ fill: color(j) }}>
            <title>{tip(categories[i], s, i)}</title>
          </rect>,
        );
        if (single && bw >= 22 && n <= 24) {
          const label = fmt(v).length * 6.2 > band ? compactValue(v) : fmt(v);
          marks.push(<text key={`v${i}`} x={x + bw / 2} y={top - 4} textAnchor="middle" className="a2ui-chart-num">{label}</text>);
          const d = s.deltas?.[i];
          if (d && bw >= 30 && d.direction !== "new") {
            marks.push(
              <text key={`d${i}`} x={x + bw / 2} y={top - 16} textAnchor="middle" className={`a2ui-chart-delta ${deltaTone(d, good)}`}>
                {deltaText(d, lang)}
              </text>,
            );
          }
        }
      }),
    );
  } else if (chartType === "stackedBar") {
    const bw = band * 0.62;
    const pos = new Array(n).fill(0);
    const neg = new Array(n).fill(0);
    series.forEach((s, j) =>
      s.values.forEach((v, i) => {
        if (typeof v !== "number" || v === 0) return;
        const base = v > 0 ? pos[i] : neg[i];
        const next = base + v;
        if (v > 0) pos[i] = next;
        else neg[i] = next;
        const top = y(Math.max(base, next));
        const h = Math.max(1, Math.abs(y(base) - y(next)));
        marks.push(
          <rect key={`s${j}-${i}`} x={cx(i) - bw / 2} y={top} width={bw} height={h} style={{ fill: color(j) }}>
            <title>{tip(categories[i], s, i)}</title>
          </rect>,
        );
      }),
    );
    if (band >= 34 && n <= 24) {
      stackTotals(series, n).forEach((t, i) =>
        marks.push(
          <text key={`t${i}`} x={cx(i)} y={y(Math.max(t, 0)) - 4} textAnchor="middle" className="a2ui-chart-num">
            {fmt(t).length * 6.2 > band ? compactValue(t) : fmt(t)}
          </text>,
        ),
      );
    }
  } else {
    series.forEach((s, j) => {
      const segs: [number, number][][] = [];
      let cur: [number, number][] = [];
      s.values.forEach((v, i) => {
        if (typeof v === "number") cur.push([cx(i), y(v)]);
        else if (cur.length) {
          segs.push(cur);
          cur = [];
        }
      });
      if (cur.length) segs.push(cur);
      segs.forEach((pts, k) => {
        const d = pts.map(([px, py], q) => `${q ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
        if (chartType === "area" && pts.length > 1) {
          const base = y(Math.max(sc.min, 0)).toFixed(1);
          marks.push(
            <path key={`a${j}-${k}`} d={`${d} L${pts[pts.length - 1][0].toFixed(1)},${base} L${pts[0][0].toFixed(1)},${base} Z`} style={{ fill: color(j) }} className="a2ui-area" />,
          );
        }
        marks.push(<path key={`l${j}-${k}`} d={d} style={{ stroke: color(j) }} className="a2ui-line" />);
      });
      s.values.forEach((v, i) => {
        if (typeof v !== "number") return;
        marks.push(
          <circle key={`p${j}-${i}`} cx={cx(i)} cy={y(v)} r={n > 30 ? 2.5 : 3.5} style={{ fill: color(j) }} className="a2ui-dot">
            <title>{tip(categories[i], s, i)}</title>
          </circle>,
        );
        const showAll = single && n <= 12 && band >= 34;
        const last = i === s.values.length - 1 || s.values.slice(i + 1).every((x) => typeof x !== "number");
        if (showAll || (last && single)) {
          // a dip (lower than both neighbours) gets its label below the dot so the line never crosses it
          const prev = s.values[i - 1];
          const next = s.values[i + 1];
          const dip = (typeof prev !== "number" || prev > v) && (typeof next !== "number" || next > v) && (typeof prev === "number" || typeof next === "number");
          const text = fmt(v);
          marks.push(
            <text key={`pv${j}-${i}`} x={cx(i)} y={dip ? y(v) + 16 : y(v) - 8} textAnchor={last && !showAll ? "end" : "middle"} className="a2ui-chart-num">
              {text.length > 8 || text.length * 6.2 > band ? compactValue(v) : text}
            </text>,
          );
        }
      });
    });
  }

  return (
    <svg width={width} height={H} viewBox={`0 0 ${width} ${H}`} className="a2ui-chart-svg">
      {sc.ticks.map((t) => (
        <g key={t}>
          <line x1={padL} x2={padL + plotW} y1={y(t)} y2={y(t)} className={t === 0 ? "a2ui-axis" : "a2ui-grid"} />
          <text x={padL - 6} y={y(t) + 4} textAnchor="end" className="a2ui-chart-num">{compactValue(t)}</text>
        </g>
      ))}
      {categories.map((c, i) =>
        i % step === 0 ? (
          <text key={`x${i}`} x={cx(i)} y={padT + plotH + 16} textAnchor="middle" className="a2ui-chart-label">
            <title>{c}</title>
            {truncate(c, maxChars)}
          </text>
        ) : null,
      )}
      {xLabel ? (
        <text x={padL + plotW / 2} y={H - 4} textAnchor="middle" className="a2ui-chart-axis-title">{truncate(xLabel, 60)}</text>
      ) : null}
      {yLabel ? (
        <text transform={`translate(12 ${padT + plotH / 2}) rotate(-90)`} textAnchor="middle" className="a2ui-chart-axis-title">{truncate(yLabel, 40)}</text>
      ) : null}
      {marks}
    </svg>
  );
}

function HBars({ chartType, categories, series, width, fmt, tip, max, lang, good = "up" }: Inner) {
  const progress = chartType === "progress";
  const s0 = series[0];
  const narrow = width < 440;
  const sideW = Math.min(Math.floor(width * (narrow ? 0.34 : 0.32)), 180);
  // On a phone, long labels (model ids, names) go on their own line above the bar instead of being cut short.
  const above = narrow && categories.some((c) => Array.from(c).length > Math.floor(sideW / 7));
  const labelW = above ? 0 : sideW;
  const labelH = above ? 16 : 0;
  const valueW = narrow ? 78 : 110;
  const barMax = Math.max(30, width - labelW - valueW - 12);
  const shown = progress ? [s0] : series;
  const barH = progress ? 12 : shown.length > 2 ? 9 : 12;
  const rowH = (progress ? 34 : shown.length * (barH + 2) + 14) + labelH;
  const all = shown.flatMap((s) => s.values).filter((v): v is number => typeof v === "number");
  const scaleMax = progress && typeof max === "number" && max > 0 ? max : Math.max(...all.map(Math.abs), 1e-9);
  const H = categories.length * rowH + 6;
  const maxChars = above ? Math.max(10, Math.floor(width / 6.6)) : Math.max(6, Math.floor(labelW / 7));
  return (
    <svg width={width} height={H} viewBox={`0 0 ${width} ${H}`} className="a2ui-chart-svg">
      {categories.map((c, i) => {
        const top = i * rowH + 4 + labelH;
        return (
          <g key={i}>
            <text
              x={above ? 0 : labelW - 8}
              y={above ? top - 4 : top + (progress ? 15 : rowH / 2)}
              textAnchor={above ? "start" : "end"}
              className="a2ui-chart-label"
            >
              <title>{c}</title>
              {truncate(c, maxChars)}
            </text>
            {progress ? <rect x={labelW} y={top + 6} width={barMax} height={barH} rx={6} className="a2ui-track" /> : null}
            {shown.map((s, j) => {
              const v = s.values[i];
              if (typeof v !== "number") return null;
              const w = Math.max(1, Math.min(1, Math.abs(v) / scaleMax) * barMax);
              const yy = progress ? top + 6 : top + 2 + j * (barH + 2);
              const d = s.deltas?.[i];
              const pctOfMax = progress ? ` (${formatValue((v / scaleMax) * 100, 1)}%)` : "";
              const valText = `${fmt(v)}${pctOfMax}`;
              return (
                <g key={j}>
                  <rect x={labelW} y={yy} width={w} height={barH} rx={progress ? 6 : 2} style={{ fill: color(progress ? 0 : j) }}>
                    <title>{tip(c, s, i)}</title>
                  </rect>
                  <text x={labelW + (progress ? barMax : w) + 6} y={yy + barH - 2} className="a2ui-chart-num">
                    {narrow && valText.length > 12 ? compactValue(v) + pctOfMax : valText}
                    {d && shown.length === 1 && d.direction !== "new" ? (
                      <tspan className={deltaTone(d, good)} dx={4}>{deltaText(d, lang)}</tspan>
                    ) : null}
                  </text>
                </g>
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

function Pie({ chartType, categories, series, width, fmt, tip, lang, unit }: Inner) {
  const s = series[0];
  const slices = pieSlices(s.values);
  const size = Math.min(width, 240);
  const r = size / 2 - 6;
  const inner = chartType === "donut" ? r * 0.58 : 0;
  const total = slices.reduce((a, x) => a + x.value, 0);
  return (
    <div className="a2ui-pie">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="a2ui-chart-svg">
        {slices.map((sl, i) =>
          sl.value > 0 ? (
            <path key={i} d={arcPath(size / 2, size / 2, r, inner, sl.start, sl.end)} style={{ fill: color(i) }} className="a2ui-slice">
              <title>{`${tip(categories[i], s, i)} · ${formatValue(sl.pct, 1)}%`}</title>
            </path>
          ) : null,
        )}
        {inner ? (
          <>
            <text x={size / 2} y={size / 2 - 2} textAnchor="middle" className="a2ui-chart-num">{CARD_UI[lang].total}</text>
            <text x={size / 2} y={size / 2 + 16} textAnchor="middle" className="a2ui-pie-total">{compactValue(total)}</text>
          </>
        ) : null}
      </svg>
      <ul className="a2ui-legend a2ui-legend-col">
        {categories.map((c, i) => (
          <li key={i}>
            <span className="a2ui-swatch" style={{ background: color(i) }} aria-hidden="true" />
            <span className="a2ui-legend-label">{c}</span>
            <span className="a2ui-legend-num">
              {fmt(s.values[i])}
              {unit ? ` ${unit}` : ""} · {formatValue(slices[i].pct, 1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ChartTable({ categories, series, fmt, lang, xLabel }: ChartViewProps & { fmt: (v: number | null | undefined) => string }) {
  const ui = CARD_UI[lang];
  const anyDelta = series.some((s) => s.deltas?.some(Boolean));
  return (
    <div className="a2ui-table-wrap">
      <table className="a2ui-table">
        <thead>
          <tr>
            <th>{xLabel || ui.category}</th>
            {series.map((s, j) => (
              <th key={j} data-align="end">{s.label}</th>
            ))}
            {anyDelta ? series.map((s, j) => <th key={`d${j}`} data-align="end">{series.length > 1 ? `${ui.change} ${s.label}` : ui.change}</th>) : null}
          </tr>
        </thead>
        <tbody>
          {categories.map((c, i) => (
            <tr key={i}>
              <td>{c}</td>
              {series.map((s, j) => (
                <td key={j} data-align="end">{fmt(s.values[i])}</td>
              ))}
              {anyDelta ? series.map((s, j) => <td key={`d${j}`} data-align="end">{deltaText(s.deltas?.[i], lang) || "–"}</td>) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
