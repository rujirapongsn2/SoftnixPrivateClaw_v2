/**
 * A2UI (spec v0.9.1) payload handling for PrivateClaw — POC.
 *
 * Pure, DOM-free and dependency-free so it can be unit-tested with `node --test`
 * (see test/a2ui-protocol.test.mjs). It is the FIRST gate every agent-authored
 * UI passes through; the official renderer (@a2ui/web_core MessageProcessor)
 * validates again against the catalog's zod schemas when it renders.
 *
 * Accepted input (model output is messy, so both are tolerated):
 *   1. The canonical v0.9 message list:
 *      [{version:"v0.9", createSurface:{surfaceId, catalogId}},
 *       {version:"v0.9", updateComponents:{surfaceId, components:[...]}},
 *       {version:"v0.9", updateDataModel:{surfaceId, path:"/", value:{...}}}]
 *      (also wrapped as {messages:[...]}).
 *   2. A compact form: {surfaceId?, components:[...], data?:{...}}.
 * Both normalize to the canonical list, pinned to the Softnix catalog id —
 * an agent can never pick a catalog (and so a component set) we did not ship.
 *
 * Security posture: only allow-listed component names; every string is capped
 * and rendered as plain text by the catalog (no HTML/markdown); actions may only
 * be `event` actions (they become an ordinary chat message the user can see) —
 * client `functionCall`s such as openUrl are rejected outright.
 */

export const A2UI_VERSION = "v0.9";
export const SOFTNIX_CATALOG_ID = "https://softnix.ai/a2ui/catalogs/privateclaw/v0.9.json";

/** Component names the Softnix catalog implements (keep in step with catalog.tsx
 * and claw/a2ui/validate.py). */
export const ALLOWED_COMPONENTS = [
  "Text",
  "Column",
  "Row",
  "Card",
  "Divider",
  "Button",
  "ChoicePicker",
  "TextField",
  "DeltaStat",
  "PriceChart",
  "DataTable",
] as const;

export const LIMITS = {
  maxPayloadChars: 60_000,
  maxComponents: 120,
  maxStringChars: 2_000,
  maxDepth: 12,
  maxMessageChars: 1_500,
};

export type A2uiMessage = Record<string, unknown> & { version: string };
export type ParseResult =
  | { ok: true; surfaceId: string; messages: A2uiMessage[] }
  | { ok: false; error: string };

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

function walkStrings(v: unknown, depth: number, path: string): string | null {
  if (depth > LIMITS.maxDepth) return `${path}: nested too deep`;
  if (typeof v === "string") return v.length > LIMITS.maxStringChars ? `${path}: string too long` : null;
  if (Array.isArray(v)) {
    for (let i = 0; i < v.length; i++) {
      const e = walkStrings(v[i], depth + 1, `${path}[${i}]`);
      if (e) return e;
    }
    return null;
  }
  if (isObj(v)) {
    for (const [k, val] of Object.entries(v)) {
      if (k === "__proto__" || k === "constructor" || k === "prototype") return `${path}.${k}: forbidden key`;
      const e = walkStrings(val, depth + 1, `${path}.${k}`);
      if (e) return e;
    }
  }
  return null;
}

function checkAction(action: unknown, where: string): string | null {
  if (!isObj(action)) return `${where}.action must be an object`;
  if ("functionCall" in action) return `${where}.action.functionCall is not allowed (only event actions)`;
  const ev = action.event;
  if (!isObj(ev) || typeof ev.name !== "string" || !ev.name) return `${where}.action.event.name is required`;
  return null;
}

/** Validate a component list against the Softnix catalog's structural rules. */
export function validateComponents(components: unknown): string | null {
  if (!Array.isArray(components) || components.length === 0) return "components must be a non-empty array";
  if (components.length > LIMITS.maxComponents) return `too many components (max ${LIMITS.maxComponents})`;
  const ids = new Set<string>();
  for (const [i, c] of components.entries()) {
    if (!isObj(c)) return `components[${i}] must be an object`;
    if (typeof c.id !== "string" || !c.id) return `components[${i}].id is required`;
    if (ids.has(c.id)) return `duplicate component id "${c.id}"`;
    ids.add(c.id);
    if (typeof c.component !== "string" || !(ALLOWED_COMPONENTS as readonly string[]).includes(c.component)) {
      return `components[${i}] ("${c.id}"): component "${String(c.component)}" is not in the Softnix catalog`;
    }
    if (c.component === "Button") {
      const e = checkAction(c.action, `components[${i}]`);
      if (e) return e;
    }
  }
  if (!ids.has("root")) return 'one component must have id "root"';
  for (const [i, c] of (components as Obj[]).entries()) {
    const refs: unknown[] = [];
    if (typeof c.child === "string") refs.push(c.child);
    if (Array.isArray(c.children)) refs.push(...c.children);
    for (const r of refs) {
      if (typeof r !== "string" || !ids.has(r)) return `components[${i}] ("${c.id}") references missing child "${String(r)}"`;
    }
  }
  return null;
}

function messagesFrom(raw: unknown): unknown[] | null {
  if (Array.isArray(raw)) return raw;
  if (isObj(raw) && Array.isArray(raw.messages)) return raw.messages;
  return null;
}

/** Parse + normalize + validate. Never throws. */
export function parseA2ui(input: string | unknown, fallbackSurfaceId = "surface"): ParseResult {
  let raw: unknown = input;
  if (typeof input === "string") {
    if (input.length > LIMITS.maxPayloadChars) return { ok: false, error: "payload too large" };
    try {
      raw = JSON.parse(input);
    } catch (e) {
      return { ok: false, error: `invalid JSON: ${(e as Error).message}` };
    }
  }
  const strErr = walkStrings(raw, 0, "$");
  if (strErr) return { ok: false, error: strErr };

  let surfaceId = fallbackSurfaceId;
  let components: unknown;
  let data: unknown;
  const list = messagesFrom(raw);
  if (list) {
    for (const m of list) {
      if (!isObj(m)) return { ok: false, error: "every message must be an object" };
      if (isObj(m.createSurface) && typeof m.createSurface.surfaceId === "string") surfaceId = m.createSurface.surfaceId;
      if (isObj(m.updateComponents)) {
        const comps = m.updateComponents.components;
        components = Array.isArray(components) && Array.isArray(comps) ? [...components, ...comps] : comps;
      }
      if (isObj(m.updateDataModel)) {
        const p = m.updateDataModel.path;
        if (p !== undefined && p !== "/" && p !== "") return { ok: false, error: "updateDataModel.path must be \"/\" in this POC" };
        data = m.updateDataModel.value;
      }
      if ("deleteSurface" in m) return { ok: false, error: "deleteSurface is not supported in chat cards" };
    }
  } else if (isObj(raw) && Array.isArray(raw.components)) {
    if (typeof raw.surfaceId === "string") surfaceId = raw.surfaceId;
    components = raw.components;
    data = raw.data ?? raw.dataModel;
  } else {
    return { ok: false, error: "expected an A2UI v0.9 message list or {components:[...]}" };
  }
  const compErr = validateComponents(components);
  if (compErr) return { ok: false, error: compErr };
  if (data !== undefined && !isObj(data)) return { ok: false, error: "data model must be an object" };
  surfaceId = surfaceId.replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 64) || fallbackSurfaceId;

  const messages: A2uiMessage[] = [
    { version: A2UI_VERSION, createSurface: { surfaceId, catalogId: SOFTNIX_CATALOG_ID } },
    { version: A2UI_VERSION, updateComponents: { surfaceId, components } },
  ];
  if (data !== undefined) messages.push({ version: A2UI_VERSION, updateDataModel: { surfaceId, path: "/", value: data } });
  return { ok: true, surfaceId, messages };
}

export type CardLang = "th" | "en";

/** Prefix of the chat message a card click sends, in the card's language. The
 * agent recognises either (skill `a2ui-cards`), so old "[การ์ด]" chats still work. */
export const CARD_PREFIX: Record<CardLang, string> = { th: "[การ์ด]", en: "[Card]" };

/** Fixed UI strings around a card, in the card's language. */
export const CARD_UI: Record<CardLang, { badge: string; sent: string; ok: string; isNew: string; flat: string }> = {
  th: { badge: "การ์ดสร้างโดย AI", sent: "ส่งคำตอบแล้ว", ok: "ตกลง", isNew: "ใหม่", flat: "คงที่" },
  en: { badge: "AI-generated card", sent: "Answer sent", ok: "OK", isNew: "new", flat: "flat" },
};

const THAI_RE = /[\u0E00-\u0E7F]/;

/** Language of a card = Thai if any of its text is Thai, else English. The model
 * writes the card in the user's language, so this follows the user too. */
export function cardLang(messages: unknown): CardLang {
  try {
    return THAI_RE.test(JSON.stringify(messages) ?? "") ? "th" : "en";
  } catch {
    return "en";
  }
}

/** Turn a dispatched action into the plain chat message sent back to the agent.
 * The user sees exactly this text in the transcript (no hidden channel). */
export function actionToMessage(
  action: { name: string; context?: Record<string, unknown>; userMessage?: string },
  lang: CardLang = "th",
): string {
  const lines: string[] = [];
  const head = (action.userMessage || action.name || "").toString().trim();
  lines.push(`${CARD_PREFIX[lang] ?? CARD_PREFIX.th} ${head}`);
  for (const [k, v] of Object.entries(action.context ?? {})) {
    if (v === undefined || v === null || v === "") continue;
    const text = Array.isArray(v) ? v.map(String).join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v);
    if (!text.trim()) continue;
    lines.push(`- ${k}: ${text}`);
  }
  return lines.join("\n").slice(0, LIMITS.maxMessageChars);
}

/** Price delta direction → arrow glyph (direction is computed server-side). */
export function deltaGlyph(direction: string | undefined): string {
  switch (direction) {
    case "up":
      return "▲";
    case "down":
      return "▼";
    case "flat":
      return "■";
    default:
      return "●";
  }
}
