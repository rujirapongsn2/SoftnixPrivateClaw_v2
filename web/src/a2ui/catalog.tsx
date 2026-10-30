/**
 * Softnix A2UI catalog (spec v0.9) for PrivateClaw — POC.
 *
 * Standard component names/props reuse the OFFICIAL v0.9 basic-catalog schemas
 * from @a2ui/web_core (TextApi, ButtonApi, ...) so payloads stay spec-shaped,
 * but every implementation here is ours, drawn with @astryxdesign/core — the
 * basic catalog's own Lit elements are never registered, so an agent can only
 * produce components that look like the rest of the product.
 *
 * DeltaStat / PriceChart / DataTable are Softnix extensions (A2UI v0.9 has no
 * chart or up/down arrow). Their numbers and directions are computed in code by
 * the tool that emits them, never by the model.
 *
 * Every string is rendered as a React text node — no HTML, no markdown, no URLs.
 */
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { createComponentImplementation } from "@a2ui/react/v0_9";
import {
  ButtonApi,
  CardApi,
  Catalog,
  ChoicePickerApi,
  ColumnApi,
  DividerApi,
  RowApi,
  TextApi,
  TextFieldApi,
} from "@a2ui/web_core/v0_9";
import { createContext, useContext, type ReactNode } from "react";
import { z } from "zod";
import { CARD_UI, type CardLang, SOFTNIX_CATALOG_ID, deltaGlyph } from "./protocol";

/** Whether the card's controls accept input (only the latest card does). */
export const A2uiInteractive = createContext(true);
/** Language of the card being rendered (see cardLang); picks the fixed UI strings. */
export const A2uiLang = createContext<CardLang>("th");

type ChildRef = string | { id: string; basePath: string };
const kids = (children: ChildRef[] | undefined, build: (id: string, basePath?: string) => ReactNode) =>
  (children ?? []).map((c) => (typeof c === "string" ? build(c) : build(c.id, c.basePath)));
const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));

const TEXT_SIZE: Record<string, "sm" | "base" | "lg" | "xl" | "2xl"> = { h1: "2xl", h2: "xl", h3: "lg", h4: "lg", h5: "base", caption: "sm", body: "base" };

const SText = createComponentImplementation(TextApi, ({ props }) => {
  const variant = str(props.variant || "body");
  const heading = variant.startsWith("h");
  return (
    <Text
      as={heading ? "div" : "p"}
      size={TEXT_SIZE[variant] ?? "base"}
      weight={heading ? "semibold" : undefined}
      color={variant === "caption" ? "secondary" : undefined}
      wordBreak="break-word"
    >
      {str(props.text)}
    </Text>
  );
});

const SColumn = createComponentImplementation(ColumnApi, ({ props, buildChild }) => (
  <div className="a2ui-col" data-align={str(props.align)} data-justify={str(props.justify)}>
    {kids(props.children as ChildRef[], buildChild)}
  </div>
));

const SRow = createComponentImplementation(RowApi, ({ props, buildChild }) => (
  <div className="a2ui-row" data-align={str(props.align)} data-justify={str(props.justify)}>
    {kids(props.children as ChildRef[], buildChild)}
  </div>
));

const SCard = createComponentImplementation(CardApi, ({ props, buildChild }) => (
  <div className="a2ui-card">{props.child ? buildChild(str(props.child)) : null}</div>
));

const SDivider = createComponentImplementation(DividerApi, () => <hr className="a2ui-divider" />);

/** Button label = plain text of its child Text component (spec: child is a Text). */
/** The binder hands us the child's *node* id; the spec id (and so the child
 * Text's literal) lives on the raw component model. */
function labelOf(context: {
  componentModel: { properties: Record<string, unknown> };
  surfaceComponents: { get(id: string): { properties?: Record<string, unknown> } | undefined };
}): string {
  const rawChild = context.componentModel.properties.child;
  if (typeof rawChild !== "string") return "";
  const t = context.surfaceComponents.get(rawChild)?.properties?.text;
  return typeof t === "string" ? t : "";
}

const SButton = createComponentImplementation(ButtonApi, ({ props, buildChild, context }) => {
  const interactive = useContext(A2uiInteractive);
  const childId = str(props.child);
  const lang = useContext(A2uiLang);
  const label = labelOf(context) || CARD_UI[lang].ok;
  const variant = props.variant === "primary" ? "primary" : props.variant === "borderless" ? "ghost" : "secondary";
  return (
    <Button
      label={label}
      variant={variant}
      size="sm"
      isDisabled={!interactive || props.isValid === false}
      clickAction={() => {
        if (interactive) void props.action?.();
      }}
    >
      {label ? undefined : buildChild(childId)}
    </Button>
  );
});

const SChoicePicker = createComponentImplementation(ChoicePickerApi, ({ props }) => {
  const interactive = useContext(A2uiInteractive);
  const selected: string[] = Array.isArray(props.value) ? (props.value as string[]) : [];
  const multi = props.variant === "multipleSelection";
  const toggle = (v: string) => {
    if (!interactive) return;
    const next = multi ? (selected.includes(v) ? selected.filter((s) => s !== v) : [...selected, v]) : [v];
    (props as { setValue?: (v: string[]) => void }).setValue?.(next);
  };
  return (
    <div className="a2ui-choice" role={multi ? "group" : "radiogroup"} aria-label={str(props.label) || undefined}>
      {props.label ? <Text size="sm" weight="semibold">{str(props.label)}</Text> : null}
      <div className="a2ui-choice-options">
        {(props.options ?? []).map((o) => {
          const on = selected.includes(o.value);
          return (
            <button
              key={o.value}
              type="button"
              role={multi ? "checkbox" : "radio"}
              aria-checked={on}
              disabled={!interactive}
              className={`a2ui-chip${on ? " is-on" : ""}`}
              onClick={() => toggle(o.value)}
            >
              {str(o.label)}
            </button>
          );
        })}
      </div>
    </div>
  );
});

const STextField = createComponentImplementation(TextFieldApi, ({ props }) => {
  const interactive = useContext(A2uiInteractive);
  const set = (props as { setValue?: (v: string) => void }).setValue;
  const common = { label: str(props.label), value: str(props.value), isDisabled: !interactive };
  return props.variant === "longText" ? (
    <TextArea {...common} rows={3} maxLength={1000} onChange={(v: string) => set?.(v)} />
  ) : (
    <TextInput {...common} onChange={(v: string) => set?.(v)} />
  );
});

// ---- Softnix extensions ----------------------------------------------------

const DeltaStatApi = {
  name: "DeltaStat",
  schema: z
    .object({
      label: z.string().max(200),
      value: z.number(),
      unit: z.string().max(40).optional(),
      decimals: z.number().int().min(0).max(6).optional(),
      direction: z.enum(["up", "down", "flat", "new"]).optional(),
      deltaPct: z.number().nullable().optional(),
      baseline: z.string().max(200).optional(),
      weight: z.number().optional(),
    })
    .strict(),
};

const fmt = (n: number, d = 2) => n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

const SDeltaStat = createComponentImplementation(DeltaStatApi, ({ props }) => {
  const dir = props.direction ?? "new";
  const pct = typeof props.deltaPct === "number" ? `${props.deltaPct > 0 ? "+" : ""}${fmt(props.deltaPct, 1)}%` : "";
  // Cheaper = good for a buyer, so "down" is green.
  const tone = dir === "down" ? "success" : dir === "up" ? "error" : "neutral";
  const ui = CARD_UI[useContext(A2uiLang)];
  return (
    <div className="a2ui-stat" aria-live="polite">
      <Text size="sm" color="secondary">{props.label}</Text>
      <div className="a2ui-stat-value">
        <Text size="lg" weight="semibold" hasTabularNumbers>
          {fmt(props.value, props.decimals ?? 2)}
          {props.unit ? ` ${props.unit}` : ""}
        </Text>
      </div>
      <div className="a2ui-stat-delta">
        <Badge
          variant={tone}
          label={`${deltaGlyph(dir)} ${dir === "new" ? ui.isNew : pct || (dir === "flat" ? ui.flat : "")}`.trim()}
        />
        {props.baseline ? <Text size="sm" color="secondary">{props.baseline}</Text> : null}
      </div>
    </div>
  );
});

const PriceChartApi = {
  name: "PriceChart",
  schema: z
    .object({
      title: z.string().max(200).optional(),
      unit: z.string().max(40).optional(),
      seriesLabels: z.tuple([z.string().max(40), z.string().max(40)]).optional(),
      items: z
        .array(z.object({ label: z.string().max(120), a: z.number().min(0), b: z.number().min(0) }).strict())
        .min(1)
        .max(40),
      weight: z.number().optional(),
    })
    .strict(),
};

/** Horizontal grouped bars, own SVG (no chart library, no script). */
const SPriceChart = createComponentImplementation(PriceChartApi, ({ props }) => {
  const items = props.items;
  const [la, lb] = (props.seriesLabels ?? ["input", "output"]).map(str);
  const max = Math.max(...items.flatMap((i) => [i.a, i.b]), 0.000001);
  const labelW = 190;
  const barW = 300;
  const rowH = 34;
  const width = labelW + barW + 90;
  const height = items.length * rowH + 30;
  return (
    <figure className="a2ui-chart">
      {props.title ? <Text size="sm" weight="semibold">{props.title}</Text> : null}
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={props.title ?? "chart"} width="100%">
        {items.map((it, i) => {
          const y = i * rowH + 4;
          const wa = (it.a / max) * barW;
          const wb = (it.b / max) * barW;
          return (
            <g key={i}>
              <text x={labelW - 8} y={y + 16} textAnchor="end" className="a2ui-chart-label">
                {it.label.length > 28 ? `${it.label.slice(0, 27)}…` : it.label}
              </text>
              <rect x={labelW} y={y} width={Math.max(wa, 1)} height={12} rx={2} className="a2ui-bar-a" />
              <text x={labelW + Math.max(wa, 1) + 4} y={y + 10} className="a2ui-chart-num">{fmt(it.a)}</text>
              <rect x={labelW} y={y + 14} width={Math.max(wb, 1)} height={12} rx={2} className="a2ui-bar-b" />
              <text x={labelW + Math.max(wb, 1) + 4} y={y + 24} className="a2ui-chart-num">{fmt(it.b)}</text>
            </g>
          );
        })}
        <g transform={`translate(${labelW}, ${height - 12})`}>
          <rect width={10} height={10} y={-9} className="a2ui-bar-a" />
          <text x={14} className="a2ui-chart-num">{`${la}${props.unit ? ` (${props.unit})` : ""}`}</text>
          <rect x={150} width={10} height={10} y={-9} className="a2ui-bar-b" />
          <text x={164} className="a2ui-chart-num">{lb}</text>
        </g>
      </svg>
    </figure>
  );
});

const cell = z.union([z.string().max(300), z.number(), z.null()]);
const DataTableApi = {
  name: "DataTable",
  schema: z
    .object({
      columns: z.array(z.object({ key: z.string().max(40), label: z.string().max(80), align: z.enum(["start", "end"]).optional() }).strict()).min(1).max(10),
      rows: z.array(z.record(cell)).max(60),
      weight: z.number().optional(),
    })
    .strict(),
};

const SDataTable = createComponentImplementation(DataTableApi, ({ props }) => (
  <div className="a2ui-table-wrap">
    <table className="a2ui-table">
      <thead>
        <tr>{props.columns.map((c) => <th key={c.key} data-align={c.align ?? "start"}>{c.label}</th>)}</tr>
      </thead>
      <tbody>
        {props.rows.map((r, i) => (
          <tr key={i}>
            {props.columns.map((c) => (
              <td key={c.key} data-align={c.align ?? "start"}>{str(r[c.key])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
));

export const softnixCatalog = new Catalog(SOFTNIX_CATALOG_ID, "v0.9", [
  SText,
  SColumn,
  SRow,
  SCard,
  SDivider,
  SButton,
  SChoicePicker,
  STextField,
  SDeltaStat,
  SPriceChart,
  SDataTable,
]);
