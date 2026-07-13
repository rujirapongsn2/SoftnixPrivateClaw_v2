export interface SessionInfo {
  id: string;
  title: string;
  updated_at: string;
  model?: string | null;
  running?: boolean;
  // "web" for normal chats, "schedule" for sessions created by a scheduled task
  // (shown with an alarm-clock marker), "telegram"/"heartbeat" for those channels.
  channel?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  meta?: { artifacts?: string[] } | null;
}

export interface AttachmentRef {
  name: string;
  path: string;
  mime: string;
  size: number;
  is_image: boolean;
}

export interface ShareFile {
  name: string;
  is_image: boolean;
}

export interface SharedMessage {
  role: "user" | "assistant";
  content: string;
  files: ShareFile[];
}

export interface SharedConversation {
  title: string;
  messages: SharedMessage[];
  created_at: string | null;
}

export interface CreatedShare {
  id: string;
  token: string;
  url: string;
  path: string;
  expires_at: string | null;
}

export interface AgentEvent {
  type:
    | "turn_started"
    | "text_delta"
    | "thinking_delta"
    | "tool_started"
    | "tool_finished"
    | "tool_progress"
    | "plan_updated"
    | "tool_confirm_request"
    | "tool_confirm_resolved"
    | "turn_completed"
    | "turn_error";
  turn_id: string;
  text?: string;
  tool?: string;
  args_preview?: string;
  result_preview?: string;
  is_error?: boolean;
  content?: string;
  message?: string;
  artifacts?: string[];
  request_id?: string;
  approved?: boolean;
  // tool_progress: live sub-step of a long tool (workflow plan/step/synthesize)
  label?: string;
  stage?: string;
  index?: number;
  total?: number;
  status?: string;
  // plan_updated: the agent's current working plan (goal + step checklist)
  goal?: string;
  steps?: { step: string; status: string }[];
}

export interface WorkingPlan {
  goal: string;
  steps: { step: string; status: string }[];
}

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  // Built-in skills ship with the platform: read-only, always enabled.
  builtin?: boolean;
}

export interface MemoryInfo {
  core: string;
  history: string[];
}

export interface ConnectorInfo {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command: string;
  url: string;
  env: Record<string, string>;
  enabled: boolean;
  runtime: { status: string; tools?: number; error?: string };
}

export interface FieldSpec {
  key: string;
  label: string;
  help: string;
  secret: boolean;
  optional: boolean;
  placeholder: string;
  prefix: string;
}

export interface ConnectorPreset {
  key: string;
  name: string;
  label: string;
  description: string;
  transport: "stdio" | "http";
  category: string;
  setup: "api_key" | "token" | "oauth" | "custom";
  command: string;
  url: string;
  // When true, `url` is only a prefilled default — the setup form should let
  // the user override it (e.g. a self-hosted Softnix ONE endpoint).
  url_configurable: boolean;
  fields: FieldSpec[];
  docs: string;
  oauth_provider: string;
  oauth_scopes: string;
  env_prefix: string;
}

export interface ScheduleInfo {
  id: string;
  name: string;
  prompt: string;
  cron: string;
  interval_seconds: number;
  session_id: string | null;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  visibility: "private" | "public";
  is_owner: boolean;
  docs: number;
  owner_id?: string;
}

export interface KnowledgeDoc {
  id: string;
  title: string;
  filename: string;
  mime: string;
  size: number;
  chars: number;
  chunks: number;
  // Background ingestion lifecycle: pending → processing → ready | failed.
  status: "pending" | "processing" | "ready" | "failed";
  error: string;
  created_at: string;
}

export interface KnowledgeDocPreview {
  available: boolean;
  status?: string; // set when available is false (e.g. pending/failed/missing)
  title?: string;
  filename?: string;
  total_chars?: number;
  offset?: number;
  next_offset?: number;
  has_more?: boolean;
  text?: string;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  role: string;
  // False for an OAuth-only or not-yet-activated (imported-pending) account
  // — the frontend uses this to decide whether to show a "change password"
  // form on the Profile settings page.
  has_password: boolean;
}

export interface AdminUser extends AuthUser {
  is_active: boolean;
  // How the account was created — informational only, shown as a badge in the
  // Control Plane's Users list. Accounts predating this field default to
  // "password" (the oldest signup path), which may not be exact for them.
  signup_method: "password" | "google" | "microsoft" | "admin_created" | "dev_token" | "imported";
  sessions: number;
  group_id: string | null;
  group_name: string | null;
  plan_id: string | null;
  plan_name: string | null;
  created_at: string;
}

export interface GroupInfo {
  id: string;
  name: string;
  is_default: boolean;
  user_count: number;
  plan_id: string | null;
  plan_name: string | null;
}

// A usage-tier plan (Free/Plus/Pro/Max/Unlimited-style): model cost ceilings +
// daily/per-minute quotas. Mirrors claw/db/models.py::PolicyPlan.
export interface PlanInfo {
  id: string;
  name: string;
  rank: number;
  max_chat_cost: ModelCost;
  allow_image: boolean;
  max_image_cost: ModelCost;
  messages_per_day: number; // 0 = unlimited
  images_per_day: number; // 0 = unlimited
  turns_per_minute: number; // 0 = inherit global
  is_default: boolean;
  user_count?: number; // attached by the admin list endpoint
}

export type PlanCreate = Omit<PlanInfo, "id" | "user_count">;
export type PlanPatch = Partial<PlanCreate>;

// GET /api/my/plan — the caller's effective plan + today's consumption.
export interface MyPlan {
  plan: PlanInfo | null;
  used: { turns: number; images: number };
  messages_remaining?: number | null;
  images_remaining?: number | null;
}

// Bulk user import (CSV/XLSX) — parse is stateless: the browser holds the
// full parsed grid and posts it back (with the chosen mapping) on commit.
export interface UserImportParseResult {
  columns: string[];
  rows: string[][];
  row_count: number;
}
export interface UserImportMapping {
  email_col: number;
  name_mode: "full" | "split" | "none";
  full_name_col?: number | null;
  first_name_col?: number | null;
  last_name_col?: number | null;
}
export type UserImportRowStatus =
  | "created"
  | "duplicate_in_file"
  | "already_exists"
  | "invalid_email"
  | "missing_email"
  | "error";
export interface UserImportRowResult {
  row_index: number;
  email: string;
  status: UserImportRowStatus;
}
export interface UserImportCommitResult {
  created: number;
  results: UserImportRowResult[];
}

export interface ActivityPoint {
  label: string;
  count: number;
}

export interface ModelUsagePoint {
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  turns: number;
}

export interface ProviderUsageSummary {
  name: string;
  enabled: boolean;
  has_key: boolean;
  model_count: number;
  enabled_model_count: number;
}

export interface SessionsByUserPoint {
  user_id: string;
  label: string;
  sessions: number;
}

export interface TokenUsagePoint {
  bucket: string;
  prompt_tokens: number;
  completion_tokens: number;
  turns: number;
}
export interface TokenUsageSeries {
  key: string;
  label: string;
  prompt_tokens: number;
  completion_tokens: number;
  turns: number;
  points: TokenUsagePoint[];
}
export interface TokenUsageReport {
  granularity: string;
  group_by: string;
  buckets: string[];
  series: TokenUsageSeries[];
  totals: { prompt_tokens: number; completion_tokens: number; turns: number };
}
export interface TokenUsageParams {
  granularity?: "daily" | "weekly" | "monthly" | "yearly";
  group_by?: "user" | "model" | "provider";
  user_id?: string;
  model?: string;
  provider?: string;
  start?: string;
  end?: string;
}

// Filter options for the Tokens Usage report — every model id and its
// resolvable provider name across every scope (admin-global + all users'
// BYOK), so private-provider usage is still selectable/labelled correctly.
export interface UsageDimensionModel {
  model_id: string;
  provider: string;
}
export interface UsageDimensions {
  providers: string[];
  models: UsageDimensionModel[];
}

export interface GuardrailHitsByUserPoint {
  user_id: string;
  label: string;
  count: number;
}
export interface GuardrailHitsByRulePoint {
  rule: string;
  count: number;
}

export interface AdminOverview {
  stats: Record<string, number | boolean>;
  activity_by_day: ActivityPoint[];
  activity_by_hour: ActivityPoint[];
  usage_by_model: ModelUsagePoint[];
  providers: ProviderUsageSummary[];
  sessions_by_user_7d: SessionsByUserPoint[];
  sessions_by_day_7d: ActivityPoint[];
  guardrail_hits_by_day: ActivityPoint[];
  guardrail_hits_by_user: GuardrailHitsByUserPoint[];
  guardrail_hits_by_rule: GuardrailHitsByRulePoint[];
  plans_report: PlansReport;
}

export interface PlansReport {
  plans: PlanInfo[];
  usage_today: PlanUsageRow[];
}

export interface PlanUsageRow {
  user_id: string;
  label: string;
  plan_name: string | null;
  turns: number;
  messages_limit: number;
  images: number;
  images_limit: number;
}

export type ModelCost = "low" | "medium" | "high" | "very_high";

export type ModelKind = "chat" | "image";

export interface LLMModelCfg {
  id: string;
  model_id: string;
  label: string;
  enabled: boolean;
  is_default: boolean;
  cost: ModelCost;
  description: string;
  // "chat" = agent chat picker; "image" = text-to-image only.
  kind: ModelKind;
}

export interface LLMProviderCfg {
  id: string;
  name: string;
  api_base: string;
  has_key: boolean;
  enabled: boolean;
  // LiteLLM routing prefix auto-applied to model ids added under this provider
  // (e.g. "openai", "openrouter"). Empty on providers created before this
  // existed — those still type the full model id manually.
  model_prefix: string;
  models: LLMModelCfg[];
}

export interface GuardrailRule {
  id: string;
  name: string;
  pattern: string;
  action: "mask" | "block" | "monitor";
  scopes: string[];
  severity: string;
  placeholder: string;
  enabled: boolean;
  is_builtin: boolean;
}

export interface AuditRow {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  user_id: string | null;
  user_label: string;
  session_id: string | null;
  created_at: string;
}

export interface TelegramAdminConfig {
  has_token: boolean;
  enabled: boolean;
  // "database" once an admin has saved anything here; "env" while still
  // running off the CLAW_TELEGRAM_BOT_TOKEN fallback; "none" if unconfigured.
  source: "database" | "env" | "none";
  running: boolean;
  bot_username: string;
}

export interface SmtpAdminConfig {
  provider: string;
  host: string;
  port: number;
  username: string;
  from_address: string;
  use_tls: boolean;
  use_ssl: boolean;
  enabled: boolean;
  has_password: boolean;
}
export interface SmtpAdminConfigBody {
  provider: string;
  host: string;
  port: number;
  username: string;
  password: string; // empty keeps the existing password
  from_address: string;
  use_tls: boolean;
  use_ssl: boolean;
  enabled: boolean;
}

export interface OAuthAppPublic {
  client_id: string;
  tenant: string;
  has_secret: boolean;
}

export interface OAuthAppsInfo {
  google: OAuthAppPublic;
  microsoft: OAuthAppPublic;
  redirect_uris: { google: string; microsoft: string };
  login_redirect_uris: { google: string; microsoft: string };
}

export interface GuardrailTestResult {
  action: "mask" | "block" | "monitor" | null;
  matched_rules: { name: string; scope: string }[];
  masked: string;
  severity: string;
  monitor_only: boolean;
}

export interface ModelOption {
  model_id: string;
  label: string;
  provider: string;
  is_default: boolean;
  cost: ModelCost;
  description: string;
  // "global" = admin-configured (Control Plane); "private" = the user's own
  // bring-your-own-key model. Absent on the env-fallback option.
  scope?: "global" | "private";
}

// Provider/model management API surface — one shape, two scopes. The admin
// Control Plane binds it to /api/admin/*, the per-user "My Models" screen binds
// it to /api/my/*. Both drive the SAME Providers UI (see LlmProviders in
// Admin.tsx), so there is a single implementation to maintain.
export interface LlmProviderCreate {
  name: string;
  api_key: string;
  api_base: string;
  enabled?: boolean;
  model_prefix?: string;
}
export interface LlmProviderPatch {
  name?: string;
  api_key?: string;
  api_base?: string;
  enabled?: boolean;
  model_prefix?: string;
}
export interface LlmModelCreate {
  model_id: string;
  label: string;
  enabled?: boolean;
  cost?: ModelCost;
  description?: string;
  kind?: ModelKind;
}
export interface LlmModelPatch {
  model_id?: string;
  label?: string;
  enabled?: boolean;
  is_default?: boolean;
  cost?: ModelCost;
  description?: string;
  kind?: ModelKind;
}
export interface LlmApi {
  list: () => Promise<{ providers: LLMProviderCfg[] }>;
  createProvider: (p: LlmProviderCreate) => Promise<LLMProviderCfg>;
  updateProvider: (id: string, p: LlmProviderPatch) => Promise<LLMProviderCfg>;
  deleteProvider: (id: string) => Promise<unknown>;
  createModel: (providerId: string, m: LlmModelCreate) => Promise<LLMModelCfg>;
  updateModel: (id: string, m: LlmModelPatch) => Promise<LLMModelCfg>;
  deleteModel: (id: string) => Promise<unknown>;
}

export interface BrowserExtensionInstance {
  extension_id: string;
  label: string;
  last_seen: number;
  online: boolean;
}

export interface BrowserExtensionStatus {
  client_extension_enabled: boolean;
  paired: boolean;
  online: boolean;
  extensions: BrowserExtensionInstance[];
}

export interface BrowserExtensionPairing {
  api_base: string;
  instance_id: string;
  pairing_ticket: string;
  expires_at: number;
}

export function getToken(): string {
  return localStorage.getItem("claw_jwt") ?? "";
}

export function setToken(token: string) {
  localStorage.setItem("claw_jwt", token);
}

export function clearToken() {
  localStorage.removeItem("claw_jwt");
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Extends Error additively (status/body) so existing catch blocks that only
// read `.message`/`String(e)` see no change, while a caller that needs to
// branch on the server's structured error (e.g. a 403 with a `reason` field)
// can inspect `.status`/`.body` instead of parsing the message string.
export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, text: string) {
    super(`${status} ${text}`);
    this.status = status;
    try {
      this.body = JSON.parse(text);
    } catch {
      this.body = undefined;
    }
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    if (resp.status === 401) clearToken();
    throw new ApiError(resp.status, await resp.text());
  }
  return resp.json();
}

export const api = {
  register: (email: string, password: string, display_name = "") =>
    request<{ access_token: string; user: AuthUser }>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, display_name }),
    }),
  login: (email: string, password: string) =>
    request<{ access_token: string; user: AuthUser }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  // For a bulk-imported user (no password yet): redeems the signed token
  // from an emailed activation link (#activate=<token>) to set their
  // password and log in immediately, same response shape as login/register.
  completeRegistration: (token: string, password: string, display_name?: string) =>
    request<{ access_token: string; user: AuthUser }>("/api/auth/complete-registration", {
      method: "POST",
      body: JSON.stringify({ token, password, display_name }),
    }),
  // Decodes an activation link token so the set-password form can be
  // prefilled with the real account's email/display name. POST (token in
  // the body, not a GET path param) so the token never appears in a URL a
  // reverse proxy/CDN would log.
  activationInfo: (token: string) =>
    request<{ email: string; display_name: string }>("/api/auth/activation", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  // Always resolves the same way regardless of account state — the backend
  // never reveals that distinction (account enumeration). If it matches an
  // account with a password, a reset link is emailed; if it matches an
  // imported account that never activated, an activation link is emailed
  // instead (see forgot_password() in claw/api/auth.py).
  forgotPassword: (email: string) =>
    request<{ ok: boolean }>("/api/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  // Redeems a signed password-reset link (#reset-password=<token>) to set a
  // new password and log in immediately, same response shape as login/register.
  resetPassword: (token: string, password: string) =>
    request<{ access_token: string; user: AuthUser }>("/api/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    }),
  // Self-service change for an already-logged-in user — requires the
  // current password (proves identity via the credential itself, since the
  // caller already holds a valid session).
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  me: () => request<AuthUser>("/api/auth/me"),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  providers: () => request<{ providers: string[] }>("/api/auth/providers"),
  features: () => request<{ speech_to_text: boolean }>("/api/features"),

  transcribe: async (audio: Blob, filename = "audio.webm"): Promise<string> => {
    const form = new FormData();
    form.append("file", audio, filename);
    const resp = await fetch("/api/transcribe", {
      method: "POST",
      headers: authHeaders(), // no Content-Type: browser sets multipart boundary
      body: form,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    return (await resp.json()).text as string;
  },

  listSessions: () => request<SessionInfo[]>("/api/sessions"),
  createSession: (title = "New chat") =>
    request<{ id: string }>("/api/sessions", { method: "POST", body: JSON.stringify({ title }) }),
  renameSession: (id: string, title: string) =>
    request(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteSession: (id: string) => request(`/api/sessions/${id}`, { method: "DELETE" }),
  listMessages: (sessionId: string) => request<ChatMessage[]>(`/api/sessions/${sessionId}/messages`),
  uploadAttachments: async (sessionId: string, files: File[]): Promise<AttachmentRef[]> => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    const resp = await fetch(`/api/sessions/${sessionId}/attachments`, {
      method: "POST",
      headers: authHeaders(), // no Content-Type: browser sets multipart boundary
      body: form,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    return resp.json();
  },

  listSkills: () => request<SkillInfo[]>("/api/skills"),
  saveSkill: (skill: Omit<SkillInfo, "id">) =>
    request<SkillInfo>(`/api/skills/${encodeURIComponent(skill.name)}`, {
      method: "PUT",
      body: JSON.stringify(skill),
    }),
  deleteSkill: (id: string) => request(`/api/skills/${id}`, { method: "DELETE" }),

  getMemory: () => request<MemoryInfo>("/api/memory"),
  saveMemory: (content: string) =>
    request("/api/memory", { method: "PUT", body: JSON.stringify({ content }) }),

  listConnectors: () => request<ConnectorInfo[]>("/api/connectors"),
  connectorPresets: () => request<ConnectorPreset[]>("/api/connectors/presets"),
  saveConnector: (c: Omit<ConnectorInfo, "id" | "runtime">) =>
    request<ConnectorInfo>(`/api/connectors/${encodeURIComponent(c.name)}`, {
      method: "PUT",
      body: JSON.stringify(c),
    }),
  deleteConnector: (id: string) => request(`/api/connectors/${id}`, { method: "DELETE" }),
  // One-click OAuth: returns the provider authorize URL for the browser to visit.
  connectorOAuthStart: (presetKey: string) =>
    request<{ url: string }>(`/api/connectors/oauth/${encodeURIComponent(presetKey)}/start`),

  // Knowledge bases (OKF): uploaded documents the agent can search to answer from.
  listKnowledge: () => request<KnowledgeBase[]>("/api/knowledge"),
  createKnowledge: (name: string, description: string, visibility: "private" | "public") =>
    request<KnowledgeBase>("/api/knowledge", {
      method: "POST",
      body: JSON.stringify({ name, description, visibility }),
    }),
  updateKnowledge: (id: string, patch: Partial<Pick<KnowledgeBase, "name" | "description" | "visibility">>) =>
    request<KnowledgeBase>(`/api/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteKnowledge: (id: string) => request(`/api/knowledge/${id}`, { method: "DELETE" }),
  listKnowledgeDocs: (id: string) => request<KnowledgeDoc[]>(`/api/knowledge/${id}/documents`),
  uploadKnowledgeDocs: async (
    id: string,
    files: File[],
    onProgress?: (done: number, total: number) => void,
  ): Promise<{ ingested: { title: string }[]; errors: string[] }> => {
    // The backend accepts at most 10 files per request, so send them in
    // sequential batches and merge the results. This lets a user pick 100 files
    // at once without hitting a 413, while keeping the same return shape.
    const BATCH = 10;
    const ingested: { title: string }[] = [];
    const errors: string[] = [];
    for (let i = 0; i < files.length; i += BATCH) {
      const form = new FormData();
      for (const f of files.slice(i, i + BATCH)) form.append("files", f);
      const resp = await fetch(`/api/knowledge/${id}/documents`, {
        method: "POST",
        headers: authHeaders(), // browser sets multipart boundary
        body: form,
      });
      if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
      const batch = (await resp.json()) as { ingested: { title: string }[]; errors: string[] };
      ingested.push(...batch.ingested);
      errors.push(...batch.errors);
      onProgress?.(Math.min(i + BATCH, files.length), files.length);
    }
    return { ingested, errors };
  },
  deleteKnowledgeDoc: (kbId: string, docId: string) =>
    request(`/api/knowledge/${kbId}/documents/${docId}`, { method: "DELETE" }),
  previewKnowledgeDoc: (kbId: string, docId: string, offset = 0) =>
    request<KnowledgeDocPreview>(
      `/api/knowledge/${kbId}/documents/${docId}/preview?offset=${offset}`,
    ),

  listSchedules: () => request<ScheduleInfo[]>("/api/schedules"),
  createSchedule: (s: Partial<ScheduleInfo>) =>
    request<ScheduleInfo>("/api/schedules", { method: "POST", body: JSON.stringify(s) }),
  updateSchedule: (id: string, s: Partial<ScheduleInfo>) =>
    request<ScheduleInfo>(`/api/schedules/${id}`, { method: "PUT", body: JSON.stringify(s) }),
  deleteSchedule: (id: string) => request(`/api/schedules/${id}`, { method: "DELETE" }),
  runScheduleNow: (id: string) =>
    request<ScheduleInfo>(`/api/schedules/${id}/run`, { method: "POST" }),

  adminListUsers: () => request<AdminUser[]>("/api/admin/users"),
  adminStats: () =>
    request<Record<string, number | boolean>>("/api/admin/stats"),
  adminCreateUser: (
    email: string,
    password: string,
    is_admin: boolean,
    display_name = "",
    group_id: string | null = null,
  ) =>
    request<AdminUser>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({ email, password, is_admin, display_name, group_id }),
    }),
  adminUpdateUser: (
    id: string,
    // Omit group_id/plan_id to leave unchanged; pass null to clear.
    patch: {
      is_admin?: boolean;
      is_active?: boolean;
      display_name?: string;
      password?: string;
      group_id?: string | null;
      plan_id?: string | null;
    },
  ) => request<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  adminDeleteUser: (id: string) => request(`/api/admin/users/${id}`, { method: "DELETE" }),
  adminResendActivation: (id: string) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}/resend-activation`, { method: "POST" }),

  // Bulk user import — parse returns the full grid (no server-side state);
  // commit takes the same rows back along with the column mapping.
  adminImportUsersParse: async (file: File): Promise<UserImportParseResult> => {
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch("/api/admin/users/import/parse", {
      method: "POST",
      headers: authHeaders(), // no Content-Type: browser sets multipart boundary
      body: form,
    });
    if (!resp.ok) throw new ApiError(resp.status, await resp.text());
    return resp.json();
  },
  adminImportUsersCommit: (payload: {
    columns: string[];
    rows: string[][];
    mapping: UserImportMapping;
    group_id?: string | null;
  }) =>
    request<UserImportCommitResult>("/api/admin/users/import/commit", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // -- admin: user groups (organizational only) --
  adminListGroups: () => request<GroupInfo[]>("/api/admin/groups"),
  adminCreateGroup: (name: string) =>
    request<GroupInfo>("/api/admin/groups", { method: "POST", body: JSON.stringify({ name }) }),
  adminDeleteGroup: (id: string) => request(`/api/admin/groups/${id}`, { method: "DELETE" }),
  adminSetDefaultGroup: (group_id: string | null) =>
    request<{ default_group_id: string | null }>("/api/admin/groups/default", {
      method: "PUT",
      body: JSON.stringify({ group_id }),
    }),
  adminUpdateGroup: (id: string, patch: { plan_id?: string | null }) =>
    request<GroupInfo>(`/api/admin/groups/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  // -- admin: usage-tier plans --
  adminListPlans: () => request<PlanInfo[]>("/api/admin/plans"),
  adminCreatePlan: (plan: PlanCreate) =>
    request<PlanInfo>("/api/admin/plans", { method: "POST", body: JSON.stringify(plan) }),
  adminUpdatePlan: (id: string, patch: PlanPatch) =>
    request<PlanInfo>(`/api/admin/plans/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  adminDeletePlan: (id: string) => request(`/api/admin/plans/${id}`, { method: "DELETE" }),
  adminSetDefaultPlan: (plan_id: string | null) =>
    request<{ default_plan_id: string | null }>("/api/admin/plans/default", {
      method: "PUT",
      body: JSON.stringify({ plan_id }),
    }),

  // -- user: my effective plan + today's usage (composer quota hint) --
  myPlan: () => request<MyPlan>("/api/my/plan"),

  // -- admin: overview / LLM providers / guardrails / audit --
  adminOverview: () => request<AdminOverview>("/api/admin/overview"),
  adminTokenUsage: (params: TokenUsageParams = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    return request<TokenUsageReport>(`/api/admin/usage/tokens?${q.toString()}`);
  },
  adminUsageDimensions: () => request<UsageDimensions>("/api/admin/usage/dimensions"),

  adminGuardrails: () =>
    request<{ monitor_only: boolean; tool_args_exempt: string[]; rules: GuardrailRule[] }>(
      "/api/admin/guardrails",
    ),
  adminSetMonitorOnly: (monitor_only: boolean) =>
    request<{ monitor_only: boolean }>("/api/admin/guardrails", {
      method: "PUT",
      body: JSON.stringify({ monitor_only }),
    }),
  // Replace the tool-args exemption list (tool-name globs). monitor_only must be
  // sent too (the endpoint owns both) — pass the current value through.
  adminSetToolArgsExempt: (monitor_only: boolean, tool_args_exempt: string[]) =>
    request<{ monitor_only: boolean; tool_args_exempt: string[] }>("/api/admin/guardrails", {
      method: "PUT",
      body: JSON.stringify({ monitor_only, tool_args_exempt }),
    }),
  adminCreateRule: (r: {
    name: string;
    kind: "keyword" | "regex";
    pattern: string;
    action: "mask" | "block" | "monitor";
    severity?: string;
  }) => request<GuardrailRule>("/api/admin/guardrails/rules", { method: "POST", body: JSON.stringify(r) }),
  adminUpdateRule: (
    id: string,
    patch: {
      enabled?: boolean;
      action?: string;
      name?: string;
      pattern?: string;
      severity?: string;
      kind?: "keyword" | "regex";
    },
  ) =>
    request<GuardrailRule>(`/api/admin/guardrails/rules/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  adminDeleteRule: (id: string) => request(`/api/admin/guardrails/rules/${id}`, { method: "DELETE" }),
  adminTestGuardrails: (text: string) =>
    request<GuardrailTestResult>("/api/admin/guardrails/test", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  adminGetOAuthApps: () => request<OAuthAppsInfo>("/api/admin/oauth-apps"),
  adminSetOAuthApp: (
    provider: "google" | "microsoft",
    body: { client_id: string; client_secret: string; tenant?: string },
  ) => request<OAuthAppPublic>(`/api/admin/oauth-apps/${provider}`, { method: "PUT", body: JSON.stringify(body) }),

  adminGetTelegramConfig: () => request<TelegramAdminConfig>("/api/admin/telegram"),
  adminSetTelegramConfig: (body: { bot_token: string; enabled: boolean }) =>
    request<TelegramAdminConfig>("/api/admin/telegram", { method: "PUT", body: JSON.stringify(body) }),

  adminGetEmailConfig: () => request<SmtpAdminConfig>("/api/admin/email/config"),
  adminSetEmailConfig: (body: SmtpAdminConfigBody) =>
    request<SmtpAdminConfig>("/api/admin/email/config", { method: "PUT", body: JSON.stringify(body) }),
  adminTestEmailConfig: (body: SmtpAdminConfigBody & { recipient: string }) =>
    request<{ ok: boolean }>("/api/admin/email/test", { method: "POST", body: JSON.stringify(body) }),

  adminAudit: (
    filters: { kind?: string; user_id?: string; search?: string; before?: string; limit?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (filters.kind) q.set("kind", filters.kind);
    if (filters.user_id) q.set("user_id", filters.user_id);
    if (filters.search) q.set("search", filters.search);
    if (filters.before) q.set("before", filters.before);
    q.set("limit", String(filters.limit ?? 50));
    return request<{ events: AuditRow[]; kinds: string[]; has_more: boolean; next_before: string | null }>(
      `/api/admin/audit?${q.toString()}`,
    );
  },

  listModels: () => request<{ models: ModelOption[]; default: string }>("/api/models"),

  // Text-to-image: the composer's "+ Image" picker + one-shot generation
  // (separate from the chat WebSocket / agent loop).
  listImageModels: () => request<{ models: ModelOption[] }>("/api/image-models"),
  generateImage: (sessionId: string, model: string, prompt: string, size?: string) =>
    request<{ path: string; prompt: string }>(`/api/sessions/${sessionId}/images`, {
      method: "POST",
      body: JSON.stringify({ model, prompt, size }),
    }),

  getHeartbeat: () =>
    request<{ interval_minutes: number; enabled: boolean; next_run_at: string | null }>("/api/heartbeat"),
  setHeartbeat: (interval_minutes: number) =>
    request<{ interval_minutes: number; enabled: boolean; next_run_at: string | null }>("/api/heartbeat", {
      method: "PUT",
      body: JSON.stringify({ interval_minutes }),
    }),

  submitFeedback: (signal: "up" | "down", opts: { session_id?: string; note?: string; message_preview?: string }) =>
    request<{ recorded: boolean }>("/api/feedback", {
      method: "POST",
      body: JSON.stringify({ signal, ...opts }),
    }),

  createShare: (
    sessionId: string,
    body: { title?: string; messages: { role: string; content: string; artifacts?: string[] }[] },
  ) =>
    request<CreatedShare>(`/api/sessions/${sessionId}/share`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  revokeShare: (id: string) => request<{ revoked: boolean }>(`/api/shares/${id}`, { method: "DELETE" }),
  getShare: (token: string) => request<SharedConversation>(`/api/share/${encodeURIComponent(token)}`),

  getPolicy: () =>
    request<{ monitor_only: boolean; rules: { name: string; action: string; severity: string }[] }>(
      "/api/policy",
    ),
  setPolicy: (monitor_only: boolean) =>
    request<{ monitor_only: boolean }>("/api/policy", {
      method: "PUT",
      body: JSON.stringify({ monitor_only }),
    }),

  getTelegramStatus: () =>
    request<{ enabled: boolean; linked: boolean; bot_username: string }>("/api/telegram/status"),
  createTelegramLink: () =>
    request<{ code: string; expires_in: number; bot_username: string }>("/api/telegram/link", {
      method: "POST",
    }),
  unlinkTelegram: () => request<{ linked: boolean }>("/api/telegram/link", { method: "DELETE" }),

  browserExtensionStatus: () =>
    request<BrowserExtensionStatus>("/api/browser-extension/status"),
  browserExtensionPairingInit: () =>
    request<BrowserExtensionPairing>("/api/browser-extension/pairing/init", { method: "POST" }),
  browserExtensionUnpair: () =>
    request<{ unpaired: number }>("/api/browser-extension/pairing", { method: "DELETE" }),
};

// One factory, two scopes: the admin Control Plane (/api/admin) and the per-user
// "My Models" screen (/api/my). Both bind the identical Providers UI, so provider
// management is defined once.
function makeLlmApi(base: string): LlmApi {
  return {
    list: () => request<{ providers: LLMProviderCfg[] }>(`${base}/llm`),
    createProvider: (p) =>
      request<LLMProviderCfg>(`${base}/providers`, { method: "POST", body: JSON.stringify(p) }),
    updateProvider: (id, p) =>
      request<LLMProviderCfg>(`${base}/providers/${id}`, { method: "PATCH", body: JSON.stringify(p) }),
    deleteProvider: (id) => request(`${base}/providers/${id}`, { method: "DELETE" }),
    createModel: (providerId, m) =>
      request<LLMModelCfg>(`${base}/providers/${providerId}/models`, {
        method: "POST",
        body: JSON.stringify(m),
      }),
    updateModel: (id, m) =>
      request<LLMModelCfg>(`${base}/models/${id}`, { method: "PATCH", body: JSON.stringify(m) }),
    deleteModel: (id) => request(`${base}/models/${id}`, { method: "DELETE" }),
  };
}

export const ADMIN_LLM_API: LlmApi = makeLlmApi("/api/admin");
export const USER_LLM_API: LlmApi = makeLlmApi("/api/my");

export function openChatSocket(sessionId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(
    `${proto}://${location.host}/ws/chat/${sessionId}?token=${encodeURIComponent(getToken())}`,
  );
}

/** URL to open/download a file the agent created in the workspace. The token is
 * in the query so a plain new-tab link authenticates (no header needed). */
export function fileUrl(sessionId: string, path: string): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `/api/sessions/${sessionId}/files/${encoded}?token=${encodeURIComponent(getToken())}`;
}

/** Public URL for a file copied into a share snapshot. No auth token — the
 * capability token in the path is the only credential. */
export function shareFileUrl(token: string, name: string): string {
  return `/api/share/${encodeURIComponent(token)}/files/${encodeURIComponent(name)}`;
}
