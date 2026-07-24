import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon, type IconName, type IconType } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Switch } from "@astryxdesign/core/Switch";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import {
  Asterisk,
  Ban,
  Brain,
  ChevronDown,
  ChevronRight,
  Cloud,
  Coins,
  Cpu,
  Diamond,
  ExternalLink,
  Gauge,
  Globe,
  Info,
  KeyRound,
  LayoutDashboard,
  Mail,
  MessageSquare,
  Moon,
  Palette,
  Pencil,
  Plus,
  Router,
  ScrollText,
  Search,
  Send,
  Server,
  Shield,
  ShieldCheck,
  ShieldOff,
  Sparkles,
  Star,
  Terminal,
  Trash2,
  Upload,
  User as UserIcon,
  UserPlus,
  Users,
  Zap,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorText } from "./ErrorText";
import { PasswordField } from "./PasswordField";
import { useBranding, useT } from "./branding";
import {
  ActivityPoint,
  AdminBranding,
  type BrandingChatBackground,
  type BrandingFontSize,
  type BrandingLanguage,
  type BrandingLogoSlot,
  AdminOverview,
  AdminUser,
  AuditRow,
  GroupInfo,
  GuardrailRule,
  GuardrailTestResult,
  LLMModelCfg,
  LLMProviderCfg,
  ModelCost,
  ModelKind,
  ModelUsagePoint,
  OAuthAppsInfo,
  type PlanCreate,
  type PlanInfo,
  SessionsByUserPoint,
  SmtpAdminConfig,
  TelegramAdminConfig,
  type TokenUsageParams,
  type TokenUsageReport,
  type TokenUsageSeries,
  type UsageDimensions,
  type UserImportCommitResult,
  type UserImportMapping,
  type UserImportParseResult,
  api,
  ADMIN_LLM_API,
  type LlmApi,
} from "./api";

export type AdminSection =
  | "overview"
  | "providers"
  | "plans"
  | "guardrails"
  | "oauth"
  | "telegram"
  | "email"
  | "preferences"
  | "audit"
  | "users";

// Reuses the chat-side cost-tier vocabulary (identical wording) rather than
// duplicating a second admin.cost.* catalog for the same four words.
export const COST_LABEL: Record<ModelCost, string> = {
  low: "chat.cost.low",
  medium: "chat.cost.medium",
  high: "chat.cost.high",
  very_high: "chat.cost.veryHigh",
};

// `labelKey` (not a pre-resolved label) because this array is built at module
// scope, before any component (and its `useT()`) exists — consumers call
// `t(s.labelKey)` at render time so the label follows the current language.
export const ADMIN_SECTIONS: { key: AdminSection; labelKey: string; icon: IconType | IconName }[] = [
  { key: "overview", labelKey: "admin.nav.overview", icon: LayoutDashboard },
  { key: "providers", labelKey: "admin.nav.providers", icon: Cpu },
  { key: "plans", labelKey: "admin.nav.plans", icon: Gauge },
  { key: "guardrails", labelKey: "admin.nav.guardrails", icon: ShieldCheck },
  { key: "oauth", labelKey: "admin.nav.oauth", icon: KeyRound },
  { key: "telegram", labelKey: "admin.nav.telegram", icon: Send },
  { key: "email", labelKey: "admin.nav.email", icon: Mail },
  { key: "preferences", labelKey: "admin.nav.preferences", icon: Palette },
  { key: "audit", labelKey: "admin.nav.audit", icon: ScrollText },
  { key: "users", labelKey: "admin.nav.users", icon: Users },
];

export function AdminPanel({ section, selfId }: { section: AdminSection; selfId: string }) {
  const t = useT();
  const meta = ADMIN_SECTIONS.find((s) => s.key === section);
  // LLM Providers is a data table (model id, cost, status, several action
  // buttons per row) — the shared 720px prose-reading column that suits every
  // other admin page (forms, prose, short lists) squeezes it into ellipsis
  // soup. Overview is the same story: a stat grid + charts that read better
  // with more horizontal room. Widen just these sections rather than the
  // whole panel.
  const isWide = section === "providers" || section === "overview" || section === "plans";
  return (
    <div className="claw-settings-panel">
      <div className={`claw-settings-panel-header${isWide ? " claw-panel-wide" : ""}`}>
        <Icon icon={meta?.icon ?? "check"} size="lg" color="secondary" />
        <Text type="display-3">{meta ? t(meta.labelKey) : ""}</Text>
      </div>
      <div className={`claw-panel${isWide ? " claw-panel-wide" : ""}`}>
        {section === "overview" && <OverviewPanel />}
        {section === "providers" && <ProvidersPanel llmApi={ADMIN_LLM_API} scope="admin" />}
        {section === "plans" && <PlansPanel />}
        {section === "guardrails" && <GuardrailsPanel />}
        {section === "oauth" && <OAuthAppsPanel />}
        {section === "telegram" && <TelegramConfigPanel />}
        {section === "email" && <EmailConfigPanel />}
        {section === "preferences" && <PreferencesPanel />}
        {section === "audit" && <AuditPanel />}
        {section === "users" && <UsersPanel selfId={selfId} />}
      </div>
    </div>
  );
}

// Case-insensitive substring filter shared by every searchable dropdown
// (User/Model filters in the Tokens tab, etc.) instead of each re-deriving
// its own `.toLowerCase().includes(...)` inline.
function filterByQuery<T>(items: T[], query: string, text: (item: T) => string): T[] {
  if (!query) return items;
  const q = query.toLowerCase();
  return items.filter((item) => text(item).toLowerCase().includes(q));
}

// Keeps the currently-selected option visible in a search-narrowed list even
// when it no longer matches the query — otherwise the <select> renders as
// blank/unselected while the (still-applied) filter silently stays active.
function keepSelected<T>(filtered: T[], all: T[], selected: string, valueOf: (item: T) => string): T[] {
  if (!selected || filtered.some((item) => valueOf(item) === selected)) return filtered;
  const found = all.find((item) => valueOf(item) === selected);
  return found ? [...filtered, found] : filtered;
}

function useAsyncError() {
  const [error, setError] = useState("");
  const guard = useCallback(async (fn: () => Promise<void>) => {
    try {
      setError("");
      await fn();
    } catch (e) {
      setError(String(e));
    }
  }, []);
  return { error, guard };
}

// ---------------------------------------------------------------- charts

function BarChart({ data, accent = "var(--color-primary, #4b6bfb)" }: { data: ActivityPoint[]; accent?: string }) {
  const max = Math.max(1, ...data.map((d) => d.count));
  const W = 100 / Math.max(1, data.length);
  return (
    <div className="claw-chart">
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="claw-chart-svg">
        {data.map((d, i) => {
          const h = (d.count / max) * 36;
          return (
            <rect
              key={i}
              x={i * W + W * 0.15}
              y={40 - h}
              width={W * 0.7}
              height={Math.max(h, d.count > 0 ? 1 : 0)}
              rx={0.6}
              fill={accent}
            >
              <title>{`${d.label}: ${d.count}`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="claw-chart-axis">
        {data.map((d, i) => (
          <span key={i}>{d.label.length > 5 ? d.label.slice(5) : d.label}</span>
        ))}
      </div>
    </div>
  );
}

// Categorical palette for stacked series (cycles for >8 groups); "Others" always
// gets a fixed muted color so the long-tail rollup reads consistently.
const STACK_PALETTE = [
  "var(--color-primary, #4b6bfb)",
  "var(--color-info, #2f9e6f)",
  "var(--color-warning, #d97706)",
  "var(--color-error, #c0392b)",
  "var(--color-accent-purple, #7c3aed)",
  "#0891b2",
  "#db2777",
  "#65a30d",
];
const STACK_OTHERS_COLOR = "var(--color-text-secondary, #9ca3af)";

function stackColor(index: number, key: string): string {
  return key === "__others__" ? STACK_OTHERS_COLOR : STACK_PALETTE[index % STACK_PALETTE.length];
}

// Per-bucket stacked bars, one segment per series — so switching the Tokens
// tab's group-by (User/Model/Provider) visibly changes each bar's composition,
// not just a single invariant total-per-day line.
function StackedBarChart({ buckets, series }: { buckets: string[]; series: TokenUsageSeries[] }) {
  const totalsByBucket = buckets.map((_, i) =>
    series.reduce((sum, s) => sum + s.points[i].prompt_tokens + s.points[i].completion_tokens, 0),
  );
  const max = Math.max(1, ...totalsByBucket);
  const W = 100 / Math.max(1, buckets.length);
  return (
    <div className="claw-chart">
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="claw-chart-svg">
        {buckets.map((b, bi) => {
          let y = 40;
          return (
            <g key={b}>
              {series.map((s, si) => {
                const v = s.points[bi].prompt_tokens + s.points[bi].completion_tokens;
                if (v <= 0) return null;
                const h = (v / max) * 36;
                y -= h;
                return (
                  <rect
                    key={s.key}
                    x={bi * W + W * 0.15}
                    y={y}
                    width={W * 0.7}
                    height={Math.max(h, 0.5)}
                    fill={stackColor(si, s.key)}
                  >
                    <title>{`${b} — ${s.label}: ${v.toLocaleString()}`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
      </svg>
      <div className="claw-chart-axis">
        {buckets.map((b, i) => (
          // Buckets are already the right width per granularity ("YYYY" for
          // yearly, "YYYY-MM" for monthly, "YYYY-MM-DD" for weekly/daily) — only
          // the full-date form has a redundant year prefix worth trimming.
          <span key={i}>{b.length > 7 ? b.slice(5) : b}</span>
        ))}
      </div>
      <div className="claw-stacked-legend">
        {series.map((s, i) => (
          <span key={s.key}>
            <i className="claw-legend-dot" style={{ background: stackColor(i, s.key) }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Overview

const STAT_CARDS: { key: string; labelKey: string; icon: IconType }[] = [
  { key: "users", labelKey: "admin.overview.stat.users", icon: Users },
  { key: "active_users", labelKey: "admin.overview.stat.activeUsers", icon: Users },
  { key: "sessions", labelKey: "admin.overview.stat.sessions", icon: MessageSquare },
  { key: "messages", labelKey: "admin.overview.stat.messages", icon: MessageSquare },
  { key: "turns", labelKey: "admin.overview.stat.turns", icon: Cpu },
  { key: "prompt_tokens", labelKey: "admin.overview.stat.promptTokens", icon: Cpu },
  { key: "consolidations", labelKey: "admin.overview.stat.consolidations", icon: Sparkles },
  { key: "memory_users", labelKey: "admin.overview.stat.memoryUsers", icon: Brain },
];

// Overview groups its metrics into tabs (Summary / Activity / Models / Safety)
// so the page stays scannable as more data is added. Everything comes from one
// adminOverview() fetch, so switching tabs is an instant client-side view swap.
const OVERVIEW_TABS: { key: string; labelKey: string; icon: IconType }[] = [
  { key: "summary", labelKey: "admin.overview.tab.summary", icon: LayoutDashboard },
  { key: "activity", labelKey: "admin.overview.tab.activity", icon: MessageSquare },
  { key: "models", labelKey: "admin.overview.tab.models", icon: Cpu },
  { key: "tokens", labelKey: "admin.overview.tab.tokens", icon: Coins },
  { key: "plans", labelKey: "admin.overview.tab.plans", icon: Gauge },
  { key: "safety", labelKey: "admin.overview.tab.safety", icon: ShieldCheck },
];

function OverviewPanel() {
  const t = useT();
  const [data, setData] = useState<AdminOverview | null>(null);
  const [tab, setTab] = useState("summary");
  const { error, guard } = useAsyncError();

  useEffect(() => {
    void guard(async () => setData(await api.adminOverview()));
  }, [guard]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!data) return <Text color="secondary">{t("admin.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <TabList value={tab} onChange={setTab} hasDivider aria-label={t("admin.overview.sectionsAria")}>
        {OVERVIEW_TABS.map((tabItem) => (
          <Tab key={tabItem.key} value={tabItem.key} label={t(tabItem.labelKey)} icon={<Icon icon={tabItem.icon} size="sm" />} />
        ))}
      </TabList>
      {tab === "summary" && <OverviewSummary data={data} />}
      {tab === "activity" && <OverviewActivity data={data} />}
      {tab === "models" && <OverviewModels data={data} />}
      {tab === "tokens" && <OverviewTokens />}
      {tab === "plans" && <OverviewPlans data={data} />}
      {tab === "safety" && <OverviewSafety data={data} />}
    </div>
  );
}

function OverviewSummary({ data }: { data: AdminOverview }) {
  const t = useT();
  const s = data.stats;
  return (
    <>
      <div className="claw-stat-grid">
        {STAT_CARDS.map((c) => (
          <Card key={c.key} padding={2} variant="muted">
            <div className="claw-stat">
              <Icon icon={c.icon} size="sm" color="secondary" />
              <Text type="display-3">{Number(s[c.key] ?? 0).toLocaleString()}</Text>
              <Text size="sm" color="secondary">
                {t(c.labelKey)}
              </Text>
            </div>
          </Card>
        ))}
      </div>

      <div className="claw-row">
        <Badge
          variant="neutral"
          icon={<Icon icon={Shield} size="xsm" />}
          label={t("admin.overview.badge.admins", { count: String(s.admins ?? 0) })}
        />
        <Badge
          variant="neutral"
          icon={<Icon icon={Ban} size="xsm" />}
          label={t("admin.overview.badge.suspended", { count: String(s.suspended ?? 0) })}
        />
        <Badge
          variant={s.policy_enforcing ? "success" : "neutral"}
          icon={<Icon icon={ShieldCheck} size="xsm" />}
          label={s.policy_enforcing ? t("admin.overview.badge.enforcing") : t("admin.overview.badge.monitorOnly")}
        />
        <Badge
          variant={s.browser_enabled ? "success" : "neutral"}
          icon={<Icon icon={Globe} size="xsm" />}
          label={s.browser_enabled ? t("admin.overview.badge.browserOn") : t("admin.overview.badge.browserOff")}
        />
        <Badge
          variant={s.telegram_enabled ? "success" : "neutral"}
          icon={<Icon icon={Send} size="xsm" />}
          label={s.telegram_enabled ? t("admin.overview.badge.telegramOn") : t("admin.overview.badge.telegramOff")}
        />
      </div>
    </>
  );
}

function OverviewActivity({ data }: { data: AdminOverview }) {
  const t = useT();
  return (
    <>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.activity.title")}</Text>
        <BarChart data={data.activity_by_day} />
      </Card>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.activity.byHour")}</Text>
        <BarChart data={data.activity_by_hour} accent="var(--color-info, #2f9e6f)" />
      </Card>

      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.activity.sessionsStarted")}</Text>
        {data.sessions_by_day_7d.every((d) => d.count === 0) ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.activity.noSessions7d")}
          </Text>
        ) : (
          <BarChart data={data.sessions_by_day_7d} accent="var(--color-warning, #d97706)" />
        )}
      </Card>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.activity.sessionsByUser")}</Text>
        {data.sessions_by_user_7d.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.activity.noSessions7d")}
          </Text>
        ) : (
          <SessionsByUserList data={data.sessions_by_user_7d} />
        )}
      </Card>
    </>
  );
}

function OverviewModels({ data }: { data: AdminOverview }) {
  const t = useT();
  return (
    <>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.models.providersInUse")}</Text>
        {data.providers.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.models.noProviders")}
          </Text>
        ) : (
          <div className="claw-provider-usage-list">
            {data.providers.map((p) => (
              <div key={p.name} className="claw-provider-usage-row">
                <Icon icon={Cpu} size="sm" color="secondary" />
                <span className="claw-provider-usage-name">{p.name}</span>
                <Badge
                  variant={p.enabled ? "success" : "neutral"}
                  label={p.enabled ? t("admin.overview.models.enabled") : t("admin.overview.models.disabled")}
                />
                <Badge
                  variant={p.has_key ? "neutral" : "warning"}
                  label={p.has_key ? t("admin.overview.models.keySet") : t("admin.overview.models.noKey")}
                />
                <span className="claw-provider-usage-models">
                  {t("admin.overview.models.modelsEnabled", {
                    enabled: String(p.enabled_model_count),
                    total: String(p.model_count),
                  })}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.models.tokensPerModel")}</Text>
        {data.usage_by_model.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.models.noUsage")}
          </Text>
        ) : (
          <ModelUsageChart data={data.usage_by_model} />
        )}
      </Card>
    </>
  );
}

// Generic ranked bar-list — reused for "hits by user" and "hits by rule" (any
// single-number-per-key breakdown), so the two Safety cards below share one
// renderer instead of two near-identical copies. `unit` is a translated noun
// supplied by the caller (e.g. t("admin.overview.unitHit")).
function RankedBarList({ data, unit }: { data: { key: string; label: string; count: number }[]; unit: string }) {
  const t = useT();
  const max = Math.max(1, ...data.map((d) => d.count));
  return (
    <div className="claw-model-usage-list">
      {data.map((d) => (
        <div key={d.key} className="claw-model-usage-row">
          <div className="claw-model-usage-head">
            <span className="claw-model-usage-name claw-model-usage-name--user">{d.label}</span>
            <span className="claw-model-usage-total">
              {t("admin.overview.countUnit", {
                count: d.count.toLocaleString(),
                unit,
                plural: d.count === 1 ? "" : "s",
              })}
            </span>
          </div>
          <div className="claw-model-usage-bar">
            <div className="claw-model-usage-bar-prompt" style={{ width: `${(d.count / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function OverviewSafety({ data }: { data: AdminOverview }) {
  const t = useT();
  // The enforcing/monitor-only badge already lives in the Summary tab (a
  // glance-level fact); Safety owns the detailed hit history, not a second
  // copy of the same badge.
  return (
    <>
      <Card padding={3}>
        <div className="claw-card-heading">
          <Text weight="semibold">{t("admin.overview.safety.hitsByDay")}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.overview.safety.hitsByDayDesc")}
          </Text>
        </div>
        {data.guardrail_hits_by_day.every((d) => d.count === 0) ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.safety.noHits")}
          </Text>
        ) : (
          <BarChart data={data.guardrail_hits_by_day} accent="var(--color-error, #c0392b)" />
        )}
      </Card>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.safety.hitsByUser")}</Text>
        {data.guardrail_hits_by_user.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.safety.noHits")}
          </Text>
        ) : (
          <RankedBarList
            data={data.guardrail_hits_by_user.map((u) => ({ key: u.user_id, label: u.label, count: u.count }))}
            unit={t("admin.overview.unitHit")}
          />
        )}
      </Card>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.safety.hitsByRule")}</Text>
        {data.guardrail_hits_by_rule.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.safety.noHits")}
          </Text>
        ) : (
          <RankedBarList
            data={data.guardrail_hits_by_rule.map((r) => ({ key: r.rule, label: r.rule, count: r.count }))}
            unit={t("admin.overview.unitHit")}
          />
        )}
      </Card>
    </>
  );
}

// Plans overview — reads entirely from the already-fetched adminOverview()
// payload (data.plans_report), so switching to this tab is an instant
// client-side view swap with no extra fetch (per CLAUDE.md rule 1).
const PLAN_LADDER_COLUMN_KEYS = [
  "admin.overview.plans.colPlan",
  "admin.overview.plans.colChatCeiling",
  "admin.overview.plans.colImage",
  "admin.overview.plans.colMessagesDay",
  "admin.overview.plans.colImagesDay",
  "admin.overview.plans.colTurnsMin",
];

// 0 encodes "unlimited" (daily quotas) — render it as ∞ rather than a literal 0.
function planLimitText(n: number): string {
  return n > 0 ? n.toLocaleString() : "∞";
}

function OverviewPlans({ data }: { data: AdminOverview }) {
  const t = useT();
  const report = data.plans_report;
  // Ranked low→high so the ladder reads as a progression, matching the Plans
  // management section's ordering.
  const plans = [...report.plans].sort((a, b) => a.rank - b.rank);
  const perPlan = plans.map((p) => ({ key: p.id, label: p.name, count: p.user_count ?? 0 }));

  return (
    <>
      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.plans.usersPerPlan")}</Text>
        {perPlan.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.plans.noPlans")}
          </Text>
        ) : (
          <RankedBarList data={perPlan} unit={t("admin.overview.plans.unitUser")} />
        )}
      </Card>

      <Card padding={3}>
        <Text weight="semibold">{t("admin.overview.plans.ladder")}</Text>
        {plans.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.plans.noPlansShort")}
          </Text>
        ) : (
          // One grid spanning the header + every plan row — same table-without-a-
          // table approach as the models grid, so columns align across all rows.
          <div className="claw-plan-ladder">
            {PLAN_LADDER_COLUMN_KEYS.map((hKey) => (
              <Text key={hKey} size="2xs" color="secondary" className="claw-models-grid-head">
                {t(hKey)}
              </Text>
            ))}
            {plans.map((p) => (
              <Fragment key={p.id}>
                <div className="claw-model-name-cell">
                  <Text className="claw-model-label">{p.name}</Text>
                  {p.is_default && (
                    <Badge variant="purple" icon={<Icon icon={Star} size="xsm" />} label={t("admin.overview.plans.default")} />
                  )}
                </div>
                <span className={`claw-cost claw-cost-${p.max_chat_cost}`}>
                  {t(COST_LABEL[p.max_chat_cost])}
                </span>
                {p.allow_image ? (
                  <span className={`claw-cost claw-cost-${p.max_image_cost}`}>
                    {t(COST_LABEL[p.max_image_cost])}
                  </span>
                ) : (
                  <Text size="sm" color="secondary">
                    {t("admin.overview.plans.off")}
                  </Text>
                )}
                <Text size="sm" color="secondary">
                  {planLimitText(p.messages_per_day)}
                </Text>
                <Text size="sm" color="secondary">
                  {planLimitText(p.images_per_day)}
                </Text>
                <Text size="sm" color="secondary">
                  {p.turns_per_minute > 0 ? p.turns_per_minute.toLocaleString() : t("admin.overview.plans.global")}
                </Text>
              </Fragment>
            ))}
          </div>
        )}
      </Card>

      <Card padding={3}>
        <div className="claw-card-heading">
          <Text weight="semibold">{t("admin.overview.plans.topUsersTitle")}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.overview.plans.topUsersDesc")}
          </Text>
        </div>
        {report.usage_today.length === 0 ? (
          <Text size="sm" color="secondary">
            {t("admin.overview.plans.noUsageToday")}
          </Text>
        ) : (
          <div className="claw-plan-usage-list">
            {/* "Top users" is already a bounded server-side ranking; the slice is
                a defensive cap so the list can never degrade the page. */}
            {report.usage_today.slice(0, 50).map((r) => {
              const msgOver = r.messages_limit > 0 && r.turns >= r.messages_limit;
              const imgOver = r.images_limit > 0 && r.images >= r.images_limit;
              return (
                <div
                  key={r.user_id}
                  className={`claw-plan-usage-row${msgOver || imgOver ? " is-over" : ""}`}
                >
                  <span className="claw-plan-usage-name">{r.label}</span>
                  <Badge variant="neutral" label={r.plan_name ?? t("admin.overview.plans.defaultPlanBadge")} />
                  <span className="claw-plan-usage-metric">
                    {t("admin.overview.plans.msgsUnit", {
                      turns: r.turns.toLocaleString(),
                      limit: planLimitText(r.messages_limit),
                    })}
                  </span>
                  <span className="claw-plan-usage-metric">
                    {t("admin.overview.plans.imgsUnit", {
                      images: r.images.toLocaleString(),
                      limit: planLimitText(r.images_limit),
                    })}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </>
  );
}

const GRANULARITIES: { key: TokenUsageParams["granularity"]; labelKey: string }[] = [
  { key: "daily", labelKey: "admin.overview.tokens.daily" },
  { key: "weekly", labelKey: "admin.overview.tokens.weekly" },
  { key: "monthly", labelKey: "admin.overview.tokens.monthly" },
  { key: "yearly", labelKey: "admin.overview.tokens.yearly" },
];
const TOKEN_GROUPS: { key: TokenUsageParams["group_by"]; labelKey: string }[] = [
  { key: "user", labelKey: "admin.overview.tokens.byUser" },
  { key: "model", labelKey: "admin.overview.tokens.byModel" },
  { key: "provider", labelKey: "admin.overview.tokens.byProvider" },
];

// Tokens Usage tab — its own lazy fetch (only runs while this tab is mounted),
// re-querying when a control changes. Reads the usage_daily rollup via
// /admin/usage/tokens, so any range/granularity is cheap regardless of history.
function OverviewTokens() {
  const t = useT();
  const [granularity, setGranularity] = useState<NonNullable<TokenUsageParams["granularity"]>>("daily");
  const [groupBy, setGroupBy] = useState<NonNullable<TokenUsageParams["group_by"]>>("user");
  const [userId, setUserId] = useState("");
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [userQuery, setUserQuery] = useState("");
  const [modelQuery, setModelQuery] = useState("");
  const [report, setReport] = useState<TokenUsageReport | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [dimensions, setDimensions] = useState<UsageDimensions | null>(null);
  const { error, guard } = useAsyncError();

  // Filter option sources (fetched once). Dimensions cover every scope —
  // admin-global and every user's BYOK — so private-provider usage is still
  // selectable/labelled correctly, not just admin-managed models.
  useEffect(() => {
    void guard(async () => {
      const [u, dims] = await Promise.all([api.adminListUsers(), api.adminUsageDimensions()]);
      setUsers(u);
      setDimensions(dims);
    });
  }, [guard]);

  // Report — refetched on any control change. `cancelled` guards against an
  // earlier (slower) request resolving after a later one and clobbering the
  // UI with results for a filter combination that's no longer selected.
  useEffect(() => {
    let cancelled = false;
    void guard(async () => {
      const r = await api.adminTokenUsage({ granularity, group_by: groupBy, user_id: userId, model, provider });
      if (!cancelled) setReport(r);
    });
    return () => {
      cancelled = true;
    };
  }, [guard, granularity, groupBy, userId, model, provider]);

  const providerNames = dimensions?.providers ?? [];
  const allModels = dimensions?.models ?? [];
  const modelsForProvider = provider ? allModels.filter((m) => m.provider === provider) : allModels;

  // Picking a provider that no longer matches the current model filter clears
  // it, instead of letting the model filter silently win server-side while
  // the provider dropdown still shows as selected.
  const handleProviderChange = (v: string) => {
    setProvider(v);
    if (v && model && !allModels.some((m) => m.model_id === model && m.provider === v)) {
      setModel("");
    }
  };

  const filteredUsers = keepSelected(
    filterByQuery(users, userQuery, (u) => u.display_name || u.email),
    users,
    userId,
    (u) => u.id,
  );
  const filteredModels = keepSelected(
    filterByQuery(modelsForProvider, modelQuery, (m) => m.model_id),
    modelsForProvider,
    model,
    (m) => m.model_id,
  );

  return (
    <>
      <Card padding={2} variant="muted">
        <div className="claw-token-controls">
          <SegmentedControl value={granularity} onChange={(v) => setGranularity(v as typeof granularity)} label={t("admin.overview.tokens.granularity")} size="sm">
            {GRANULARITIES.map((g) => (
              <SegmentedControlItem key={g.key} value={g.key!} label={t(g.labelKey)} />
            ))}
          </SegmentedControl>
          <SegmentedControl value={groupBy} onChange={(v) => setGroupBy(v as typeof groupBy)} label={t("admin.overview.tokens.groupBy")} size="sm">
            {TOKEN_GROUPS.map((g) => (
              <SegmentedControlItem key={g.key} value={g.key!} label={t(g.labelKey)} />
            ))}
          </SegmentedControl>
          <div className="claw-token-filter-group">
            {users.length > 10 && (
              <TextInput
                label={t("admin.overview.tokens.searchUsers")}
                isLabelHidden
                startIcon={<Icon icon={Search} size="sm" color="secondary" />}
                placeholder={t("admin.overview.tokens.searchUsersPlaceholder")}
                value={userQuery}
                onChange={setUserQuery}
                hasClear
              />
            )}
            <select className="claw-token-filter" value={userId} onChange={(e) => setUserId(e.target.value)} aria-label={t("admin.overview.tokens.filterByUser")}>
              <option value="">{t("admin.overview.tokens.allUsers")}</option>
              {filteredUsers.map((u) => (
                <option key={u.id} value={u.id}>{u.display_name || u.email}</option>
              ))}
            </select>
          </div>
          <select className="claw-token-filter" value={provider} onChange={(e) => handleProviderChange(e.target.value)} aria-label={t("admin.overview.tokens.filterByProvider")}>
            <option value="">{t("admin.overview.tokens.allProviders")}</option>
            {providerNames.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <div className="claw-token-filter-group">
            {modelsForProvider.length > 10 && (
              <TextInput
                label={t("admin.overview.tokens.searchModels")}
                isLabelHidden
                startIcon={<Icon icon={Search} size="sm" color="secondary" />}
                placeholder={t("admin.overview.tokens.searchModelsPlaceholder")}
                value={modelQuery}
                onChange={setModelQuery}
                hasClear
              />
            )}
            <select className="claw-token-filter" value={model} onChange={(e) => setModel(e.target.value)} aria-label={t("admin.overview.tokens.filterByModel")}>
              <option value="">{t("admin.overview.tokens.allModels")}</option>
              {filteredModels.map((m) => (
                <option key={m.model_id} value={m.model_id}>{m.model_id}</option>
              ))}
            </select>
          </div>
        </div>
      </Card>

      {error && <ErrorText>{error}</ErrorText>}
      {!report ? (
        <Text color="secondary">{t("admin.common.loading")}</Text>
      ) : report.series.length === 0 ? (
        <Text size="sm" color="secondary">{t("admin.overview.tokens.noUsage")}</Text>
      ) : (
        <>
          <div className="claw-row">
            <Badge variant="neutral" icon={<Icon icon={Cpu} size="xsm" />} label={t("admin.overview.tokens.turnsCount", { count: report.totals.turns.toLocaleString() })} />
            <Badge variant="neutral" label={t("admin.overview.tokens.promptCount", { count: report.totals.prompt_tokens.toLocaleString() })} />
            <Badge variant="neutral" label={t("admin.overview.tokens.completionCount", { count: report.totals.completion_tokens.toLocaleString() })} />
            <Badge
              variant="success"
              label={t("admin.overview.tokens.totalTokens", {
                count: (report.totals.prompt_tokens + report.totals.completion_tokens).toLocaleString(),
              })}
            />
          </div>
          <Card padding={3}>
            <Text weight="semibold">
              {t("admin.overview.tokens.overTimeBy", { groupBy })}
            </Text>
            <StackedBarChart buckets={report.buckets} series={report.series} />
          </Card>
          <Card padding={3}>
            <Text weight="semibold">
              {t("admin.overview.tokens.byGroupAndGranularity", { groupBy, granularity })}
            </Text>
            <ModelUsageChart
              data={report.series.map((s) => ({
                model: s.label,
                prompt_tokens: s.prompt_tokens,
                completion_tokens: s.completion_tokens,
                turns: s.turns,
              }))}
            />
          </Card>
        </>
      )}
    </>
  );
}

function SessionsByUserList({ data }: { data: SessionsByUserPoint[] }) {
  const t = useT();
  const max = Math.max(1, ...data.map((d) => d.sessions));
  return (
    <div className="claw-model-usage-list">
      {data.map((d) => (
        <div key={d.user_id} className="claw-model-usage-row">
          <div className="claw-model-usage-head">
            <span className="claw-model-usage-name claw-model-usage-name--user">{d.label}</span>
            <span className="claw-model-usage-total">
              {t("admin.overview.activity.sessionsCount", { count: d.sessions.toLocaleString(), plural: d.sessions === 1 ? "" : "s" })}
            </span>
          </div>
          <div className="claw-model-usage-bar">
            <div
              className="claw-model-usage-bar-prompt"
              style={{ width: `${(d.sessions / max) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function ModelUsageChart({ data }: { data: ModelUsagePoint[] }) {
  const t = useT();
  const totals = data.map((d) => d.prompt_tokens + d.completion_tokens);
  const max = Math.max(1, ...totals);
  return (
    <div className="claw-model-usage-list">
      {data.map((d, i) => {
        const total = totals[i];
        const promptPct = (d.prompt_tokens / max) * 100;
        const completionPct = (d.completion_tokens / max) * 100;
        return (
          <div key={d.model} className="claw-model-usage-row">
            <div className="claw-model-usage-head">
              <span className="claw-model-usage-name">{d.model}</span>
              <span className="claw-model-usage-total">
                {t("admin.overview.models.tokensAndTurns", { tokens: total.toLocaleString(), turns: d.turns.toLocaleString() })}
              </span>
            </div>
            <div className="claw-model-usage-bar">
              <div className="claw-model-usage-bar-prompt" style={{ width: `${promptPct}%` }} />
              <div
                className="claw-model-usage-bar-completion"
                style={{ width: `${completionPct}%`, left: `${promptPct}%` }}
              />
            </div>
            <div className="claw-model-usage-legend">
              <span>
                <i className="claw-legend-dot claw-legend-dot--prompt" /> {t("admin.overview.models.promptLegend")}{" "}
                {d.prompt_tokens.toLocaleString()}
              </span>
              <span>
                <i className="claw-legend-dot claw-legend-dot--completion" /> {t("admin.overview.models.completionLegend")}{" "}
                {d.completion_tokens.toLocaleString()}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- LLM Providers

// Presets so adding a provider is "pick a type", not "know the endpoint". Each
// carries LiteLLM's model-id prefix + example, and whether a base URL is
// required (only self-hosted / OpenAI-compatible servers need a custom one —
// LiteLLM already knows the hosted endpoints).
interface ProviderPreset {
  key: string;
  name: string;
  subtitle: string;
  icon: IconType;
  apiBase: string;
  needsBase: boolean;
  prefix: string;
  example: string;
}

const PROVIDER_PRESETS: ProviderPreset[] = [
  { key: "openai", name: "OpenAI", subtitle: "GPT-4o, o-series", icon: Sparkles, apiBase: "", needsBase: false, prefix: "openai/", example: "openai/gpt-4o" },
  { key: "anthropic", name: "Claude", subtitle: "Anthropic", icon: Asterisk, apiBase: "", needsBase: false, prefix: "anthropic/", example: "anthropic/claude-sonnet-5" },
  { key: "gemini", name: "Gemini", subtitle: "Google", icon: Diamond, apiBase: "", needsBase: false, prefix: "gemini/", example: "gemini/gemini-2.5-pro" },
  { key: "softnix", name: "Softnix GenAI", subtitle: "Private GenAI Platform", icon: Cpu, apiBase: "https://genai.softnix.ai/external/openai", needsBase: false, prefix: "openai/", example: "openai/gemini-3.1-pro-preview" },
  { key: "openrouter", name: "OpenRouter", subtitle: "Any model", icon: Router, apiBase: "https://openrouter.ai/api/v1", needsBase: false, prefix: "openrouter/", example: "openrouter/anthropic/claude-sonnet-5" },
  { key: "compatible", name: "OpenAI-compatible", subtitle: "vLLM, Ollama, LM Studio", icon: Server, apiBase: "", needsBase: true, prefix: "openai/", example: "openai/your-model" },
  { key: "moonshot", name: "Kimi", subtitle: "Moonshot AI", icon: Moon, apiBase: "https://api.moonshot.ai/v1", needsBase: false, prefix: "moonshot/", example: "moonshot/kimi-k2" },
  { key: "zai", name: "Z.AI", subtitle: "Zhipu GLM", icon: Zap, apiBase: "https://api.z.ai/api/paas/v4", needsBase: false, prefix: "zai/", example: "zai/glm-4.6" },
  { key: "dashscope", name: "Qwen", subtitle: "Alibaba Cloud", icon: Cloud, apiBase: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", needsBase: false, prefix: "dashscope/", example: "dashscope/qwen3-max" },
];

// Brand logos via github.com/TypingMind/model-icons (MIT) — see
// public/llm-providers/NOTICE.md. Keyed by both the preset key (picker) and
// the LiteLLM model-id prefix (saved provider/model rows), which happen to
// coincide for every branded preset. "compatible" has no fixed brand, so it
// keeps its generic Server glyph.
const PROVIDER_LOGO: Record<string, string> = {
  openai: "/llm-providers/openai.svg",
  anthropic: "/llm-providers/anthropic.webp",
  gemini: "/llm-providers/gemini.png",
  openrouter: "/llm-providers/openrouter.png",
  deepseek: "/llm-providers/deepseek.png",
  groq: "/llm-providers/groq.svg",
  softnix: "/llm-providers/softnix.png",
  moonshot: "/llm-providers/moonshot.png",
  zai: "/llm-providers/zai.png",
  dashscope: "/llm-providers/qwen.png",
};

function logoForModelId(modelId: string): string | null {
  const prefix = modelId.trim().split("/")[0];
  return PROVIDER_LOGO[prefix] ?? null;
}

// Provider brand mark: a fixed-HEIGHT, auto-width lockup (no square frame) so
// square icon glyphs (OpenAI, Anthropic, …) and wide wordmarks (Softnix) both
// render at a legible, consistent size instead of being crushed into a small
// square tile. Only the no-logo fallback gets a neutral badge for visual anchor.
function ProviderBrandTile({
  logo,
  fallback,
  size = "sm",
}: {
  logo: string | null;
  fallback: IconType;
  size?: "sm" | "lg";
}) {
  if (!logo) {
    return (
      <div className={`claw-provider-logo-fallback claw-provider-logo-fallback--${size}`}>
        <Icon icon={fallback} size={size === "lg" ? "lg" : "md"} color="secondary" />
      </div>
    );
  }
  return (
    <div className={`claw-provider-logo claw-provider-logo--${size}`}>
      <img src={logo} alt="" aria-hidden="true" />
    </div>
  );
}

const COSTS: ModelCost[] = ["low", "medium", "high", "very_high"];

// LiteLLM routes by the model id's leading prefix. A model id without a known
// prefix (e.g. a raw "qwen/qwen3.6-27b" slug) makes LiteLLM reject the call
// with "LLM Provider NOT provided" — so warn before it's saved.
const LITELLM_PREFIXES = new Set([
  "openai", "anthropic", "gemini", "vertex_ai", "openrouter", "azure", "bedrock",
  "cohere", "mistral", "groq", "ollama", "deepseek", "xai", "together_ai",
  "fireworks_ai", "perplexity", "cerebras", "replicate", "huggingface", "watsonx",
  "moonshot", "zai", "dashscope",
]);

function modelPrefixWarning(t: (key: string) => string, modelId: string): string | null {
  const id = modelId.trim();
  if (!id) return null;
  const prefix = id.split("/")[0];
  if (id.includes("/") && LITELLM_PREFIXES.has(prefix)) return null;
  return t("admin.providers.modelPrefixWarning");
}

// When a provider has a known LiteLLM prefix, admins only ever type the part
// after it — these two helpers move between "what's stored" (the full id
// LiteLLM needs) and "what's typed" (the model's real name, per the vendor's
// own docs) so the prefix never has to be remembered or re-typed.
function stripKnownPrefix(prefix: string, fullId: string): string {
  if (!prefix) return fullId;
  const withSlash = `${prefix}/`;
  return fullId.startsWith(withSlash) ? fullId.slice(withSlash.length) : fullId;
}

function composeModelId(prefix: string, rest: string): string {
  const cleaned = rest.trim().replace(/^\/+/, "");
  return prefix ? `${prefix}/${cleaned}` : cleaned;
}

// Single-select cost tier — a connected segmented control, not a row of
// independent buttons (which read as separate actions).
function CostSegmented({ value, onChange }: { value: ModelCost; onChange: (c: ModelCost) => void }) {
  const t = useT();
  return (
    <div className="claw-segmented" role="group" aria-label={t("admin.providers.costTierAria")}>
      {COSTS.map((c) => (
        <button
          key={c}
          type="button"
          className={value === c ? "is-active" : ""}
          aria-pressed={value === c}
          onClick={() => onChange(c)}
        >
          {t(COST_LABEL[c])}
        </button>
      ))}
    </div>
  );
}

// Chat vs image classification — image models are text-to-image only, kept out
// of the chat picker (they can't do tool calling) and offered in the composer's
// separate "+ Image" picker instead.
function KindSegmented({ value, onChange }: { value: ModelKind; onChange: (k: ModelKind) => void }) {
  const t = useT();
  return (
    <div className="claw-segmented" role="group" aria-label={t("admin.providers.modelTypeAria")}>
      {(["chat", "image"] as ModelKind[]).map((k) => (
        <button
          key={k}
          type="button"
          className={value === k ? "is-active" : ""}
          aria-pressed={value === k}
          onClick={() => onChange(k)}
        >
          {k === "chat" ? t("admin.providers.kindChat") : t("admin.providers.kindImage")}
        </button>
      ))}
    </div>
  );
}

// Ownership scope: "admin" drives the global Control Plane (all users see the
// models); "user" drives a person's own private "bring your own key" providers,
// visible only to them. Both mount this same component with a different `llmApi`
// (ADMIN_LLM_API vs USER_LLM_API) so there is one implementation to maintain.
export type ProvidersScope = "admin" | "user";

export function ProvidersPanel({ llmApi, scope }: { llmApi: LlmApi; scope: ProvidersScope }) {
  const t = useT();
  const [providers, setProviders] = useState<LLMProviderCfg[]>([]);
  const [adding, setAdding] = useState(false);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => llmApi.list().then((r) => setProviders(r.providers)), [llmApi]);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          {scope === "user" ? t("admin.providers.userDescription") : t("admin.providers.adminDescription")}
        </Text>
        {!adding && (
          <Button
            label={t("admin.providers.addProvider")}
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {adding && (
        <AddProviderForm llmApi={llmApi} guard={guard} reload={reload} onClose={() => setAdding(false)} />
      )}

      {providers.length === 0 && !adding ? (
        <EmptyState
          title={t("admin.providers.noProvidersTitle")}
          description={scope === "user" ? t("admin.providers.noProvidersUserDesc") : t("admin.providers.noProvidersAdminDesc")}
        />
      ) : (
        providers.map((p) => (
          <ProviderCard key={p.id} provider={p} reload={reload} guard={guard} llmApi={llmApi} scope={scope} />
        ))
      )}
    </div>
  );
}

function AddProviderForm({
  llmApi,
  guard,
  reload,
  onClose,
}: {
  llmApi: LlmApi;
  guard: (fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
  onClose: () => void;
}) {
  const t = useT();
  const [preset, setPreset] = useState<ProviderPreset | null>(null);
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const toast = useToast();

  const pick = (p: ProviderPreset) => {
    setPreset(p);
    setName(p.name);
    setApiBase(p.apiBase);
    setShowAdvanced(false);
  };

  return (
    <Card padding={2}>
      <div className="claw-panel">
        <Text size="sm" weight="semibold" color="secondary">
          {t("admin.providers.chooseProvider")}
        </Text>
        <div className="claw-preset-grid">
          {PROVIDER_PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={`claw-preset-card${preset?.key === p.key ? " is-active" : ""}`}
              onClick={() => pick(p)}
            >
              <div className="claw-preset-logo-band">
                <ProviderBrandTile logo={PROVIDER_LOGO[p.key] ?? null} fallback={p.icon} size="lg" />
              </div>
              <span className="claw-preset-name">{p.name}</span>
              <span className="claw-preset-sub">{p.subtitle}</span>
            </button>
          ))}
        </div>

        {preset && (
          <>
            <Divider />
            <TextInput label={t("admin.providers.displayName")} placeholder={preset.name} value={name} onChange={setName} />

            {preset.needsBase ? (
              <TextInput
                label={t("admin.providers.baseUrl")}
                description={t("admin.providers.baseUrlDesc")}
                placeholder="http://localhost:8000/v1"
                value={apiBase}
                onChange={setApiBase}
              />
            ) : (
              <>
                <button
                  type="button"
                  className="claw-link-btn"
                  onClick={() => setShowAdvanced((s) => !s)}
                >
                  {showAdvanced ? t("admin.providers.hideBaseUrlOverride") : t("admin.providers.showBaseUrlOverride")}
                </button>
                {showAdvanced && (
                  <TextInput
                    label={t("admin.providers.baseUrlOptional")}
                    description={t("admin.providers.baseUrlOptionalDesc")}
                    placeholder={preset.apiBase || t("admin.providers.standardEndpoint")}
                    value={apiBase}
                    onChange={setApiBase}
                  />
                )}
              </>
            )}

            <TextInput
              label={preset.needsBase ? t("admin.providers.apiKeyOptionalLocal") : t("admin.providers.apiKey")}
              type="password"
              value={apiKey}
              onChange={setApiKey}
            />

            <div className="claw-info-box">
              <Icon icon={Info} size="sm" color="secondary" />
              <Text size="sm" color="secondary">
                {t("admin.providers.addModelsInfo1")}{" "}
                <code>{preset.example.replace(preset.prefix, "")}</code>
                {t("admin.providers.addModelsInfo2")}{" "}
                <code>{preset.prefix}</code> {t("admin.providers.addModelsInfo3")}
              </Text>
            </div>

            <div className="claw-row">
              <Button
                label={t("admin.providers.createProvider")}
                variant="primary"
                icon={<Icon icon="check" size="sm" />}
                isDisabled={!name.trim() || (preset.needsBase && !apiBase.trim())}
                clickAction={() =>
                  guard(async () => {
                    await llmApi.createProvider({
                      name: name.trim(),
                      api_key: apiKey,
                      api_base: apiBase.trim(),
                      model_prefix: preset.prefix.replace(/\/$/, ""),
                    });
                    toast({ body: t("admin.providers.addedToast", { name: name.trim() }), type: "info", autoHideDuration: 2500 });
                    onClose();
                    await reload();
                  })
                }
              />
              <Button label={t("admin.common.cancel")} variant="ghost" clickAction={onClose} />
            </div>
          </>
        )}
      </div>
    </Card>
  );
}

function ProviderCard({
  provider,
  reload,
  guard,
  llmApi,
  scope,
}: {
  provider: LLMProviderCfg;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
  llmApi: LlmApi;
  scope: ProvidersScope;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [addingModel, setAddingModel] = useState(false);
  const [name, setName] = useState(provider.name);
  const [apiBase, setApiBase] = useState(provider.api_base);
  const [apiKey, setApiKey] = useState("");
  const [modelPrefix, setModelPrefix] = useState(provider.model_prefix);
  const toast = useToast();

  const logo = provider.models.length > 0 ? logoForModelId(provider.models[0].model_id) : null;

  return (
    <Card padding={2} variant={provider.enabled ? "default" : "muted"}>
      <div className="claw-row claw-row-between">
        <div className="claw-row">
          <ProviderBrandTile logo={logo} fallback={Cpu} />
          <div>
            <div className="claw-row">
              <Text weight="semibold">{provider.name}</Text>
              {provider.has_key && (
                <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={t("admin.providers.keySet")} />
              )}
            </div>
            <Text size="sm" color="secondary" as="p">
              {provider.api_base || t("admin.providers.defaultEndpoint")}
            </Text>
          </div>
        </div>
        <div className="claw-row">
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              {t("admin.providers.enabled")}
            </Text>
            <Switch
              value={provider.enabled}
              label={t("admin.providers.enableName", { name: provider.name })}
              isLabelHidden
              changeAction={(checked) =>
                guard(async () => {
                  await llmApi.updateProvider(provider.id, { enabled: checked });
                  await reload();
                })
              }
            />
          </label>
          <Button
            label={t("admin.common.edit")}
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              setName(provider.name);
              setApiBase(provider.api_base);
              setApiKey("");
              setModelPrefix(provider.model_prefix);
              setEditing((e) => !e);
            }}
          />
          <Button
            label={t("admin.common.delete")}
            icon={<Icon icon={Trash2} size="sm" />}
            size="sm"
            variant="destructive"
            clickAction={() =>
              guard(async () => {
                await llmApi.deleteProvider(provider.id);
                toast({ body: t("admin.providers.deletedToast", { name: provider.name }), type: "info", autoHideDuration: 2500 });
                await reload();
              })
            }
          />
        </div>
      </div>

      {editing && (
        <Card padding={2} variant="muted">
          <div className="claw-panel">
            <TextInput label={t("admin.providers.name")} value={name} onChange={setName} />
            <TextInput label={t("admin.providers.apiBaseUrl")} value={apiBase} onChange={setApiBase} />
            <TextInput
              label={provider.has_key ? t("admin.providers.apiKeyKeepCurrent") : t("admin.providers.apiKey")}
              type="password"
              value={apiKey}
              onChange={setApiKey}
            />
            <TextInput
              label={t("admin.providers.modelPrefixLabel")}
              description={
                provider.model_prefix
                  ? t("admin.providers.modelPrefixDescSet")
                  : t("admin.providers.modelPrefixDescUnset")
              }
              placeholder="openai"
              value={modelPrefix}
              onChange={(v) => setModelPrefix(v.trim().toLowerCase())}
            />
            <div className="claw-row">
              <Button
                label={t("admin.common.saveChanges")}
                variant="primary"
                icon={<Icon icon="check" size="sm" />}
                size="sm"
                clickAction={() =>
                  guard(async () => {
                    await llmApi.updateProvider(provider.id, {
                      name: name.trim(),
                      api_base: apiBase.trim(),
                      api_key: apiKey.trim(),
                      model_prefix: modelPrefix,
                    });
                    setEditing(false);
                    toast({ body: t("admin.providers.providerUpdatedToast"), type: "info", autoHideDuration: 2500 });
                    await reload();
                  })
                }
              />
              <Button label={t("admin.common.cancel")} variant="ghost" size="sm" clickAction={() => setEditing(false)} />
            </div>
          </div>
        </Card>
      )}

      <Divider />
      <Text size="sm" weight="semibold" color="secondary">
        {t("admin.providers.modelsHeading")}
      </Text>
      {provider.models.length === 0 && !addingModel && (
        <Text size="sm" color="secondary">
          {t("admin.providers.noModelsYet")}
        </Text>
      )}
      {provider.models.length > 0 && (
        // One grid spanning the header AND every model row (not one grid per
        // row) so columns size and align from the widest cell across the
        // whole table — real table column behavior, without a <table> element
        // (which would fight the inline Edit-expands-to-a-form pattern below).
        <div className={`claw-models-grid claw-models-grid-${scope}`}>
          <Text size="2xs" color="secondary" className="claw-models-grid-head">
            {t("admin.providers.colModel")}
          </Text>
          <Text size="2xs" color="secondary" className="claw-models-grid-head">
            {t("admin.providers.colCost")}
          </Text>
          <Text size="2xs" color="secondary" className="claw-models-grid-head">
            {t("admin.providers.colModelId")}
          </Text>
          <Text size="2xs" color="secondary" className="claw-models-grid-head">
            {t("admin.providers.colStatus")}
          </Text>
          {scope === "admin" && (
            <Text size="2xs" color="secondary" className="claw-models-grid-head">
              {t("admin.providers.colDefault")}
            </Text>
          )}
          <span className="claw-models-grid-head" />
          <span className="claw-models-grid-head" />
          {/* Chat models first, then image models — keeps the two kinds
              visually grouped within the provider. */}
          {[...provider.models]
            .sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "image" ? 1 : -1))
            .map((m) => (
              <ModelRow
                key={m.id}
                model={m}
                modelPrefix={provider.model_prefix}
                reload={reload}
                guard={guard}
                llmApi={llmApi}
                scope={scope}
              />
            ))}
        </div>
      )}

      {addingModel ? (
        <AddModelForm
          providerId={provider.id}
          modelPrefix={provider.model_prefix}
          guard={guard}
          reload={reload}
          llmApi={llmApi}
          onClose={() => setAddingModel(false)}
        />
      ) : (
        <Button
          label={t("admin.providers.addModel")}
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          variant="secondary"
          clickAction={() => setAddingModel(true)}
        />
      )}
    </Card>
  );
}

// One saved model: a read-only summary row that expands into an edit form on
// demand (so the model id / label / cost can actually be corrected in place).
function ModelRow({
  model,
  modelPrefix,
  reload,
  guard,
  llmApi,
  scope,
}: {
  model: LLMModelCfg;
  modelPrefix: string;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
  llmApi: LlmApi;
  scope: ProvidersScope;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  // Whether this model's stored id actually starts with the provider's known
  // prefix — decided silently from the data, never exposed as a UI choice.
  // Mismatched/legacy rows (no prefix, or an id that predates one) fall back
  // to plain full-id editing with no visible difference in the form.
  const prefixApplies = Boolean(modelPrefix) && model.model_id.startsWith(`${modelPrefix}/`);
  const [modelId, setModelId] = useState(
    prefixApplies ? stripKnownPrefix(modelPrefix, model.model_id) : model.model_id,
  );
  const [label, setLabel] = useState(model.label);
  const [cost, setCost] = useState<ModelCost>(model.cost);
  const [description, setDescription] = useState(model.description);
  const [kind, setKind] = useState<ModelKind>(model.kind);
  const toast = useToast();

  if (editing) {
    return (
      // Spans every column of the shared models grid — the edit form is a
      // free-form multi-field layout, not another row of table cells.
      <Card padding={2} variant="muted" className="claw-models-grid-span">
        <div className="claw-panel">
          <div className="claw-info-box">
            <Icon icon={Pencil} size="sm" color="secondary" />
            <Text size="sm" color="secondary">
              {t("admin.providers.editingPrefix")} <b>{model.label || stripKnownPrefix(modelPrefix, model.model_id)}</b>
            </Text>
          </div>
          <div className="claw-row claw-row-2col">
            <TextInput
              label={t("admin.providers.modelId")}
              placeholder={prefixApplies ? "your-model-name" : "anthropic/claude-sonnet-5"}
              value={modelId}
              onChange={setModelId}
            />
            <TextInput label={t("admin.providers.displayName")} placeholder={modelId} value={label} onChange={setLabel} />
          </div>
          {!prefixApplies && modelPrefixWarning(t, modelId) && (
            <div className="claw-info-box is-warning">
              <Icon icon={Info} size="sm" color="warning" />
              <Text size="sm" color="secondary">
                {modelPrefixWarning(t, modelId)}
              </Text>
            </div>
          )}
          <TextInput
            label={t("admin.providers.description")}
            description={t("admin.providers.descriptionHint")}
            value={description}
            onChange={setDescription}
          />
          <div className="claw-row">
            <Text size="sm" color="secondary">
              {t("admin.providers.type")}
            </Text>
            <KindSegmented value={kind} onChange={setKind} />
          </div>
          <div className="claw-row">
            <Text size="sm" color="secondary">
              {t("admin.providers.costTier")}
            </Text>
            <CostSegmented value={cost} onChange={setCost} />
          </div>
          <div className="claw-row">
            <Button
              label={t("admin.common.saveChanges")}
              variant="primary"
              icon={<Icon icon="check" size="sm" />}
              size="sm"
              isDisabled={!modelId.trim()}
              clickAction={() =>
                guard(async () => {
                  await llmApi.updateModel(model.id, {
                    model_id: prefixApplies ? composeModelId(modelPrefix, modelId) : modelId.trim(),
                    label: label.trim(),
                    cost,
                    description: description.trim(),
                    kind,
                  });
                  setEditing(false);
                  toast({ body: t("admin.providers.modelSavedToast"), type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
            <Button
              label={t("admin.common.cancel")}
              variant="ghost"
              size="sm"
              clickAction={() => {
                setModelId(prefixApplies ? stripKnownPrefix(modelPrefix, model.model_id) : model.model_id);
                setLabel(model.label);
                setCost(model.cost);
                setDescription(model.description);
                setKind(model.kind);
                setEditing(false);
              }}
            />
          </div>
        </div>
      </Card>
    );
  }

  return (
    <>
      <div className="claw-model-name-cell">
        <Text className="claw-model-label">{model.label || stripKnownPrefix(modelPrefix, model.model_id)}</Text>
        {model.is_default && (
          <Badge variant="purple" icon={<Icon icon={Star} size="xsm" />} label={t("admin.providers.defaultBadge")} />
        )}
      </div>
      <div className="claw-model-cost-cell">
        {model.kind === "image" && <Badge variant="neutral" label={t("admin.providers.kindImage")} />}
        <span className={`claw-cost claw-cost-${model.cost}`}>{t(COST_LABEL[model.cost])}</span>
      </div>
      <Text size="sm" color="secondary" className="claw-model-id">
        {stripKnownPrefix(modelPrefix, model.model_id)}
      </Text>
      <label className="claw-toggle-inline">
        <Text size="sm" color="secondary">
          {model.enabled ? t("admin.providers.on") : t("admin.providers.off")}
        </Text>
        <Switch
          value={model.enabled}
          label={t("admin.providers.enableName", { name: model.label })}
          isLabelHidden
          changeAction={(checked) =>
            guard(async () => {
              await llmApi.updateModel(model.id, { enabled: checked });
              await reload();
            })
          }
        />
      </label>
      {/* The auto-selected default is an admin-global concept; a user's private
          model is never the global default, so this control is admin-only. */}
      {scope === "admin" && (
        <Button
          label={model.is_default ? t("admin.providers.defaultLabel") : t("admin.providers.setDefault")}
          size="sm"
          variant={model.is_default ? "secondary" : "ghost"}
          // An image model can never be the chat default.
          isDisabled={model.is_default || !model.enabled || model.kind === "image"}
          clickAction={() =>
            guard(async () => {
              await llmApi.updateModel(model.id, { is_default: true });
              await reload();
            })
          }
        />
      )}
      <Button
        label={t("admin.common.edit")}
        icon={<Icon icon={Pencil} size="sm" />}
        size="sm"
        variant="ghost"
        clickAction={() => setEditing(true)}
      />
      <Button
        label={t("admin.providers.remove")}
        icon={<Icon icon={Trash2} size="sm" />}
        size="sm"
        variant="ghost"
        clickAction={() =>
          guard(async () => {
            await llmApi.deleteModel(model.id);
            await reload();
          })
        }
      />
    </>
  );
}

function AddModelForm({
  providerId,
  modelPrefix,
  guard,
  reload,
  llmApi,
  onClose,
}: {
  providerId: string;
  modelPrefix: string;
  guard: (fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
  llmApi: LlmApi;
  onClose: () => void;
}) {
  const t = useT();
  const [modelId, setModelId] = useState("");
  const [label, setLabel] = useState("");
  const [cost, setCost] = useState<ModelCost>("medium");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<ModelKind>("chat");
  const toast = useToast();

  const hasPrefix = Boolean(modelPrefix);

  return (
    <Card padding={2} variant="muted">
      <div className="claw-panel">
        <div className="claw-info-box">
          <Icon icon={Plus} size="sm" color="secondary" />
          <Text size="sm" color="secondary">
            {t("admin.providers.addingModelInfo")}
          </Text>
        </div>
        <div className="claw-row claw-row-2col">
          <TextInput
            label={t("admin.providers.modelId")}
            placeholder={hasPrefix ? "your-model-name" : "anthropic/claude-sonnet-5"}
            value={modelId}
            onChange={setModelId}
          />
          <TextInput label={t("admin.providers.displayName")} placeholder={modelId || "Claude Sonnet 5"} value={label} onChange={setLabel} />
        </div>
        {!hasPrefix && modelPrefixWarning(t, modelId) && (
          <div className="claw-info-box is-warning">
            <Icon icon={Info} size="sm" color="warning" />
            <Text size="sm" color="secondary">
              {modelPrefixWarning(t, modelId)}
            </Text>
          </div>
        )}
        <TextInput
          label={t("admin.providers.description")}
          description={t("admin.providers.descriptionHint")}
          value={description}
          onChange={setDescription}
        />
        <div className="claw-row">
          <Text size="sm" color="secondary">
            {t("admin.providers.type")}
          </Text>
          <KindSegmented value={kind} onChange={setKind} />
        </div>
        <div className="claw-row">
          <Text size="sm" color="secondary">
            {t("admin.providers.costTier")}
          </Text>
          <CostSegmented value={cost} onChange={setCost} />
        </div>
        <div className="claw-row">
          <Button
            label={t("admin.providers.addModel")}
            variant="primary"
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            isDisabled={!modelId.trim()}
            clickAction={() =>
              guard(async () => {
                await llmApi.createModel(providerId, {
                  model_id: hasPrefix ? composeModelId(modelPrefix, modelId) : modelId.trim(),
                  label: label.trim(),
                  cost,
                  description: description.trim(),
                  kind,
                });
                toast({ body: t("admin.providers.modelAddedToast"), type: "info", autoHideDuration: 2500 });
                onClose();
                await reload();
              })
            }
          />
          <Button label={t("admin.common.cancel")} variant="ghost" size="sm" clickAction={onClose} />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- Plans

// Usage-tier plans (Free/Plus/Pro/…): each sets model cost ceilings plus
// daily / per-minute quotas, and can be assigned to groups or individual
// users. Structurally mirrors the LLM Providers panel — a list of Cards, each
// with an inline Edit form, plus an Add form toggled from the header.
function PlansPanel() {
  const t = useT();
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [adding, setAdding] = useState(false);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.adminListPlans().then(setPlans), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  // Ranked low→high so the list reads as a ladder (Free → Plus → Pro → …).
  const sorted = [...plans].sort((a, b) => a.rank - b.rank);

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          {t("admin.plans.intro")}
        </Text>
        {!adding && (
          <Button
            label={t("admin.plans.addPlan")}
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {adding && <AddPlanForm guard={guard} reload={reload} onClose={() => setAdding(false)} />}

      {plans.length === 0 && !adding ? (
        <EmptyState
          title={t("admin.plans.noPlansTitle")}
          description={t("admin.plans.noPlansDesc")}
        />
      ) : (
        sorted.map((p) => <PlanCard key={p.id} plan={p} reload={reload} guard={guard} />)
      )}
    </div>
  );
}

// Parse a number-input string, guarding NaN/negatives to 0 (which every quota
// field treats as "unlimited"/"inherit", so a blank/garbage entry is safe).
function planNum(s: string): number {
  const n = Number(s);
  return Number.isNaN(n) ? 0 : Math.max(0, Math.trunc(n));
}

function PlanCard({
  plan,
  reload,
  guard,
}: {
  plan: PlanInfo;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(plan.name);
  const [rank, setRank] = useState(String(plan.rank));
  const [maxChatCost, setMaxChatCost] = useState<ModelCost>(plan.max_chat_cost);
  const [maxImageCost, setMaxImageCost] = useState<ModelCost>(plan.max_image_cost);
  const [allowImage, setAllowImage] = useState(plan.allow_image);
  const [messagesPerDay, setMessagesPerDay] = useState(String(plan.messages_per_day));
  const [imagesPerDay, setImagesPerDay] = useState(String(plan.images_per_day));
  const [turnsPerMinute, setTurnsPerMinute] = useState(String(plan.turns_per_minute));
  const [isDefault, setIsDefault] = useState(plan.is_default);
  const toast = useToast();

  const resetForm = () => {
    setName(plan.name);
    setRank(String(plan.rank));
    setMaxChatCost(plan.max_chat_cost);
    setMaxImageCost(plan.max_image_cost);
    setAllowImage(plan.allow_image);
    setMessagesPerDay(String(plan.messages_per_day));
    setImagesPerDay(String(plan.images_per_day));
    setTurnsPerMinute(String(plan.turns_per_minute));
    setIsDefault(plan.is_default);
  };

  return (
    <Card padding={2} variant="default">
      <div className="claw-row claw-row-between">
        <div>
          <div className="claw-row">
            <Text weight="semibold">{plan.name}</Text>
            {plan.is_default && (
              <Badge variant="purple" icon={<Icon icon={Star} size="xsm" />} label={t("admin.overview.plans.default")} />
            )}
            <Badge variant="neutral" label={t("admin.plans.rankBadge", { rank: String(plan.rank) })} />
            <Badge
              variant="neutral"
              icon={<Icon icon={Users} size="xsm" />}
              label={t("admin.plans.userCount", { count: String(plan.user_count ?? 0), plural: (plan.user_count ?? 0) === 1 ? "" : "s" })}
            />
          </div>
          <div className="claw-plan-specs">
            <span>{t("admin.plans.chatSpec", { cost: t(COST_LABEL[plan.max_chat_cost]) })}</span>
            <span>
              {plan.allow_image
                ? t("admin.plans.imageSpecOn", { cost: t(COST_LABEL[plan.max_image_cost]) })
                : t("admin.plans.imageSpecOff")}
            </span>
            <span>
              {plan.messages_per_day > 0
                ? t("admin.plans.messagesPerDaySpec", { count: plan.messages_per_day.toLocaleString() })
                : t("admin.plans.unlimitedMessages")}
            </span>
            <span>
              {plan.images_per_day > 0
                ? t("admin.plans.imagesPerDaySpec", { count: plan.images_per_day.toLocaleString() })
                : t("admin.plans.unlimitedImages")}
            </span>
            <span>
              {plan.turns_per_minute > 0
                ? t("admin.plans.turnsPerMinSpec", { count: plan.turns_per_minute.toLocaleString() })
                : t("admin.plans.globalTurnRate")}
            </span>
          </div>
        </div>
        <div className="claw-row">
          {!plan.is_default && (
            <Button
              label={t("admin.plans.makeDefault")}
              size="sm"
              variant="ghost"
              clickAction={() =>
                guard(async () => {
                  await api.adminSetDefaultPlan(plan.id);
                  toast({ body: t("admin.plans.nowDefaultToast", { name: plan.name }), type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
          )}
          <Button
            label={t("admin.common.edit")}
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              resetForm();
              setEditing((e) => !e);
            }}
          />
          <Button
            label={t("admin.common.delete")}
            icon={<Icon icon={Trash2} size="sm" />}
            size="sm"
            variant="destructive"
            clickAction={() =>
              guard(async () => {
                if (!window.confirm(t("admin.plans.deleteConfirm", { name: plan.name }))) {
                  return;
                }
                await api.adminDeletePlan(plan.id);
                toast({ body: t("admin.plans.deletedToast", { name: plan.name }), type: "info", autoHideDuration: 2500 });
                await reload();
              })
            }
          />
        </div>
      </div>

      {editing && (
        <Card padding={2} variant="muted">
          <div className="claw-panel">
            <div className="claw-row claw-row-2col">
              <TextInput label={t("admin.providers.name")} value={name} onChange={setName} />
              <TextInput
                label={t("admin.plans.rank")}
                description={t("admin.plans.rankDesc")}
                value={rank}
                onChange={setRank}
              />
            </div>
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.plans.maxChatCost")}
              </Text>
              <CostSegmented value={maxChatCost} onChange={setMaxChatCost} />
            </div>
            <label className="claw-toggle-inline">
              <Switch value={allowImage} label={t("admin.plans.allowImageGen")} isLabelHidden changeAction={setAllowImage} />
              <Text size="sm" color="secondary">
                {t("admin.plans.allowImageGen")}
              </Text>
            </label>
            {allowImage && (
              <div className="claw-row">
                <Text size="sm" color="secondary">
                  {t("admin.plans.maxImageCost")}
                </Text>
                <CostSegmented value={maxImageCost} onChange={setMaxImageCost} />
              </div>
            )}
            <div className="claw-row claw-row-2col">
              <TextInput
                label={t("admin.plans.messagesPerDay")}
                description={t("admin.plans.zeroUnlimited")}
                value={messagesPerDay}
                onChange={setMessagesPerDay}
              />
              <TextInput
                label={t("admin.plans.imagesPerDay")}
                description={t("admin.plans.zeroUnlimited")}
                value={imagesPerDay}
                onChange={setImagesPerDay}
              />
            </div>
            <TextInput
              label={t("admin.plans.turnsPerMinute")}
              description={t("admin.plans.zeroInheritGlobal")}
              value={turnsPerMinute}
              onChange={setTurnsPerMinute}
            />
            <label className="claw-toggle-inline">
              <Switch value={isDefault} label={t("admin.plans.defaultPlan")} isLabelHidden changeAction={setIsDefault} />
              <Text size="sm" color="secondary">
                {t("admin.plans.defaultPlanHint")}
              </Text>
            </label>
            <div className="claw-row">
              <Button
                label={t("admin.common.saveChanges")}
                variant="primary"
                icon={<Icon icon="check" size="sm" />}
                size="sm"
                isDisabled={!name.trim()}
                clickAction={() =>
                  guard(async () => {
                    await api.adminUpdatePlan(plan.id, {
                      name: name.trim(),
                      rank: planNum(rank),
                      max_chat_cost: maxChatCost,
                      allow_image: allowImage,
                      max_image_cost: maxImageCost,
                      messages_per_day: planNum(messagesPerDay),
                      images_per_day: planNum(imagesPerDay),
                      turns_per_minute: planNum(turnsPerMinute),
                      is_default: isDefault,
                    });
                    setEditing(false);
                    toast({ body: t("admin.plans.planSavedToast"), type: "info", autoHideDuration: 2500 });
                    await reload();
                  })
                }
              />
              <Button
                label={t("admin.common.cancel")}
                variant="ghost"
                size="sm"
                clickAction={() => {
                  resetForm();
                  setEditing(false);
                }}
              />
            </div>
          </div>
        </Card>
      )}
    </Card>
  );
}

function AddPlanForm({
  guard,
  reload,
  onClose,
}: {
  guard: (fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
  onClose: () => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [rank, setRank] = useState("0");
  const [maxChatCost, setMaxChatCost] = useState<ModelCost>("medium");
  const [maxImageCost, setMaxImageCost] = useState<ModelCost>("medium");
  const [allowImage, setAllowImage] = useState(false);
  const [messagesPerDay, setMessagesPerDay] = useState("0");
  const [imagesPerDay, setImagesPerDay] = useState("0");
  const [turnsPerMinute, setTurnsPerMinute] = useState("0");
  const [isDefault, setIsDefault] = useState(false);
  const toast = useToast();

  return (
    <Card padding={2}>
      <div className="claw-panel">
        <div className="claw-info-box">
          <Icon icon={Plus} size="sm" color="secondary" />
          <Text size="sm" color="secondary">
            {t("admin.plans.addingPlanInfo")}
          </Text>
        </div>
        <div className="claw-row claw-row-2col">
          <TextInput label={t("admin.providers.name")} placeholder="e.g. Pro" value={name} onChange={setName} />
          <TextInput
            label={t("admin.plans.rank")}
            description={t("admin.plans.rankDesc")}
            value={rank}
            onChange={setRank}
          />
        </div>
        <div className="claw-row">
          <Text size="sm" color="secondary">
            {t("admin.plans.maxChatCost")}
          </Text>
          <CostSegmented value={maxChatCost} onChange={setMaxChatCost} />
        </div>
        <label className="claw-toggle-inline">
          <Switch value={allowImage} label={t("admin.plans.allowImageGen")} isLabelHidden changeAction={setAllowImage} />
          <Text size="sm" color="secondary">
            {t("admin.plans.allowImageGen")}
          </Text>
        </label>
        {allowImage && (
          <div className="claw-row">
            <Text size="sm" color="secondary">
              {t("admin.plans.maxImageCost")}
            </Text>
            <CostSegmented value={maxImageCost} onChange={setMaxImageCost} />
          </div>
        )}
        <div className="claw-row claw-row-2col">
          <TextInput
            label={t("admin.plans.messagesPerDay")}
            description={t("admin.plans.zeroUnlimited")}
            value={messagesPerDay}
            onChange={setMessagesPerDay}
          />
          <TextInput
            label={t("admin.plans.imagesPerDay")}
            description={t("admin.plans.zeroUnlimited")}
            value={imagesPerDay}
            onChange={setImagesPerDay}
          />
        </div>
        <TextInput
          label={t("admin.plans.turnsPerMinute")}
          description={t("admin.plans.zeroInheritGlobal")}
          value={turnsPerMinute}
          onChange={setTurnsPerMinute}
        />
        <label className="claw-toggle-inline">
          <Switch value={isDefault} label={t("admin.plans.defaultPlan")} isLabelHidden changeAction={setIsDefault} />
          <Text size="sm" color="secondary">
            {t("admin.plans.defaultPlanHint")}
          </Text>
        </label>
        <div className="claw-row">
          <Button
            label={t("admin.plans.createPlan")}
            variant="primary"
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            isDisabled={!name.trim()}
            clickAction={() =>
              guard(async () => {
                const body: PlanCreate = {
                  name: name.trim(),
                  rank: planNum(rank),
                  max_chat_cost: maxChatCost,
                  allow_image: allowImage,
                  max_image_cost: maxImageCost,
                  messages_per_day: planNum(messagesPerDay),
                  images_per_day: planNum(imagesPerDay),
                  turns_per_minute: planNum(turnsPerMinute),
                  is_default: isDefault,
                };
                await api.adminCreatePlan(body);
                toast({ body: t("admin.providers.addedToast", { name: name.trim() }), type: "info", autoHideDuration: 2500 });
                onClose();
                await reload();
              })
            }
          />
          <Button label={t("admin.common.cancel")} variant="ghost" size="sm" clickAction={onClose} />
        </div>
      </div>
    </Card>
  );
}

// Single-select plan picker (chips) for the Add/Edit user forms and the group
// manager — mirrors GroupPicker, minus the inline "create" (plans are made in
// the Plans section). A "Default plan" chip clears any explicit assignment.
function PlanPicker({
  plans,
  value,
  onChange,
  label,
}: {
  plans: PlanInfo[];
  value: string | null;
  onChange: (id: string | null) => void;
  label?: string;
}) {
  const t = useT();
  return (
    <div className="claw-field-group">
      <Text size="sm" color="secondary">
        {label ?? t("admin.plans.planLabel")}
      </Text>
      <div className="claw-row">
        <Button
          label={t("admin.plans.defaultPlan")}
          size="sm"
          variant={value === null ? "primary" : "secondary"}
          clickAction={() => onChange(null)}
        />
        {plans.map((p) => (
          <Button
            key={p.id}
            label={p.name}
            size="sm"
            variant={value === p.id ? "primary" : "secondary"}
            clickAction={() => onChange(p.id)}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Guardrails

const ACTION_VARIANT: Record<string, "error" | "warning" | "neutral"> = {
  block: "error",
  mask: "warning",
  monitor: "neutral",
};

// Reused wherever a rule's raw action/severity enum value is shown to the
// admin, so the same word isn't duplicated as both a state value and its
// translated label (mirrors the COST_LABEL pattern for model cost tiers).
const ACTION_LABEL_KEY: Record<string, string> = {
  block: "admin.guardrails.actionBlock",
  mask: "admin.guardrails.actionMask",
  monitor: "admin.guardrails.actionMonitor",
};
const SEVERITY_LABEL_KEY: Record<string, string> = {
  low: "admin.guardrails.severityLow",
  medium: "admin.guardrails.severityMedium",
  high: "admin.guardrails.severityHigh",
  critical: "admin.guardrails.severityCritical",
};

// Runs sample text through the live policy so admins can prove the rules fire.
function GuardrailTester({ guard }: { guard: (fn: () => Promise<void>) => Promise<void> }) {
  const t = useT();
  const [text, setText] = useState("");
  const [result, setResult] = useState<GuardrailTestResult | null>(null);

  const ACTION_TONE: Record<string, string> = {
    block: "error",
    mask: "warning",
    monitor: "neutral",
  };

  return (
    <Card padding={2} variant="muted">
      <div className="claw-panel">
        <div>
          <Text weight="semibold">{t("admin.guardrails.testTitle")}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.guardrails.testDesc")}
          </Text>
        </div>
        <TextArea
          label={t("admin.guardrails.sampleText")}
          isLabelHidden
          placeholder="e.g. My card is 4111 1111 1111 1111 and email jane@acme.com"
          value={text}
          onChange={setText}
          rows={3}
        />
        <div className="claw-row">
          <Button
            label={t("admin.guardrails.runTest")}
            variant="primary"
            size="sm"
            isDisabled={!text.trim()}
            clickAction={() =>
              guard(async () => {
                setResult(await api.adminTestGuardrails(text));
              })
            }
          />
          {result && (
            <Button
              label={t("admin.guardrails.clear")}
              variant="ghost"
              size="sm"
              clickAction={() => {
                setResult(null);
                setText("");
              }}
            />
          )}
        </div>
        {result && (
          <div className="claw-panel">
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.guardrails.result")}
              </Text>
              {result.action ? (
                <Badge
                  variant={(ACTION_TONE[result.action] ?? "neutral") as "error" | "warning" | "neutral"}
                  label={
                    result.monitor_only
                      ? t("admin.guardrails.actionMonitorOnly", { action: t(ACTION_LABEL_KEY[result.action] ?? result.action) })
                      : t(ACTION_LABEL_KEY[result.action] ?? result.action)
                  }
                />
              ) : (
                <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={t("admin.guardrails.noMatch")} />
              )}
              {result.matched_rules.map((m) => (
                <Badge key={m.name} variant="neutral" label={`${m.name} · ${m.scope}`} />
              ))}
            </div>
            {result.action === "mask" && (
              <div className="claw-info-box">
                <Icon icon={Info} size="sm" color="secondary" />
                <Text size="sm" color="secondary">
                  {t("admin.guardrails.outputWouldBeSentAs")} <code>{result.masked}</code>
                </Text>
              </div>
            )}
            {result.action === "block" && (
              <div className="claw-info-box is-warning">
                <Icon icon={Info} size="sm" color="warning" />
                <Text size="sm" color="secondary">
                  {t("admin.guardrails.blockedWarning")}
                </Text>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function GuardrailsPanel() {
  const t = useT();
  const [rules, setRules] = useState<GuardrailRule[]>([]);
  const [monitorOnly, setMonitorOnly] = useState(false);
  const [exempt, setExempt] = useState<string[]>([]);
  const [newExempt, setNewExempt] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [pattern, setPattern] = useState("");
  const [kind, setKind] = useState<"keyword" | "regex">("keyword");
  const [action, setAction] = useState<"mask" | "block" | "monitor">("block");
  const { error, guard } = useAsyncError();

  const reload = useCallback(
    () =>
      api.adminGuardrails().then((r) => {
        setRules(r.rules);
        setMonitorOnly(r.monitor_only);
        setExempt(r.tool_args_exempt);
      }),
    [],
  );

  const saveExempt = (next: string[]) =>
    guard(async () => {
      const r = await api.adminSetToolArgsExempt(monitorOnly, next);
      setExempt(r.tool_args_exempt);
    });
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  return (
    <div className="claw-panel">
      <Card padding={2} variant="muted">
        <div className="claw-row claw-row-between">
          <div>
            <Text weight="semibold">{t("admin.guardrails.enforcementMode")}</Text>
            <Text size="sm" color="secondary" as="p">
              {monitorOnly ? t("admin.guardrails.monitorOnlyDesc") : t("admin.guardrails.enforcingDesc")}
            </Text>
          </div>
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              {t("admin.guardrails.enforce")}
            </Text>
            <Switch
              value={!monitorOnly}
              label={t("admin.guardrails.enforceGuardrails")}
              isLabelHidden
              changeAction={(checked) =>
                guard(async () => {
                  await api.adminSetMonitorOnly(!checked);
                  await reload();
                })
              }
            />
          </label>
        </div>
      </Card>

      <Card padding={2} variant="muted">
        <div className="claw-panel">
          <div>
            <Text weight="semibold">{t("admin.guardrails.exemptToolsTitle")}</Text>
            <Text size="sm" color="secondary" as="p">
              {t("admin.guardrails.exemptToolsDesc1")}{" "}
              <code>[REDACTED_EMAIL]</code>
              {t("admin.guardrails.exemptToolsDesc2")}{" "}
              <code>mcp_outlook_*</code> {t("admin.guardrails.exemptToolsDesc3")}{" "}
              <code>mcp_*_send_*</code>. {t("admin.guardrails.exemptToolsDesc4")}
            </Text>
          </div>
          <div className="claw-chip-list">
            {exempt.length === 0 ? (
              <Text size="sm" color="secondary">
                {t("admin.guardrails.noExemptions")}
              </Text>
            ) : (
              exempt.map((g) => (
                <span key={g} className="claw-chip">
                  <code>{g}</code>
                  <button
                    type="button"
                    aria-label={t("admin.guardrails.removeExemption", { name: g })}
                    className="claw-chip-x"
                    onClick={() => saveExempt(exempt.filter((x) => x !== g))}
                  >
                    <Icon icon="close" size="xsm" />
                  </button>
                </span>
              ))
            )}
          </div>
          <div className="claw-row">
            <TextInput
              label={t("admin.guardrails.addExemption")}
              isLabelHidden
              placeholder="mcp_outlook_*"
              value={newExempt}
              onChange={setNewExempt}
            />
            <Button
              label={t("admin.guardrails.add")}
              size="sm"
              variant="secondary"
              icon={<Icon icon={Plus} size="sm" />}
              isDisabled={!newExempt.trim() || exempt.includes(newExempt.trim())}
              clickAction={() =>
                saveExempt([...exempt, newExempt.trim()]).then(() => setNewExempt(""))
              }
            />
          </div>
        </div>
      </Card>

      <GuardrailTester guard={guard} />

      <div className="claw-row claw-row-between">
        <Text color="secondary">
          {t("admin.guardrails.rulesIntro")}
        </Text>
        {!adding && (
          <Button
            label={t("admin.guardrails.addRule")}
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {adding && (
        <Card padding={2}>
          <div className="claw-panel">
            <TextInput label={t("admin.guardrails.ruleName")} value={name} onChange={setName} />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.guardrails.matchType")}
              </Text>
              <Button
                label={t("admin.guardrails.keyword")}
                size="sm"
                variant={kind === "keyword" ? "primary" : "secondary"}
                clickAction={() => setKind("keyword")}
              />
              <Button
                label={t("admin.guardrails.regex")}
                size="sm"
                variant={kind === "regex" ? "primary" : "secondary"}
                clickAction={() => setKind("regex")}
              />
            </div>
            <TextInput
              label={kind === "keyword" ? t("admin.guardrails.keywordPhrase") : t("admin.guardrails.regularExpression")}
              value={pattern}
              onChange={setPattern}
            />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.guardrails.action")}
              </Text>
              {(["block", "mask", "monitor"] as const).map((a) => (
                <Button
                  key={a}
                  label={t(ACTION_LABEL_KEY[a])}
                  size="sm"
                  variant={action === a ? "primary" : "secondary"}
                  clickAction={() => setAction(a)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Button
                label={t("admin.guardrails.createRule")}
                variant="primary"
                icon={<Icon icon="check" size="sm" />}
                isDisabled={!name.trim() || !pattern.trim()}
                clickAction={() =>
                  guard(async () => {
                    await api.adminCreateRule({ name: name.trim(), kind, pattern, action });
                    setAdding(false);
                    setName("");
                    setPattern("");
                    await reload();
                  })
                }
              />
              <Button label={t("admin.common.cancel")} variant="ghost" clickAction={() => setAdding(false)} />
            </div>
          </div>
        </Card>
      )}

      {rules.map((r) => (
        <RuleCard key={r.id} rule={r} reload={reload} guard={guard} />
      ))}
    </div>
  );
}

function RuleCard({
  rule,
  reload,
  guard,
}: {
  rule: GuardrailRule;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(rule.name);
  const [pattern, setPattern] = useState(rule.pattern);
  const [action, setAction] = useState(rule.action);
  const [severity, setSeverity] = useState(rule.severity);

  return (
    <Card padding={2} variant={rule.enabled ? "default" : "muted"}>
      <div className="claw-row claw-row-between">
        <div>
          <div className="claw-row">
            <Text weight="semibold">{rule.name}</Text>
            <Badge variant={ACTION_VARIANT[rule.action] ?? "neutral"} label={t(ACTION_LABEL_KEY[rule.action] ?? rule.action)} />
            <Badge variant="neutral" label={t(SEVERITY_LABEL_KEY[rule.severity] ?? rule.severity)} />
            {rule.is_builtin && <Badge variant="neutral" label={t("admin.guardrails.builtIn")} />}
          </div>
          <Text size="sm" color="secondary" as="p" className="claw-rule-pattern">
            {rule.pattern}
          </Text>
        </div>
        <div className="claw-row">
          <Switch
            value={rule.enabled}
            label={t("admin.providers.enableName", { name: rule.name })}
            isLabelHidden
            changeAction={(checked) =>
              guard(async () => {
                await api.adminUpdateRule(rule.id, { enabled: checked });
                await reload();
              })
            }
          />
          <Button
            label={t("admin.common.edit")}
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              setName(rule.name);
              setPattern(rule.pattern);
              setAction(rule.action);
              setSeverity(rule.severity);
              setEditing((e) => !e);
            }}
          />
          {!rule.is_builtin && (
            <Button
              label={t("admin.common.delete")}
              icon={<Icon icon={Trash2} size="sm" />}
              size="sm"
              variant="destructive"
              clickAction={() =>
                guard(async () => {
                  await api.adminDeleteRule(rule.id);
                  await reload();
                })
              }
            />
          )}
        </div>
      </div>

      {editing && (
        <Card padding={2} variant="muted">
          <div className="claw-panel">
            <TextInput
              label={t("admin.providers.name")}
              value={name}
              onChange={setName}
              isDisabled={rule.is_builtin}
            />
            <TextInput
              label={rule.is_builtin ? t("admin.guardrails.patternBuiltinReadonly") : t("admin.guardrails.patternRegex")}
              value={pattern}
              onChange={setPattern}
              isDisabled={rule.is_builtin}
            />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.guardrails.action")}
              </Text>
              {(["block", "mask", "monitor"] as const).map((a) => (
                <Button
                  key={a}
                  label={t(ACTION_LABEL_KEY[a])}
                  size="sm"
                  variant={action === a ? "primary" : "secondary"}
                  clickAction={() => setAction(a)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Text size="sm" color="secondary">
                {t("admin.guardrails.severity")}
              </Text>
              {(["low", "medium", "high", "critical"] as const).map((s) => (
                <Button
                  key={s}
                  label={t(SEVERITY_LABEL_KEY[s])}
                  size="sm"
                  variant={severity === s ? "primary" : "secondary"}
                  clickAction={() => setSeverity(s)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Button
                label={t("admin.common.saveChanges")}
                variant="primary"
                icon={<Icon icon="check" size="sm" />}
                size="sm"
                clickAction={() =>
                  guard(async () => {
                    // Built-in patterns/names are fixed; only action/severity are editable.
                    await api.adminUpdateRule(rule.id, {
                      action,
                      severity,
                      ...(rule.is_builtin ? {} : { name: name.trim(), pattern, kind: "regex" }),
                    });
                    setEditing(false);
                    await reload();
                  })
                }
              />
              <Button label={t("admin.common.cancel")} variant="ghost" size="sm" clickAction={() => setEditing(false)} />
            </div>
          </div>
        </Card>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- OAuth apps

function OAuthAppsPanel() {
  const t = useT();
  const [apps, setApps] = useState<OAuthAppsInfo | null>(null);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.adminGetOAuthApps().then(setApps), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!apps) return <Text color="secondary">{t("admin.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("admin.oauth.intro")}</Text>
      <OAuthAppCard
        provider="google"
        label="Google"
        app={apps.google}
        redirectUri={apps.redirect_uris.google}
        loginRedirectUri={apps.login_redirect_uris.google}
        onSaved={reload}
        guard={guard}
      />
      <OAuthAppCard
        provider="microsoft"
        label="Microsoft"
        app={apps.microsoft}
        redirectUri={apps.redirect_uris.microsoft}
        loginRedirectUri={apps.login_redirect_uris.microsoft}
        onSaved={reload}
        guard={guard}
        withTenant
      />
    </div>
  );
}

// Concise, do-able setup steps + the exact scopes this app actually requests
// (sign-in via OIDC + the connector OAuth flows). Kept in sync with
// claw/auth/oidc.py and claw/core/connector_presets.py.
const OAUTH_GUIDE: Record<
  "google" | "microsoft",
  {
    consoleUrl: string;
    consoleLabelKey: string;
    stepKeys: string[];
    scopes: { labelKey: string; items: string[] }[];
  }
> = {
  google: {
    consoleUrl: "https://console.cloud.google.com/apis/credentials",
    consoleLabelKey: "admin.oauth.google.consoleLabel",
    stepKeys: [
      "admin.oauth.google.step1",
      "admin.oauth.google.step2",
      "admin.oauth.google.step3",
      "admin.oauth.google.step4",
      "admin.oauth.google.step5",
      "admin.oauth.google.step6",
    ],
    scopes: [
      { labelKey: "admin.oauth.scopes.signIn", items: ["openid", "email", "profile"] },
      {
        labelKey: "admin.oauth.scopes.gmail",
        items: ["https://www.googleapis.com/auth/gmail.modify"],
      },
      {
        labelKey: "admin.oauth.scopes.sheets",
        items: ["https://www.googleapis.com/auth/spreadsheets"],
      },
    ],
  },
  microsoft: {
    consoleUrl:
      "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
    consoleLabelKey: "admin.oauth.microsoft.consoleLabel",
    stepKeys: [
      "admin.oauth.microsoft.step1",
      "admin.oauth.microsoft.step2",
      "admin.oauth.microsoft.step3",
      "admin.oauth.microsoft.step4",
      "admin.oauth.microsoft.step5",
    ],
    scopes: [
      { labelKey: "admin.oauth.scopes.signIn", items: ["openid", "email", "profile"] },
      {
        labelKey: "admin.oauth.scopes.msConnectors",
        items: [
          "offline_access",
          "User.Read",
          "Mail.ReadWrite",
          "Mail.Send",
          "Calendars.ReadWrite",
          "Files.ReadWrite.All",
        ],
      },
    ],
  },
};

function OAuthQuickStart({ provider }: { provider: "google" | "microsoft" }) {
  const t = useT();
  const guide = OAUTH_GUIDE[provider];
  return (
    <details className="claw-guide">
      <summary>{t("admin.oauth.quickStart")}</summary>
      <div className="claw-guide-body">
        <ol className="claw-telegram-steps">
          {guide.stepKeys.map((key, i) => (
            <li key={i}>{t(key)}</li>
          ))}
        </ol>
        {guide.scopes.map((group) => (
          <div key={group.labelKey} className="claw-setup-field">
            <Text size="sm" color="secondary">
              {t("admin.oauth.scopesFor", { label: t(group.labelKey) })}
            </Text>
            <div className="claw-scope-chips">
              {group.items.map((scope) => (
                <code key={scope}>{scope}</code>
              ))}
            </div>
          </div>
        ))}
        <div className="claw-row">
          <Button
            label={t(guide.consoleLabelKey)}
            icon={<Icon icon={ExternalLink} size="sm" />}
            variant="secondary"
            size="sm"
            href={guide.consoleUrl}
            target="_blank"
            rel="noopener noreferrer"
          />
        </div>
      </div>
    </details>
  );
}

function OAuthAppCard({
  provider,
  label,
  app,
  redirectUri,
  loginRedirectUri,
  onSaved,
  guard,
  withTenant = false,
}: {
  provider: "google" | "microsoft";
  label: string;
  app: { client_id: string; tenant: string; has_secret: boolean };
  redirectUri: string;
  loginRedirectUri: string;
  onSaved: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
  withTenant?: boolean;
}) {
  const t = useT();
  const [clientId, setClientId] = useState(app.client_id);
  const [clientSecret, setClientSecret] = useState("");
  const [tenant, setTenant] = useState(app.tenant);
  const toast = useToast();

  return (
    <Card padding={2}>
      <div className="claw-panel">
        <div className="claw-row">
          <Text weight="semibold">{label}</Text>
          {app.has_secret ? (
            <Badge
              variant="success"
              icon={<Icon icon="check" size="xsm" />}
              label={t("admin.oauth.configured")}
            />
          ) : (
            <Badge variant="neutral" label={t("admin.oauth.notConfigured")} />
          )}
        </div>
        <OAuthQuickStart provider={provider} />
        <div className="claw-setup-field">
          <Text size="sm" color="secondary">
            {t("admin.oauth.redirectUrisDesc", { label })}
          </Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.oauth.signInRedirect")}
          </Text>
          <Text type="code">{loginRedirectUri}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.oauth.connectorsRedirect")}
          </Text>
          <Text type="code">{redirectUri}</Text>
        </div>
        <TextInput label={t("admin.oauth.clientId")} value={clientId} onChange={setClientId} />
        <TextInput
          label={
            app.has_secret
              ? t("admin.oauth.clientSecretKeepCurrent")
              : t("admin.oauth.clientSecret")
          }
          type="password"
          value={clientSecret}
          onChange={setClientSecret}
        />
        {withTenant && (
          <TextInput
            label={t("admin.oauth.tenant")}
            value={tenant}
            onChange={setTenant}
          />
        )}
        <div className="claw-row">
          <Button
            label={t("admin.oauth.save")}
            variant="primary"
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                await api.adminSetOAuthApp(provider, {
                  client_id: clientId.trim(),
                  client_secret: clientSecret.trim(),
                  tenant: tenant.trim(),
                });
                setClientSecret("");
                toast({
                  body: t("admin.oauth.savedToast", { label }),
                  type: "info",
                  autoHideDuration: 2500,
                });
                await onSaved();
              })
            }
          />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- Telegram

function telegramStatusBadge(cfg: TelegramAdminConfig, t: (key: string, params?: Record<string, string>) => string) {
  if (!cfg.has_token) return <Badge variant="neutral" label={t("admin.oauth.notConfigured")} />;
  if (!cfg.running) return <Badge variant="neutral" label={t("admin.telegram.configuredDisabled")} />;
  return (
    <Badge
      variant="success"
      icon={<Icon icon="check" size="xsm" />}
      label={
        cfg.bot_username
          ? t("admin.telegram.connectedAs", { username: cfg.bot_username })
          : t("admin.telegram.connected")
      }
    />
  );
}

function TelegramConfigPanel() {
  const t = useT();
  const [cfg, setCfg] = useState<TelegramAdminConfig | null>(null);
  const [token, setToken] = useState("");
  const [enabled, setEnabled] = useState(true);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const reload = useCallback(
    () =>
      api.adminGetTelegramConfig().then((c) => {
        setCfg(c);
        setEnabled(c.enabled);
      }),
    [],
  );
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!cfg) return <Text color="secondary">{t("admin.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("admin.telegram.intro")}</Text>
      {cfg.source === "env" && (
        <Text size="sm" color="secondary">
          {t("admin.telegram.envSourceNote")}
        </Text>
      )}
      <Card padding={2} variant="muted">
        <Text weight="semibold">{t("admin.telegram.stepsTitle")}</Text>
        <ol className="claw-telegram-steps">
          <li>
            {t("admin.telegram.step1")} <Text type="code">/newbot</Text>.
          </li>
          <li>{t("admin.telegram.step2")}</li>
          <li>{t("admin.telegram.step3")}</li>
          <li>{t("admin.telegram.step4")}</li>
        </ol>
      </Card>
      <div className="claw-row">
        <Button
          label={t("admin.telegram.openBotFather")}
          icon={<Icon icon={ExternalLink} size="sm" />}
          variant="secondary"
          href="https://t.me/BotFather"
          target="_blank"
          rel="noopener noreferrer"
        />
      </div>
      <Card padding={2}>
        <div className="claw-panel">
          <div className="claw-row">
            <Text weight="semibold">{t("admin.telegram.bot")}</Text>
            {telegramStatusBadge(cfg, t)}
          </div>
          <TextInput
            label={cfg.has_token ? t("admin.telegram.botTokenKeepCurrent") : t("admin.telegram.botToken")}
            type="password"
            value={token}
            onChange={setToken}
            placeholder="123456:ABC-DEF..."
          />
          <label className="claw-kb-visibility">
            <Switch value={enabled} changeAction={setEnabled} label={t("admin.telegram.enabled")} />
            <Text size="sm" color="secondary">
              {enabled ? t("admin.telegram.botActive") : t("admin.telegram.botPaused")}
            </Text>
          </label>
          <div className="claw-row">
            <Button
              label={t("admin.oauth.save")}
              variant="primary"
              icon={<Icon icon="check" size="sm" />}
              clickAction={() =>
                guard(async () => {
                  const res = await api.adminSetTelegramConfig({ bot_token: token.trim(), enabled });
                  setToken("");
                  setCfg(res);
                  setEnabled(res.enabled);
                  toast({ body: t("admin.telegram.savedToast"), type: "info", autoHideDuration: 2500 });
                })
              }
            />
          </div>
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- Email Notification (SMTP)
// Only used for auth-related transactional email today (the imported-user
// activation link, claw/api/auth.py) — not a general notification channel.

const SMTP_PRESETS: Record<string, { host: string; port: number; security: "tls" | "ssl" }> = {
  "Microsoft Outlook 365": { host: "smtp.office365.com", port: 587, security: "tls" },
  "Gmail": { host: "smtp.gmail.com", port: 587, security: "tls" },
};

const LOGO_SLOT_META: { slot: BrandingLogoSlot; titleKey: string; hintKey: string }[] = [
  { slot: "login", titleKey: "admin.preferences.logo.loginTitle", hintKey: "admin.preferences.logo.loginHint" },
  { slot: "chat", titleKey: "admin.preferences.logo.chatTitle", hintKey: "admin.preferences.logo.chatHint" },
  { slot: "sidebar", titleKey: "admin.preferences.logo.sidebarTitle", hintKey: "admin.preferences.logo.sidebarHint" },
];

function LogoUploadRow({
  meta,
  filename,
  onChange,
}: {
  meta: { slot: BrandingLogoSlot; titleKey: string; hintKey: string };
  filename: string | null;
  onChange: (next: AdminBranding) => void;
}) {
  const t = useT();
  const title = t(meta.titleKey);
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const pick = (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    void guard(async () => {
      try {
        const next = await api.adminUploadBrandingLogo(meta.slot, file);
        onChange(next);
        toast({ body: t("admin.preferences.logo.updatedToast", { title }), type: "info", autoHideDuration: 2500 });
      } finally {
        setBusy(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    });
  };

  const clear = () => {
    setBusy(true);
    void guard(async () => {
      try {
        const next = await api.adminDeleteBrandingLogo(meta.slot);
        onChange(next);
        toast({ body: t("admin.preferences.logo.resetToast", { title }), type: "info", autoHideDuration: 2500 });
      } finally {
        setBusy(false);
      }
    });
  };

  return (
    <Card padding={2}>
      <div className="claw-branding-logo-row">
        <div className="claw-branding-logo-preview">
          <img
            // Cache-bust on the stored filename so a replace shows immediately.
            src={filename ? `/api/branding/assets/${meta.slot}?v=${encodeURIComponent(filename)}` : "/logo-softnix.png"}
            alt={t("admin.preferences.logo.altText", { title })}
          />
        </div>
        <div className="claw-branding-logo-body">
          <Text weight="semibold">{title}</Text>
          <Text size="sm" color="secondary">{t(meta.hintKey)}</Text>
          <Text size="sm" color="secondary">{t("admin.preferences.logo.fileHint")}</Text>
          {error && <ErrorText>{error}</ErrorText>}
          <div className="claw-branding-logo-actions">
            <input
              ref={inputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              style={{ display: "none" }}
              onChange={(e) => pick(e.target.files?.[0])}
            />
            <Button
              label={filename ? t("admin.preferences.logo.replace") : t("admin.preferences.logo.upload")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={Upload} size="sm" />}
              isDisabled={busy}
              clickAction={() => inputRef.current?.click()}
            />
            {filename && (
              <Button
                label={t("admin.preferences.logo.resetToDefault")}
                variant="ghost"
                size="sm"
                isDisabled={busy}
                clickAction={clear}
              />
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

function PreferencesPanel() {
  const t = useT();
  const { refresh } = useBranding();
  const [cfg, setCfg] = useState<AdminBranding | null>(null);
  const [language, setLanguage] = useState<BrandingLanguage>("en");
  const [fontSize, setFontSize] = useState<BrandingFontSize>("small");
  const [chatBg, setChatBg] = useState<BrandingChatBackground>("solid");
  const [saving, setSaving] = useState(false);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const apply = useCallback((c: AdminBranding) => {
    setCfg(c);
    setLanguage(c.language);
    setFontSize(c.font_size);
    setChatBg(c.chat_background);
  }, []);

  const reload = useCallback(() => api.adminGetBranding().then(apply), [apply]);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  if (error && !cfg) return <ErrorText>{error}</ErrorText>;
  if (!cfg) return <Text color="secondary">{t("admin.common.loading")}</Text>;

  const dirty = language !== cfg.language || fontSize !== cfg.font_size || chatBg !== cfg.chat_background;

  const save = () => {
    setSaving(true);
    void guard(async () => {
      try {
        const next = await api.adminSetBranding({ language, font_size: fontSize, chat_background: chatBg });
        setCfg((prev) => (prev ? { ...prev, ...next } : next));
        await refresh(); // apply live (font size / chat bg / language) without a reload
        toast({ body: t("admin.preferences.savedToast"), type: "info", autoHideDuration: 2500 });
      } finally {
        setSaving(false);
      }
    });
  };

  // After a logo upload/delete, refresh both the admin state here and the live
  // branding context so the new logo appears immediately app-wide.
  const onLogoChange = (next: AdminBranding) => {
    setCfg(next);
    void refresh();
  };

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("admin.preferences.intro")}</Text>

      <Text weight="semibold">{t("admin.preferences.logosTitle")}</Text>
      {LOGO_SLOT_META.map((m) => (
        <LogoUploadRow
          key={m.slot}
          meta={m}
          filename={cfg[`logo_${m.slot}` as const] as string | null}
          onChange={onLogoChange}
        />
      ))}

      <Card padding={2}>
        <div className="claw-panel">
          <div>
            <Text weight="semibold">{t("admin.preferences.language")}</Text>
            <Text size="sm" color="secondary">
              {t("admin.preferences.languageDesc")}
            </Text>
            <SegmentedControl value={language} onChange={(v) => setLanguage(v as BrandingLanguage)} label={t("admin.preferences.language")}>
              <SegmentedControlItem value="en" label={t("admin.preferences.languageEnglish")} />
              <SegmentedControlItem value="th" label={t("admin.preferences.languageThai")} />
            </SegmentedControl>
          </div>
          <div>
            <Text weight="semibold">{t("admin.preferences.fontSize")}</Text>
            <SegmentedControl value={fontSize} onChange={(v) => setFontSize(v as BrandingFontSize)} label={t("admin.preferences.fontSize")}>
              <SegmentedControlItem value="small" label={t("admin.preferences.fontSmall")} />
              <SegmentedControlItem value="medium" label={t("admin.preferences.fontMedium")} />
              <SegmentedControlItem value="large" label={t("admin.preferences.fontLarge")} />
            </SegmentedControl>
          </div>
          <div>
            <Text weight="semibold">{t("admin.preferences.chatBackground")}</Text>
            <Text size="sm" color="secondary">
              {t("admin.preferences.chatBackgroundDesc")}
            </Text>
            <SegmentedControl value={chatBg} onChange={(v) => setChatBg(v as BrandingChatBackground)} label={t("admin.preferences.chatBackground")}>
              <SegmentedControlItem value="solid" label={t("admin.preferences.bgSolid")} />
              <SegmentedControlItem value="dots" label={t("admin.preferences.bgDots")} />
              <SegmentedControlItem value="grid" label={t("admin.preferences.bgGrid")} />
            </SegmentedControl>
          </div>
          {error && <ErrorText>{error}</ErrorText>}
          <div>
            <Button label={t("admin.preferences.savePreferences")} isDisabled={!dirty || saving} clickAction={save} />
          </div>
        </div>
      </Card>
    </div>
  );
}

function EmailConfigPanel() {
  const t = useT();
  const [cfg, setCfg] = useState<SmtpAdminConfig | null>(null);
  const [provider, setProvider] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromAddress, setFromAddress] = useState("");
  const [security, setSecurity] = useState<"tls" | "ssl">("tls");
  const [enabled, setEnabled] = useState(false);
  const [testRecipient, setTestRecipient] = useState("");
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const reload = useCallback(
    () =>
      api.adminGetEmailConfig().then((c) => {
        setCfg(c);
        setProvider(c.provider);
        setHost(c.host);
        setPort(String(c.port));
        setUsername(c.username);
        setFromAddress(c.from_address);
        setSecurity(c.use_ssl ? "ssl" : "tls");
        setEnabled(c.enabled);
      }),
    [],
  );
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!cfg) return <Text color="secondary">{t("admin.common.loading")}</Text>;

  const currentBody = () => ({
    provider,
    host: host.trim(),
    port: Number(port) || 587,
    username: username.trim(),
    password,
    from_address: fromAddress.trim(),
    use_tls: security === "tls",
    use_ssl: security === "ssl",
    enabled,
  });

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("admin.email.intro")}</Text>
      <Card padding={2}>
        <div className="claw-panel">
          <Text weight="semibold">{t("admin.email.provider")}</Text>
          <select
            className="claw-token-filter"
            value={provider}
            onChange={(e) => {
              const value = e.target.value;
              setProvider(value);
              const preset = SMTP_PRESETS[value];
              if (preset) {
                setHost(preset.host);
                setPort(String(preset.port));
                setSecurity(preset.security);
              }
            }}
          >
            <option value="">{t("admin.email.custom")}</option>
            {Object.keys(SMTP_PRESETS).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>

          <Text weight="semibold" size="sm">
            {t("admin.email.connection")}
          </Text>
          <TextInput label={t("admin.email.smtpHost")} value={host} onChange={setHost} placeholder="smtp.office365.com" />
          <TextInput label={t("admin.email.port")} value={port} onChange={setPort} placeholder="587" />
          <TextInput label={t("admin.email.username")} value={username} onChange={setUsername} placeholder="notification@company.com" />
          <TextInput
            label={t("admin.email.fromAddress")}
            value={fromAddress}
            onChange={setFromAddress}
            placeholder="notification@company.com"
          />
          <PasswordField
            label={cfg.has_password ? t("admin.email.passwordKeepExisting") : t("admin.email.password")}
            value={password}
            onChange={setPassword}
          />

          <SegmentedControl
            value={security}
            onChange={(v) => setSecurity(v as "tls" | "ssl")}
            label={t("admin.email.security")}
            size="sm"
          >
            <SegmentedControlItem value="tls" label={t("admin.email.useStartTls")} />
            <SegmentedControlItem value="ssl" label={t("admin.email.useSsl")} />
          </SegmentedControl>

          <label className="claw-kb-visibility">
            <Switch value={enabled} changeAction={setEnabled} label={t("admin.telegram.enabled")} />
            <Text size="sm" color="secondary">
              {enabled ? t("admin.email.sendingActive") : t("admin.email.sendingPaused")}
            </Text>
          </label>

          <div className="claw-row">
            <Button
              label={t("admin.oauth.save")}
              variant="primary"
              icon={<Icon icon="check" size="sm" />}
              clickAction={() =>
                guard(async () => {
                  const res = await api.adminSetEmailConfig(currentBody());
                  setCfg(res);
                  setPassword("");
                  toast({ body: t("admin.email.savedToast"), type: "info", autoHideDuration: 2500 });
                })
              }
            />
          </div>
        </div>
      </Card>

      <Card padding={2} variant="muted">
        <Text weight="semibold">{t("admin.email.testSendTitle")}</Text>
        <Text size="sm" color="secondary">
          {t("admin.email.testSendDesc")}
        </Text>
        <TextInput
          label={t("admin.email.recipientEmail")}
          type="email"
          value={testRecipient}
          onChange={setTestRecipient}
          placeholder="you@company.com"
        />
        {testResult && (testResult.ok ? <Text size="sm">{testResult.message}</Text> : <ErrorText>{testResult.message}</ErrorText>)}
        <div className="claw-row">
          <Button
            label={testBusy ? t("admin.email.sending") : t("admin.email.testSendButton")}
            variant="secondary"
            isDisabled={testBusy || !testRecipient}
            clickAction={async () => {
              setTestBusy(true);
              setTestResult(null);
              try {
                await api.adminTestEmailConfig({ ...currentBody(), recipient: testRecipient });
                setTestResult({ ok: true, message: t("admin.email.testSentMessage") });
              } catch (e) {
                setTestResult({ ok: false, message: String(e).replace(/^Error:\s*/, "") });
              } finally {
                setTestBusy(false);
              }
            }}
          />
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- Audit Logs

const AUDIT_PAGE = 50;

// One audit event: a compact clickable header (kind · who · preview · time)
// that expands to the full, pretty-printed payload.
function AuditEventRow({ event: e }: { event: AuditRow }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <div className="claw-audit-item">
      <button
        type="button"
        className="claw-audit-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon icon={open ? ChevronDown : ChevronRight} size="sm" color="secondary" />
        <Badge variant="neutral" label={e.kind} />
        <span className="claw-audit-user">
          <Icon icon={UserIcon} size="xsm" color="secondary" />
          {e.user_label}
        </span>
        <span className="claw-audit-payload">{JSON.stringify(e.payload)}</span>
        <span className="claw-audit-time">{new Date(e.created_at).toLocaleString()}</span>
      </button>
      {open && (
        <div className="claw-audit-detail">
          <div className="claw-audit-detail-meta">
            <span>{t("admin.audit.actor", { user: e.user_label })}</span>
            {e.session_id && <span>{t("admin.audit.session", { session: e.session_id })}</span>}
          </div>
          <pre className="claw-audit-json">{JSON.stringify(e.payload, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function AuditPanel() {
  const t = useT();
  const [events, setEvents] = useState<AuditRow[]>([]);
  const [kinds, setKinds] = useState<string[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const { error, guard } = useAsyncError();

  // First page — refetched when the kind filter or (debounced) search changes.
  useEffect(() => {
    const timer = setTimeout(() => {
      void guard(async () => {
        const r = await api.adminAudit({
          kind: filter || undefined,
          search: search || undefined,
          limit: AUDIT_PAGE,
        });
        setEvents(r.events);
        setKinds(r.kinds);
        setHasMore(r.has_more);
      });
    }, search ? 300 : 0);
    return () => clearTimeout(timer);
  }, [guard, filter, search]);

  const loadMore = () =>
    guard(async () => {
      setBusy(true);
      try {
        const before = events[events.length - 1]?.created_at;
        const r = await api.adminAudit({
          kind: filter || undefined,
          search: search || undefined,
          before,
          limit: AUDIT_PAGE,
        });
        setEvents((prev) => [...prev, ...r.events]);
        setHasMore(r.has_more);
      } finally {
        setBusy(false);
      }
    });

  return (
    <div className="claw-panel">
      <TextInput
        label={t("admin.audit.searchEvents")}
        value={search}
        placeholder={t("admin.audit.searchPlaceholder")}
        onChange={setSearch}
      />
      <div className="claw-row">
        <Button
          label={t("admin.audit.all")}
          size="sm"
          variant={filter === "" ? "primary" : "secondary"}
          clickAction={() => setFilter("")}
        />
        {kinds.map((k) => (
          <Button
            key={k}
            label={k}
            size="sm"
            variant={filter === k ? "primary" : "secondary"}
            clickAction={() => setFilter(k)}
          />
        ))}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {events.length === 0 ? (
        <EmptyState
          title={t("admin.audit.noEvents")}
          description={search ? t("admin.audit.noEventsMatch") : t("admin.audit.noEventsYet")}
        />
      ) : (
        <>
          <div className="claw-audit-table">
            {events.map((e) => (
              <AuditEventRow key={e.id} event={e} />
            ))}
          </div>
          {hasMore && (
            <div className="claw-row">
              <Button
                label={busy ? t("admin.common.loading") : t("admin.audit.loadMore")}
                variant="secondary"
                size="sm"
                isDisabled={busy}
                clickAction={loadMore}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Users

const USERS_PAGE = 20;

// Single-select group picker (chips) for the Add/Edit user forms, with an
// inline "create group" so a group can be added without leaving the form.
function GroupPicker({
  groups,
  value,
  onChange,
  onCreate,
}: {
  groups: GroupInfo[];
  value: string | null;
  onChange: (id: string | null) => void;
  onCreate: (name: string) => Promise<GroupInfo>;
}) {
  const t = useT();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const create = () =>
    void onCreate(name.trim()).then((g) => {
      onChange(g.id);
      setName("");
      setAdding(false);
    });
  return (
    <div className="claw-field-group">
      <Text size="sm" color="secondary">
        {t("admin.users.group")}
      </Text>
      <div className="claw-row">
        <Button
          label={t("admin.users.noGroup")}
          size="sm"
          variant={value === null ? "primary" : "secondary"}
          clickAction={() => onChange(null)}
        />
        {groups.map((g) => (
          <Button
            key={g.id}
            label={g.name}
            size="sm"
            variant={value === g.id ? "primary" : "secondary"}
            clickAction={() => onChange(g.id)}
          />
        ))}
        {adding ? (
          <>
            <TextInput
              label={t("admin.users.newGroupName")}
              isLabelHidden
              placeholder={t("admin.users.newGroupPlaceholder")}
              value={name}
              onChange={setName}
              onEnter={create}
            />
            <Button label={t("admin.users.add")} size="sm" variant="primary" isDisabled={!name.trim()} clickAction={create} />
            <Button label={t("admin.common.cancel")} size="sm" variant="ghost" clickAction={() => { setName(""); setAdding(false); }} />
          </>
        ) : (
          <Button
            label={t("admin.users.newGroup")}
            icon={<Icon icon={Plus} size="xsm" />}
            size="sm"
            variant="ghost"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
    </div>
  );
}

// Housekeeping card: create/delete groups and choose which one new self-signups
// land in. Groups are organizational only — no permission effect.
function GroupsManager({
  groups,
  plans,
  reload,
  guard,
}: {
  groups: GroupInfo[];
  plans: PlanInfo[];
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const t = useT();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const toast = useToast();

  const addGroup = () =>
    guard(async () => {
      await api.adminCreateGroup(name.trim());
      setName("");
      setAdding(false);
      await reload();
    });

  return (
    <Card padding={2} variant="muted">
      <div className="claw-panel">
        <div>
          <Text weight="semibold">{t("admin.users.groupsTitle")}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("admin.users.groupsDesc")}
          </Text>
        </div>
        {groups.length === 0 && (
          <Text size="sm" color="secondary">
            {t("admin.users.noGroupsYet")}
          </Text>
        )}
        {groups.map((g) => (
          <div key={g.id} className="claw-plan-group-item">
            <div className="claw-row claw-row-between claw-model-row">
              <div className="claw-row">
                <Icon icon={Users} size="sm" color="secondary" />
                <Text>{g.name}</Text>
                <Text size="sm" color="secondary">
                  {t("admin.users.memberCount", { count: String(g.user_count), plural: g.user_count === 1 ? "" : "s" })}
                </Text>
                {g.is_default && (
                  <Badge variant="purple" icon={<Icon icon={Star} size="xsm" />} label={t("admin.users.defaultSignUp")} />
                )}
                {g.plan_name && (
                  <Badge variant="neutral" icon={<Icon icon={Gauge} size="xsm" />} label={g.plan_name} />
                )}
              </div>
              <div className="claw-row">
                <Button
                  label={g.is_default ? t("admin.users.defaultSignUpGroup") : t("admin.users.setAsDefault")}
                  size="sm"
                  variant={g.is_default ? "secondary" : "ghost"}
                  clickAction={() =>
                    guard(async () => {
                      await api.adminSetDefaultGroup(g.is_default ? null : g.id);
                      await reload();
                    })
                  }
                />
                <IconButton
                  label={t("admin.users.deleteGroup")}
                  icon={<Icon icon={Trash2} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() =>
                    guard(async () => {
                      if (
                        !window.confirm(
                          t("admin.users.deleteGroupConfirm", { name: g.name, count: String(g.user_count) }),
                        )
                      ) {
                        return;
                      }
                      await api.adminDeleteGroup(g.id);
                      toast({ body: t("admin.users.groupDeletedToast", { name: g.name }), type: "info", autoHideDuration: 2500 });
                      await reload();
                    })
                  }
                />
              </div>
            </div>
            {/* Default plan for everyone in this group (members without their own
                assigned plan inherit it). */}
            {plans.length > 0 && (
              <PlanPicker
                plans={plans}
                value={g.plan_id}
                label={t("admin.plans.defaultPlanForGroup")}
                onChange={(planId) =>
                  void guard(async () => {
                    await api.adminUpdateGroup(g.id, { plan_id: planId });
                    await reload();
                  })
                }
              />
            )}
          </div>
        ))}
        {adding ? (
          <div className="claw-row">
            <TextInput
              label={t("admin.users.groupName")}
              isLabelHidden
              placeholder={t("admin.users.newGroupPlaceholder")}
              value={name}
              onChange={setName}
              onEnter={() => name.trim() && addGroup()}
            />
            <Button label={t("admin.users.create")} size="sm" variant="primary" isDisabled={!name.trim()} clickAction={addGroup} />
            <Button label={t("admin.common.cancel")} size="sm" variant="ghost" clickAction={() => { setName(""); setAdding(false); }} />
          </div>
        ) : (
          <Button
            label={t("admin.users.addGroup")}
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            variant="secondary"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
    </Card>
  );
}

type ImportStep = "upload" | "map" | "confirm" | "results";

// Mirrors claw/api/admin.py's _EMAIL_RE so the wizard's preview count
// matches what the backend will actually accept on commit.
const IMPORT_EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

// Target fields a column can map to. Each field can be claimed by at most one
// column (picking a field elsewhere clears it from its previous column).
const IMPORT_FIELD_OPTIONS: { value: string; labelKey: string }[] = [
  { value: "skip", labelKey: "admin.users.import.field.skip" },
  { value: "email", labelKey: "admin.users.import.field.email" },
  { value: "full_name", labelKey: "admin.users.import.field.fullName" },
  { value: "first_name", labelKey: "admin.users.import.field.firstName" },
  { value: "last_name", labelKey: "admin.users.import.field.lastName" },
];

const IMPORT_STATUS_LABEL_KEY: Record<string, string> = {
  created: "admin.users.import.status.created",
  duplicate_in_file: "admin.users.import.status.duplicateInFile",
  already_exists: "admin.users.import.status.alreadyExists",
  invalid_email: "admin.users.import.status.invalidEmail",
  missing_email: "admin.users.import.status.missingEmail",
  error: "admin.users.import.status.error",
};

// Bulk user import — upload -> map columns -> confirm -> results. Stateless:
// the parse step returns the full grid and this dialog holds it in memory
// while the admin maps columns, then posts the same rows back on commit (no
// server-side staging). No password is set for imported rows — each person
// sets their own the first time they try to log in (see Auth's
// "complete-setup" mode in App.tsx).
function UserImportDialog({
  isOpen,
  onOpenChange,
  groups,
  onCreateGroup,
  onImported,
}: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  groups: GroupInfo[];
  onCreateGroup: (name: string) => Promise<GroupInfo>;
  onImported: () => void;
}) {
  const t = useT();
  const [step, setStep] = useState<ImportStep>("upload");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [parsed, setParsed] = useState<UserImportParseResult | null>(null);
  const [fieldByCol, setFieldByCol] = useState<string[]>([]);
  const [groupId, setGroupId] = useState<string | null>(null);
  const [result, setResult] = useState<UserImportCommitResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setStep("upload");
    setError("");
    setParsed(null);
    setFieldByCol([]);
    setGroupId(null);
    setResult(null);
  };

  const close = () => {
    onOpenChange(false);
    reset();
  };

  const onPick = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const res = await api.adminImportUsersParse(file);
      setParsed(res);
      setFieldByCol(res.columns.map(() => "skip"));
      setStep("map");
    } catch (e) {
      setError(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const setColumnField = (col: number, value: string) => {
    setFieldByCol((prev) => {
      const next = [...prev];
      if (value !== "skip") {
        for (let j = 0; j < next.length; j++) if (next[j] === value) next[j] = "skip";
      }
      next[col] = value;
      return next;
    });
  };

  const emailCol = fieldByCol.indexOf("email");
  const fullNameCol = fieldByCol.indexOf("full_name");
  const firstNameCol = fieldByCol.indexOf("first_name");
  const lastNameCol = fieldByCol.indexOf("last_name");
  const nameMode: UserImportMapping["name_mode"] =
    firstNameCol >= 0 || lastNameCol >= 0 ? "split" : fullNameCol >= 0 ? "full" : "none";
  // Memoized so its identity is stable across renders when the underlying
  // selection hasn't changed — otherwise a fresh object literal every
  // render would defeat `summary`'s useMemo below (it depends on `mapping`).
  const mapping: UserImportMapping | null = useMemo(
    () =>
      emailCol >= 0
        ? {
            email_col: emailCol,
            name_mode: nameMode,
            full_name_col: fullNameCol >= 0 ? fullNameCol : undefined,
            first_name_col: firstNameCol >= 0 ? firstNameCol : undefined,
            last_name_col: lastNameCol >= 0 ? lastNameCol : undefined,
          }
        : null,
    [emailCol, nameMode, fullNameCol, firstNameCol, lastNameCol],
  );

  const computeDisplayName = (row: string[]): string => {
    if (nameMode === "full" && fullNameCol >= 0) return (row[fullNameCol] || "").trim();
    if (nameMode === "split") {
      const first = firstNameCol >= 0 ? (row[firstNameCol] || "").trim() : "";
      const last = lastNameCol >= 0 ? (row[lastNameCol] || "").trim() : "";
      return `${first} ${last}`.trim();
    }
    return "";
  };

  // Client-side validation summary — mirrors the backend's commit-time dedup
  // logic so the admin sees accurate counts before committing anything.
  const summary = useMemo(() => {
    if (!parsed || !mapping) return null;
    const seen = new Set<string>();
    let valid = 0;
    let dup = 0;
    let missing = 0;
    let invalid = 0;
    for (const row of parsed.rows) {
      const email = (row[mapping.email_col] || "").trim().toLowerCase();
      if (!email) {
        missing++;
        continue;
      }
      if (!IMPORT_EMAIL_RE.test(email)) {
        invalid++;
        continue;
      }
      if (seen.has(email)) {
        dup++;
        continue;
      }
      seen.add(email);
      valid++;
    }
    return { valid, dup, missing, invalid };
  }, [parsed, mapping]);

  const commit = async () => {
    if (!parsed || !mapping) return;
    setBusy(true);
    setError("");
    try {
      const res = await api.adminImportUsersCommit({
        columns: parsed.columns,
        rows: parsed.rows,
        mapping,
        group_id: groupId,
      });
      setResult(res);
      setStep("results");
    } catch (e) {
      setError(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  const done = () => {
    onImported();
    close();
  };

  return (
    <Dialog isOpen={isOpen} onOpenChange={(open: boolean) => (open ? onOpenChange(true) : close())} variant="fullscreen" purpose="info">
      <Layout
        header={
          <DialogHeader
            title={t("admin.users.import.title")}
            subtitle={t("admin.users.import.subtitle")}
            onOpenChange={(open: boolean) => (open ? undefined : close())}
          />
        }
        content={
          <LayoutContent>
            <div className="claw-panel">
              {error && <ErrorText>{error}</ErrorText>}
              {step === "upload" && (
                <>
                  <Text color="secondary">{t("admin.users.import.uploadDesc")}</Text>
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".csv,.xlsx"
                    style={{ display: "none" }}
                    onChange={(e) => void onPick(e.target.files)}
                  />
                  <Button
                    label={busy ? t("admin.users.import.uploading") : t("admin.users.import.chooseFile")}
                    icon={<Icon icon={Upload} size="sm" />}
                    isDisabled={busy}
                    clickAction={() => fileRef.current?.click()}
                  />
                </>
              )}
              {step === "map" && parsed && (
                <>
                  <Text weight="semibold">{t("admin.users.import.mapColumns", { count: String(parsed.row_count) })}</Text>
                  <div className="claw-import-map-grid">
                    {parsed.columns.map((col, i) => (
                      <div key={i} className="claw-import-map-row">
                        <Text size="sm">{col || t("admin.users.import.columnN", { n: String(i + 1) })}</Text>
                        <select
                          className="claw-token-filter"
                          value={fieldByCol[i] ?? "skip"}
                          onChange={(e) => setColumnField(i, e.target.value)}
                        >
                          {IMPORT_FIELD_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>
                              {t(o.labelKey)}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                  {!mapping && <ErrorText>{t("admin.users.import.mapEmailRequired")}</ErrorText>}
                  {summary && (
                    <div className="claw-row">
                      <Badge variant="success" label={t("admin.users.import.willImport", { count: String(summary.valid) })} />
                      {summary.dup > 0 && (
                        <Badge variant="neutral" label={t("admin.users.import.duplicateInFileCount", { count: String(summary.dup) })} />
                      )}
                      {summary.missing > 0 && (
                        <Badge variant="neutral" label={t("admin.users.import.missingEmailCount", { count: String(summary.missing) })} />
                      )}
                      {summary.invalid > 0 && (
                        <Badge variant="neutral" label={t("admin.users.import.invalidEmailCount", { count: String(summary.invalid) })} />
                      )}
                    </div>
                  )}
                  <Text weight="semibold" size="sm">
                    {t("admin.users.import.preview")}
                  </Text>
                  <div className="claw-import-preview-table">
                    <table>
                      <thead>
                        <tr>
                          <th>{t("admin.users.import.emailHeader")}</th>
                          <th>{t("admin.users.import.displayNameHeader")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {parsed.rows.slice(0, 20).map((row, i) => (
                          <tr key={i}>
                            <td>{mapping ? row[mapping.email_col] || "—" : "—"}</td>
                            <td>{computeDisplayName(row) || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
              {step === "confirm" && summary && (
                <>
                  <Text weight="semibold">{t("admin.users.import.confirmTitle")}</Text>
                  <Text color="secondary">
                    {t("admin.users.import.confirmDesc", { count: String(summary.valid) })}
                  </Text>
                  <GroupPicker groups={groups} value={groupId} onChange={setGroupId} onCreate={onCreateGroup} />
                </>
              )}
              {step === "results" && result && (
                <>
                  <Text weight="semibold">{t("admin.users.import.completeTitle")}</Text>
                  <div className="claw-row">
                    <Badge variant="success" label={t("admin.users.import.createdCount", { count: String(result.created) })} />
                    {Object.entries(IMPORT_STATUS_LABEL_KEY).map(([status, labelKey]) => {
                      if (status === "created") return null; // already shown above, success-colored
                      const n = result.results.filter((r) => r.status === status).length;
                      return n > 0 ? <Badge key={status} variant="neutral" label={`${n} ${t(labelKey)}`} /> : null;
                    })}
                  </div>
                  {(() => {
                    // Only the actionable rows (skipped/errored) are worth listing —
                    // "created" rows are already summarized by the badge above. Cap
                    // the rendered list too: with up to 5,000 rows per import, an
                    // unbounded table would degrade badly as an import grows (see
                    // CLAUDE.md's "avoid layouts that degrade as data grows" rule).
                    const problems = result.results.filter((r) => r.status !== "created");
                    const shown = problems.slice(0, 200);
                    if (problems.length === 0) return null;
                    return (
                      <>
                        <Text weight="semibold" size="sm">
                          {t("admin.users.import.skippedRows")}
                        </Text>
                        {problems.length > shown.length && (
                          <Text size="sm" color="secondary">
                            {t("admin.users.import.showingFirstOf", { shown: String(shown.length), total: String(problems.length) })}
                          </Text>
                        )}
                        <div className="claw-import-preview-table">
                          <table>
                            <thead>
                              <tr>
                                <th>{t("admin.users.import.fileRowHeader")}</th>
                                <th>{t("admin.users.import.emailHeader")}</th>
                                <th>{t("admin.users.import.statusHeader")}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {shown.map((r) => (
                                // +2, not +1: row_index is 0-based over data rows with the header
                                // already stripped server-side, and the header itself occupies
                                // line 1 of the admin's actual source file.
                                <tr key={r.row_index}>
                                  <td>{r.row_index + 2}</td>
                                  <td>{r.email || "—"}</td>
                                  <td>{t(IMPORT_STATUS_LABEL_KEY[r.status] ?? "") || r.status}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </>
                    );
                  })()}
                </>
              )}
            </div>
          </LayoutContent>
        }
        footer={
          <LayoutFooter hasDivider>
            {step === "map" && (
              <>
                <Button label={t("admin.common.cancel")} variant="ghost" clickAction={close} />
                <Button label={t("admin.users.import.next")} isDisabled={!mapping} clickAction={() => setStep("confirm")} />
              </>
            )}
            {step === "confirm" && (
              <>
                <Button label={t("admin.users.import.back")} variant="ghost" clickAction={() => setStep("map")} />
                <Button
                  label={busy ? t("admin.users.import.importing") : t("admin.users.import.importNUsers", { count: String(summary?.valid ?? 0) })}
                  isDisabled={busy}
                  clickAction={commit}
                />
              </>
            )}
            {step === "results" && <Button label={t("admin.users.import.done")} clickAction={done} />}
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

function UsersPanel({ selfId }: { selfId: string }) {
  const t = useT();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [creating, setCreating] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [newGroupId, setNewGroupId] = useState<string | null>(null);
  const [newPlanId, setNewPlanId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [visible, setVisible] = useState(USERS_PAGE);
  // Group filter: "all" | "none" (ungrouped) | a group id.
  const [groupFilter, setGroupFilter] = useState<string>("all");
  const [managingGroups, setManagingGroups] = useState(false);
  const [importing, setImporting] = useState(false);
  // Bulk group reassignment: a set of selected user ids plus the chosen
  // target group (undefined = none picked yet, distinct from null = "No
  // group" explicitly chosen, so the Apply button can't fire on an
  // unconsidered default).
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkTarget, setBulkTarget] = useState<string | null | undefined>(undefined);
  const [bulkApplying, setBulkApplying] = useState(false);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const reloadUsers = useCallback(() => api.adminListUsers().then(setUsers), []);
  const reloadGroups = useCallback(() => api.adminListGroups().then(setGroups), []);
  const reloadPlans = useCallback(() => api.adminListPlans().then(setPlans), []);
  // Group/plan changes (create/delete/default, or assigning a user) affect
  // several lists — a deleted group ungroups its members, plan user_counts
  // shift, etc. — so refresh them together.
  const reloadAll = useCallback(async () => {
    await Promise.all([reloadUsers(), reloadGroups(), reloadPlans()]);
  }, [reloadUsers, reloadGroups, reloadPlans]);

  useEffect(() => {
    void guard(async () => await reloadAll());
  }, [guard, reloadAll]);

  // Reset the "load more" window and any bulk selection whenever the search
  // or group filter changes — a selection made under one filter shouldn't
  // silently carry over and get bulk-applied under a different one.
  useEffect(() => {
    setVisible(USERS_PAGE);
    setSelectedIds(new Set());
    setBulkTarget(undefined);
  }, [query, groupFilter]);
  // A default group is a fine starting selection for a new user; leave the plan
  // on "Default plan" (null) so new users inherit the deployment default.
  useEffect(() => {
    setNewGroupId(groups.find((g) => g.is_default)?.id ?? null);
    setNewPlanId(null);
  }, [groups, creating]);

  const resetForm = () => {
    setCreating(false);
    setEmail("");
    setPassword("");
    setDisplayName("");
    setIsAdmin(false);
  };

  // Inline "create group" from a form's GroupPicker: persist, refresh, return it.
  const createGroup = useCallback(
    async (name: string): Promise<GroupInfo> => {
      const g = await api.adminCreateGroup(name);
      await reloadGroups();
      return g;
    },
    [reloadGroups],
  );

  const admins = users.filter((u) => u.is_admin).length;
  const suspended = users.filter((u) => !u.is_active).length;
  const q = query.trim().toLowerCase();
  const filtered = users.filter((u) => {
    if (groupFilter === "none" && u.group_id) return false;
    if (groupFilter !== "all" && groupFilter !== "none" && u.group_id !== groupFilter) return false;
    if (q && !(u.display_name || "").toLowerCase().includes(q) && !u.email.toLowerCase().includes(q))
      return false;
    return true;
  });
  const shown = filtered.slice(0, visible);
  const ungrouped = users.filter((u) => !u.group_id).length;

  const toggleSelect = (id: string) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const shownSelectedCount = shown.filter((u) => selectedIds.has(u.id)).length;
  const toggleSelectAllShown = () =>
    setSelectedIds((prev) => {
      if (shownSelectedCount === shown.length) {
        // All loaded rows are selected — deselect just those (keep any
        // selection from a previous filter/page out of scope entirely, since
        // selection is cleared on filter change anyway).
        const next = new Set(prev);
        shown.forEach((u) => next.delete(u.id));
        return next;
      }
      const next = new Set(prev);
      shown.forEach((u) => next.add(u.id));
      return next;
    });

  // Client-side loop, one request per user (sequential, not parallel, so a
  // large selection doesn't fire dozens of concurrent PATCHes at once) — a
  // per-user failure doesn't abort the rest; the toast reports how many of
  // each.
  const applyBulkGroup = () => {
    if (bulkTarget === undefined || selectedIds.size === 0) return;
    const targetName = bulkTarget === null ? t("admin.users.noGroup") : groups.find((g) => g.id === bulkTarget)?.name || t("admin.users.group");
    if (!window.confirm(t("admin.users.moveConfirm", { count: String(selectedIds.size), target: targetName }))) return;
    void guard(async () => {
      setBulkApplying(true);
      const ids = Array.from(selectedIds);
      let failed = 0;
      for (const id of ids) {
        try {
          await api.adminUpdateUser(id, { group_id: bulkTarget });
        } catch {
          failed += 1;
        }
      }
      setBulkApplying(false);
      setSelectedIds(new Set());
      setBulkTarget(undefined);
      toast({
        body:
          failed === 0
            ? t("admin.users.movedToast", { count: String(ids.length), target: targetName })
            : t("admin.users.movedPartialToast", { moved: String(ids.length - failed), failed: String(failed) }),
        type: failed === 0 ? "info" : "error",
        autoHideDuration: failed === 0 ? 2500 : 4000,
      });
      await reloadAll();
    });
  };

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <div className="claw-panel">
          <Text color="secondary">{t("admin.users.intro")}</Text>
          <div className="claw-row">
            <Badge variant="neutral" icon={<Icon icon={Users} size="xsm" />} label={t("admin.users.usersCount", { count: String(users.length) })} />
            <Badge variant="neutral" icon={<Icon icon={Shield} size="xsm" />} label={t("admin.users.adminsCount", { count: String(admins) })} />
            {suspended > 0 && (
              <Badge variant="neutral" icon={<Icon icon={Ban} size="xsm" />} label={t("admin.users.suspendedCount", { count: String(suspended) })} />
            )}
          </div>
        </div>
        <div className="claw-row">
          <Button
            label={t("admin.users.manageGroups")}
            icon={<Icon icon={Users} size="sm" />}
            size="sm"
            variant={managingGroups ? "secondary" : "ghost"}
            clickAction={() => setManagingGroups((m) => !m)}
          />
          <Button
            label={t("admin.users.import")}
            icon={<Icon icon={Upload} size="sm" />}
            size="sm"
            variant="secondary"
            clickAction={() => setImporting(true)}
          />
          {!creating && (
            <Button
              label={t("admin.users.addUser")}
              icon={<Icon icon={Plus} size="sm" />}
              size="sm"
              clickAction={() => setCreating(true)}
            />
          )}
        </div>
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      <UserImportDialog
        isOpen={importing}
        onOpenChange={setImporting}
        groups={groups}
        onCreateGroup={createGroup}
        onImported={() => {
          void reloadAll();
          toast({ body: t("admin.users.importedToast"), type: "info", autoHideDuration: 3000 });
        }}
      />

      {managingGroups && <GroupsManager groups={groups} plans={plans} reload={reloadAll} guard={guard} />}

      {users.length > USERS_PAGE / 2 && (
        <TextInput
          label={t("admin.users.searchUsers")}
          isLabelHidden
          startIcon={<Icon icon={Search} size="sm" color="secondary" />}
          placeholder={t("admin.users.searchPlaceholder")}
          value={query}
          onChange={setQuery}
          hasClear
        />
      )}

      {/* Filter by group — only shown once groups exist. */}
      {groups.length > 0 && (
        <div className="claw-row">
          <Text size="sm" color="secondary">
            {t("admin.users.group")}
          </Text>
          <Button
            label={t("admin.audit.all")}
            size="sm"
            variant={groupFilter === "all" ? "primary" : "secondary"}
            clickAction={() => setGroupFilter("all")}
          />
          {groups.map((g) => (
            <Button
              key={g.id}
              label={`${g.name} (${g.user_count})`}
              size="sm"
              variant={groupFilter === g.id ? "primary" : "secondary"}
              clickAction={() => setGroupFilter(g.id)}
            />
          ))}
          {ungrouped > 0 && (
            <Button
              label={t("admin.users.noGroupCount", { count: String(ungrouped) })}
              size="sm"
              variant={groupFilter === "none" ? "primary" : "secondary"}
              clickAction={() => setGroupFilter("none")}
            />
          )}
        </div>
      )}

      {creating && (
        <Card padding={2}>
          <div className="claw-panel">
            <TextInput label={t("admin.users.fullName")} placeholder="Jane Doe" value={displayName} onChange={setDisplayName} />
            <TextInput label={t("admin.users.email")} type="email" placeholder="jane@company.com" value={email} onChange={setEmail} />
            <PasswordField
              label={t("admin.users.password")}
              description={t("admin.users.passwordHint")}
              value={password}
              onChange={setPassword}
            />
            <GroupPicker groups={groups} value={newGroupId} onChange={setNewGroupId} onCreate={createGroup} />
            {plans.length > 0 && <PlanPicker plans={plans} value={newPlanId} onChange={setNewPlanId} />}
            <label className="claw-toggle-inline">
              <Switch value={isAdmin} label={t("admin.users.makeAdmin")} isLabelHidden changeAction={setIsAdmin} />
              <Text size="sm" color="secondary">
                {t("admin.users.adminHint")}
              </Text>
            </label>
            <div className="claw-row">
              <Button
                label={t("admin.users.createUser")}
                variant="primary"
                icon={<Icon icon={Plus} size="sm" />}
                isDisabled={!email.trim() || password.length < 8}
                clickAction={() =>
                  guard(async () => {
                    // adminCreateUser has no plan_id param, so assign the chosen
                    // plan in a follow-up PATCH once we have the new user's id.
                    const created = await api.adminCreateUser(
                      email.trim(),
                      password,
                      isAdmin,
                      displayName.trim(),
                      newGroupId,
                    );
                    if (newPlanId) await api.adminUpdateUser(created.id, { plan_id: newPlanId });
                    toast({ body: t("admin.providers.addedToast", { name: displayName.trim() || email.trim() }), type: "info", autoHideDuration: 2500 });
                    resetForm();
                    await reloadAll();
                  })
                }
              />
              <Button label={t("admin.common.cancel")} variant="ghost" clickAction={resetForm} />
            </div>
          </div>
        </Card>
      )}

      {filtered.length === 0 ? (
        <EmptyState
          title={t("admin.users.noMatchTitle")}
          description={
            groupFilter !== "all" ? t("admin.users.noMatchInGroup") : t("admin.users.noMatchTryDifferent")
          }
        />
      ) : (
        <>
          {groups.length > 0 && (
            <div className="claw-row claw-row-between">
              <label className="claw-toggle-inline">
                <CheckboxInput
                  value={
                    shownSelectedCount === 0 ? false : shownSelectedCount === shown.length ? true : "indeterminate"
                  }
                  label={t("admin.users.selectAllLoaded")}
                  isLabelHidden
                  onChange={toggleSelectAllShown}
                />
                <Text size="sm" color="secondary">
                  {selectedIds.size > 0 ? t("admin.users.selectedCount", { count: String(selectedIds.size) }) : t("admin.users.selectAll")}
                </Text>
              </label>
              {selectedIds.size > 0 && (
                <div className="claw-row">
                  <Text size="sm" color="secondary">
                    {t("admin.users.moveTo")}
                  </Text>
                  <Button
                    label={t("admin.users.noGroup")}
                    size="sm"
                    variant={bulkTarget === null ? "primary" : "secondary"}
                    clickAction={() => setBulkTarget(null)}
                  />
                  {groups.map((g) => (
                    <Button
                      key={g.id}
                      label={g.name}
                      size="sm"
                      variant={bulkTarget === g.id ? "primary" : "secondary"}
                      clickAction={() => setBulkTarget(g.id)}
                    />
                  ))}
                  <Button
                    label={t("admin.users.apply", { count: String(selectedIds.size) })}
                    size="sm"
                    variant="primary"
                    isLoading={bulkApplying}
                    isDisabled={bulkApplying || bulkTarget === undefined}
                    clickAction={applyBulkGroup}
                  />
                  <Button
                    label={t("admin.users.clear")}
                    size="sm"
                    variant="ghost"
                    isDisabled={bulkApplying}
                    clickAction={() => {
                      setSelectedIds(new Set());
                      setBulkTarget(undefined);
                    }}
                  />
                </div>
              )}
            </div>
          )}
          <div className="claw-user-list">
            {shown.map((u) => (
              <UserRow
                key={u.id}
                user={u}
                selfId={selfId}
                groups={groups}
                plans={plans}
                reload={reloadAll}
                createGroup={createGroup}
                guard={guard}
                selectable={groups.length > 0}
                selected={selectedIds.has(u.id)}
                onToggleSelect={() => toggleSelect(u.id)}
              />
            ))}
          </div>
          {filtered.length > shown.length && (
            <div className="claw-row">
              <Button
                label={t("admin.users.loadMoreCount", { count: String(filtered.length - shown.length) })}
                variant="secondary"
                size="sm"
                clickAction={() => setVisible((v) => v + USERS_PAGE)}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}

// How an account was created — informational badge in the Users list. Brand
// logos for OAuth providers reuse the login page's assets; other methods get a
// plain lucide glyph.
const SIGNUP_METHOD_META: Record<string, { labelKey: string; icon: IconType; logo?: string }> = {
  password: { labelKey: "admin.users.signup.password", icon: Mail },
  google: { labelKey: "admin.users.signup.google", icon: Mail, logo: "/oauth-providers/google-g.png" },
  microsoft: { labelKey: "admin.users.signup.microsoft", icon: Mail, logo: "/oauth-providers/microsoft.png" },
  admin_created: { labelKey: "admin.users.signup.addedByAdmin", icon: UserPlus },
  dev_token: { labelKey: "admin.users.signup.devToken", icon: Terminal },
  imported: { labelKey: "admin.users.signup.imported", icon: Upload },
};

function SignupMethodBadge({ method }: { method: string }) {
  const t = useT();
  const meta = SIGNUP_METHOD_META[method] ?? SIGNUP_METHOD_META.password;
  return (
    <Badge
      variant="neutral"
      icon={
        meta.logo ? (
          <img src={meta.logo} alt="" aria-hidden="true" className="claw-signup-logo" />
        ) : (
          <Icon icon={meta.icon} size="xsm" />
        )
      }
      label={t(meta.labelKey)}
    />
  );
}

// Initials for the avatar: first letters of the first two name words, else the
// first two characters of the name/email.
function userInitials(name: string, email: string): string {
  const base = (name || email || "?").trim();
  const parts = base.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

function UserRow({
  user: u,
  selfId,
  groups,
  plans,
  reload,
  createGroup,
  guard,
  selectable = false,
  selected = false,
  onToggleSelect,
}: {
  user: AdminUser;
  selfId: string;
  groups: GroupInfo[];
  plans: PlanInfo[];
  reload: () => Promise<void>;
  createGroup: (name: string) => Promise<GroupInfo>;
  guard: (fn: () => Promise<void>) => Promise<void>;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [displayName, setDisplayName] = useState(u.display_name);
  const [newPassword, setNewPassword] = useState("");
  const [groupId, setGroupId] = useState<string | null>(u.group_id);
  const [planId, setPlanId] = useState<string | null>(u.plan_id);
  const toast = useToast();
  const isSelf = u.id === selfId;
  const label = u.display_name || u.email;

  return (
    <div className={`claw-user-row${!u.is_active ? " is-suspended" : ""}`}>
      <div className="claw-user-head">
        {selectable && (
          <CheckboxInput
            value={selected}
            label={t("admin.users.selectUser", { name: label })}
            isLabelHidden
            onChange={() => onToggleSelect?.()}
          />
        )}
        <div className="claw-user-avatar" aria-hidden="true">
          {userInitials(u.display_name, u.email)}
        </div>
        <div className="claw-user-main">
          <div className="claw-user-name-line">
            <span className="claw-user-name">{label}</span>
            {u.is_admin && (
              <Badge variant="purple" icon={<Icon icon={Shield} size="xsm" />} label={t("admin.users.adminBadge")} />
            )}
            {isSelf && <Badge variant="neutral" label={t("admin.users.you")} />}
            <SignupMethodBadge method={u.signup_method} />
            {u.group_name && (
              <Badge variant="neutral" icon={<Icon icon={Users} size="xsm" />} label={u.group_name} />
            )}
            {u.plan_name && (
              <Badge variant="neutral" icon={<Icon icon={Gauge} size="xsm" />} label={u.plan_name} />
            )}
            {!u.is_active && (
              <Badge variant="error" icon={<Icon icon={ShieldOff} size="xsm" />} label={t("admin.users.suspendedBadge")} />
            )}
          </div>
          <span className="claw-user-meta">
            {t("admin.users.metaLine", {
              email: u.email,
              sessions: String(u.sessions),
              date: new Date(u.created_at).toLocaleDateString(),
            })}
          </span>
        </div>
        <div className="claw-user-actions">
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              {t("admin.users.adminToggle")}
            </Text>
            <Switch
              value={u.is_admin}
              label={t("admin.users.adminToggleFor", { email: u.email })}
              isLabelHidden
              isDisabled={isSelf}
              changeAction={(checked) =>
                guard(async () => {
                  await api.adminUpdateUser(u.id, { is_admin: checked });
                  await reload();
                })
              }
            />
          </label>
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              {t("admin.users.activeToggle")}
            </Text>
            <Switch
              value={u.is_active}
              label={t("admin.users.activeToggleFor", { email: u.email })}
              isLabelHidden
              isDisabled={isSelf}
              changeAction={(checked) =>
                guard(async () => {
                  await api.adminUpdateUser(u.id, { is_active: checked });
                  await reload();
                })
              }
            />
          </label>
          {u.signup_method === "imported" && !u.has_password && (
            <IconButton
              label={t("admin.users.resendActivation")}
              icon={<Icon icon={Mail} size="sm" />}
              size="sm"
              variant="ghost"
              clickAction={() =>
                guard(async () => {
                  await api.adminResendActivation(u.id);
                  toast({ body: t("admin.users.activationSentToast", { email: u.email }), type: "info", autoHideDuration: 2500 });
                })
              }
            />
          )}
          <IconButton
            label={editing ? t("admin.users.closeEditor") : t("admin.users.editUser")}
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              setDisplayName(u.display_name);
              setNewPassword("");
              setGroupId(u.group_id);
              setPlanId(u.plan_id);
              setEditing((e) => !e);
            }}
          />
          <span className="claw-user-delete">
            <IconButton
              label={t("admin.users.deleteUser")}
              icon={<Icon icon={Trash2} size="sm" />}
              size="sm"
              variant="ghost"
              isDisabled={isSelf}
              clickAction={() =>
                guard(async () => {
                  if (
                    !window.confirm(t("admin.users.deleteUserConfirm", { name: label }))
                  ) {
                    return;
                  }
                  await api.adminDeleteUser(u.id);
                  toast({ body: t("admin.users.userDeletedToast", { name: label }), type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
          </span>
        </div>
      </div>

      {editing && (
        <div className="claw-user-edit">
          <TextInput label={t("admin.users.displayName")} value={displayName} onChange={setDisplayName} />
          <PasswordField
            label={t("admin.users.resetPassword")}
            description={t("admin.users.resetPasswordHint")}
            value={newPassword}
            onChange={setNewPassword}
          />
          <GroupPicker groups={groups} value={groupId} onChange={setGroupId} onCreate={createGroup} />
          {plans.length > 0 && <PlanPicker plans={plans} value={planId} onChange={setPlanId} />}
          <div className="claw-row">
            <Button
              label={t("admin.common.saveChanges")}
              variant="primary"
              icon={<Icon icon="check" size="sm" />}
              size="sm"
              isDisabled={newPassword.length > 0 && newPassword.length < 8}
              clickAction={() =>
                guard(async () => {
                  await api.adminUpdateUser(u.id, {
                    display_name: displayName.trim(),
                    group_id: groupId,
                    plan_id: planId,
                    ...(newPassword ? { password: newPassword } : {}),
                  });
                  setEditing(false);
                  setNewPassword("");
                  toast({ body: t("admin.users.userUpdatedToast"), type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
            <Button label={t("admin.common.cancel")} variant="ghost" size="sm" clickAction={() => setEditing(false)} />
          </div>
        </div>
      )}
    </div>
  );
}
