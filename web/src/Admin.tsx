import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon, type IconName, type IconType } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Switch } from "@astryxdesign/core/Switch";
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
  Cpu,
  Diamond,
  Globe,
  Info,
  KeyRound,
  LayoutDashboard,
  MessageSquare,
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
  Trash2,
  User as UserIcon,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ErrorText } from "./ErrorText";
import {
  ActivityPoint,
  AdminOverview,
  AdminUser,
  AuditRow,
  GuardrailRule,
  GuardrailTestResult,
  LLMModelCfg,
  LLMProviderCfg,
  ModelCost,
  ModelUsagePoint,
  OAuthAppsInfo,
  SessionsByUserPoint,
  api,
} from "./api";

export type AdminSection = "overview" | "providers" | "guardrails" | "oauth" | "audit" | "users";

export const COST_LABEL: Record<ModelCost, string> = {
  low: "Low cost",
  medium: "Medium cost",
  high: "High cost",
  very_high: "Very high cost",
};

export const ADMIN_SECTIONS: { key: AdminSection; label: string; icon: IconType | IconName }[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "providers", label: "LLM Providers", icon: Cpu },
  { key: "guardrails", label: "Guardrails", icon: ShieldCheck },
  { key: "oauth", label: "OAuth apps", icon: KeyRound },
  { key: "audit", label: "Audit Logs", icon: ScrollText },
  { key: "users", label: "Users", icon: Users },
];

export function AdminPanel({ section, selfId }: { section: AdminSection; selfId: string }) {
  const meta = ADMIN_SECTIONS.find((s) => s.key === section);
  return (
    <div className="claw-settings-panel">
      <div className="claw-settings-panel-header">
        <Icon icon={meta?.icon ?? "check"} size="lg" color="secondary" />
        <Text type="display-3">{meta?.label}</Text>
      </div>
      <div className="claw-panel">
        {section === "overview" && <OverviewPanel />}
        {section === "providers" && <ProvidersPanel />}
        {section === "guardrails" && <GuardrailsPanel />}
        {section === "oauth" && <OAuthAppsPanel />}
        {section === "audit" && <AuditPanel />}
        {section === "users" && <UsersPanel selfId={selfId} />}
      </div>
    </div>
  );
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

// ---------------------------------------------------------------- Overview

const STAT_CARDS: { key: string; label: string; icon: IconType }[] = [
  { key: "users", label: "Users", icon: Users },
  { key: "active_users", label: "Active (7d)", icon: Users },
  { key: "sessions", label: "Sessions", icon: MessageSquare },
  { key: "messages", label: "Messages", icon: MessageSquare },
  { key: "turns", label: "LLM turns", icon: Cpu },
  { key: "prompt_tokens", label: "Prompt tokens", icon: Cpu },
  { key: "consolidations", label: "Memory consolidations", icon: Sparkles },
  { key: "memory_users", label: "Users with memory", icon: Brain },
];

function OverviewPanel() {
  const [data, setData] = useState<AdminOverview | null>(null);
  const { error, guard } = useAsyncError();

  useEffect(() => {
    void guard(async () => setData(await api.adminOverview()));
  }, [guard]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!data) return <Text color="secondary">Loading…</Text>;
  const s = data.stats;

  return (
    <div className="claw-panel">
      <div className="claw-stat-grid">
        {STAT_CARDS.map((c) => (
          <Card key={c.key} padding={2} variant="muted">
            <div className="claw-stat">
              <Icon icon={c.icon} size="sm" color="secondary" />
              <Text type="display-3">{Number(s[c.key] ?? 0).toLocaleString()}</Text>
              <Text size="sm" color="secondary">
                {c.label}
              </Text>
            </div>
          </Card>
        ))}
      </div>

      <div className="claw-row">
        <Badge variant="neutral" icon={<Icon icon={Shield} size="xsm" />} label={`${s.admins ?? 0} admins`} />
        <Badge variant="neutral" icon={<Icon icon={Ban} size="xsm" />} label={`${s.suspended ?? 0} suspended`} />
        <Badge
          variant={s.policy_enforcing ? "success" : "neutral"}
          icon={<Icon icon={ShieldCheck} size="xsm" />}
          label={s.policy_enforcing ? "guardrails enforcing" : "monitor-only"}
        />
        <Badge
          variant={s.browser_enabled ? "success" : "neutral"}
          icon={<Icon icon={Globe} size="xsm" />}
          label={s.browser_enabled ? "browser on" : "browser off"}
        />
        <Badge
          variant={s.telegram_enabled ? "success" : "neutral"}
          icon={<Icon icon={Send} size="xsm" />}
          label={s.telegram_enabled ? "telegram on" : "telegram off"}
        />
      </div>

      <Card padding={3}>
        <Text weight="semibold">CLAW activity — last 14 days</Text>
        <BarChart data={data.activity_by_day} />
      </Card>
      <Card padding={3}>
        <Text weight="semibold">Activity by hour of day</Text>
        <BarChart data={data.activity_by_hour} accent="var(--color-info, #2f9e6f)" />
      </Card>

      <Card padding={3}>
        <Text weight="semibold">Guardrail hits — last 14 days</Text>
        <Text size="sm" color="secondary" as="p">
          Turns where a guardrail rule matched the input, output, or a tool call.
        </Text>
        {data.guardrail_hits_by_day.every((d) => d.count === 0) ? (
          <Text size="sm" color="secondary">
            No guardrail matches in the last 14 days.
          </Text>
        ) : (
          <BarChart data={data.guardrail_hits_by_day} accent="var(--color-error, #c0392b)" />
        )}
      </Card>

      <Card padding={3}>
        <Text weight="semibold">Sessions started — last 7 days</Text>
        {data.sessions_by_day_7d.every((d) => d.count === 0) ? (
          <Text size="sm" color="secondary">
            No sessions started in the last 7 days.
          </Text>
        ) : (
          <BarChart data={data.sessions_by_day_7d} accent="var(--color-warning, #d97706)" />
        )}
      </Card>
      <Card padding={3}>
        <Text weight="semibold">Sessions by user — last 7 days</Text>
        {data.sessions_by_user_7d.length === 0 ? (
          <Text size="sm" color="secondary">
            No sessions started in the last 7 days.
          </Text>
        ) : (
          <SessionsByUserList data={data.sessions_by_user_7d} />
        )}
      </Card>

      <Card padding={3}>
        <Text weight="semibold">LLM providers in use</Text>
        {data.providers.length === 0 ? (
          <Text size="sm" color="secondary">
            No providers configured yet — add one in the LLM Providers section.
          </Text>
        ) : (
          <div className="claw-provider-usage-list">
            {data.providers.map((p) => (
              <div key={p.name} className="claw-provider-usage-row">
                <Icon icon={Cpu} size="sm" color="secondary" />
                <span className="claw-provider-usage-name">{p.name}</span>
                <Badge
                  variant={p.enabled ? "success" : "neutral"}
                  label={p.enabled ? "enabled" : "disabled"}
                />
                <Badge
                  variant={p.has_key ? "neutral" : "warning"}
                  label={p.has_key ? "key set" : "no key"}
                />
                <span className="claw-provider-usage-models">
                  {p.enabled_model_count}/{p.model_count} models enabled
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card padding={3}>
        <Text weight="semibold">Tokens used per model</Text>
        {data.usage_by_model.length === 0 ? (
          <Text size="sm" color="secondary">
            No usage recorded yet — token counts appear here after chats run.
          </Text>
        ) : (
          <ModelUsageChart data={data.usage_by_model} />
        )}
      </Card>
    </div>
  );
}

function SessionsByUserList({ data }: { data: SessionsByUserPoint[] }) {
  const max = Math.max(1, ...data.map((d) => d.sessions));
  return (
    <div className="claw-model-usage-list">
      {data.map((d) => (
        <div key={d.user_id} className="claw-model-usage-row">
          <div className="claw-model-usage-head">
            <span className="claw-model-usage-name claw-model-usage-name--user">{d.label}</span>
            <span className="claw-model-usage-total">
              {d.sessions.toLocaleString()} session{d.sessions === 1 ? "" : "s"}
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
                {total.toLocaleString()} tokens · {d.turns.toLocaleString()} turns
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
                <i className="claw-legend-dot claw-legend-dot--prompt" /> prompt{" "}
                {d.prompt_tokens.toLocaleString()}
              </span>
              <span>
                <i className="claw-legend-dot claw-legend-dot--completion" /> completion{" "}
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
  { key: "openrouter", name: "OpenRouter", subtitle: "Any model", icon: Router, apiBase: "https://openrouter.ai/api/v1", needsBase: false, prefix: "openrouter/", example: "openrouter/anthropic/claude-sonnet-5" },
  { key: "compatible", name: "OpenAI-compatible", subtitle: "vLLM, Ollama, LM Studio", icon: Server, apiBase: "", needsBase: true, prefix: "openai/", example: "openai/your-model" },
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
};

function logoForModelId(modelId: string): string | null {
  const prefix = modelId.trim().split("/")[0];
  return PROVIDER_LOGO[prefix] ?? null;
}

// Same tile treatment as connector brand logos (ConnectorBrandTile) — a
// tinted frame so mismatched brand assets read as one consistent icon set.
function ProviderBrandTile({ logo, fallback }: { logo: string | null; fallback: IconType }) {
  return (
    <div className="claw-connector-tile">
      {logo ? <img src={logo} alt="" aria-hidden="true" /> : <Icon icon={fallback} size="md" color="secondary" />}
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
]);

function modelPrefixWarning(modelId: string): string | null {
  const id = modelId.trim();
  if (!id) return null;
  const prefix = id.split("/")[0];
  if (id.includes("/") && LITELLM_PREFIXES.has(prefix)) return null;
  return "Start the model id with a provider prefix — e.g. openrouter/, openai/, anthropic/, gemini/. Without it the model can't be reached.";
}

// Single-select cost tier — a connected segmented control, not a row of
// independent buttons (which read as separate actions).
function CostSegmented({ value, onChange }: { value: ModelCost; onChange: (c: ModelCost) => void }) {
  return (
    <div className="claw-segmented" role="group" aria-label="Cost tier">
      {COSTS.map((c) => (
        <button
          key={c}
          type="button"
          className={value === c ? "is-active" : ""}
          aria-pressed={value === c}
          onClick={() => onChange(c)}
        >
          {COST_LABEL[c]}
        </button>
      ))}
    </div>
  );
}

function ProvidersPanel() {
  const [providers, setProviders] = useState<LLMProviderCfg[]>([]);
  const [adding, setAdding] = useState(false);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.adminListLLM().then((r) => setProviders(r.providers)), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Configure upstream LLM providers and the models users can pick in chat. API keys are stored
          encrypted.
        </Text>
        {!adding && (
          <Button
            label="Add provider"
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            clickAction={() => setAdding(true)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {adding && (
        <AddProviderForm guard={guard} reload={reload} onClose={() => setAdding(false)} />
      )}

      {providers.length === 0 && !adding ? (
        <EmptyState title="No providers" description="Add a provider to let users choose models in chat." />
      ) : (
        providers.map((p) => <ProviderCard key={p.id} provider={p} reload={reload} guard={guard} />)
      )}
    </div>
  );
}

function AddProviderForm({
  guard,
  reload,
  onClose,
}: {
  guard: (fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
  onClose: () => void;
}) {
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
          Choose a provider
        </Text>
        <div className="claw-preset-grid">
          {PROVIDER_PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={`claw-preset-card${preset?.key === p.key ? " is-active" : ""}`}
              onClick={() => pick(p)}
            >
              <ProviderBrandTile logo={PROVIDER_LOGO[p.key] ?? null} fallback={p.icon} />
              <span className="claw-preset-name">{p.name}</span>
              <span className="claw-preset-sub">{p.subtitle}</span>
            </button>
          ))}
        </div>

        {preset && (
          <>
            <Divider />
            <TextInput label="Display name" placeholder={preset.name} value={name} onChange={setName} />

            {preset.needsBase ? (
              <TextInput
                label="Base URL"
                description="Required for OpenAI-compatible servers, e.g. http://localhost:8000/v1"
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
                  {showAdvanced ? "Hide" : "Advanced"} — override base URL
                </button>
                {showAdvanced && (
                  <TextInput
                    label="Base URL (optional)"
                    description="Leave blank to use the standard endpoint."
                    placeholder={preset.apiBase || "standard endpoint"}
                    value={apiBase}
                    onChange={setApiBase}
                  />
                )}
              </>
            )}

            <TextInput
              label={preset.needsBase ? "API key (optional for local servers)" : "API key"}
              type="password"
              value={apiKey}
              onChange={setApiKey}
            />

            <div className="claw-info-box">
              <Icon icon={Info} size="sm" color="secondary" />
              <Text size="sm" color="secondary">
                Model ids for this type start with <code>{preset.prefix}</code> — e.g.{" "}
                <code>{preset.example}</code>. You'll add models after creating the provider.
              </Text>
            </div>

            <div className="claw-row">
              <Button
                label="Create provider"
                icon={<Icon icon="check" size="sm" />}
                isDisabled={!name.trim() || (preset.needsBase && !apiBase.trim())}
                clickAction={() =>
                  guard(async () => {
                    await api.adminCreateProvider({
                      name: name.trim(),
                      api_key: apiKey,
                      api_base: apiBase.trim(),
                    });
                    toast({ body: `${name.trim()} added`, type: "info", autoHideDuration: 2500 });
                    onClose();
                    await reload();
                  })
                }
              />
              <Button label="Cancel" variant="ghost" clickAction={onClose} />
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
}: {
  provider: LLMProviderCfg;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [addingModel, setAddingModel] = useState(false);
  const [name, setName] = useState(provider.name);
  const [apiBase, setApiBase] = useState(provider.api_base);
  const [apiKey, setApiKey] = useState("");
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
                <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="key set" />
              )}
            </div>
            <Text size="sm" color="secondary" as="p">
              {provider.api_base || "default endpoint"}
            </Text>
          </div>
        </div>
        <div className="claw-row">
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              Enabled
            </Text>
            <Switch
              value={provider.enabled}
              label={`Enable ${provider.name}`}
              isLabelHidden
              changeAction={(checked) =>
                guard(async () => {
                  await api.adminUpdateProvider(provider.id, { enabled: checked });
                  await reload();
                })
              }
            />
          </label>
          <Button
            label="Edit"
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              setName(provider.name);
              setApiBase(provider.api_base);
              setApiKey("");
              setEditing((e) => !e);
            }}
          />
          <Button
            label="Delete"
            icon={<Icon icon={Trash2} size="sm" />}
            size="sm"
            variant="destructive"
            clickAction={() =>
              guard(async () => {
                await api.adminDeleteProvider(provider.id);
                toast({ body: `${provider.name} deleted`, type: "info", autoHideDuration: 2500 });
                await reload();
              })
            }
          />
        </div>
      </div>

      {editing && (
        <Card padding={2} variant="muted">
          <div className="claw-panel">
            <TextInput label="Name" value={name} onChange={setName} />
            <TextInput label="API base URL" value={apiBase} onChange={setApiBase} />
            <TextInput
              label={provider.has_key ? "API key (leave blank to keep current)" : "API key"}
              type="password"
              value={apiKey}
              onChange={setApiKey}
            />
            <div className="claw-row">
              <Button
                label="Save changes"
                icon={<Icon icon="check" size="sm" />}
                size="sm"
                clickAction={() =>
                  guard(async () => {
                    await api.adminUpdateProvider(provider.id, {
                      name: name.trim(),
                      api_base: apiBase.trim(),
                      api_key: apiKey.trim(),
                    });
                    setEditing(false);
                    toast({ body: "Provider updated", type: "info", autoHideDuration: 2500 });
                    await reload();
                  })
                }
              />
              <Button label="Cancel" variant="ghost" size="sm" clickAction={() => setEditing(false)} />
            </div>
          </div>
        </Card>
      )}

      <Divider />
      <Text size="sm" weight="semibold" color="secondary">
        Models
      </Text>
      {provider.models.length === 0 && !addingModel && (
        <Text size="sm" color="secondary">
          No models yet. Add one so it appears in the chat picker.
        </Text>
      )}
      {provider.models.map((m) => (
        <ModelRow key={m.id} model={m} reload={reload} guard={guard} />
      ))}

      {addingModel ? (
        <AddModelForm
          providerId={provider.id}
          guard={guard}
          reload={reload}
          onClose={() => setAddingModel(false)}
        />
      ) : (
        <Button
          label="Add model"
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
  reload,
  guard,
}: {
  model: LLMModelCfg;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [modelId, setModelId] = useState(model.model_id);
  const [label, setLabel] = useState(model.label);
  const [cost, setCost] = useState<ModelCost>(model.cost);
  const [description, setDescription] = useState(model.description);
  const toast = useToast();

  if (editing) {
    return (
      <Card padding={2} variant="muted">
        <div className="claw-panel">
          <div className="claw-info-box">
            <Icon icon={Pencil} size="sm" color="secondary" />
            <Text size="sm" color="secondary">
              Editing <b>{model.label || model.model_id}</b>
            </Text>
          </div>
          <div className="claw-row claw-row-2col">
            <TextInput
              label="Model id"
              placeholder="anthropic/claude-sonnet-5"
              value={modelId}
              onChange={setModelId}
            />
            <TextInput label="Display name" placeholder={modelId} value={label} onChange={setLabel} />
          </div>
          {modelPrefixWarning(modelId) && (
            <div className="claw-info-box is-warning">
              <Icon icon={Info} size="sm" color="warning" />
              <Text size="sm" color="secondary">
                {modelPrefixWarning(modelId)}
              </Text>
            </div>
          )}
          <TextInput
            label="Description"
            description="Shown in the chat model picker"
            value={description}
            onChange={setDescription}
          />
          <div className="claw-row">
            <Text size="sm" color="secondary">
              Cost tier
            </Text>
            <CostSegmented value={cost} onChange={setCost} />
          </div>
          <div className="claw-row">
            <Button
              label="Save changes"
              icon={<Icon icon="check" size="sm" />}
              size="sm"
              isDisabled={!modelId.trim()}
              clickAction={() =>
                guard(async () => {
                  await api.adminUpdateModel(model.id, {
                    model_id: modelId.trim(),
                    label: label.trim(),
                    cost,
                    description: description.trim(),
                  });
                  setEditing(false);
                  toast({ body: "Model saved", type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
            <Button
              label="Cancel"
              variant="ghost"
              size="sm"
              clickAction={() => {
                setModelId(model.model_id);
                setLabel(model.label);
                setCost(model.cost);
                setDescription(model.description);
                setEditing(false);
              }}
            />
          </div>
        </div>
      </Card>
    );
  }

  return (
    <div className="claw-row claw-row-between claw-model-row">
      <div className="claw-row">
        <Text>{model.label || model.model_id}</Text>
        <span className={`claw-cost claw-cost-${model.cost}`}>{COST_LABEL[model.cost]}</span>
        <Text size="sm" color="secondary">
          {model.model_id}
        </Text>
        {model.is_default && (
          <Badge variant="purple" icon={<Icon icon={Star} size="xsm" />} label="default" />
        )}
      </div>
      <div className="claw-row">
        <label className="claw-toggle-inline">
          <Text size="sm" color="secondary">
            {model.enabled ? "On" : "Off"}
          </Text>
          <Switch
            value={model.enabled}
            label={`Enable ${model.label}`}
            isLabelHidden
            changeAction={(checked) =>
              guard(async () => {
                await api.adminUpdateModel(model.id, { enabled: checked });
                await reload();
              })
            }
          />
        </label>
        <Button
          label={model.is_default ? "Default" : "Set default"}
          size="sm"
          variant={model.is_default ? "secondary" : "ghost"}
          isDisabled={model.is_default || !model.enabled}
          clickAction={() =>
            guard(async () => {
              await api.adminUpdateModel(model.id, { is_default: true });
              await reload();
            })
          }
        />
        <Button
          label="Edit"
          icon={<Icon icon={Pencil} size="sm" />}
          size="sm"
          variant="ghost"
          clickAction={() => setEditing(true)}
        />
        <Button
          label="Remove"
          icon={<Icon icon={Trash2} size="sm" />}
          size="sm"
          variant="ghost"
          clickAction={() =>
            guard(async () => {
              await api.adminDeleteModel(model.id);
              await reload();
            })
          }
        />
      </div>
    </div>
  );
}

function AddModelForm({
  providerId,
  guard,
  reload,
  onClose,
}: {
  providerId: string;
  guard: (fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
  onClose: () => void;
}) {
  const [modelId, setModelId] = useState("");
  const [label, setLabel] = useState("");
  const [cost, setCost] = useState<ModelCost>("medium");
  const [description, setDescription] = useState("");
  const toast = useToast();

  return (
    <Card padding={2} variant="muted">
      <div className="claw-panel">
        <div className="claw-row claw-row-2col">
          <TextInput
            label="Model id"
            placeholder="anthropic/claude-sonnet-5"
            value={modelId}
            onChange={setModelId}
          />
          <TextInput label="Display name" placeholder={modelId || "Claude Sonnet 5"} value={label} onChange={setLabel} />
        </div>
        {modelPrefixWarning(modelId) && (
          <div className="claw-info-box is-warning">
            <Icon icon={Info} size="sm" color="warning" />
            <Text size="sm" color="secondary">
              {modelPrefixWarning(modelId)}
            </Text>
          </div>
        )}
        <TextInput
          label="Description"
          description="Shown in the chat model picker"
          value={description}
          onChange={setDescription}
        />
        <div className="claw-row">
          <Text size="sm" color="secondary">
            Cost tier
          </Text>
          <CostSegmented value={cost} onChange={setCost} />
        </div>
        <div className="claw-row">
          <Button
            label="Add model"
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            isDisabled={!modelId.trim()}
            clickAction={() =>
              guard(async () => {
                await api.adminCreateModel(providerId, {
                  model_id: modelId.trim(),
                  label: label.trim(),
                  cost,
                  description: description.trim(),
                });
                toast({ body: "Model added", type: "info", autoHideDuration: 2500 });
                onClose();
                await reload();
              })
            }
          />
          <Button label="Cancel" variant="ghost" size="sm" clickAction={onClose} />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- Guardrails

const ACTION_VARIANT: Record<string, "error" | "warning" | "neutral"> = {
  block: "error",
  mask: "warning",
  monitor: "neutral",
};

// Runs sample text through the live policy so admins can prove the rules fire.
function GuardrailTester({ guard }: { guard: (fn: () => Promise<void>) => Promise<void> }) {
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
          <Text weight="semibold">Test guardrails</Text>
          <Text size="sm" color="secondary" as="p">
            Paste sample text to see exactly what the current rules would mask or block.
          </Text>
        </div>
        <TextArea
          label="Sample text"
          isLabelHidden
          placeholder="e.g. My card is 4111 1111 1111 1111 and email jane@acme.com"
          value={text}
          onChange={setText}
          rows={3}
        />
        <div className="claw-row">
          <Button
            label="Run test"
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
              label="Clear"
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
                Result
              </Text>
              {result.action ? (
                <Badge
                  variant={(ACTION_TONE[result.action] ?? "neutral") as "error" | "warning" | "neutral"}
                  label={result.monitor_only ? `${result.action} (monitor-only)` : result.action}
                />
              ) : (
                <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="no match" />
              )}
              {result.matched_rules.map((m) => (
                <Badge key={m.name} variant="neutral" label={`${m.name} · ${m.scope}`} />
              ))}
            </div>
            {result.action === "mask" && (
              <div className="claw-info-box">
                <Icon icon={Info} size="sm" color="secondary" />
                <Text size="sm" color="secondary">
                  Output would be sent as: <code>{result.masked}</code>
                </Text>
              </div>
            )}
            {result.action === "block" && (
              <div className="claw-info-box is-warning">
                <Icon icon={Info} size="sm" color="warning" />
                <Text size="sm" color="secondary">
                  This content would be blocked before reaching the model or the user.
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
  const [rules, setRules] = useState<GuardrailRule[]>([]);
  const [monitorOnly, setMonitorOnly] = useState(false);
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
      }),
    [],
  );
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  return (
    <div className="claw-panel">
      <Card padding={2} variant="muted">
        <div className="claw-row claw-row-between">
          <div>
            <Text weight="semibold">Enforcement mode</Text>
            <Text size="sm" color="secondary" as="p">
              {monitorOnly
                ? "Monitor-only: matches are logged for audit but never masked or blocked."
                : "Enforcing: rules mask or block matching content across all users."}
            </Text>
          </div>
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              Enforce
            </Text>
            <Switch
              value={!monitorOnly}
              label="Enforce guardrails"
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

      <GuardrailTester guard={guard} />

      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Rules are checked in order against input, output, and tool arguments. Built-in rules can be
          toggled and have their action or severity changed; their name and pattern are fixed.
        </Text>
        {!adding && (
          <Button
            label="Add rule"
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
            <TextInput label="Rule name" value={name} onChange={setName} />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                Match type
              </Text>
              <Button
                label="Keyword"
                size="sm"
                variant={kind === "keyword" ? "primary" : "secondary"}
                clickAction={() => setKind("keyword")}
              />
              <Button
                label="Regex"
                size="sm"
                variant={kind === "regex" ? "primary" : "secondary"}
                clickAction={() => setKind("regex")}
              />
            </div>
            <TextInput
              label={kind === "keyword" ? "Keyword / phrase" : "Regular expression"}
              value={pattern}
              onChange={setPattern}
            />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                Action
              </Text>
              {(["block", "mask", "monitor"] as const).map((a) => (
                <Button
                  key={a}
                  label={a}
                  size="sm"
                  variant={action === a ? "primary" : "secondary"}
                  clickAction={() => setAction(a)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Button
                label="Create rule"
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
              <Button label="Cancel" variant="ghost" clickAction={() => setAdding(false)} />
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
            <Badge variant={ACTION_VARIANT[rule.action] ?? "neutral"} label={rule.action} />
            <Badge variant="neutral" label={rule.severity} />
            {rule.is_builtin && <Badge variant="neutral" label="built-in" />}
          </div>
          <Text size="sm" color="secondary" as="p" className="claw-rule-pattern">
            {rule.pattern}
          </Text>
        </div>
        <div className="claw-row">
          <Switch
            value={rule.enabled}
            label={`Enable ${rule.name}`}
            isLabelHidden
            changeAction={(checked) =>
              guard(async () => {
                await api.adminUpdateRule(rule.id, { enabled: checked });
                await reload();
              })
            }
          />
          <Button
            label="Edit"
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
              label="Delete"
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
              label="Name"
              value={name}
              onChange={setName}
              isDisabled={rule.is_builtin}
            />
            <TextInput
              label={rule.is_builtin ? "Pattern (built-in — read-only)" : "Pattern (regex)"}
              value={pattern}
              onChange={setPattern}
              isDisabled={rule.is_builtin}
            />
            <div className="claw-row">
              <Text size="sm" color="secondary">
                Action
              </Text>
              {(["block", "mask", "monitor"] as const).map((a) => (
                <Button
                  key={a}
                  label={a}
                  size="sm"
                  variant={action === a ? "primary" : "secondary"}
                  clickAction={() => setAction(a)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Text size="sm" color="secondary">
                Severity
              </Text>
              {(["low", "medium", "high", "critical"] as const).map((s) => (
                <Button
                  key={s}
                  label={s}
                  size="sm"
                  variant={severity === s ? "primary" : "secondary"}
                  clickAction={() => setSeverity(s)}
                />
              ))}
            </div>
            <div className="claw-row">
              <Button
                label="Save changes"
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
              <Button label="Cancel" variant="ghost" size="sm" clickAction={() => setEditing(false)} />
            </div>
          </div>
        </Card>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- OAuth apps

function OAuthAppsPanel() {
  const [apps, setApps] = useState<OAuthAppsInfo | null>(null);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.adminGetOAuthApps().then(setApps), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!apps) return <Text color="secondary">Loading…</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">
        Register a Google and/or Microsoft OAuth app once here. It powers both social sign-in
        (login and register) and one-click connectors like Gmail, Outlook, Calendar, and OneDrive —
        no keys in .env. Add both redirect URIs below to the provider's app so both flows work.
      </Text>
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
            <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="Configured" />
          ) : (
            <Badge variant="neutral" label="Not configured" />
          )}
        </div>
        <div className="claw-setup-field">
          <Text size="sm" color="secondary">
            Redirect URIs — add BOTH to your {label} app's authorized redirect URIs:
          </Text>
          <Text size="sm" color="secondary" as="p">
            Sign-in (login / register):
          </Text>
          <Text type="code">{loginRedirectUri}</Text>
          <Text size="sm" color="secondary" as="p">
            Connectors:
          </Text>
          <Text type="code">{redirectUri}</Text>
        </div>
        <TextInput label="Client ID" value={clientId} onChange={setClientId} />
        <TextInput
          label={app.has_secret ? "Client secret (leave blank to keep current)" : "Client secret"}
          type="password"
          value={clientSecret}
          onChange={setClientSecret}
        />
        {withTenant && (
          <TextInput
            label="Tenant (optional — default: common)"
            value={tenant}
            onChange={setTenant}
          />
        )}
        <div className="claw-row">
          <Button
            label="Save"
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                await api.adminSetOAuthApp(provider, {
                  client_id: clientId.trim(),
                  client_secret: clientSecret.trim(),
                  tenant: tenant.trim(),
                });
                setClientSecret("");
                toast({ body: `${label} OAuth saved`, type: "info", autoHideDuration: 2500 });
                await onSaved();
              })
            }
          />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- Audit Logs

const AUDIT_PAGE = 50;

// One audit event: a compact clickable header (kind · who · preview · time)
// that expands to the full, pretty-printed payload.
function AuditEventRow({ event: e }: { event: AuditRow }) {
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
            <span>Actor: {e.user_label}</span>
            {e.session_id && <span>Session: {e.session_id}</span>}
          </div>
          <pre className="claw-audit-json">{JSON.stringify(e.payload, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

function AuditPanel() {
  const [events, setEvents] = useState<AuditRow[]>([]);
  const [kinds, setKinds] = useState<string[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const { error, guard } = useAsyncError();

  // First page — refetched when the kind filter or (debounced) search changes.
  useEffect(() => {
    const t = setTimeout(() => {
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
    return () => clearTimeout(t);
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
        label="Search events"
        value={search}
        placeholder="Filter by tool name, rule, payload text…"
        onChange={setSearch}
      />
      <div className="claw-row">
        <Button
          label="All"
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
          title="No events"
          description={search ? "No events match your search." : "Audit events appear here as the system runs."}
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
                label={busy ? "Loading…" : "Load more"}
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

function UsersPanel({ selfId }: { selfId: string }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [creating, setCreating] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [query, setQuery] = useState("");
  const [visible, setVisible] = useState(USERS_PAGE);
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const reload = useCallback(() => api.adminListUsers().then(setUsers), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  // Reset the "load more" window whenever the search changes.
  useEffect(() => setVisible(USERS_PAGE), [query]);

  const resetForm = () => {
    setCreating(false);
    setEmail("");
    setPassword("");
    setDisplayName("");
    setIsAdmin(false);
  };

  const admins = users.filter((u) => u.is_admin).length;
  const suspended = users.filter((u) => !u.is_active).length;
  const q = query.trim().toLowerCase();
  const filtered = q
    ? users.filter(
        (u) =>
          (u.display_name || "").toLowerCase().includes(q) || u.email.toLowerCase().includes(q),
      )
    : users;
  const shown = filtered.slice(0, visible);

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <div className="claw-panel">
          <Text color="secondary">
            Manage everyone with access to this Softnix PrivateClaw deployment.
          </Text>
          <div className="claw-row">
            <Badge variant="neutral" icon={<Icon icon={Users} size="xsm" />} label={`${users.length} users`} />
            <Badge variant="neutral" icon={<Icon icon={Shield} size="xsm" />} label={`${admins} admins`} />
            {suspended > 0 && (
              <Badge variant="neutral" icon={<Icon icon={Ban} size="xsm" />} label={`${suspended} suspended`} />
            )}
          </div>
        </div>
        {!creating && (
          <Button
            label="Add user"
            icon={<Icon icon={Plus} size="sm" />}
            size="sm"
            clickAction={() => setCreating(true)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {users.length > USERS_PAGE / 2 && (
        <TextInput
          label="Search users"
          isLabelHidden
          startIcon={<Icon icon={Search} size="sm" color="secondary" />}
          placeholder="Search by name or email…"
          value={query}
          onChange={setQuery}
          hasClear
        />
      )}

      {creating && (
        <Card padding={2}>
          <div className="claw-panel">
            <TextInput label="Full name" placeholder="Jane Doe" value={displayName} onChange={setDisplayName} />
            <TextInput label="Email" type="email" placeholder="jane@company.com" value={email} onChange={setEmail} />
            <TextInput
              label="Password"
              type="password"
              description="At least 8 characters."
              value={password}
              onChange={setPassword}
            />
            <label className="claw-toggle-inline">
              <Switch value={isAdmin} label="Make administrator" isLabelHidden changeAction={setIsAdmin} />
              <Text size="sm" color="secondary">
                Administrator (full access to this console)
              </Text>
            </label>
            <div className="claw-row">
              <Button
                label="Create user"
                icon={<Icon icon={Plus} size="sm" />}
                isDisabled={!email.trim() || password.length < 8}
                clickAction={() =>
                  guard(async () => {
                    await api.adminCreateUser(email.trim(), password, isAdmin, displayName.trim());
                    toast({ body: `${displayName.trim() || email.trim()} added`, type: "info", autoHideDuration: 2500 });
                    resetForm();
                    await reload();
                  })
                }
              />
              <Button label="Cancel" variant="ghost" clickAction={resetForm} />
            </div>
          </div>
        </Card>
      )}

      {filtered.length === 0 ? (
        <EmptyState
          title="No users match"
          description="Try a different name or email."
        />
      ) : (
        <>
          <div className="claw-user-list">
            {shown.map((u) => (
              <UserRow key={u.id} user={u} selfId={selfId} reload={reload} guard={guard} />
            ))}
          </div>
          {filtered.length > shown.length && (
            <div className="claw-row">
              <Button
                label={`Load more (${filtered.length - shown.length})`}
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
  reload,
  guard,
}: {
  user: AdminUser;
  selfId: string;
  reload: () => Promise<void>;
  guard: (fn: () => Promise<void>) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [displayName, setDisplayName] = useState(u.display_name);
  const [newPassword, setNewPassword] = useState("");
  const toast = useToast();
  const isSelf = u.id === selfId;
  const label = u.display_name || u.email;

  return (
    <div className={`claw-user-row${!u.is_active ? " is-suspended" : ""}`}>
      <div className="claw-user-head">
        <div className="claw-user-avatar" aria-hidden="true">
          {userInitials(u.display_name, u.email)}
        </div>
        <div className="claw-user-main">
          <div className="claw-user-name-line">
            <span className="claw-user-name">{label}</span>
            {u.is_admin && (
              <Badge variant="purple" icon={<Icon icon={Shield} size="xsm" />} label="admin" />
            )}
            {isSelf && <Badge variant="neutral" label="you" />}
            {!u.is_active && (
              <Badge variant="error" icon={<Icon icon={ShieldOff} size="xsm" />} label="suspended" />
            )}
          </div>
          <span className="claw-user-meta">
            {u.email} · {u.sessions} sessions · joined {new Date(u.created_at).toLocaleDateString()}
          </span>
        </div>
        <div className="claw-user-actions">
          <label className="claw-toggle">
            <Text size="sm" color="secondary">
              Admin
            </Text>
            <Switch
              value={u.is_admin}
              label={`Admin ${u.email}`}
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
              Active
            </Text>
            <Switch
              value={u.is_active}
              label={`Active ${u.email}`}
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
          <IconButton
            label={editing ? "Close editor" : "Edit user"}
            icon={<Icon icon={Pencil} size="sm" />}
            size="sm"
            variant="ghost"
            clickAction={() => {
              setDisplayName(u.display_name);
              setNewPassword("");
              setEditing((e) => !e);
            }}
          />
          <span className="claw-user-delete">
            <IconButton
              label="Delete user"
              icon={<Icon icon={Trash2} size="sm" />}
              size="sm"
              variant="ghost"
              isDisabled={isSelf}
              clickAction={() =>
                guard(async () => {
                  if (
                    !window.confirm(
                      `Delete ${label}? This removes their chats, memory, and settings. This cannot be undone.`,
                    )
                  ) {
                    return;
                  }
                  await api.adminDeleteUser(u.id);
                  toast({ body: `${label} deleted`, type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
          </span>
        </div>
      </div>

      {editing && (
        <div className="claw-user-edit">
          <TextInput label="Display name" value={displayName} onChange={setDisplayName} />
          <TextInput
            label="Reset password"
            type="password"
            description="At least 8 characters. Leave blank to keep the current password."
            value={newPassword}
            onChange={setNewPassword}
          />
          <div className="claw-row">
            <Button
              label="Save changes"
              icon={<Icon icon="check" size="sm" />}
              size="sm"
              isDisabled={newPassword.length > 0 && newPassword.length < 8}
              clickAction={() =>
                guard(async () => {
                  await api.adminUpdateUser(u.id, {
                    display_name: displayName.trim(),
                    ...(newPassword ? { password: newPassword } : {}),
                  });
                  setEditing(false);
                  setNewPassword("");
                  toast({ body: "User updated", type: "info", autoHideDuration: 2500 });
                  await reload();
                })
              }
            />
            <Button label="Cancel" variant="ghost" size="sm" clickAction={() => setEditing(false)} />
          </div>
        </div>
      )}
    </div>
  );
}
