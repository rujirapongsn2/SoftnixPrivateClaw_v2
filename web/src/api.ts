export interface SessionInfo {
  id: string;
  title: string;
  updated_at: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
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
}

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
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

export interface ConnectorPreset {
  key: string;
  name: string;
  label: string;
  description: string;
  transport: "stdio" | "http";
  command: string;
  url: string;
  env_fields: string[];
  docs: string;
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
  adminCreateUser: (email: string, password: string, is_admin: boolean) =>
    request<AdminUser>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({ email, password, is_admin }),
    }),
  adminUpdateUser: (id: string, patch: { is_admin?: boolean; is_active?: boolean }) =>
    request<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

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
};

export function openChatSocket(sessionId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(
    `${proto}://${location.host}/ws/chat/${sessionId}?token=${encodeURIComponent(getToken())}`,
  );
}
