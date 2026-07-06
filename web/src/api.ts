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

export interface AgentEvent {
  type:
    | "turn_started"
    | "text_delta"
    | "thinking_delta"
    | "tool_started"
    | "tool_finished"
    | "tool_progress"
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
  created_at: string;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  role: string;
}

export interface AdminUser extends AuthUser {
  is_active: boolean;
  sessions: number;
  created_at: string;
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

export interface AdminOverview {
  stats: Record<string, number | boolean>;
  activity_by_day: ActivityPoint[];
  activity_by_hour: ActivityPoint[];
  usage_by_model: ModelUsagePoint[];
  providers: ProviderUsageSummary[];
  sessions_by_user_7d: SessionsByUserPoint[];
  sessions_by_day_7d: ActivityPoint[];
  guardrail_hits_by_day: ActivityPoint[];
}

export type ModelCost = "low" | "medium" | "high" | "very_high";

export interface LLMModelCfg {
  id: string;
  model_id: string;
  label: string;
  enabled: boolean;
  is_default: boolean;
  cost: ModelCost;
  description: string;
}

export interface LLMProviderCfg {
  id: string;
  name: string;
  api_base: string;
  has_key: boolean;
  enabled: boolean;
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    if (resp.status === 401) clearToken();
    throw new Error(`${resp.status} ${await resp.text()}`);
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
  me: () => request<AuthUser>("/api/auth/me"),
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
  ): Promise<{ ingested: { title: string }[]; errors: string[] }> => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    const resp = await fetch(`/api/knowledge/${id}/documents`, {
      method: "POST",
      headers: authHeaders(), // browser sets multipart boundary
      body: form,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    return resp.json();
  },
  deleteKnowledgeDoc: (kbId: string, docId: string) =>
    request(`/api/knowledge/${kbId}/documents/${docId}`, { method: "DELETE" }),

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
  adminCreateUser: (email: string, password: string, is_admin: boolean, display_name = "") =>
    request<AdminUser>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({ email, password, is_admin, display_name }),
    }),
  adminUpdateUser: (
    id: string,
    patch: { is_admin?: boolean; is_active?: boolean; display_name?: string; password?: string },
  ) => request<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  adminDeleteUser: (id: string) => request(`/api/admin/users/${id}`, { method: "DELETE" }),

  // -- admin: overview / LLM providers / guardrails / audit --
  adminOverview: () => request<AdminOverview>("/api/admin/overview"),

  adminListLLM: () => request<{ providers: LLMProviderCfg[] }>("/api/admin/llm"),
  adminCreateProvider: (p: { name: string; api_key: string; api_base: string; enabled?: boolean }) =>
    request<LLMProviderCfg>("/api/admin/providers", { method: "POST", body: JSON.stringify(p) }),
  adminUpdateProvider: (
    id: string,
    p: { name?: string; api_key?: string; api_base?: string; enabled?: boolean },
  ) => request<LLMProviderCfg>(`/api/admin/providers/${id}`, { method: "PATCH", body: JSON.stringify(p) }),
  adminDeleteProvider: (id: string) => request(`/api/admin/providers/${id}`, { method: "DELETE" }),
  adminCreateModel: (
    providerId: string,
    m: { model_id: string; label: string; enabled?: boolean; cost?: ModelCost; description?: string },
  ) =>
    request<LLMModelCfg>(`/api/admin/providers/${providerId}/models`, {
      method: "POST",
      body: JSON.stringify(m),
    }),
  adminUpdateModel: (
    id: string,
    m: {
      model_id?: string;
      label?: string;
      enabled?: boolean;
      is_default?: boolean;
      cost?: ModelCost;
      description?: string;
    },
  ) => request<LLMModelCfg>(`/api/admin/models/${id}`, { method: "PATCH", body: JSON.stringify(m) }),
  adminDeleteModel: (id: string) => request(`/api/admin/models/${id}`, { method: "DELETE" }),

  adminGuardrails: () =>
    request<{ monitor_only: boolean; rules: GuardrailRule[] }>("/api/admin/guardrails"),
  adminSetMonitorOnly: (monitor_only: boolean) =>
    request<{ monitor_only: boolean }>("/api/admin/guardrails", {
      method: "PUT",
      body: JSON.stringify({ monitor_only }),
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
