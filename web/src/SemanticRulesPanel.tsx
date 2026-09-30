import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Icon } from "@astryxdesign/core/Icon";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { api, type SemanticRule, type SemanticRuleAction, type SemanticRuleDraft, type SemanticRuleGroup } from "./shared-api";
import { useT } from "./branding";
import { ErrorText } from "./ErrorText";
import { ShieldAlert, HeartHandshake, Building2, ScanSearch, Layers, Plus, Pencil, Trash2, RotateCcw } from "lucide-react";

// Same page anatomy as the keyword/regex tab (intro + "Add rule", then one Card per
// rule with badges, a switch, Edit and Delete). What differs is the vocabulary of a
// semantic rule: a group, the points it is checked at, a yes/no condition with
// exclusions, and a sensitivity instead of action/severity.
//
// Every built-in template is listed under its group as a rule that is off by default:
// flip the switch to use it as-is, or Edit to tailor it. It is only stored (as an
// ordinary rule remembering its template_id) the first time it is changed, and
// deleting that stored copy resets the template to its default.
const groups = [
  { id: "technical", icon: ShieldAlert }, { id: "personal", icon: HeartHandshake },
  { id: "internal", icon: Building2 }, { id: "compliance", icon: ScanSearch }, { id: "custom", icon: Layers },
] as const;
const SENSITIVITY = [{ key: "low", value: 0.25 }, { key: "medium", value: 0.5 }, { key: "high", value: 0.75 }] as const;
const TPL = "tpl:";
const ACTIONS: SemanticRuleAction[] = ["monitor", "warn", "confirm", "block"];
const ACTION_VARIANT: Record<SemanticRuleAction, "neutral" | "warning" | "error"> = { monitor: "neutral", warn: "warning", confirm: "warning", block: "error" };
const ACT_LEVELS = [{ key: "80", value: 0.8 }, { key: "90", value: 0.9 }, { key: "95", value: 0.95 }] as const;
// The probability at which a rule acts: its alert level (1 - sensitivity) or its own action
// threshold, whichever is stricter. Mirrors SemanticMonitor.evaluate on the server.
const actLevel = (rule: { scale?: number; act_threshold?: number }) => Math.max(1 - (rule.scale ?? 0), rule.act_threshold ?? 0.9);

type Row = SemanticRule & { unsaved?: boolean };

const blank = (): SemanticRuleDraft => ({
  name: "", condition: "", exclusions: "", scopes: ["input", "output"], enabled: false, scale: 0.5, group: "custom",
  action: "monitor", act_threshold: 0.9, dry_run: true, message: "",
});
const draftOf = ({ name, condition, exclusions, scopes, enabled, scale, group, template_id, action, act_threshold, dry_run, message }: Row): SemanticRuleDraft => ({
  name, condition, exclusions, scopes, enabled, scale: typeof scale === "number" ? scale : 0.5, group: group ?? "custom", template_id: template_id ?? null,
  action: action ?? "monitor", act_threshold: act_threshold ?? 0.9, dry_run: dry_run ?? true, message: message ?? "",
});

const PROVIDER_NAME: Record<string, string> = { jev: "Jev", laya: "OpenThai SystemOne" };

// Guidance and examples stay out of the way until asked for.
function Hint({ summary, children }: { summary: string; children: string }) {
  return (
    <details className="claw-semantic-hint">
      <summary>{summary}</summary>
      <Text size="sm" color="secondary" as="p" display="block">{children}</Text>
    </details>
  );
}

export function SemanticRulesPanel({ connected, fallback, usingFallback, primaryError }: {
  connected: boolean; fallback?: string | null; usingFallback?: boolean; primaryError?: string | null;
}) {
  const t = useT();
  const editor = useRef<HTMLDivElement>(null);
  const [stored, setStored] = useState<SemanticRule[]>([]);
  const [templates, setTemplates] = useState<SemanticRule[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<SemanticRuleDraft>(blank);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [testing, setTesting] = useState<string | null>(null);
  const [sample, setSample] = useState("");
  const [scope, setScope] = useState<"input" | "output">("input");
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.adminTestSemanticGuardrails>> | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (editing !== null) {
      editor.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      editor.current?.querySelector<HTMLInputElement>("input")?.focus({ preventScroll: true });
    }
  }, [editing]);
  const reload = useCallback(async () => {
    const data = await api.adminSemanticRules();
    setStored(data.rules); setTemplates(data.templates);
  }, []);
  useEffect(() => { void reload().catch(() => setError(t("admin.semantic.loadError"))); }, [reload, t]);
  const run = async (task: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await task(); } catch (e) { setError(e instanceof Error ? e.message : t("admin.semantic.loadError")); }
    finally { setBusy(false); }
  };

  // One list: stored rules plus every template that has no stored copy yet. Ordering is
  // stable (templates in catalogue order, then custom rules by name) so toggling a row
  // never makes it jump.
  const rows = useMemo<Row[]>(() => {
    const order = new Map(templates.map((item, index) => [item.id, index]));
    const byName = new Map(templates.map((item) => [item.name, item.id]));
    const tplOf = (rule: SemanticRule) => rule.template_id ?? byName.get(rule.name) ?? null;
    const taken = new Set(stored.map(tplOf).filter(Boolean) as string[]);
    const unsaved: Row[] = templates.filter((item) => !taken.has(item.id)).map((item) => ({ ...item, id: TPL + item.id, template_id: item.id, enabled: false, unsaved: true }));
    const all: Row[] = [...stored.map((rule) => ({ ...rule, template_id: tplOf(rule) })), ...unsaved];
    const rank = (row: Row) => (row.template_id && order.has(row.template_id) ? order.get(row.template_id)! : 1000);
    return all.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  }, [stored, templates]);

  const reset = () => { setEditing(null); setDraft(blank()); };
  // Explain a failed check in plain words. Upstream bodies are never returned by the API,
  // only a reason code (and HTTP status), so nothing sensitive can surface here.
  const failure = (r: NonNullable<typeof result>) => {
    const key = `admin.semantic.err.${r.reason ?? r.status}`;
    const text = t(key, { code: String(r.http_status ?? ""), provider: PROVIDER_NAME[r.provider ?? ""] ?? r.provider ?? "" });
    return text === key ? t("admin.guardrails.semanticError") : text;
  };
  const scopeLabel = (rule: Row) => rule.scopes.map((value) => t(`admin.semantic.${value}`)).join(" · ");
  const valid = draft.name.trim() && draft.condition.trim() && draft.scopes.length > 0
    && draft.name.length <= 100 && draft.condition.length <= 2000 && draft.exclusions.length <= 1000;

  // Persist a row's new state. Unsaved templates are created first (creation is always
  // off on the server), then updated when the desired state is "enabled".
  const persist = async (row: Row | null, next: SemanticRuleDraft) => {
    if (row && !row.unsaved) { await api.adminUpdateSemanticRule(row.id, next); return; }
    const created = await api.adminCreateSemanticRule({ ...next, enabled: false });
    if (next.enabled) await api.adminUpdateSemanticRule(created.id, next);
  };
  const editingRow = rows.find((row) => row.id === editing) ?? null;
  const save = () => run(async () => {
    // New custom rules start off; editing keeps whatever on/off state the rule already had.
    await persist(editingRow, { ...draft, enabled: editingRow ? editingRow.enabled : false });
    reset(); setTesting(null); setResult(null); await reload();
  });

  const form = (
    <Card padding={2} variant={editing === "new" ? "default" : "muted"}>
      <div className="claw-panel" ref={editor}>
        <TextInput label={t("admin.guardrails.ruleName")} value={draft.name} onChange={(name) => setDraft({ ...draft, name })} />
        <div className="claw-row" style={{ flexWrap: "wrap" }}>
          <Text size="sm" color="secondary">{t("admin.semantic.group")}</Text>
          {groups.map((g) => (
            <Button key={g.id} label={t(`admin.semantic.group.${g.id}`)} size="sm" isDisabled={busy}
              variant={(draft.group ?? "custom") === g.id ? "primary" : "secondary"}
              clickAction={() => setDraft({ ...draft, group: g.id as SemanticRuleGroup })} />
          ))}
        </div>
        <TextArea label={t("admin.semantic.condition")} placeholder={t("admin.semantic.condition.placeholder")} value={draft.condition} onChange={(condition) => setDraft({ ...draft, condition })} rows={5} />
        <Hint summary={t("admin.semantic.hintToggle")}>{t("admin.semantic.condition.hint")}</Hint>
        <TextArea label={t("admin.semantic.exclusions")} placeholder={t("admin.semantic.exclusions.placeholder")} value={draft.exclusions} onChange={(exclusions) => setDraft({ ...draft, exclusions })} rows={3} />
        <Hint summary={t("admin.semantic.hintToggle")}>{t("admin.semantic.exclusions.hint")}</Hint>
        <div className="claw-row">
          <Text size="sm" color="secondary">{t("admin.semantic.scope")}</Text>
          {(["input", "output"] as const).map((item) => (
            <Button key={item} label={t(`admin.semantic.${item}`)} size="sm" isDisabled={busy}
              variant={draft.scopes.includes(item) ? "primary" : "secondary"}
              clickAction={() => {
                const scopes = draft.scopes.includes(item) ? draft.scopes.filter((v) => v !== item) : [...draft.scopes, item];
                // Confirm only exists for chat input; without it the rule falls back to a warning.
                setDraft({ ...draft, scopes, action: draft.action === "confirm" && !scopes.includes("input") ? "warn" : draft.action });
              }} />
          ))}
        </div>
        <div className="claw-row" style={{ flexWrap: "wrap" }}>
          <Text size="sm" color="secondary">{t("admin.semantic.scale")}</Text>
          {SENSITIVITY.map((s) => (
            <Button key={s.key} label={t(`admin.semantic.sensitivity.${s.key}`)} size="sm" isDisabled={busy}
              variant={draft.scale === s.value ? "primary" : "secondary"}
              clickAction={() => setDraft({ ...draft, scale: s.value })} />
          ))}
          <input type="range" min={0} max={1} step={0.01} value={draft.scale} disabled={busy}
            aria-label={t("admin.semantic.scale")} className="claw-semantic-range"
            onChange={(e) => setDraft({ ...draft, scale: Number(e.target.value) })} />
          <Text size="sm" color="secondary">{draft.scale.toFixed(2)}</Text>
        </div>
        <Text size="sm" color="secondary" as="p" display="block">{t("admin.semantic.scaleHint")}{editing === "new" ? ` · ${t("admin.semantic.savedOff")}` : ""}</Text>
        <div className="claw-row" style={{ flexWrap: "wrap" }}>
          <Text size="sm" color="secondary">{t("admin.semantic.action")}</Text>
          {ACTIONS.map((a) => (
            <Button key={a} label={t(`admin.semantic.action.${a}`)} size="sm"
              isDisabled={busy || (a === "confirm" && !draft.scopes.includes("input"))}
              variant={(draft.action ?? "monitor") === a ? "primary" : "secondary"}
              clickAction={() => setDraft({ ...draft, action: a })} />
          ))}
        </div>
        <Hint summary={t("admin.semantic.hintToggle")}>{t(`admin.semantic.action.${draft.action ?? "monitor"}.hint`)}</Hint>
        {(draft.action ?? "monitor") !== "monitor" && (
          <>
            <div className="claw-row" style={{ flexWrap: "wrap" }}>
              <Text size="sm" color="secondary">{t("admin.semantic.actAt")}</Text>
              {ACT_LEVELS.map((l) => (
                <Button key={l.key} label={`≥ ${l.key}%`} size="sm" isDisabled={busy}
                  variant={Math.abs((draft.act_threshold ?? 0.9) - l.value) < 0.001 ? "primary" : "secondary"}
                  clickAction={() => setDraft({ ...draft, act_threshold: l.value })} />
              ))}
              <input type="range" min={0.5} max={1} step={0.01} value={draft.act_threshold ?? 0.9} disabled={busy}
                aria-label={t("admin.semantic.actAt")} className="claw-semantic-range"
                onChange={(e) => setDraft({ ...draft, act_threshold: Number(e.target.value) })} />
              <Text size="sm" color="secondary">≥ {(actLevel(draft) * 100).toFixed(0)}%</Text>
            </div>
            <Text size="sm" color="secondary" as="p" display="block">{t("admin.semantic.actAtHint")}</Text>
            <div className="claw-row claw-row-between">
              <div>
                <Text size="sm" weight="semibold" display="block">{t("admin.semantic.dryRun")}</Text>
                <Text size="sm" color="secondary" display="block">{t("admin.semantic.dryRunHint")}</Text>
              </div>
              <Switch value={draft.dry_run ?? true} label={t("admin.semantic.dryRun")} isLabelHidden isDisabled={busy}
                changeAction={(dry_run) => setDraft({ ...draft, dry_run })} />
            </div>
            <TextInput label={t("admin.semantic.message")} placeholder={t(`policy.semantic.${draft.action}`)}
              value={draft.message ?? ""} onChange={(message) => setDraft({ ...draft, message })} />
          </>
        )}
        <div className="claw-row">
          <Button label={editing === "new" ? t("admin.guardrails.createRule") : t("admin.common.saveChanges")}
            variant="primary" icon={<Icon icon="check" size="sm" />} size={editing === "new" ? undefined : "sm"}
            isDisabled={busy || !valid} clickAction={save} />
          <Button label={t("admin.common.cancel")} variant="ghost" size={editing === "new" ? undefined : "sm"} isDisabled={busy} clickAction={reset} />
        </div>
      </div>
    </Card>
  );

  return (
    <div className="claw-guardrails-pane">
      <Card padding={2} variant="muted">
        <div className="claw-row claw-row-between">
          <div>
            <Text weight="semibold" display="block">{t("admin.semantic.title")}</Text>
            <Text size="sm" color="secondary" as="p" display="block">{t("admin.semantic.monitor")}</Text>
          </div>
          <Badge variant="neutral" label={t("admin.guardrails.actionMonitor")} />
        </div>
        {fallback && (
          <Text size="sm" color="secondary" as="p" display="block">
            {usingFallback
              ? t("admin.semantic.usingFallback", { provider: PROVIDER_NAME[fallback] ?? fallback, reason: primaryError ? t(`admin.semantic.err.${primaryError}`, { code: "", provider: "" }) : "" })
              : t("admin.semantic.fallbackReady", { provider: PROVIDER_NAME[fallback] ?? fallback })}
          </Text>
        )}
      </Card>

      <div className="claw-row claw-row-between">
        <Text color="secondary">{t("admin.semantic.intro")}</Text>
        {editing !== "new" && (
          <Button label={t("admin.semantic.addCustom")} icon={<Icon icon={Plus} size="sm" />} size="sm" isDisabled={busy}
            clickAction={() => { reset(); setEditing("new"); setTesting(null); }} />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {editing === "new" && form}
      {rows.length > 0 && !rows.some((rule) => rule.enabled) && <Text size="sm" color="secondary" as="p" display="block">{t("admin.semantic.noActive")}</Text>}

      <div className="claw-semantic-groups">
        {groups.filter((g) => rows.some((rule) => (rule.group ?? "custom") === g.id)).map((g) => {
          const items = rows.filter((rule) => (rule.group ?? "custom") === g.id);
          return (
            <section className="claw-semantic-group" key={g.id} aria-labelledby={`semantic-group-${g.id}`}>
              <div className="claw-semantic-group-header">
                <g.icon size={18} aria-hidden="true" />
                <h3 id={`semantic-group-${g.id}`}>{t(`admin.semantic.group.${g.id}`)}</h3>
                <span className="claw-semantic-count" title={t("admin.semantic.enabledCount")}>{items.filter((rule) => rule.enabled).length}/{items.length}</span>
              </div>
              {items.map((rule) => (
                <Card key={rule.id} padding={2} variant={rule.enabled ? "default" : "muted"}>
                  <div className="claw-semantic-rule-head">
                    <div className="claw-row" style={{ flexWrap: "wrap" }}>
                      <Text weight="semibold">{rule.name}</Text>
                      <Badge variant="neutral" label={scopeLabel(rule)} />
                      <Badge variant="neutral" label={`${t("admin.semantic.scale")} ${(rule.scale ?? 0).toFixed(2)}`} />
                      <Badge variant={ACTION_VARIANT[rule.action ?? "monitor"]} label={t(`admin.semantic.action.${rule.action ?? "monitor"}`)} />
                      {(rule.action ?? "monitor") !== "monitor" && (rule.dry_run ?? true) && <Badge variant="neutral" label={t("admin.semantic.dryRun")} />}
                    </div>
                    <div className="claw-row claw-semantic-rule-actions">
                      <Switch value={rule.enabled} label={t("admin.semantic.enable", { name: rule.name })} isLabelHidden isDisabled={busy}
                        changeAction={(enabled) => run(async () => { await persist(rule, { ...draftOf(rule), enabled }); await reload(); })} />
                      <Button label={t("admin.common.edit")} icon={<Icon icon={Pencil} size="sm" />} size="sm" variant="ghost" isDisabled={busy}
                        clickAction={() => { setEditing(editing === rule.id ? null : rule.id); setDraft(draftOf(rule)); setTesting(null); }} />
                      <Button label={t("admin.guardrails.runTest")} size="sm" variant="ghost" isDisabled={busy || !connected}
                        clickAction={() => { setTesting(testing === rule.id ? null : rule.id); setSample(""); setScope(rule.scopes[0]); setResult(null); setEditing(null); }} />
                      {(!rule.unsaved || !rule.template_id) && (
                        <Button label={rule.template_id ? t("admin.semantic.resetDefault") : t("admin.common.delete")}
                          icon={<Icon icon={rule.template_id ? RotateCcw : Trash2} size="sm" />} size="sm" variant="ghost" isDisabled={busy}
                          clickAction={() => {
                            if (window.confirm(t(rule.template_id ? "admin.semantic.confirmReset" : "admin.semantic.confirmDelete", { name: rule.name }))) {
                              void run(async () => { await api.adminDeleteSemanticRule(rule.id); if (editing === rule.id) reset(); if (testing === rule.id) setTesting(null); await reload(); });
                            }
                          }} />
                      )}
                    </div>
                  </div>
                  <Text size="sm" color="secondary" as="p" display="block"
                    className={`claw-semantic-desc${expanded.has(rule.id) ? " is-open" : ""}`}
                    onClick={() => setExpanded((prev) => { const next = new Set(prev); if (!next.delete(rule.id)) next.add(rule.id); return next; })}>
                    {rule.condition}
                  </Text>
                  {expanded.has(rule.id) && rule.exclusions && (
                    <Text size="sm" color="secondary" as="p" display="block" className="claw-semantic-exclusions">
                      <strong>{t("admin.semantic.exclusionsShort")}:</strong> {rule.exclusions}
                    </Text>
                  )}
                  {editing === rule.id && form}
                  {testing === rule.id && (
                    <Card padding={2} variant="muted">
                      <div className="claw-panel">
                        <div className="claw-row">
                          <Text size="sm" color="secondary">{t("admin.semantic.scope")}</Text>
                          {rule.scopes.map((item) => (
                            <Button key={item} label={t(`admin.semantic.${item}`)} size="sm" isDisabled={busy}
                              variant={scope === item ? "primary" : "secondary"}
                              clickAction={() => { setScope(item); setResult(null); }} />
                          ))}
                        </div>
                        <TextArea label={t("admin.guardrails.sampleText")} isLabelHidden placeholder={t("admin.guardrails.sampleText")}
                          value={sample} onChange={(value) => { setSample(value); setResult(null); }} rows={3} />
                        <div className="claw-row">
                          <Button label={t("admin.guardrails.runTest")} variant="primary" size="sm" isDisabled={busy || !sample.trim() || sample.length > 8000}
                            clickAction={() => run(async () => { setResult(null); setResult(await api.adminTestSemanticGuardrails(sample, rule.id, scope)); })} />
                          {result && (result.status === "checked" ? (
                            <>
                              {result.fallback_from && <Badge variant="neutral" label={t("admin.semantic.viaFallback", { provider: PROVIDER_NAME[result.provider ?? ""] ?? result.provider ?? "" })} />}
                              <Text size="sm" color="secondary">{t("admin.semantic.probability")}: {((result.scores?.[rule.id] ?? 0) * 100).toFixed(1)}%</Text>
                              <Badge variant={result.alerts?.[rule.id] ? "warning" : "neutral"} label={t(result.alerts?.[rule.id] ? "admin.semantic.alertYes" : "admin.semantic.alertNo")} />
                              {(rule.action ?? "monitor") !== "monitor" && (result.scores?.[rule.id] ?? 0) >= actLevel(rule) && (
                                <Badge variant={ACTION_VARIANT[rule.action ?? "monitor"]}
                                  label={t((rule.dry_run ?? true) ? "admin.semantic.wouldAct" : "admin.semantic.willAct", { action: t(`admin.semantic.action.${rule.action}`) })} />
                              )}
                            </>
                          ) : <ErrorText>{failure(result)}</ErrorText>)}
                        </div>
                      </div>
                    </Card>
                  )}
                </Card>
              ))}
            </section>
          );
        })}
      </div>
    </div>
  );
}
