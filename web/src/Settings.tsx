import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon, type IconName, type IconType } from "@astryxdesign/core/Icon";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import {
  Brain,
  Puzzle,
  ChevronDown,
  ChevronRight,
  Copy,
  Cpu,
  Download,
  ExternalLink,
  Eye,
  FileSpreadsheet,
  FileText,
  FileType,
  Globe,
  HeartPulse,
  Library,
  LineChart,
  Link as LinkIcon,
  Lock,
  Maximize2,
  Pencil,
  Play,
  Plug,
  Presentation,
  Plus,
  Scale,
  Send,
  Sparkles,
  Trash2,
  Upload,
  User as UserIcon,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ProvidersPanel } from "./Admin";
import { useBranding } from "./branding";
import { ErrorText } from "./ErrorText";
import { PasswordField } from "./PasswordField";
import {
  AuthUser,
  BrandingChatBackground,
  BrandingFontSize,
  BrandingLanguage,
  ConnectorInfo,
  ConnectorPreset,
  KnowledgeBase,
  KnowledgeDoc,
  MemoryInfo,
  ScheduleInfo,
  SimpleGroup,
  SkillInfo,
  USER_LLM_API,
  api,
} from "./api";
import { MOBILE_QUERY, useMediaQuery } from "./useMediaQuery";

export type SettingsSection =
  | "profile"
  | "skills"
  | "knowledge"
  | "memory"
  | "models"
  | "connectors"
  | "schedules"
  | "heartbeat"
  | "telegram"
  | "browser-extension";

export const SETTINGS_SECTIONS: { key: SettingsSection; label: string; icon: IconType | IconName }[] = [
  { key: "profile", label: "Profile", icon: UserIcon },
  { key: "skills", label: "Skills", icon: Sparkles },
  { key: "knowledge", label: "Knowledge", icon: Library },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "models", label: "My Models", icon: Cpu },
  { key: "connectors", label: "Connectors", icon: Plug },
  { key: "schedules", label: "Schedule", icon: "calendar" },
  { key: "heartbeat", label: "Heartbeat", icon: HeartPulse },
  { key: "telegram", label: "Telegram", icon: Send },
  { key: "browser-extension", label: "Browser extension", icon: Puzzle },
];

export function SettingsPanel({ section }: { section: SettingsSection }) {
  const meta = SETTINGS_SECTIONS.find((s) => s.key === section);
  // My Models is a data table, same reasoning as the admin LLM Providers page
  // (see AdminPanel) — give it the wider column instead of the shared 720px
  // prose width.
  const isWide = section === "models";
  return (
    <div className="claw-settings-panel">
      <div className={`claw-settings-panel-header${isWide ? " claw-panel-wide" : ""}`}>
        <Icon icon={meta?.icon ?? "check"} size="lg" color="secondary" />
        <Text type="display-3">{meta?.label}</Text>
      </div>
      <div className={`claw-panel${isWide ? " claw-panel-wide" : ""}`}>
        {section === "profile" && <ProfilePanel />}
        {section === "skills" && <SkillsPanel />}
        {section === "knowledge" && <KnowledgePanel />}
        {section === "memory" && <MemoryPanel />}
        {section === "models" && <ProvidersPanel llmApi={USER_LLM_API} scope="user" />}
        {section === "connectors" && <ConnectorsPanel />}
        {section === "schedules" && <SchedulesPanel />}
        {section === "heartbeat" && <HeartbeatPanel />}
        {section === "telegram" && <TelegramPanel />}
        {section === "browser-extension" && <BrowserExtensionPanel />}
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

// ---------------------------------------------------------------- Profile

function ProfilePanel() {
  const [me, setMe] = useState<AuthUser | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const { error, guard } = useAsyncError();
  const toast = useToast();

  const reload = useCallback(() => api.me().then(setMe), []);
  useEffect(() => {
    void guard(async () => await reload());
  }, [guard, reload]);

  const submit = async () => {
    setBusy(true);
    setFormError("");
    try {
      await api.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      toast({ body: "Password updated", type: "info", autoHideDuration: 2500 });
    } catch (e) {
      // Inline, not an early-return replacing the whole panel — a wrong
      // current password shouldn't wipe out the form the user is mid-typing.
      setFormError(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!me) return <Text color="secondary">Loading…</Text>;

  return (
    <div className="claw-panel">
      <Card padding={2}>
        <div className="claw-panel">
          <Text weight="semibold">{me.display_name || me.email}</Text>
          <Text size="sm" color="secondary">
            {me.email}
          </Text>
        </div>
      </Card>
      <PreferencesCard me={me} onSaved={setMe} />
      <Card padding={2}>
        <div className="claw-panel">
          <Text weight="semibold">Change password</Text>
          {!me.has_password ? (
            <Text size="sm" color="secondary">
              This account doesn't use a password (signed in via Google/Microsoft, or hasn't finished
              activation yet).
            </Text>
          ) : (
            <>
              <PasswordField label="Current password" value={currentPassword} onChange={setCurrentPassword} />
              <PasswordField
                label="New password"
                description="At least 8 characters."
                value={newPassword}
                onChange={setNewPassword}
              />
              {formError && <ErrorText>{formError}</ErrorText>}
              <div className="claw-row">
                <Button
                  label={busy ? "…" : "Update password"}
                  variant="primary"
                  icon={<Icon icon="check" size="sm" />}
                  isDisabled={busy || !currentPassword || newPassword.length < 8}
                  clickAction={submit}
                />
              </div>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}

/** Settings > Profile > Preferences — a personal override of the Control
 * Plane's global branding defaults (Admin.tsx's PreferencesPanel). Unset
 * fields (me.language/font_size/chat_background === null) show whichever
 * value is currently in effect (the global default), same as the admin
 * panel's own fields — saving only sends the field(s) actually changed, so
 * the other two stay whatever they already were (override or inherited). */
function PreferencesCard({ me, onSaved }: { me: AuthUser; onSaved: (user: AuthUser) => void }) {
  const { branding, setUserOverride } = useBranding();
  const [language, setLanguage] = useState<BrandingLanguage>(me.language ?? branding.language);
  const [fontSize, setFontSize] = useState<BrandingFontSize>(me.font_size ?? branding.font_size);
  const [chatBg, setChatBg] = useState<BrandingChatBackground>(me.chat_background ?? branding.chat_background);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const toast = useToast();

  const dirtyLanguage = language !== (me.language ?? branding.language);
  const dirtyFontSize = fontSize !== (me.font_size ?? branding.font_size);
  const dirtyChatBg = chatBg !== (me.chat_background ?? branding.chat_background);
  const dirty = dirtyLanguage || dirtyFontSize || dirtyChatBg;

  const save = async () => {
    setSaving(true);
    setSaveError("");
    try {
      // Only send the field(s) actually changed — the backend leaves
      // omitted fields' stored overrides untouched, so sending all three
      // unconditionally would silently pin the other two forever.
      const updated = await api.updateMyPreferences({
        ...(dirtyLanguage ? { language } : {}),
        ...(dirtyFontSize ? { font_size: fontSize } : {}),
        ...(dirtyChatBg ? { chat_background: chatBg } : {}),
      });
      onSaved(updated);
      setUserOverride({ language: updated.language, font_size: updated.font_size, chat_background: updated.chat_background });
      toast({ body: "Preferences saved", type: "info", autoHideDuration: 2500 });
    } catch (e) {
      setSaveError(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card padding={2}>
      <div className="claw-panel">
        <div>
          <Text weight="semibold">Language</Text>
          <Text size="sm" color="secondary">
            Applies to key interface surfaces and the AI's replies. English is the default.
          </Text>
          <SegmentedControl value={language} onChange={(v) => setLanguage(v as BrandingLanguage)} label="Language">
            <SegmentedControlItem value="en" label="English" />
            <SegmentedControlItem value="th" label="ไทย (Thai)" />
          </SegmentedControl>
        </div>
        <div>
          <Text weight="semibold">Font size</Text>
          <SegmentedControl value={fontSize} onChange={(v) => setFontSize(v as BrandingFontSize)} label="Font size">
            <SegmentedControlItem value="small" label="Small" />
            <SegmentedControlItem value="medium" label="Medium" />
            <SegmentedControlItem value="large" label="Large" />
          </SegmentedControl>
        </div>
        <div>
          <Text weight="semibold">Chat background</Text>
          <Text size="sm" color="secondary">
            Solid, Dots, or Grid — a subtle brand accent glow is always present in the corner.
          </Text>
          <SegmentedControl value={chatBg} onChange={(v) => setChatBg(v as BrandingChatBackground)} label="Chat background">
            <SegmentedControlItem value="solid" label="Solid" />
            <SegmentedControlItem value="dots" label="Dots" />
            <SegmentedControlItem value="grid" label="Grid" />
          </SegmentedControl>
        </div>
        {saveError && <ErrorText>{saveError}</ErrorText>}
        <div>
          <Button label="Save preferences" isDisabled={!dirty || saving} clickAction={save} />
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- Skills

const BUILTIN_SKILL_ICONS: Record<string, IconType> = {
  pptx: Presentation,
  xlsx: FileSpreadsheet,
  pdf: FileType,
  docx: FileText,
  "legal-risk-assessment": Scale,
  "financial-statement-analyzer": LineChart,
};

function builtinSkillIcon(name: string): IconType {
  return BUILTIN_SKILL_ICONS[name] ?? Sparkles;
}

function SkillDetailModal({
  skill,
  isOpen,
  onOpenChange,
}: {
  skill: SkillInfo | null;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const capabilities = skill?.capabilities ?? [];
  return (
    // Kept mounted always, with only `isOpen` toggling — Dialog's own close
    // effect (dialog.close() + returning focus to the trigger) runs on an
    // isOpen transition while mounted, but never fires if we unmount it
    // instead (see the KB-doc-preview Dialog elsewhere in this file for the
    // same pattern).
    <Dialog isOpen={isOpen} onOpenChange={onOpenChange} width={640} purpose="info">
      <Layout
        header={
          <DialogHeader
            title={skill?.name ?? ""}
            subtitle="Skill"
            startContent={<Icon icon={builtinSkillIcon(skill?.name ?? "")} size="md" />}
            onOpenChange={onOpenChange}
          />
        }
        content={
          <LayoutContent>
            <div className="claw-panel">
              {capabilities.length > 0 && (
                <>
                  <Text size="sm" color="secondary" weight="semibold" className="claw-skill-capability-label">
                    Capabilities covered
                  </Text>
                  <div className="claw-skill-capability-grid">
                    {capabilities.map((cap) => (
                      <Card key={cap.title} padding={2}>
                        <Text weight="semibold">{cap.title}</Text>
                        <Text size="sm" color="secondary" as="p">
                          {cap.description}
                        </Text>
                      </Card>
                    ))}
                  </div>
                </>
              )}
              {skill?.summary && (
                <div className="claw-skill-summary-box">
                  <Text size="sm" color="secondary" as="p">
                    {skill.summary}
                  </Text>
                </div>
              )}
              <TextArea
                label="Instructions (loaded when the agent uses this skill)"
                value={skill?.content ?? ""}
                onChange={() => {}}
                rows={14}
                isDisabled
              />
            </div>
          </LayoutContent>
        }
        footer={
          <LayoutFooter hasDivider>
            <Button label="Close" variant="ghost" clickAction={() => onOpenChange(false)} />
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

function SkillsPanel() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [editing, setEditing] = useState<Partial<SkillInfo> | null>(null);
  const [viewingDetail, setViewingDetail] = useState<SkillInfo | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.listSkills().then(setSkills), []);
  useEffect(() => {
    void reload();
    void api.listConnectors().then(setConnectors);
  }, [reload]);

  if (editing) {
    const readOnly = !!editing.builtin;
    return (
      <div className="claw-panel">
        {readOnly && (
          <Text size="sm" color="secondary">
            This is a built-in skill — view only. Use it as a reference for authoring your own.
          </Text>
        )}
        <TextInput
          label="Name"
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
          isDisabled={readOnly || !!editing.id}
        />
        <TextInput
          label="Description (shown to the agent in every chat)"
          value={editing.description ?? ""}
          onChange={(v) => setEditing({ ...editing, description: v })}
          isDisabled={readOnly}
        />
        <TextArea
          label="Instructions (loaded when the agent uses this skill)"
          value={editing.content ?? ""}
          onChange={(v) => setEditing({ ...editing, content: v })}
          rows={10}
          isDisabled={readOnly}
        />
        {!readOnly && connectors.length > 0 && (
          <div className="claw-field-group">
            <Text size="sm" color="secondary">
              Linked connector (optional)
            </Text>
            <Text size="sm" color="secondary" as="p">
              If this skill calls a connector's tools, link it here instead of writing the tool's
              exact name in the instructions above — Claw resolves the current tool names for you,
              so it keeps working even if the connector is later renamed.
            </Text>
            <div className="claw-row">
              <Button
                label="None"
                size="sm"
                variant={!editing.connector_id ? "primary" : "secondary"}
                clickAction={() => setEditing({ ...editing, connector_id: null })}
              />
              {connectors.map((c) => (
                <Button
                  key={c.id}
                  label={c.name}
                  size="sm"
                  variant={editing.connector_id === c.id ? "primary" : "secondary"}
                  clickAction={() => setEditing({ ...editing, connector_id: c.id })}
                />
              ))}
            </div>
          </div>
        )}
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          {!readOnly && (
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
                    connector_id: editing.connector_id ?? null,
                  });
                  setEditing(null);
                  await reload();
                })
              }
            />
          )}
          <Button label={readOnly ? "Back" : "Cancel"} variant="ghost" clickAction={() => setEditing(null)} />
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
                <div className="claw-row">
                  <Text weight="semibold">{skill.name}</Text>
                  {skill.builtin && <Badge variant="info" label="Built-in" />}
                </div>
                <Text size="sm" color="secondary" as="p">
                  {skill.description || "—"}
                </Text>
                {skill.shadows_builtin && (
                  <Text size="sm" color="secondary" as="p">
                    This name matches a built-in skill — yours takes priority, and the built-in is hidden.
                  </Text>
                )}
              </div>
              {skill.builtin ? (
                <Button
                  label="View"
                  icon={<Icon icon={ExternalLink} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() => {
                    if (skill.capabilities?.length) {
                      setViewingDetail(skill);
                      setDetailOpen(true);
                    } else {
                      setEditing(skill);
                    }
                  }}
                />
              ) : (
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
              )}
            </div>
          </Card>
        ))
      )}
      <SkillDetailModal skill={viewingDetail} isOpen={detailOpen} onOpenChange={setDetailOpen} />
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

// Brand logos carried over from the original softnix-agenticclaw project
// (nanobot/admin/static/app.js CONNECTOR_IMAGE_ASSET_MAP), copied into
// public/connectors/. Keyed by the preset `key` from the backend catalog;
// falls back to a neutral plug icon when a preset has no logo asset.
const PRESET_LOGO: Record<string, string> = {
  github: "/connectors/github.png",
  gmail: "/connectors/gmail.png",
  outlook: "/connectors/outlook.png",
  "outlook-calendar": "/connectors/outlook-calendar.png",
  onedrive: "/connectors/onedrive.png",
  notion: "/connectors/notion.png",
  tavily: "/connectors/tavily.png",
  composio: "/connectors/composio.png",
  "softnix-one": "/connectors/softnix-one.png",
};

// A short auth-method chip on the catalog card, so users know what setup to
// expect (one-click sign-in vs. pasting a key) before they open the form.
function presetAuthHint(p: ConnectorPreset): string | null {
  if (p.setup === "oauth") return "1-click sign-in";
  if (p.setup === "api_key" || p.setup === "token") return "API key";
  return null;
}

function ConnectorBrandTile({ presetKey }: { presetKey: string }) {
  const logo = PRESET_LOGO[presetKey];
  return (
    <div className="claw-connector-tile">
      {logo ? (
        <img src={logo} alt="" aria-hidden="true" />
      ) : (
        <Icon icon={Plug} size="md" color="secondary" />
      )}
    </div>
  );
}

const OAUTH_PROVIDER_LABEL: Record<string, string> = { google: "Google", microsoft: "Microsoft" };

/** Friendly, no-jargon setup for a preset connector. Renders a one-click OAuth
 * panel, or a small labeled-fields form for API-key/token connectors — the raw
 * MCP editor is never shown here. */
function GuidedSetup({
  preset,
  installed,
  onCancel,
  onSaved,
  onManage,
}: {
  preset: ConnectorPreset;
  installed?: ConnectorInfo;
  onCancel: () => void;
  onSaved: () => Promise<void>;
  onManage: (c: ConnectorInfo) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [url, setUrl] = useState(preset.url);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const header = (
    <div className="claw-setup-header">
      <ConnectorBrandTile presetKey={preset.key} />
      <div>
        <Text type="display-3">{preset.label}</Text>
        <Text color="secondary" as="p">
          {preset.description}
        </Text>
      </div>
    </div>
  );

  // ---- OAuth: one-click connect ----
  if (preset.setup === "oauth") {
    const provider = OAUTH_PROVIDER_LABEL[preset.oauth_provider] ?? preset.oauth_provider;
    const connect = async () => {
      setBusy(true);
      setError("");
      try {
        const { url } = await api.connectorOAuthStart(preset.key);
        window.location.href = url;
      } catch (e) {
        const msg = String(e);
        setBusy(false);
        setError(
          /not_configured/.test(msg)
            ? `${provider} sign-in isn't set up yet. Ask your administrator to enable it in the Control Plane.`
            : "Couldn't start sign-in. Please try again.",
        );
      }
    };
    return (
      <div className="claw-panel claw-setup">
        {header}
        {installed?.runtime.status === "connected" && (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="Connected" />
        )}
        <Card padding={3} variant="muted">
          <Text weight="semibold">Sign in with {provider}</Text>
          <Text size="sm" color="secondary" as="p">
            You'll be redirected to {provider} to grant access. Nothing is stored except a secure token,
            encrypted at rest.
          </Text>
        </Card>
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label={busy ? "Redirecting…" : installed ? `Reconnect with ${provider}` : `Connect with ${provider}`}
            icon={<Icon icon={LinkIcon} size="sm" />}
            isDisabled={busy}
            clickAction={connect}
          />
          {installed && (
            <Button label="Manage" variant="secondary" clickAction={() => onManage(installed)} />
          )}
          <Button label="Cancel" variant="ghost" clickAction={onCancel} />
        </div>
      </div>
    );
  }

  // ---- API key / token: labeled fields ----
  const save = async () => {
    setError("");
    setBusy(true);
    try {
      const env: Record<string, string> = {};
      for (const f of preset.fields) {
        const raw = (values[f.key] ?? "").trim();
        if (!raw) {
          if (!f.optional) throw new Error(`${f.label} is required.`);
          continue;
        }
        env[f.key] = f.prefix && !raw.startsWith(f.prefix) ? f.prefix + raw : raw;
      }
      const effectiveUrl = preset.url_configurable ? url.trim() || preset.url : preset.url;
      if (preset.url_configurable && !effectiveUrl) throw new Error("MCP endpoint URL is required.");
      await api.saveConnector({
        name: preset.name,
        description: preset.description ?? "",
        transport: preset.transport,
        command: preset.command,
        url: effectiveUrl,
        env,
        timeout_ms: null,
        enabled: true,
      });
      await onSaved();
    } catch (e) {
      setError(String(e).replace(/^Error:\s*/, ""));
      setBusy(false);
    }
  };

  return (
    <div className="claw-panel claw-setup">
      {header}
      {preset.url_configurable && (
        <div className="claw-setup-field">
          <TextInput
            label="MCP endpoint URL"
            type="text"
            value={url}
            placeholder={preset.url}
            onChange={setUrl}
          />
          <Text size="sm" color="secondary" as="p" className="claw-setup-help">
            Defaults to Softnix's hosted endpoint. Change this only if your organization runs its
            own Softnix ONE instance.
          </Text>
        </div>
      )}
      {preset.fields.map((f) => (
        <div key={f.key} className="claw-setup-field">
          <TextInput
            label={f.optional ? `${f.label} (optional)` : f.label}
            type={f.secret ? "password" : "text"}
            value={values[f.key] ?? ""}
            placeholder={f.placeholder}
            onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
          />
          {f.help && (
            <Text size="sm" color="secondary" as="p" className="claw-setup-help">
              {f.help}
            </Text>
          )}
        </div>
      ))}
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row">
        <Button
          label={busy ? "Saving…" : "Add connector"}
          icon={<Icon icon="check" size="sm" />}
          isDisabled={busy}
          clickAction={save}
        />
        <Button label="Cancel" variant="ghost" clickAction={onCancel} />
      </div>
    </div>
  );
}

// Env keys with this prefix become HTTP headers server-side (see
// _HEADER_ENV_PREFIX in claw/core/connectors.py) — accepting the raw
// "Header-Name: value" shorthand here (in addition to "KEY=value") means a
// header line pasted straight from docs/curl no longer silently becomes a
// single malformed env key with an empty value (no "=" to split on) that
// then fails to match the HEADER_ prefix and so is never actually sent.
const HEADER_ENV_PREFIX = "HEADER_";

function parseEnvText(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const eq = line.indexOf("=");
    // Colon WITHOUT requiring a following space, so "Name:value" (no space,
    // e.g. copied from a curl one-liner) is recognized too, not just
    // "Name: value".
    const colon = line.indexOf(":");
    if (eq !== -1 && (colon === -1 || eq < colon)) {
      // KEY=value — everything after the first "=" is the value verbatim,
      // so a value that itself contains "=" or ":" (e.g. a JWT/base64
      // token, a URL) round-trips correctly.
      const key = line.slice(0, eq).trim();
      if (key) env[key] = line.slice(eq + 1).trim();
    } else if (colon !== -1) {
      // "Header-Name: value" (or "Header-Name:value") shorthand for an HTTP
      // header — becomes HEADER_<Header-Name> internally (see
      // _HEADER_ENV_PREFIX in claw/core/connectors.py).
      const key = line.slice(0, colon).trim();
      const rest = line.slice(colon + 1);
      const value = (rest.startsWith(" ") ? rest.slice(1) : rest).trim();
      if (key) env[`${HEADER_ENV_PREFIX}${key}`] = value;
    } else {
      // No "=" or ":" at all (e.g. a bare flag like "DEBUG") — keep the
      // whole line as a key with an empty value rather than silently
      // discarding user input.
      env[line] = "";
    }
  }
  return env;
}

function formatEnvText(env: Record<string, string> | undefined): string {
  return Object.entries(env ?? {})
    .map(([k, v]) => {
      if (k.startsWith(HEADER_ENV_PREFIX)) {
        // Always keep the colon, even for an empty value — dropping it for
        // "v is falsy" would render a bare name indistinguishable from a
        // plain env var, losing the HEADER_ prefix on the next parse.
        const name = k.slice(HEADER_ENV_PREFIX.length);
        return v ? `${name}: ${v}` : `${name}:`;
      }
      // Omit the trailing "=" for an empty value: re-parsing a bare key
      // (the "else" branch above) round-trips to the same {key: ""} entry,
      // and — critically — it also means a pre-existing malformed key that
      // itself contains ": " (e.g. the historical "Authorization: Bearer
      // <token>" -> "" shape a missing "=" used to produce) is rendered
      // as-is instead of gaining a spurious trailing "=" that would then
      // get misread as part of the token value on the next save.
      return v ? `${k}=${v}` : k;
    })
    .join("\n");
}

// The exact mcp_{connector}_{tool} names a skill must reference — collapsed
// by default since a connector like an all-in-one CRM can expose 80+ tools,
// which would otherwise dwarf the rest of the connector card.
function ConnectorToolNames({ names }: { names: string[] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="claw-connector-tool-names">
      <button
        type="button"
        className="claw-connector-tool-names-toggle"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <Icon icon={expanded ? ChevronDown : ChevronRight} size="xsm" color="secondary" />
        <Text size="sm" color="secondary">
          {names.length} tool name{names.length === 1 ? "" : "s"} to reference in a skill
        </Text>
      </button>
      {expanded && (
        <Text size="sm" color="secondary" as="p" className="claw-connector-tool-names-list">
          {names.join(", ")}
        </Text>
      )}
    </div>
  );
}

// Connector names must match the backend's `^[a-z0-9_-]+$` (max 64 chars) —
// sanitize as the user types instead of letting a friendly name like
// "Softnix KB Intelligence" reach the API and bounce back as a raw 422.
function slugifyConnectorName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 64);
}

function ConnectorsPanel() {
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [editing, setEditing] = useState<Partial<ConnectorInfo> | null>(null);
  const [setupPreset, setSetupPreset] = useState<ConnectorPreset | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const { error, guard } = useAsyncError();
  // Timeout field keeps its own raw text so a keystroke that doesn't parse
  // (e.g. a stray non-digit, or a value mid-edit) doesn't silently stop
  // updating the visible input while `editing.timeout_ms` stays unchanged —
  // reset only when we start editing a different connector (or a new one).
  const [timeoutText, setTimeoutText] = useState("");
  const [timeoutTextFor, setTimeoutTextFor] = useState<string | null>(null);
  const editingKey = editing ? (editing.id ?? "__new__") : null;
  if (editingKey !== timeoutTextFor) {
    setTimeoutTextFor(editingKey);
    setTimeoutText(editing?.timeout_ms != null ? String(editing.timeout_ms) : "");
  }
  // Live-sanitizing the name on every keystroke (slugifyConnectorName) would
  // otherwise reset the caret to the end of the field whenever sanitization
  // changes the string (React must overwrite a controlled input's value when
  // it differs from the raw DOM edit) — capture where the caret should land
  // in the SANITIZED string, then restore it after the re-render.
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (pendingCaretRef.current !== null && nameInputRef.current) {
      const pos = pendingCaretRef.current;
      nameInputRef.current.setSelectionRange(pos, pos);
      pendingCaretRef.current = null;
    }
  }, [editing?.name]);

  const reload = useCallback(() => api.listConnectors().then(setConnectors), []);
  useEffect(() => {
    void reload();
    api.connectorPresets().then(setPresets).catch(() => setPresets([]));
    void api.me().then((me) => setIsAdmin(me.is_admin));
  }, [reload]);

  const installedByName = new Map(connectors.map((c) => [c.name.toLowerCase(), c]));

  // Group presets into ordered categories for the catalog grid.
  const categoryOrder = [
    "Productivity",
    "Communication",
    "Search",
    "Finance",
    "Automation",
    "Softnix",
    "Other",
  ];
  const grouped = new Map<string, ConnectorPreset[]>();
  for (const p of presets) {
    const cat = p.category || "Other";
    (grouped.get(cat) ?? grouped.set(cat, []).get(cat)!).push(p);
  }
  const categories = [...grouped.keys()].sort(
    (a, b) => (categoryOrder.indexOf(a) + 1 || 99) - (categoryOrder.indexOf(b) + 1 || 99),
  );

  if (editing) {
    const transport = editing.transport ?? "http";
    return (
      <div className="claw-panel">
        <TextInput
          ref={nameInputRef}
          label="Name (lowercase, e.g. github)"
          value={editing.name ?? ""}
          onChange={(v, e) => {
            const caret = e?.target?.selectionStart ?? v.length;
            // Approximate the caret's new position by sanitizing just the
            // portion before it — good enough for a slug field, and avoids
            // the end-of-string jump on every mid-string edit.
            pendingCaretRef.current = slugifyConnectorName(v.slice(0, caret)).length;
            setEditing({ ...editing, name: slugifyConnectorName(v) });
          }}
          isDisabled={!!editing.id}
        />
        <TextArea
          label="Description (optional)"
          value={editing.description ?? ""}
          onChange={(v) => setEditing({ ...editing, description: v })}
          rows={2}
        />
        <div className="claw-row">
          {/* stdio spawns a real subprocess on the server (unsandboxed) — only
              an admin may pick it for a NEW connector. An existing stdio
              connector (a built-in preset the user installed) still shows so
              they can manage it, but see the disabled Command field below. */}
          {(isAdmin || transport === "stdio") && (
            <Button
              label="stdio (local command)"
              size="sm"
              variant={transport === "stdio" ? "primary" : "secondary"}
              clickAction={() => setEditing({ ...editing, transport: "stdio" })}
            />
          )}
          <Button
            label="HTTP (remote server)"
            size="sm"
            variant={transport === "http" ? "primary" : "secondary"}
            clickAction={() => setEditing({ ...editing, transport: "http" })}
          />
        </div>
        {transport === "stdio" ? (
          <>
            <TextInput
              label="Command (e.g. npx -y @modelcontextprotocol/server-github)"
              value={editing.command ?? ""}
              onChange={(v) => setEditing({ ...editing, command: v })}
              isDisabled={!isAdmin}
            />
            {!isAdmin && (
              <Text size="sm" color="secondary">
                Only an administrator can set a custom local command — ask them to change this.
              </Text>
            )}
          </>
        ) : (
          <TextInput
            label="Server URL (e.g. https://mcp.example.com/mcp)"
            value={editing.url ?? ""}
            onChange={(v) => setEditing({ ...editing, url: v })}
          />
        )}
        <TextArea
          label="Environment variables (KEY=value, or Header-Name: value for an HTTP header, or QUERY_name=value for a URL query param — one per line)"
          value={formatEnvText(editing.env)}
          onChange={(v) => setEditing({ ...editing, env: parseEnvText(v) })}
          rows={3}
        />
        <TextInput
          label="Timeout (ms)"
          placeholder="Default (instance-wide setting)"
          description="1000-120000. Out-of-range values are clamped on save."
          value={timeoutText}
          onChange={(v) => {
            setTimeoutText(v);
            if (v.trim() === "") {
              setEditing({ ...editing, timeout_ms: null });
              return;
            }
            const parsed = parseInt(v, 10);
            if (Number.isNaN(parsed)) return;
            // Don't clamp while typing — clamping a partial value (e.g. the
            // "5" in "5000") mid-entry rewrites the field out from under the
            // next keystroke. Clamp once, on save, instead.
            setEditing({ ...editing, timeout_ms: parsed });
          }}
        />
        <Switch
          value={editing.enabled ?? true}
          label="Enable server"
          changeAction={(checked) => setEditing({ ...editing, enabled: checked })}
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
                  description: editing.description ?? "",
                  transport,
                  command: editing.command ?? "",
                  url: editing.url ?? "",
                  env: editing.env ?? {},
                  timeout_ms:
                    editing.timeout_ms != null
                      ? Math.max(1000, Math.min(120000, editing.timeout_ms))
                      : null,
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

  if (setupPreset) {
    return (
      <GuidedSetup
        preset={setupPreset}
        installed={installedByName.get(setupPreset.name.toLowerCase())}
        onCancel={() => setSetupPreset(null)}
        onSaved={async () => {
          setSetupPreset(null);
          await reload();
        }}
        onManage={(c) => {
          setSetupPreset(null);
          setEditing(c);
        }}
      />
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">
          Add a connector to give Claw access to your apps. You supply your own keys — stored
          encrypted.
        </Text>
        <Button
          label="Add custom"
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          variant="secondary"
          clickAction={() => setEditing({ transport: isAdmin ? "stdio" : "http", enabled: true })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {categories.map((cat) => (
        <div key={cat} className="claw-connector-category">
          <Text type="label" color="secondary" className="claw-connector-cat-title">
            {cat}
          </Text>
          <Divider />
          <div className="claw-connector-grid">
            {(grouped.get(cat) ?? []).map((p) => {
              const installed = installedByName.get(p.name.toLowerCase());
              const menuItems = [
                ...(p.docs
                  ? [{ label: "View docs", icon: ExternalLink, onClick: () => window.open(p.docs, "_blank", "noopener") }]
                  : []),
                ...(installed
                  ? [
                      { label: "Edit", icon: Pencil, onClick: () => setEditing(installed) },
                      { type: "divider" as const },
                      {
                        label: "Remove",
                        icon: Trash2,
                        onClick: () =>
                          guard(async () => {
                            await api.deleteConnector(installed.id);
                            // If an edit form for this same connector was opened
                            // (from a stale pre-delete snapshot) while the delete
                            // was in flight, close it — otherwise Save would
                            // resurrect the just-deleted connector as a new row.
                            setEditing((prev) => (prev?.id === installed.id ? null : prev));
                            await reload();
                          }),
                      },
                    ]
                  : []),
              ];
              return (
                <Card key={p.key} padding={2} className="claw-connector-card">
                  <ConnectorBrandTile presetKey={p.key} />
                  <div className="claw-connector-body">
                    <Text weight="semibold" className="claw-connector-name">
                      {p.label}
                    </Text>
                    <Text size="sm" color="secondary" as="p" className="claw-connector-desc">
                      {p.description}
                    </Text>
                    <div className="claw-connector-meta">
                      {installed?.runtime.status === "connected" ? (
                        <Badge
                          variant="success"
                          icon={<Icon icon="check" size="xsm" />}
                          label={`${installed.runtime.tools ?? 0} tools`}
                        />
                      ) : installed?.runtime.status === "error" ? (
                        <Badge variant="error" icon={<Icon icon="error" size="xsm" />} label="Error" />
                      ) : (
                        (() => {
                          const hint = presetAuthHint(p);
                          return hint ? <span className="claw-connector-auth">{hint}</span> : null;
                        })()
                      )}
                    </div>
                  </div>
                  <div className="claw-connector-actions">
                    {installed ? (
                      <Button
                        label="Manage"
                        size="sm"
                        variant="secondary"
                        clickAction={() => setEditing(installed)}
                      />
                    ) : (
                      <Button label="Add" size="sm" variant="primary" clickAction={() => setSetupPreset(p)} />
                    )}
                    {menuItems.length > 0 && <MoreMenu label={`${p.label} options`} size="sm" items={menuItems} />}
                  </div>
                </Card>
              );
            })}
          </div>
        </div>
      ))}

      {connectors.length > 0 && (
        <div className="claw-connector-category">
          <Text type="label" color="secondary" className="claw-connector-cat-title">
            Your connectors
          </Text>
          <Divider />
          {connectors.map((c) => (
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
                  {(c.runtime.tool_names?.length ?? 0) > 0 && (
                    <ConnectorToolNames names={c.runtime.tool_names!} />
                  )}
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
                        // Keep an already-open edit form for this same connector
                        // in sync, so a stale `editing.enabled` snapshot can't
                        // later overwrite this toggle when the form is saved.
                        setEditing((prev) => (prev?.id === c.id ? { ...prev, enabled: checked } : prev));
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
                        // See the "Remove" MoreMenu item above — closes a
                        // same-connector edit form opened mid-delete so Save
                        // can't resurrect the row that was just removed.
                        setEditing((prev) => (prev?.id === c.id ? null : prev));
                        await reload();
                      })
                    }
                  />
                </div>
              </div>
            </Card>
          ))}
        </div>
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
          Telegram isn't set up on this workspace yet. Ask an administrator to connect a bot in
          Control Plane → Telegram.
        </Text>
      </div>
    );
  }

  const botHandle = status.bot_username ? `@${status.bot_username}` : "the bot";
  const botUrl = status.bot_username ? `https://t.me/${status.bot_username}` : "";

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
          <Card padding={2} variant="muted">
            <Text weight="semibold">3 steps to link</Text>
            <ol className="claw-telegram-steps">
              <li>
                Open {botHandle} in Telegram and tap <strong>Start</strong>.
              </li>
              <li>Click "Generate link code" below.</li>
              <li>Send the code back to the bot as a message.</li>
            </ol>
          </Card>
          <div className="claw-row">
            {botUrl && (
              <Button
                label="Open in Telegram"
                icon={<Icon icon={ExternalLink} size="sm" />}
                variant="secondary"
                href={botUrl}
                target="_blank"
                rel="noopener noreferrer"
              />
            )}
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
          </div>
          {code && (
            <Card padding={2} variant="muted">
              <Text weight="semibold">Your link code</Text>
              <Text type="display-3">{code}</Text>
              <Text size="sm" color="secondary" as="p">
                Send this to {botHandle} as a message:
              </Text>
              <Text type="code">/link {code}</Text>
              <Text size="sm" color="secondary" as="p">
                Expires in a few minutes. Once sent, come back and refresh this page.
              </Text>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Browser extension

function BrowserExtensionPanel() {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof api.browserExtensionStatus>> | null>(
    null,
  );
  const [pairing, setPairing] = useState<Awaited<ReturnType<typeof api.browserExtensionPairingInit>> | null>(
    null,
  );
  const [copied, setCopied] = useState(false);
  const { error, guard } = useAsyncError();

  const isMobile = useMediaQuery(MOBILE_QUERY);

  const reload = useCallback(() => api.browserExtensionStatus().then(setStatus), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  // Pairing installs a Chrome extension on the user's computer — impossible on
  // iPhone/iPad (no desktop-Chrome extensions on iOS), so guide them to desktop
  // rather than showing a flow that can't complete here.
  if (isMobile) {
    return (
      <div className="claw-panel">
        <Text color="secondary">
          The browser extension pairs Claw with Chrome on your computer, so it's set up on a
          desktop — not on a phone or tablet. Open Softnix PrivateClaw on your computer, then go to
          Settings → Browser extension.
        </Text>
      </div>
    );
  }

  if (!status) return <Text color="secondary">Loading…</Text>;

  if (!status.client_extension_enabled) {
    return (
      <div className="claw-panel">
        <Text color="secondary">
          The browser extension is not enabled on this server. An administrator must set
          CLAW_BROWSER__CLIENT_EXTENSION_ENABLED=true to allow pairing.
        </Text>
      </div>
    );
  }

  const pairingText = pairing
    ? `Admin API: ${pairing.api_base}\nInstance: ${pairing.instance_id}\nTicket: ${pairing.pairing_ticket}`
    : "";

  return (
    <div className="claw-panel">
      <Text color="secondary">
        Pair your own Chrome so Claw can act inside your real browser tabs (logged-in sites,
        multi-step flows) instead of an isolated server browser.
      </Text>

      <div className="claw-row">
        {status.paired ? (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label="Paired" />
        ) : (
          <Badge variant="neutral" label="Not paired" />
        )}
        {status.paired &&
          (status.online ? (
            <Badge variant="success" label="Online" />
          ) : (
            <Badge variant="warning" label="Offline" />
          ))}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      <Card padding={2} variant="muted">
        <Text weight="semibold">1. Install the extension</Text>
        <Text size="sm" color="secondary" as="p">
          Download the package, unzip it, then load it in Chrome via chrome://extensions → enable
          Developer mode → “Load unpacked” → pick the unzipped folder.
        </Text>
        <div className="claw-row">
          <Button
            label="Download extension"
            icon={<Icon icon={Download} size="sm" />}
            clickAction={() => {
              window.location.href = "/api/browser-extension/download";
            }}
          />
        </div>
      </Card>

      <Card padding={2} variant="muted">
        <Text weight="semibold">2. Pair your browser</Text>
        <Text size="sm" color="secondary" as="p">
          Generate pairing details, copy them, open the extension popup, and paste into “Paste
          pairing details”. The ticket expires in a few minutes.
        </Text>
        <div className="claw-row">
          <Button
            label="Generate pairing details"
            icon={<Icon icon={LinkIcon} size="sm" />}
            clickAction={() =>
              guard(async () => {
                setCopied(false);
                setPairing(await api.browserExtensionPairingInit());
              })
            }
          />
        </div>
        {pairing && (
          <Card padding={2}>
            <Text type="code">{pairingText}</Text>
            <div className="claw-row">
              <Button
                label={copied ? "Copied" : "Copy pairing details"}
                icon={<Icon icon={Copy} size="sm" />}
                variant="secondary"
                clickAction={() =>
                  guard(async () => {
                    await navigator.clipboard.writeText(pairingText);
                    setCopied(true);
                  })
                }
              />
            </div>
          </Card>
        )}
      </Card>

      {status.paired && (
        <Button
          label="Unpair browser"
          icon={<Icon icon={Trash2} size="sm" />}
          variant="destructive"
          clickAction={() =>
            guard(async () => {
              await api.browserExtensionUnpair();
              setPairing(null);
              await reload();
            })
          }
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Knowledge

const KB_ACCEPT = ".pdf,.docx,.txt,.md,.markdown,.html,.htm,.csv";

// Shared by the create form and the per-card editor: a 3-way Private/Group/
// Public picker, plus (when Group is selected) the owner's default group and
// a multi-select of additional groups to share with.
function VisibilitySelector({
  visibility,
  onChange,
  sharedGroupIds,
  onSharedGroupIdsChange,
  myGroupId,
  myGroupName,
  groups,
}: {
  visibility: "private" | "group" | "public";
  onChange: (v: "private" | "group" | "public") => void;
  sharedGroupIds: string[];
  onSharedGroupIdsChange: (ids: string[]) => void;
  myGroupId: string | null;
  myGroupName: string | null;
  groups: SimpleGroup[];
}) {
  const otherGroups = groups.filter((g) => g.id !== myGroupId);
  return (
    <div className="claw-field-group">
      <Text size="sm" color="secondary">
        Visibility
      </Text>
      <div className="claw-row">
        <Button
          label="Private"
          size="sm"
          variant={visibility === "private" ? "primary" : "secondary"}
          clickAction={() => onChange("private")}
        />
        <Button
          label="Group"
          size="sm"
          variant={visibility === "group" ? "primary" : "secondary"}
          isDisabled={!myGroupId}
          clickAction={() => onChange("group")}
        />
        <Button
          label="Public"
          size="sm"
          variant={visibility === "public" ? "primary" : "secondary"}
          clickAction={() => onChange("public")}
        />
      </div>
      {!myGroupId && (
        <Text size="sm" color="secondary">
          Join a group first to use group visibility.
        </Text>
      )}
      {visibility === "group" && myGroupId && (
        <>
          <Text size="sm" color="secondary">
            Default: shared with your group ({myGroupName ?? "—"})
          </Text>
          {otherGroups.length > 0 && (
            <>
              <Text size="sm" color="secondary">
                Also share with:
              </Text>
              <div className="claw-row">
                {otherGroups.map((g) => (
                  <Button
                    key={g.id}
                    label={g.name}
                    size="sm"
                    variant={sharedGroupIds.includes(g.id) ? "primary" : "secondary"}
                    clickAction={() =>
                      onSharedGroupIdsChange(
                        sharedGroupIds.includes(g.id)
                          ? sharedGroupIds.filter((id) => id !== g.id)
                          : [...sharedGroupIds, g.id],
                      )
                    }
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function KnowledgePanel() {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<"private" | "group" | "public">("private");
  const [sharedGroupIds, setSharedGroupIds] = useState<string[]>([]);
  const [groups, setGroups] = useState<SimpleGroup[]>([]);
  const [myGroupId, setMyGroupId] = useState<string | null>(null);
  const toast = useToast();

  const load = useCallback(() => {
    setLoading(true);
    api
      .listKnowledge()
      .then(setBases)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  // Patch one base in place from a mutation response we already have, instead
  // of re-fetching the whole list (and its per-base doc-count aggregate) for a
  // single-field change.
  const patchBase = useCallback((id: string, patch: Partial<KnowledgeBase>) => {
    setBases((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));
  }, []);

  useEffect(() => load(), [load]);
  useEffect(() => {
    void api.listGroups().then(setGroups);
    void api.me().then((me) => setMyGroupId(me.group_id));
  }, []);

  const myGroupName = groups.find((g) => g.id === myGroupId)?.name ?? null;

  const create = async () => {
    if (!name.trim()) return;
    setError("");
    try {
      await api.createKnowledge(
        name.trim(),
        description.trim(),
        visibility,
        visibility === "group" ? sharedGroupIds : undefined,
      );
      setName("");
      setDescription("");
      setVisibility("private");
      setSharedGroupIds([]);
      setCreating(false);
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary" size="sm">
          Upload documents (PDF, Word, text, Markdown, HTML) to build a knowledge base the agent can
          search when answering. Choose Private (only you), Group (your org group), or Public (everyone).
        </Text>
        {!creating && (
          <Button
            label="New knowledge base"
            variant="secondary"
            icon={<Icon icon={Plus} size="sm" />}
            onClick={() => setCreating(true)}
          >
            New
          </Button>
        )}
      </div>

      {error && <ErrorText>{error}</ErrorText>}

      {creating && (
        <Card padding={3}>
          <div className="claw-kb-form">
            <TextInput label="Name" value={name} onChange={setName} placeholder="e.g. Company Handbook" />
            <TextInput
              label="Description"
              value={description}
              onChange={setDescription}
              placeholder="What's in this knowledge base?"
            />
            <VisibilitySelector
              visibility={visibility}
              onChange={setVisibility}
              sharedGroupIds={sharedGroupIds}
              onSharedGroupIdsChange={setSharedGroupIds}
              myGroupId={myGroupId}
              myGroupName={myGroupName}
              groups={groups}
            />
            <div className="claw-row">
              <Button label="Create" variant="primary" onClick={create}>
                Create
              </Button>
              <Button label="Cancel" variant="ghost" onClick={() => setCreating(false)}>
                Cancel
              </Button>
            </div>
          </div>
        </Card>
      )}

      {loading ? (
        <Text color="secondary">Loading…</Text>
      ) : bases.length === 0 && !creating ? (
        <EmptyState
          icon={<Icon icon={Library} size="lg" />}
          title="No knowledge bases yet"
          description="Create one and upload documents so Claw can answer from them."
        />
      ) : (
        <div className="claw-kb-grid">
          {bases.map((kb) => (
            <KnowledgeCard
              key={kb.id}
              kb={kb}
              onChanged={load}
              onPatch={patchBase}
              toast={toast}
              groups={groups}
              myGroupId={myGroupId}
              myGroupName={myGroupName}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Unicode code-point count, matching Python's len() on the backend — JS's
// .length counts UTF-16 code units, which over-counts surrogate-pair (e.g.
// emoji) characters relative to total_chars from the API.
function codePointLength(s: string): number {
  return s.length - (s.match(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g)?.length ?? 0);
}

function KnowledgeCard({
  kb,
  onChanged,
  onPatch,
  toast,
  groups,
  myGroupId,
  myGroupName,
}: {
  kb: KnowledgeBase;
  onChanged: () => void;
  onPatch: (id: string, patch: Partial<KnowledgeBase>) => void;
  toast: ReturnType<typeof useToast>;
  groups: SimpleGroup[];
  myGroupId: string | null;
  myGroupName: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [docs, setDocs] = useState<KnowledgeDoc[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  // Separate from `busy` (which gates delete actions) so flipping visibility
  // never disables unrelated Delete buttons for the duration of the request.
  const [visibilityBusy, setVisibilityBusy] = useState(false);
  const [editingVisibility, setEditingVisibility] = useState(false);
  const [draftVisibility, setDraftVisibility] = useState<"private" | "group" | "public">(kb.visibility);
  const [draftSharedGroupIds, setDraftSharedGroupIds] = useState<string[]>(kb.shared_group_ids ?? []);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Extracted-text preview (one open at a time), paged via next_offset.
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  const [previewTitle, setPreviewTitle] = useState("");
  const [previewText, setPreviewText] = useState("");
  const [previewTotal, setPreviewTotal] = useState(0);
  const [previewNext, setPreviewNext] = useState(0);
  const [previewMore, setPreviewMore] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewExpanded, setPreviewExpanded] = useState(false);
  // Bumped on every preview request; a response only writes state if its token
  // is still current, so a slow reply can't clobber a doc the user switched to.
  const previewReq = useRef(0);
  const previewLoadedChars = useMemo(() => codePointLength(previewText), [previewText]);

  const openPreview = async (docId: string) => {
    if (previewFor === docId) {
      setPreviewFor(null);
      setPreviewExpanded(false);
      return;
    }
    const token = ++previewReq.current;
    setPreviewFor(docId);
    setPreviewTitle("");
    setPreviewText("");
    // Clear the previous doc's pagination stats too — otherwise its stale
    // total/has-more would render against this doc's empty text while loading.
    setPreviewTotal(0);
    setPreviewNext(0);
    setPreviewMore(false);
    setPreviewLoading(true);
    try {
      const r = await api.previewKnowledgeDoc(kb.id, docId, 0);
      if (token !== previewReq.current) return;
      setPreviewTitle(r.title ?? "");
      setPreviewText(r.text ?? "");
      setPreviewTotal(r.total_chars ?? 0);
      setPreviewNext(r.next_offset ?? 0);
      setPreviewMore(Boolean(r.has_more));
    } catch (e) {
      if (token !== previewReq.current) return;
      setPreviewText(`Preview unavailable: ${String(e)}`);
      setPreviewMore(false);
    } finally {
      if (token === previewReq.current) setPreviewLoading(false);
    }
  };

  const loadMorePreview = async (docId: string) => {
    const token = ++previewReq.current;
    setPreviewLoading(true);
    try {
      const r = await api.previewKnowledgeDoc(kb.id, docId, previewNext);
      if (token !== previewReq.current) return;
      setPreviewText((t) => t + (r.text ?? ""));
      setPreviewNext(r.next_offset ?? previewNext);
      setPreviewMore(Boolean(r.has_more));
    } finally {
      if (token === previewReq.current) setPreviewLoading(false);
    }
  };

  const loadDocs = useCallback(() => {
    api.listKnowledgeDocs(kb.id).then(setDocs).catch(() => setDocs([]));
  }, [kb.id]);

  // While any document is still being parsed in the background, poll the list so
  // its status flips to ready/failed without the user refreshing.
  useEffect(() => {
    if (!expanded || !docs) return;
    const busy = docs.some((d) => d.status === "pending" || d.status === "processing");
    if (!busy) return;
    const t = setInterval(loadDocs, 2000);
    return () => clearInterval(t);
  }, [expanded, docs, loadDocs]);

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && docs === null) loadDocs();
  };

  const onPick = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const res = await api.uploadKnowledgeDocs(kb.id, Array.from(files));
      if (res.ingested.length) {
        toast({ body: `Queued ${res.ingested.length} document(s) in ${kb.name} — processing…`, type: "info", autoHideDuration: 2500 });
      }
      if (res.errors.length) {
        toast({ body: res.errors.join("; "), type: "error" });
      }
      loadDocs();
      setExpanded(true);
      onChanged();
    } catch (e) {
      toast({ body: `Upload failed: ${String(e)}`, type: "error" });
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const removeDoc = async (docId: string) => {
    setBusy(true);
    try {
      await api.deleteKnowledgeDoc(kb.id, docId);
      loadDocs();
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const removeBase = async () => {
    if (!window.confirm(`Delete knowledge base "${kb.name}" and all its documents?`)) return;
    setBusy(true);
    try {
      await api.deleteKnowledge(kb.id);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const visibilityBadge = (v: KnowledgeBase["visibility"]) => {
    const icon = v === "public" ? Globe : v === "group" ? Users : Lock;
    const variant = v === "public" ? "success" : v === "group" ? "info" : "neutral";
    const label = v === "public" ? "Public" : v === "group" ? "Group" : "Private";
    return <Badge variant={variant} icon={<Icon icon={icon} size="xsm" />} label={label} />;
  };

  const saveVisibility = async () => {
    setVisibilityBusy(true);
    try {
      const r = await api.updateKnowledge(kb.id, {
        visibility: draftVisibility,
        shared_group_ids: draftVisibility === "group" ? draftSharedGroupIds : [],
      });
      // Use the mutation response directly — no need to re-fetch the whole list
      // (with its per-base doc-count aggregate) for a single-field change.
      onPatch(kb.id, { visibility: r.visibility, shared_group_ids: r.shared_group_ids });
      setEditingVisibility(false);
    } catch (e) {
      toast({ body: `Failed to update visibility: ${String(e)}`, type: "error" });
    } finally {
      setVisibilityBusy(false);
    }
  };

  return (
    <Card padding={3}>
      <div className="claw-kb-card">
        <div className="claw-kb-card-head">
          <Icon icon={Library} size="md" color="secondary" />
          <div className="claw-kb-card-title">
            <Text weight="semibold">{kb.name}</Text>
            {kb.is_owner ? (
              <button
                type="button"
                className="claw-kb-visibility-badge"
                disabled={busy}
                aria-label="Edit visibility"
                title={
                  kb.visibility === "group" && kb.owner_group_name
                    ? `Group — shared with ${kb.owner_group_name}. Click to edit.`
                    : "Click to edit visibility"
                }
                onClick={() => {
                  setDraftVisibility(kb.visibility);
                  setDraftSharedGroupIds(kb.shared_group_ids ?? []);
                  setEditingVisibility((v) => !v);
                }}
              >
                {visibilityBadge(kb.visibility)}
              </button>
            ) : (
              visibilityBadge(kb.visibility)
            )}
          </div>
        </div>
        {editingVisibility && (
          <Card padding={2}>
            <VisibilitySelector
              visibility={draftVisibility}
              onChange={setDraftVisibility}
              sharedGroupIds={draftSharedGroupIds}
              onSharedGroupIdsChange={setDraftSharedGroupIds}
              myGroupId={myGroupId}
              myGroupName={myGroupName}
              groups={groups}
            />
            <div className="claw-row">
              <Button
                label="Save"
                size="sm"
                variant="primary"
                isDisabled={visibilityBusy}
                clickAction={saveVisibility}
              />
              <Button
                label="Cancel"
                size="sm"
                variant="ghost"
                isDisabled={visibilityBusy}
                clickAction={() => setEditingVisibility(false)}
              />
            </div>
          </Card>
        )}
        {kb.description && (
          <Text size="sm" color="secondary" className="claw-kb-desc">
            {kb.description}
          </Text>
        )}
        <div className="claw-kb-meta">
          <span>
            <Icon icon={FileText} size="xsm" color="secondary" /> {kb.docs} document{kb.docs === 1 ? "" : "s"}
          </span>
          {!kb.is_owner && <span className="claw-kb-shared">shared</span>}
        </div>

        <input
          ref={fileRef}
          type="file"
          multiple
          accept={KB_ACCEPT}
          style={{ display: "none" }}
          onChange={(e) => void onPick(e.target.files)}
        />
        <div className="claw-kb-actions">
          {kb.is_owner && (
            <Button
              label={uploading ? "Uploading…" : "Upload"}
              variant="secondary"
              size="sm"
              isDisabled={uploading}
              icon={<Icon icon={Upload} size="sm" />}
              onClick={() => fileRef.current?.click()}
            />
          )}
          <Button label={expanded ? "Hide documents" : "View documents"} variant="ghost" size="sm" onClick={toggle}>
            {expanded ? "Hide" : "Documents"}
          </Button>
          {kb.is_owner && (
            <Button
              label="Delete knowledge base"
              variant="ghost"
              size="sm"
              isIconOnly
              isDisabled={busy}
              icon={<Icon icon={Trash2} size="sm" color="error" />}
              onClick={removeBase}
            />
          )}
        </div>

        {expanded && (
          <div className="claw-kb-docs">
            {docs === null ? (
              <Text size="sm" color="secondary">
                Loading…
              </Text>
            ) : docs.length === 0 ? (
              <Text size="sm" color="secondary">
                No documents yet — upload one to get started.
              </Text>
            ) : (
              docs.map((d) => (
                <div key={d.id}>
                  <div className="claw-kb-doc">
                    <Icon icon={FileText} size="sm" color="secondary" />
                    <span className="claw-kb-doc-name" title={d.filename}>
                      {d.title}
                    </span>
                    {d.status === "failed" ? (
                      <span className="claw-kb-doc-meta claw-kb-doc-failed" title={d.error}>
                        Failed
                      </span>
                    ) : d.status === "pending" || d.status === "processing" ? (
                      <span className="claw-kb-doc-meta claw-kb-doc-processing">Processing…</span>
                    ) : (
                      <span className="claw-kb-doc-meta">
                        {d.chunks} chunk{d.chunks === 1 ? "" : "s"}
                      </span>
                    )}
                    {d.status === "ready" && (
                      <button
                        type="button"
                        className="claw-kb-doc-del"
                        aria-label="Preview extracted text"
                        onClick={() => openPreview(d.id)}
                      >
                        <Icon icon={Eye} size="xsm" color={previewFor === d.id ? "primary" : "secondary"} />
                      </button>
                    )}
                    {kb.is_owner && (
                      <button
                        type="button"
                        className="claw-kb-doc-del"
                        aria-label="Delete document"
                        disabled={busy}
                        onClick={() => removeDoc(d.id)}
                      >
                        <Icon icon={Trash2} size="xsm" color="secondary" />
                      </button>
                    )}
                  </div>
                  {previewFor === d.id && (
                    <div className="claw-kb-doc-preview">
                      <div className="claw-kb-doc-preview-head">
                        <Text size="sm" color="secondary">
                          Extracted text{" "}
                          {previewTotal > 0 &&
                            `· ${previewLoadedChars.toLocaleString()} / ${previewTotal.toLocaleString()} chars`}
                        </Text>
                        <button
                          type="button"
                          className="claw-kb-doc-expand"
                          aria-label="Open in expanded view"
                          onClick={() => setPreviewExpanded(true)}
                        >
                          <Icon icon={Maximize2} size="xsm" color="secondary" />
                        </button>
                      </div>
                      <pre className="claw-kb-doc-preview-body">{previewText}</pre>
                      {previewMore && (
                        <Button
                          label={previewLoading ? "Loading…" : "Load more"}
                          size="sm"
                          variant="ghost"
                          isDisabled={previewLoading}
                          onClick={() => loadMorePreview(d.id)}
                        />
                      )}
                    </div>
                  )}
                  {previewFor === d.id && (
                    // Kept mounted whenever this doc's preview is active, with only
                    // `isOpen` toggling — Dialog's own close effect (dialog.close() +
                    // returning focus to the trigger) runs on an isOpen transition
                    // while mounted, but never fires if we unmount it instead.
                    <Dialog
                      isOpen={previewExpanded}
                      onOpenChange={setPreviewExpanded}
                      variant="fullscreen"
                      purpose="info"
                    >
                      <Layout
                        header={
                          <DialogHeader
                            title={previewTitle || d.title}
                            subtitle={
                              previewTotal > 0
                                ? `${previewLoadedChars.toLocaleString()} / ${previewTotal.toLocaleString()} chars extracted`
                                : undefined
                            }
                            onOpenChange={setPreviewExpanded}
                          />
                        }
                        content={
                          <LayoutContent>
                            <pre className="claw-kb-doc-preview-body claw-kb-doc-preview-body--expanded">
                              {previewText}
                            </pre>
                          </LayoutContent>
                        }
                        footer={
                          previewMore ? (
                            <LayoutFooter hasDivider>
                              <Button
                                label={previewLoading ? "Loading…" : "Load more"}
                                size="sm"
                                variant="secondary"
                                isDisabled={previewLoading}
                                onClick={() => loadMorePreview(d.id)}
                              />
                            </LayoutFooter>
                          ) : undefined
                        }
                      />
                    </Dialog>
                  )}
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
