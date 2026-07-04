import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon, type IconName, type IconType } from "@astryxdesign/core/Icon";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  Brain,
  HeartPulse,
  Pencil,
  Play,
  Plug,
  Plus,
  Send,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ErrorText } from "./ErrorText";
import { ConnectorInfo, ConnectorPreset, MemoryInfo, ScheduleInfo, SkillInfo, api } from "./api";

export type SettingsSection =
  | "skills"
  | "memory"
  | "connectors"
  | "schedules"
  | "heartbeat"
  | "telegram";

export const SETTINGS_SECTIONS: { key: SettingsSection; label: string; icon: IconType | IconName }[] = [
  { key: "skills", label: "Skills", icon: Sparkles },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "connectors", label: "Connectors", icon: Plug },
  { key: "schedules", label: "Schedule", icon: "calendar" },
  { key: "heartbeat", label: "Heartbeat", icon: HeartPulse },
  { key: "telegram", label: "Telegram", icon: Send },
];

export function SettingsPanel({ section }: { section: SettingsSection }) {
  const meta = SETTINGS_SECTIONS.find((s) => s.key === section);
  return (
    <div className="claw-settings-panel">
      <div className="claw-settings-panel-header">
        <Icon icon={meta?.icon ?? "check"} size="lg" color="secondary" />
        <Text type="display-3">{meta?.label}</Text>
      </div>
      <div className="claw-panel">
        {section === "skills" && <SkillsPanel />}
        {section === "memory" && <MemoryPanel />}
        {section === "connectors" && <ConnectorsPanel />}
        {section === "schedules" && <SchedulesPanel />}
        {section === "heartbeat" && <HeartbeatPanel />}
        {section === "telegram" && <TelegramPanel />}
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

// ---------------------------------------------------------------- Skills

function SkillsPanel() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [editing, setEditing] = useState<Partial<SkillInfo> | null>(null);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.listSkills().then(setSkills), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  if (editing) {
    return (
      <div className="claw-panel">
        <TextInput
          label="Name"
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
          isDisabled={!!editing.id}
        />
        <TextInput
          label="Description (shown to the agent in every chat)"
          value={editing.description ?? ""}
          onChange={(v) => setEditing({ ...editing, description: v })}
        />
        <TextArea
          label="Instructions (loaded when the agent uses this skill)"
          value={editing.content ?? ""}
          onChange={(v) => setEditing({ ...editing, content: v })}
          rows={10}
        />
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label="Save skill"
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                await api.saveSkill({
                  name: (editing.name ?? "").trim(),
                  description: editing.description ?? "",
                  content: editing.content ?? "",
                  enabled: editing.enabled ?? true,
                });
                setEditing(null);
                await reload();
              })
            }
          />
          <Button label="Cancel" variant="ghost" clickAction={() => setEditing(null)} />
        </div>
      </div>
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Skills teach Claw reusable procedures. Enabled skills appear in its context.
        </Text>
        <Button
          label="New skill"
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          clickAction={() => setEditing({ enabled: true })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {skills.length === 0 ? (
        <EmptyState title="No skills yet" description="Create a skill to teach Claw a procedure." />
      ) : (
        skills.map((skill) => (
          <Card key={skill.id} padding={2}>
            <div className="claw-row claw-row-between">
              <div>
                <Text weight="semibold">{skill.name}</Text>
                <Text size="sm" color="secondary" as="p">
                  {skill.description || "—"}
                </Text>
              </div>
              <div className="claw-row">
                <Switch
                  value={skill.enabled}
                  label={`Enable ${skill.name}`}
                  isLabelHidden
                  changeAction={(checked) =>
                    guard(async () => {
                      await api.saveSkill({ ...skill, enabled: checked });
                      await reload();
                    })
                  }
                />
                <Button
                  label="Edit"
                  icon={<Icon icon={Pencil} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() => setEditing(skill)}
                />
                <Button
                  label="Delete"
                  icon={<Icon icon={Trash2} size="sm" />}
                  size="sm"
                  variant="destructive"
                  clickAction={() =>
                    guard(async () => {
                      await api.deleteSkill(skill.id);
                      await reload();
                    })
                  }
                />
              </div>
            </div>
          </Card>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Memory

function MemoryPanel() {
  const [memory, setMemory] = useState<MemoryInfo | null>(null);
  const [draft, setDraft] = useState("");
  const [saved, setSaved] = useState(false);
  const { error, guard } = useAsyncError();

  useEffect(() => {
    api.getMemory().then((m) => {
      setMemory(m);
      setDraft(m.core);
    });
  }, []);

  if (!memory) return <Text color="secondary">Loading…</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">
        Core memory is included in every conversation. Edit it to correct or remove what Claw
        remembers about you.
      </Text>
      <TextArea
        label="Core memory"
        value={draft}
        onChange={(v) => {
          setDraft(v);
          setSaved(false);
        }}
        rows={10}
      />
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row">
        <Button
          label="Save memory"
          icon={<Icon icon="check" size="sm" />}
          clickAction={() =>
            guard(async () => {
              await api.saveMemory(draft);
              setSaved(true);
            })
          }
        />
        {saved && <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="Saved" />}
      </div>
      <Divider />
      <Text weight="semibold">Recent consolidation history</Text>
      {memory.history.length === 0 ? (
        <Text color="secondary" size="sm">
          Nothing consolidated yet — history entries appear as conversations grow.
        </Text>
      ) : (
        memory.history
          .slice()
          .reverse()
          .map((entry, i) => (
            <Card key={i} padding={2} variant="muted">
              <Text size="sm">{entry}</Text>
            </Card>
          ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Connectors

function ConnectorsPanel() {
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [editing, setEditing] = useState<Partial<ConnectorInfo> | null>(null);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.listConnectors().then(setConnectors), []);
  useEffect(() => {
    void reload();
    api.connectorPresets().then(setPresets).catch(() => setPresets([]));
  }, [reload]);

  // Prefill the editor from a preset: command/transport filled, env keys blank
  // for the user to paste their own secrets.
  const fromPreset = (p: ConnectorPreset) =>
    setEditing({
      name: p.name,
      transport: p.transport,
      command: p.command,
      url: p.url,
      env: Object.fromEntries(p.env_fields.map((k) => [k, ""])),
      enabled: true,
    });

  if (editing) {
    const transport = editing.transport ?? "stdio";
    return (
      <div className="claw-panel">
        <TextInput
          label="Name (lowercase, e.g. github)"
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
          isDisabled={!!editing.id}
        />
        <div className="claw-row">
          <Button
            label="stdio (local command)"
            size="sm"
            variant={transport === "stdio" ? "primary" : "secondary"}
            clickAction={() => setEditing({ ...editing, transport: "stdio" })}
          />
          <Button
            label="HTTP (remote server)"
            size="sm"
            variant={transport === "http" ? "primary" : "secondary"}
            clickAction={() => setEditing({ ...editing, transport: "http" })}
          />
        </div>
        {transport === "stdio" ? (
          <TextInput
            label="Command (e.g. npx -y @modelcontextprotocol/server-github)"
            value={editing.command ?? ""}
            onChange={(v) => setEditing({ ...editing, command: v })}
          />
        ) : (
          <TextInput
            label="Server URL (e.g. https://mcp.example.com/mcp)"
            value={editing.url ?? ""}
            onChange={(v) => setEditing({ ...editing, url: v })}
          />
        )}
        <TextArea
          label="Environment variables (KEY=value, one per line)"
          value={Object.entries(editing.env ?? {})
            .map(([k, v]) => `${k}=${v}`)
            .join("\n")}
          onChange={(v) =>
            setEditing({
              ...editing,
              env: Object.fromEntries(
                v
                  .split("\n")
                  .map((line) => line.split(/=(.*)/s))
                  .filter((kv) => kv[0]?.trim())
                  .map((kv) => [kv[0].trim(), (kv[1] ?? "").trim()]),
              ),
            })
          }
          rows={3}
        />
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label="Save connector"
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                await api.saveConnector({
                  name: (editing.name ?? "").trim(),
                  transport,
                  command: editing.command ?? "",
                  url: editing.url ?? "",
                  env: editing.env ?? {},
                  enabled: editing.enabled ?? true,
                });
                setEditing(null);
                await reload();
              })
            }
          />
          <Button label="Cancel" variant="ghost" clickAction={() => setEditing(null)} />
        </div>
      </div>
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Connectors add MCP tools (Gmail, GitHub, Notion, …) to your agent.
        </Text>
        <Button
          label="Add connector"
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          clickAction={() => setEditing({ transport: "stdio", enabled: true })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {presets.length > 0 && (
        <div className="claw-panel" style={{ gap: 6 }}>
          <Text size="sm" color="secondary">Add from a preset (you supply your own keys):</Text>
          <div className="claw-row">
            {presets.map((p) => (
              <Button
                key={p.key}
                label={p.label}
                size="sm"
                variant="secondary"
                clickAction={() => fromPreset(p)}
              />
            ))}
          </div>
        </div>
      )}
      {connectors.length === 0 ? (
        <EmptyState
          title="No connectors"
          description="Add an MCP server to give Claw access to your apps."
        />
      ) : (
        connectors.map((c) => (
          <Card key={c.id} padding={2}>
            <div className="claw-row claw-row-between">
              <div>
                <div className="claw-row">
                  <Text weight="semibold">{c.name}</Text>
                  <Badge variant="neutral" label={c.transport} />
                  {c.runtime.status === "connected" && (
                    <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={`${c.runtime.tools} tools`} />
                  )}
                  {c.runtime.status === "error" && (
                    <Badge variant="error" icon={<Icon icon="error" size="xsm" />} label="error" />
                  )}
                </div>
                <Text size="sm" color="secondary" as="p">
                  {c.transport === "stdio" ? c.command : c.url}
                </Text>
                {c.runtime.error && (
                  <Text size="sm" color="secondary" as="p">
                    {c.runtime.error}
                  </Text>
                )}
              </div>
              <div className="claw-row">
                <Switch
                  value={c.enabled}
                  label={`Enable ${c.name}`}
                  isLabelHidden
                  changeAction={(checked) =>
                    guard(async () => {
                      await api.saveConnector({ ...c, enabled: checked });
                      await reload();
                    })
                  }
                />
                <Button
                  label="Edit"
                  icon={<Icon icon={Pencil} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() => setEditing(c)}
                />
                <Button
                  label="Delete"
                  icon={<Icon icon={Trash2} size="sm" />}
                  size="sm"
                  variant="destructive"
                  clickAction={() =>
                    guard(async () => {
                      await api.deleteConnector(c.id);
                      await reload();
                    })
                  }
                />
              </div>
            </div>
          </Card>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Schedules

function SchedulesPanel() {
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([]);
  const [editing, setEditing] = useState<Partial<ScheduleInfo> | null>(null);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.listSchedules().then(setSchedules), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  if (editing) {
    return (
      <div className="claw-panel">
        <TextInput
          label="Name"
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
        />
        <TextArea
          label="Prompt to send to Claw"
          value={editing.prompt ?? ""}
          onChange={(v) => setEditing({ ...editing, prompt: v })}
          rows={4}
        />
        <TextInput
          label="Cron expression (e.g. 0 9 * * * = daily 09:00) — leave empty to use interval"
          value={editing.cron ?? ""}
          onChange={(v) => setEditing({ ...editing, cron: v })}
        />
        <TextInput
          label="Interval minutes (used when cron is empty; 0 = run once now)"
          value={String(Math.round((editing.interval_seconds ?? 0) / 60))}
          onChange={(v) =>
            setEditing({ ...editing, interval_seconds: Math.max(0, parseInt(v) || 0) * 60 })
          }
        />
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label="Save schedule"
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                const body = {
                  name: (editing.name ?? "").trim(),
                  prompt: editing.prompt ?? "",
                  cron: editing.cron ?? "",
                  interval_seconds: editing.interval_seconds ?? 0,
                  enabled: editing.enabled ?? true,
                  ...(editing.cron || editing.interval_seconds
                    ? {}
                    : { run_at: new Date().toISOString() }),
                };
                if (editing.id) await api.updateSchedule(editing.id, body);
                else await api.createSchedule(body);
                setEditing(null);
                await reload();
              })
            }
          />
          <Button label="Cancel" variant="ghost" clickAction={() => setEditing(null)} />
        </div>
      </div>
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Scheduled prompts run automatically — results appear as new chats.
        </Text>
        <Button
          label="New schedule"
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          clickAction={() => setEditing({ enabled: true, interval_seconds: 0 })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {schedules.length === 0 ? (
        <EmptyState
          title="No schedules"
          description="Create one, e.g. “Summarize my tasks” every morning at 9:00."
        />
      ) : (
        schedules.map((s) => (
          <Card key={s.id} padding={2}>
            <div className="claw-row claw-row-between">
              <div>
                <div className="claw-row">
                  <Text weight="semibold">{s.name}</Text>
                  <Badge
                    variant="neutral"
                    label={
                      s.cron ||
                      (s.interval_seconds ? `every ${Math.round(s.interval_seconds / 60)}m` : "once")
                    }
                  />
                  {s.last_status && (
                    <Badge
                      variant={s.last_status.startsWith("ok") ? "success" : "error"}
                      icon={<Icon icon={s.last_status.startsWith("ok") ? "check" : "error"} size="xsm" />}
                      label={s.last_status.slice(0, 24)}
                    />
                  )}
                </div>
                <Text size="sm" color="secondary" as="p">
                  {s.prompt.slice(0, 90)}
                  {s.next_run_at ? ` · next: ${new Date(s.next_run_at).toLocaleString()}` : ""}
                </Text>
              </div>
              <div className="claw-row">
                <Switch
                  value={s.enabled}
                  label={`Enable ${s.name}`}
                  isLabelHidden
                  changeAction={(checked) =>
                    guard(async () => {
                      await api.updateSchedule(s.id, { ...s, enabled: checked });
                      await reload();
                    })
                  }
                />
                <Button
                  label="Run now"
                  icon={<Icon icon={Play} size="sm" />}
                  size="sm"
                  variant="secondary"
                  clickAction={() =>
                    guard(async () => {
                      await api.runScheduleNow(s.id);
                      await reload();
                    })
                  }
                />
                <Button
                  label="Edit"
                  icon={<Icon icon={Pencil} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() => setEditing(s)}
                />
                <Button
                  label="Delete"
                  icon={<Icon icon={Trash2} size="sm" />}
                  size="sm"
                  variant="destructive"
                  clickAction={() =>
                    guard(async () => {
                      await api.deleteSchedule(s.id);
                      await reload();
                    })
                  }
                />
              </div>
            </div>
          </Card>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Heartbeat

const HEARTBEAT_PRESETS = [
  { label: "Off", minutes: 0 },
  { label: "Every 30 min", minutes: 30 },
  { label: "Hourly", minutes: 60 },
  { label: "Every 4 hours", minutes: 240 },
  { label: "Daily", minutes: 1440 },
];

function HeartbeatPanel() {
  const [state, setState] = useState<{ interval_minutes: number; enabled: boolean; next_run_at: string | null } | null>(
    null,
  );
  const { error, guard } = useAsyncError();

  useEffect(() => {
    api.getHeartbeat().then(setState);
  }, []);

  if (!state) return <Text color="secondary">Loading…</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">
        When enabled, Claw periodically reviews your memory and reaches out proactively only if
        something is genuinely worth raising (a reminder or follow-up). Results appear as new chats.
      </Text>
      <div className="claw-row">
        {HEARTBEAT_PRESETS.map((p) => (
          <Button
            key={p.minutes}
            label={p.label}
            size="sm"
            variant={state.interval_minutes === p.minutes ? "primary" : "secondary"}
            clickAction={() =>
              guard(async () => {
                setState(await api.setHeartbeat(p.minutes));
              })
            }
          />
        ))}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row">
        {state.enabled ? (
          <Badge
            variant="success"
            icon={<Icon icon={HeartPulse} size="xsm" />}
            label={`On · every ${state.interval_minutes} min`}
          />
        ) : (
          <Badge variant="neutral" label="Off" />
        )}
        {state.next_run_at && (
          <Text size="sm" color="secondary">
            Next check-in: {new Date(state.next_run_at).toLocaleString()}
          </Text>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Telegram

function TelegramPanel() {
  const [status, setStatus] = useState<{ enabled: boolean; linked: boolean; bot_username: string } | null>(
    null,
  );
  const [code, setCode] = useState("");
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.getTelegramStatus().then(setStatus), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  if (!status) return <Text color="secondary">Loading…</Text>;

  if (!status.enabled) {
    return (
      <div className="claw-panel">
        <Text color="secondary">
          Telegram is not enabled on this server. An administrator must set a bot token
          (CLAW_TELEGRAM_BOT_TOKEN) to allow linking.
        </Text>
      </div>
    );
  }

  const botHandle = status.bot_username ? `@${status.bot_username}` : "the bot";

  return (
    <div className="claw-panel">
      <Text color="secondary">
        Link your Telegram account so you can chat with Claw from Telegram using the same memory,
        skills, and history as here.
      </Text>
      <div className="claw-row">
        {status.linked ? (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="Linked" />
        ) : (
          <Badge variant="neutral" label="Not linked" />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {status.linked ? (
        <Button
          label="Unlink Telegram"
          icon={<Icon icon={Trash2} size="sm" />}
          variant="destructive"
          clickAction={() =>
            guard(async () => {
              await api.unlinkTelegram();
              setCode("");
              await reload();
            })
          }
        />
      ) : (
        <>
          <Button
            label="Generate link code"
            icon={<Icon icon={Send} size="sm" />}
            clickAction={() =>
              guard(async () => {
                const res = await api.createTelegramLink();
                setCode(res.code);
              })
            }
          />
          {code && (
            <Card padding={2} variant="muted">
              <Text weight="semibold">Your link code</Text>
              <Text type="display-3">{code}</Text>
              <Text size="sm" color="secondary" as="p">
                Open {botHandle} in Telegram and send:
              </Text>
              <Text type="code">/link {code}</Text>
              <Text size="sm" color="secondary" as="p">
                The code expires in a few minutes. After linking, refresh this tab.
              </Text>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
