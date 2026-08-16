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
import { useBranding, useT } from "./branding";
import { ErrorText } from "./ErrorText";
import { PasswordField } from "./PasswordField";
import {
  ApiOperation,
  ApiOperationParam,
  AuthUser,
  BrandingChatBackground,
  BrandingFontSize,
  BrandingLanguage,
  ConnectorGlobalSummary,
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

// `labelKey` (not a pre-resolved label) because this array is built at module
// scope, outside any component — it can't call the `useT()` hook itself.
// Consumers (this file's SettingsPanel, and App.tsx's nav) resolve it via
// `t(s.labelKey)` at render time so the label follows the current language.
export const SETTINGS_SECTIONS: { key: SettingsSection; labelKey: string; icon: IconType | IconName }[] = [
  { key: "profile", labelKey: "settings.nav.profile", icon: UserIcon },
  { key: "skills", labelKey: "settings.nav.skills", icon: Sparkles },
  { key: "knowledge", labelKey: "settings.nav.knowledge", icon: Library },
  { key: "memory", labelKey: "settings.nav.memory", icon: Brain },
  { key: "models", labelKey: "settings.nav.models", icon: Cpu },
  { key: "connectors", labelKey: "settings.nav.connectors", icon: Plug },
  { key: "schedules", labelKey: "settings.nav.schedules", icon: "calendar" },
  { key: "heartbeat", labelKey: "settings.nav.heartbeat", icon: HeartPulse },
  { key: "telegram", labelKey: "settings.nav.telegram", icon: Send },
  { key: "browser-extension", labelKey: "settings.nav.browserExt", icon: Puzzle },
];

export function SettingsPanel({ section }: { section: SettingsSection }) {
  const t = useT();
  const meta = SETTINGS_SECTIONS.find((s) => s.key === section);
  // My Models is a data table, same reasoning as the admin LLM Providers page
  // (see AdminPanel) — give it the wider column instead of the shared 720px
  // prose width.
  const isWide = section === "models";
  return (
    <div className="claw-settings-panel">
      <div className={`claw-settings-panel-header${isWide ? " claw-panel-wide" : ""}`}>
        <Icon icon={meta?.icon ?? "check"} size="lg" color="secondary" />
        <Text type="display-3">{meta ? t(meta.labelKey) : ""}</Text>
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
      // .message, not String(e): a thrown Error renders as "Error: <text>",
      // which reads as a crash when the text is a validation message written
      // for the user.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  return { error, guard };
}

// ---------------------------------------------------------------- Profile

function ProfilePanel() {
  const t = useT();
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
      toast({ body: t("settings.profile.passwordUpdated"), type: "info", autoHideDuration: 2500 });
    } catch (e) {
      // Inline, not an early-return replacing the whole panel — a wrong
      // current password shouldn't wipe out the form the user is mid-typing.
      setFormError(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  if (error) return <ErrorText>{error}</ErrorText>;
  if (!me) return <Text color="secondary">{t("settings.common.loading")}</Text>;

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
          <Text weight="semibold">{t("settings.profile.changePassword")}</Text>
          {!me.has_password ? (
            <Text size="sm" color="secondary">
              {t("settings.profile.noPasswordAccount")}
            </Text>
          ) : (
            <>
              <PasswordField
                label={t("settings.profile.currentPassword")}
                value={currentPassword}
                onChange={setCurrentPassword}
              />
              <PasswordField
                label={t("settings.profile.newPassword")}
                description={t("settings.profile.newPasswordDesc")}
                value={newPassword}
                onChange={setNewPassword}
              />
              {formError && <ErrorText>{formError}</ErrorText>}
              <div className="claw-row">
                <Button
                  label={busy ? "…" : t("settings.profile.updatePassword")}
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
  const t = useT();
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
      toast({ body: t("settings.profile.preferencesSaved"), type: "info", autoHideDuration: 2500 });
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
          <Text weight="semibold">{t("settings.profile.language")}</Text>
          <Text size="sm" color="secondary">
            {t("settings.profile.languageDesc")}
          </Text>
          <SegmentedControl
            value={language}
            onChange={(v) => setLanguage(v as BrandingLanguage)}
            label={t("settings.profile.language")}
          >
            {/* Language names are shown as endonyms (in their own language), not
                translated per the current UI language — same convention as any
                language picker. */}
            <SegmentedControlItem value="en" label="English" />
            <SegmentedControlItem value="th" label="ไทย (Thai)" />
          </SegmentedControl>
        </div>
        <div>
          <Text weight="semibold">{t("settings.profile.fontSize")}</Text>
          <SegmentedControl
            value={fontSize}
            onChange={(v) => setFontSize(v as BrandingFontSize)}
            label={t("settings.profile.fontSize")}
          >
            <SegmentedControlItem value="small" label={t("settings.profile.fontSize.small")} />
            <SegmentedControlItem value="medium" label={t("settings.profile.fontSize.medium")} />
            <SegmentedControlItem value="large" label={t("settings.profile.fontSize.large")} />
          </SegmentedControl>
        </div>
        <div>
          <Text weight="semibold">{t("settings.profile.chatBackground")}</Text>
          <Text size="sm" color="secondary">
            {t("settings.profile.chatBackgroundDesc")}
          </Text>
          <SegmentedControl
            value={chatBg}
            onChange={(v) => setChatBg(v as BrandingChatBackground)}
            label={t("settings.profile.chatBackground")}
          >
            <SegmentedControlItem value="solid" label={t("settings.profile.bg.solid")} />
            <SegmentedControlItem value="dots" label={t("settings.profile.bg.dots")} />
            <SegmentedControlItem value="grid" label={t("settings.profile.bg.grid")} />
          </SegmentedControl>
        </div>
        {saveError && <ErrorText>{saveError}</ErrorText>}
        <div>
          <Button label={t("settings.profile.savePreferences")} isDisabled={!dirty || saving} clickAction={save} />
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
  const t = useT();
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
            subtitle={t("settings.skills.subtitle")}
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
                    {t("settings.skills.capabilities")}
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
                label={t("settings.skills.instructions")}
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
            <Button label={t("settings.common.close")} variant="ghost" clickAction={() => onOpenChange(false)} />
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

function SkillsPanel() {
  const t = useT();
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [globalConnectors, setGlobalConnectors] = useState<ConnectorGlobalSummary[]>([]);
  const [editing, setEditing] = useState<Partial<SkillInfo> | null>(null);
  const [viewingDetail, setViewingDetail] = useState<SkillInfo | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.listSkills().then(setSkills), []);
  useEffect(() => {
    void reload();
    void api.listConnectors().then(setConnectors);
    // Admin-global ("Pre-built") connectors are picked from a separate
    // endpoint since they have no owner_id row of this user's own — a skill
    // can link to either kind, so both lists feed the picker below.
    void api.listGlobalConnectors().then(setGlobalConnectors).catch(() => setGlobalConnectors([]));
  }, [reload]);

  // A user's own ENABLED connector shadows a global one of the same name
  // (mirrors the backend's own-vs-global tie-break, which only shadows with
  // enabled connectors — see claw/core/connectors.py's sync_tools own_names,
  // built from enabled_for_user), so it shouldn't also be offered here as a
  // separate, dead-ended pick. A disabled same-named connector must NOT
  // shadow here, or the global one would be wrongly hidden from the picker
  // even though it's actually live and usable.
  const ownNames = new Set(connectors.filter((c) => c.enabled).map((c) => c.name));
  const pickableGlobalConnectors = globalConnectors.filter((c) => !ownNames.has(c.name));

  if (editing) {
    const readOnly = !!editing.builtin;
    return (
      <div className="claw-panel">
        {readOnly && (
          <Text size="sm" color="secondary">
            {t("settings.skills.builtinNotice")}
          </Text>
        )}
        <TextInput
          label={t("settings.skills.name")}
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
          isDisabled={readOnly || !!editing.id}
        />
        <TextInput
          label={t("settings.skills.description")}
          value={editing.description ?? ""}
          onChange={(v) => setEditing({ ...editing, description: v })}
          isDisabled={readOnly}
        />
        <TextArea
          label={t("settings.skills.instructions")}
          value={editing.content ?? ""}
          onChange={(v) => setEditing({ ...editing, content: v })}
          rows={10}
          isDisabled={readOnly}
        />
        {!readOnly && (connectors.length > 0 || pickableGlobalConnectors.length > 0) && (
          <div className="claw-field-group">
            <Text size="sm" color="secondary">
              {t("settings.skills.linkedConnector")}
            </Text>
            <Text size="sm" color="secondary" as="p">
              {t("settings.skills.linkedConnectorDesc")}
            </Text>
            <div className="claw-row">
              <Button
                label={t("settings.skills.none")}
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
              {pickableGlobalConnectors.map((c) => (
                <Button
                  key={c.id}
                  label={`${c.name} (${t("settings.skills.globalConnectorBadge")})`}
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
              label={t("settings.skills.save")}
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
          <Button
            label={readOnly ? t("settings.skills.back") : t("settings.common.cancel")}
            variant="ghost"
            clickAction={() => setEditing(null)}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">{t("settings.skills.intro")}</Text>
        <Button
          label={t("settings.skills.new")}
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          clickAction={() => setEditing({ enabled: true })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {skills.length === 0 ? (
        <EmptyState title={t("settings.skills.emptyTitle")} description={t("settings.skills.emptyDesc")} />
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
                    {t("settings.skills.shadowsBuiltin")}
                  </Text>
                )}
              </div>
              {skill.builtin ? (
                <Button
                  label={t("settings.skills.view")}
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
                    label={t("settings.common.enable", { name: skill.name })}
                    isLabelHidden
                    changeAction={(checked) =>
                      guard(async () => {
                        await api.saveSkill({ ...skill, enabled: checked });
                        await reload();
                      })
                    }
                  />
                  <Button
                    label={t("settings.common.edit")}
                    icon={<Icon icon={Pencil} size="sm" />}
                    size="sm"
                    variant="ghost"
                    clickAction={() => setEditing(skill)}
                  />
                  <Button
                    label={t("settings.common.delete")}
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
  const t = useT();
  const [memory, setMemory] = useState<MemoryInfo | null>(null);
  const [draft, setDraft] = useState("");
  const [saved, setSaved] = useState(false);
  const [dropped, setDropped] = useState<string[]>([]);
  const { error, guard } = useAsyncError();

  useEffect(() => {
    api.getMemory().then((m) => {
      setMemory(m);
      setDraft(m.core);
    });
  }, []);

  if (!memory) return <Text color="secondary">{t("settings.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("settings.memory.intro")}</Text>
      <TextArea
        label={t("settings.memory.label")}
        value={draft}
        onChange={(v) => {
          setDraft(v);
          setSaved(false);
          setDropped([]);
        }}
        rows={10}
      />
      {error && <ErrorText>{error}</ErrorText>}
      {dropped.length > 0 && (
        <Card padding={2} variant="muted">
          <Text size="sm" weight="semibold">
            {t("settings.memory.dropped")}
          </Text>
          {dropped.map((d, i) => (
            <Text key={i} size="sm" color="secondary">
              {d}
            </Text>
          ))}
        </Card>
      )}
      <div className="claw-row">
        <Button
          label={t("settings.memory.save")}
          icon={<Icon icon="check" size="sm" />}
          clickAction={() =>
            guard(async () => {
              const result = await api.saveMemory(draft);
              // Show what was stored, not what was typed: the server sanitizes,
              // so leaving the draft up would present rejected lines as saved.
              setDraft(result.core);
              setDropped(result.dropped);
              setSaved(true);
            })
          }
        />
        {saved && (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={t("settings.memory.saved")} />
        )}
      </div>
      <Divider />
      <Text weight="semibold">{t("settings.memory.historyTitle")}</Text>
      {memory.history.length === 0 ? (
        <Text color="secondary" size="sm">
          {t("settings.memory.historyEmpty")}
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
  "google-sheets": "/connectors/google-sheets.png",
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
export function presetAuthHint(p: ConnectorPreset, t: (key: string) => string): string | null {
  if (p.setup === "oauth") return t("settings.connectors.authHint.oauth");
  if (p.setup === "api_key" || p.setup === "token") return t("settings.connectors.authHint.apiKey");
  return null;
}

export function ConnectorBrandTile({ presetKey }: { presetKey: string }) {
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
  const t = useT();
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
            ? t("settings.connectors.oauthNotConfigured", { provider })
            : t("settings.connectors.oauthStartFailed"),
        );
      }
    };
    return (
      <div className="claw-panel claw-setup">
        {header}
        {installed?.runtime.status === "connected" && (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={t("settings.connectors.connected")} />
        )}
        <Card padding={3} variant="muted">
          <Text weight="semibold">{t("settings.connectors.signInWith", { provider })}</Text>
          <Text size="sm" color="secondary" as="p">
            {t("settings.connectors.oauthDesc", { provider })}
          </Text>
        </Card>
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label={
              busy
                ? t("settings.connectors.redirecting")
                : installed
                  ? t("settings.connectors.reconnectWith", { provider })
                  : t("settings.connectors.connectWith", { provider })
            }
            icon={<Icon icon={LinkIcon} size="sm" />}
            isDisabled={busy}
            clickAction={connect}
          />
          {installed && (
            <Button label={t("settings.connectors.manage")} variant="secondary" clickAction={() => onManage(installed)} />
          )}
          <Button label={t("settings.common.cancel")} variant="ghost" clickAction={onCancel} />
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
          if (!f.optional) throw new Error(t("settings.connectors.fieldRequired", { label: f.label }));
          continue;
        }
        env[f.key] = f.prefix && !raw.startsWith(f.prefix) ? f.prefix + raw : raw;
      }
      const effectiveUrl = preset.url_configurable ? url.trim() || preset.url : preset.url;
      if (preset.url_configurable && !effectiveUrl) throw new Error(t("settings.connectors.mcpUrlRequired"));
      await api.saveConnector({
        name: preset.name,
        description: preset.description ?? "",
        kind: "mcp",
        transport: preset.transport,
        command: preset.command,
        url: effectiveUrl,
        env,
        operations: [],
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
            label={t("settings.connectors.mcpUrlLabel")}
            type="text"
            value={url}
            placeholder={preset.url}
            onChange={setUrl}
          />
          <Text size="sm" color="secondary" as="p" className="claw-setup-help">
            {t("settings.connectors.mcpUrlHelp")}
          </Text>
        </div>
      )}
      {preset.fields.map((f) => (
        <div key={f.key} className="claw-setup-field">
          <TextInput
            label={f.optional ? t("settings.connectors.fieldOptional", { label: f.label }) : f.label}
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
          label={busy ? t("settings.connectors.saving") : t("settings.connectors.addConnector")}
          icon={<Icon icon="check" size="sm" />}
          isDisabled={busy}
          clickAction={save}
        />
        <Button label={t("settings.common.cancel")} variant="ghost" clickAction={onCancel} />
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

export function parseEnvText(text: string): Record<string, string> {
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

/** HEADER_* keys that name the same HTTP header, as "first / second" pairs.
 *
 * The textarea above keeps whatever case was typed, so "Authorization: a" and
 * "authorization: b" become two keys for one header. The backend rejects that
 * on save, and its 422 reaches the user as a raw pydantic blob — so both
 * connector editors check for it first and say it in words instead. */
export function duplicateHeaderKeys(env: Record<string, string>): string[] {
  const seen = new Map<string, string>();
  const duplicates: string[] = [];
  for (const key of Object.keys(env)) {
    if (!key.startsWith(HEADER_ENV_PREFIX)) continue;
    const first = seen.get(key.toLowerCase());
    if (first === undefined) seen.set(key.toLowerCase(), key);
    else duplicates.push(`${first} / ${key}`);
  }
  return duplicates;
}

export function formatEnvText(env: Record<string, string> | undefined): string {
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
export function ConnectorToolNames({ names }: { names: string[] }) {
  const t = useT();
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
          {t("settings.connectors.toolNamesCount", {
            count: String(names.length),
            plural: names.length === 1 ? "" : "s",
          })}
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

// A tool lost to a name collision is otherwise invisible: the connector still
// reports "connected", just with fewer tools than its operation list, which
// reads as the server being broken rather than as a fixable naming clash.
export function ConnectorShadowedTools({ names }: { names: string[] }) {
  const t = useT();
  return (
    <Text size="sm" color="inherit" as="p" className="claw-connector-shadowed-tools">
      {t("settings.connectors.shadowedTools", {
        count: String(names.length),
        plural: names.length === 1 ? "" : "s",
        names: names.join(", "),
      })}
    </Text>
  );
}

// Connector names must match the backend's `^[a-z0-9_-]+$` (max 64 chars) —
// sanitize as the user types instead of letting a friendly name like
// "Softnix KB Intelligence" reach the API and bounce back as a raw 422.
export function slugifyConnectorName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 64);
}

// Operation parameter names must match the backend's
// `^[a-zA-Z_][a-zA-Z0-9_.\-]*$` (max 64) — sanitized as the user types, like
// the connector and operation name fields already are, so a pasted "X-Api Key"
// or a leading digit doesn't bounce back as a raw 422 on save. Case, dots and
// dashes are preserved (unlike slugifyConnectorName): header and query
// parameters are sent verbatim and real APIs need "X-Api-Key"/"filter.name".
// Path and body names are stricter — they're substituted into `{placeholder}`
// templates, which only match identifiers (connector_shared.py's
// _IDENTIFIER_PARAM_RE), so a dot or dash there would never be filled in.
export function sanitizeApiParamName(raw: string, location: ApiOperationParam["location"]): string {
  const allowed = location === "path" || location === "body" ? /[^a-zA-Z0-9_]+/g : /[^a-zA-Z0-9_.-]+/g;
  return raw.replace(allowed, "").replace(/^[^a-zA-Z_]+/, "").slice(0, 64);
}

// Best-effort curl-command parser feeding CurlImportPanel below — regex-based
// rather than a full shell tokenizer, tuned for the common case (one quoted
// -H per header, at most one -d/--data flag, one bare URL); doesn't attempt
// exotic shell quoting/variable expansion.
export interface ParsedCurl {
  method: string;
  origin: string;
  path: string;
  query: Record<string, string>;
  headers: Record<string, string>;
  hasBody: boolean;
  // Raw captured -d/--data* content, if any — a literal example payload, not
  // yet a {placeholder} template (see CurlImportPanel.handleParse). "" when
  // hasBody is false, or when the captured value isn't valid JSON.
  body: string;
}

// btoa() throws on any code point above U+00FF. Encode as UTF-8 first, which
// is what RFC 7617 specifies for Basic credentials anyway.
function base64Utf8(input: string): string {
  let binary = "";
  for (const byte of new TextEncoder().encode(input)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

const _URL_TOKEN_RE = /(['"]?)(https?:\/\/[^\s'"]+)\1/;
const _EXPLICIT_URL_FLAG_RE = /(?:^|\s)--url\s+(['"]?)(https?:\/\/[^\s'"]+)\1/;
// Flags whose value can itself contain a URL — a JSON body's "callback_url", a
// Referer header, and so on. Their values must be excluded before searching for
// the request target, or the first URL in the string wins even when it's buried
// inside a -d payload that precedes the real endpoint.
const _VALUE_FLAG_RE =
  /(?:^|\s)(?:--data(?:-urlencode|-binary|-ascii|-raw)?|--user-agent|--referer|--header|--cookie|--output|--form|--user|-H|-d|-F|-e|-A|-b|-o|-u)\s+(?:(['"])[\s\S]*?\1|\S+)/g;

function extractRequestUrl(normalized: string): string | null {
  const explicit = normalized.match(_EXPLICIT_URL_FLAG_RE);
  if (explicit) return explicit[2];
  const stripped = normalized.replace(_VALUE_FLAG_RE, " ");
  const match = stripped.match(_URL_TOKEN_RE) ?? normalized.match(_URL_TOKEN_RE);
  return match ? match[2] : null;
}

export function parseCurlCommand(input: string): ParsedCurl | { error: true } {
  // Collapse shell line-continuations ("\" at end of line — how browser
  // devtools' "Copy as cURL" formats a multi-line command) before matching.
  const normalized = input.replace(/\\\r?\n/g, " ").trim();
  const rawUrl = extractRequestUrl(normalized);
  if (!rawUrl) return { error: true };
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return { error: true };
  }

  const headers: Record<string, string> = {};
  for (const m of normalized.matchAll(/(?:-H|--header)\s+(['"])([\s\S]*?)\1/g)) {
    const colon = m[2].indexOf(":");
    if (colon === -1) continue;
    const key = m[2].slice(0, colon).trim();
    if (key) headers[key] = m[2].slice(colon + 1).trim();
  }
  const userMatch = normalized.match(/(?:-u|--user)\s+(['"]?)([^\s'"]+)\1/);
  if (userMatch && !Object.keys(headers).some((k) => k.toLowerCase() === "authorization")) {
    headers.Authorization = `Basic ${base64Utf8(userMatch[2])}`;
  }
  const cookieMatch = normalized.match(/(?:-b|--cookie)\s+(['"])([\s\S]*?)\1/);
  if (cookieMatch && !Object.keys(headers).some((k) => k.toLowerCase() === "cookie")) {
    headers.Cookie = cookieMatch[2];
  }

  const methodMatch = normalized.match(/(?:-X|--request)\s+(['"]?)([A-Za-z]+)\1/);
  // Only -d/--data/--data-raw/--data-binary carry a request payload we can
  // reuse as-is; --data-urlencode's value is (key=)value, not JSON, and
  // -F/--form is multipart, not a JSON body — neither is captured here.
  const bodyMatch = normalized.match(/(?:^|\s)(?:-d|--data(?:-raw|-binary)?)\s+(['"])([\s\S]*?)\1/);
  // No explicit -X but a body flag present -> curl itself defaults to POST.
  const hasBody = !!bodyMatch;
  const method = (methodMatch ? methodMatch[2] : hasBody ? "POST" : "GET").toUpperCase();

  const query: Record<string, string> = {};
  url.searchParams.forEach((v, k) => {
    query[k] = v;
  });

  return { method, origin: url.origin, path: url.pathname || "/", query, headers, hasBody, body: bodyMatch?.[2] ?? "" };
}

// Headers a captured example carries that describe that one connection rather
// than the request, and so must never be stored and replayed. Backed up by the
// same drop server-side (_CONNECTION_HEADERS in claw/tools/api.py) — filtered
// here too so the user isn't shown env entries that are silently ignored. The
// sharpest one is Content-Length: replayed against a different body it makes
// every call of the connector fail outright.
const _UNIMPORTABLE_HEADERS = new Set([
  "host",
  "content-length",
  // Not hop-by-hop, but equally unreplayable: a devtools paste advertises
  // "br, zstd", which the backend can only decode if the optional codec deps
  // are installed — otherwise the response comes back compressed and the
  // model reads binary garbage under a 200.
  "accept-encoding",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

// Browser devtools' "Copy as cURL" adds a dozen client-fingerprint headers that
// are noise in a server-to-server connector — kept out of the imported env so
// the auth headers that matter aren't buried.
const _DEVTOOLS_NOISE_HEADER_RE = /^(sec-|sec-ch-|dnt$|upgrade-insecure-requests$|priority$|pragma$)/i;

function isImportableHeader(name: string): boolean {
  const lower = name.toLowerCase();
  return !_UNIMPORTABLE_HEADERS.has(lower) && !_DEVTOOLS_NOISE_HEADER_RE.test(lower);
}

// Credential-shaped fields in a pasted request body. Mirrors
// _is_secret_value/_collect_secret_literals in claw/core/connectors.py, which
// keep such a value out of transcripts; this is the other half — telling the
// user it is about to be stored in an operation's body, which (unlike the
// connector's auth env) is not encrypted at rest and has no BODY_* env
// equivalent to move it to. Kept deliberately in step with the backend: key
// matching is head-noun based, not substring, or "author" (-> auth) and
// "token_count" trip the warning on every ordinary paste and users learn to
// ignore it.
const _STRONG_SECRET_WORDS = new Set([
  "secret",
  "password",
  "passwd",
  "passphrase",
  "pwd",
  "pw",
  "pass",
  "credential",
  "credentials",
  "apikey",
  "bearer",
  "otp",
  "pin",
  "salt",
]);
const _SECRET_HEAD_WORDS = new Set([
  ..._STRONG_SECRET_WORDS,
  "key",
  "keys",
  "token",
  "tokens",
  "auth",
  "authorization",
  "cookie",
  "cookies",
  "session",
  "signature",
  "sig",
  "pat",
  "private",
  "access",
]);
// Length and charset alone are not enough: "-" and "_" are in the class, so an
// ordinary slug ("in_progress_review") clears both. Requiring a digit, a
// letter, and either mixed case or an 8-char unbroken alphanumeric run keeps
// human-written slugs from tripping the warning on every paste.
const _TOKEN_CHARSET_RE = /^[A-Za-z0-9_\-.=+]{16,}$/;
const _LONG_ALNUM_RUN_RE = /[A-Za-z0-9]{8,}/;

function valueLooksLikeAToken(value: string): boolean {
  if (!_TOKEN_CHARSET_RE.test(value)) return false;
  if (!/[0-9]/.test(value) || !/[A-Za-z]/.test(value)) return false;
  return /[A-Z]/.test(value) || _LONG_ALNUM_RUN_RE.test(value);
}

function keyNamesASecret(key: string): boolean {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((w) => w.toLowerCase());
  if (!words.length) return false;
  if (_SECRET_HEAD_WORDS.has(words[words.length - 1])) return true;
  return words.some((w) => _STRONG_SECRET_WORDS.has(w));
}

function holdsSecretLiteral(node: unknown, key: string): boolean {
  if (Array.isArray(node)) return node.some((child) => holdsSecretLiteral(child, key));
  if (node !== null && typeof node === "object")
    return Object.entries(node as Record<string, unknown>).some(([k, v]) => holdsSecretLiteral(v, k));
  if (typeof node === "string") return node.length >= 4 && (keyNamesASecret(key) || valueLooksLikeAToken(node));
  // Matches the backend's floor: a number that short can't be a credential,
  // and warning about {"private": 1} would make the notice noise.
  if (typeof node === "number") return String(node).length >= 6 && keyNamesASecret(key);
  return false;
}

export function bodyLooksLikeItHoldsACredential(body: string): boolean {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return false;
  }
  return holdsSecretLiteral(parsed, "");
}

const _NUMERIC_SEGMENT_RE = /^\d+$/;
const _UUID_SEGMENT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Turns a literal example path like "/users/12345" into a reusable template
// "/users/{id}" plus a declared, required path parameter (path parameters
// must be required — see claw/api/connector_shared.py) — the most tedious
// manual step in defining an operation by hand. Only numeric/UUID segments
// are templated; anything else (e.g. "/search") is almost never a resource
// id, so it's left as a literal route segment.
function templatizePath(pathname: string): { path: string; parameters: ApiOperationParam[] } {
  // A pasted API-doc example carries its own placeholders ("/v1/users/{user_id}"),
  // and new URL() percent-encodes the braces on the way in. Left encoded they
  // are an ordinary literal segment: nothing is declared, the backend's
  // placeholder check sees no braces either, and every call the agent makes
  // 404s on a URL that looks right to everyone. Decode them, and fold a
  // human-written name the backend can't accept ("{user-id}") to the
  // identifier it obviously means rather than importing a dead operation.
  const decoded = pathname
    .replace(/%7B/gi, "{")
    .replace(/%7D/gi, "}")
    .replace(/\{([^{}/]+)\}/g, (whole, inner: string) => {
      const name = sanitizeApiParamName(inner, "path");
      return name ? `{${name}}` : whole;
    });
  const taken = new Set([...pathPlaceholderNames(decoded)]);
  let idCount = 0;
  const nextIdName = () => {
    let name: string;
    do {
      idCount += 1;
      name = idCount === 1 ? "id" : `id${idCount}`;
    } while (taken.has(name));
    taken.add(name);
    return name;
  };
  const path =
    decoded
      .split("/")
      .map((seg) =>
        _NUMERIC_SEGMENT_RE.test(seg) || _UUID_SEGMENT_RE.test(seg) ? `{${nextIdName()}}` : seg
      )
      .join("/") || "/";
  // Declared from the finished path, so a placeholder that came in with the
  // paste is treated exactly like one this function generated — the backend
  // requires the two sets to match exactly.
  const parameters: ApiOperationParam[] = [];
  const declared = new Set<string>();
  for (const name of pathPlaceholderNames(path)) {
    if (declared.has(name)) continue;
    declared.add(name);
    parameters.push({
      name,
      location: "path",
      // Always "string", never "number", even for an all-digit segment. A
      // URL path segment is an opaque string: typing it as a number tells
      // the model to send JSON numbers, which drops leading zeros
      // ("/users/007" becomes /users/7) and rounds any id past 2^53 to a
      // different id — both silently hitting the wrong record.
      type: "string",
      required: true,
      description: "",
    });
  }
  return { path, parameters };
}

// Mirrors _PATH_PLACEHOLDER_RE in claw/api/connector_shared.py.
function* pathPlaceholderNames(text: string): Generator<string> {
  for (const m of text.matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g)) yield m[1];
}

function toastLines(lines: string[]) {
  if (lines.length === 1) return lines[0];
  return (
    <div className="claw-toast-lines">
      {lines.map((line, i) => (
        <span key={i}>{line}</span>
      ))}
    </div>
  );
}

function suggestOperationName(method: string, pathname: string, existing: string[]): string {
  const literalSegments = pathname
    .split("/")
    .filter((s) => s && !_NUMERIC_SEGMENT_RE.test(s) && !_UUID_SEGMENT_RE.test(s));
  const base =
    slugifyConnectorName(`${method.toLowerCase()}_${literalSegments[literalSegments.length - 1] || "request"}`)
      .replace(/-/g, "_")
      .slice(0, 49) || "operation";
  let name = base;
  let n = 2;
  while (existing.includes(name)) {
    name = `${base.slice(0, 46)}_${n}`;
    n += 1;
  }
  return name;
}

const _KNOWN_OPERATION_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

// A second way to define an API operation besides filling the form by hand —
// paste a real example request (e.g. browser devtools' "Copy as cURL", or an
// API doc's example call) and get a pre-filled operation, base URL, and auth
// env in one step. Shared by ConnectorsPanel and Admin.tsx's
// PrebuiltConnectorsPanel exactly like ApiOperationsEditor above.
export function CurlImportPanel({
  operationNames,
  baseUrl,
  env,
  onImport,
}: {
  operationNames: string[];
  // The connector as it stands right now. Needed, not just cosmetic: an
  // imported operation is executed against this base URL with these stored
  // credentials, so both have to be reconciled with the paste before merging
  // (see handleParse).
  baseUrl: string;
  env: Record<string, string>;
  onImport: (result: { origin: string; envAdditions: Record<string, string>; operation: ApiOperation }) => void;
}) {
  const t = useT();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");

  const handleParse = () => {
    // Arbitrary pasted text is a boundary — a parse failure must surface as a
    // toast, never as an unhandled exception in the click handler.
    let parsed: ParsedCurl | { error: true };
    try {
      parsed = parseCurlCommand(text);
    } catch {
      parsed = { error: true };
    }
    if ("error" in parsed) {
      toast({ body: t("settings.connectors.curlImportError"), type: "error" });
      return;
    }
    // Reconcile the paste with the base URL the connector already has. An
    // operation is only ever sent to that base URL, carrying that connector's
    // stored credentials, so a paste aimed at a different origin can't be
    // merged in: the request would go to the configured vendor at a path that
    // only exists on the pasted one, with the configured vendor's auth
    // attached. Refuse rather than silently retarget it.
    const currentBase = baseUrl.trim();
    let basePath = "";
    if (currentBase) {
      let base: URL | null = null;
      try {
        base = new URL(currentBase);
      } catch {
        base = null; // half-typed URL — nothing to reconcile against yet
      }
      if (base) {
        if (base.origin !== parsed.origin) {
          toast({
            body: t("settings.connectors.curlImportOriginMismatch", { base: base.origin, pasted: parsed.origin }),
            type: "error",
          });
          return;
        }
        basePath = base.pathname.replace(/\/+$/, "");
      }
    }
    // The base URL's own path prefix is prepended at call time, so keeping it
    // in the operation path too yields "/v1/v1/users/42".
    let requestPath = parsed.path;
    if (basePath) {
      if (requestPath !== basePath && !requestPath.startsWith(`${basePath}/`)) {
        // Not under the base path, so there is nothing to strip — and keeping
        // it whole means the call goes to basePath + requestPath, e.g. base
        // "/v1" plus a pasted "/v2/reports" hits "/v1/v2/reports". That URL
        // usually 404s, but it can just as easily be a real, different
        // endpoint. Same reasoning as the origin check above: refuse rather
        // than silently retarget.
        toast({
          body: t("settings.connectors.curlImportBasePathMismatch", { base: basePath, pasted: requestPath }),
          type: "error",
        });
        return;
      }
      requestPath = requestPath.slice(basePath.length) || "/";
    }
    const { path, parameters } = templatizePath(requestPath);
    // Headers/query values in a captured example are almost always static
    // config (auth tokens, api keys) — import them the same way manual
    // setup already does (HEADER_*/QUERY_* env, see parseEnvText above), not
    // as per-call operation parameters. A user who does want one of these to
    // vary per call can move it into the operation's declared parameters
    // afterward, same as any hand-authored operation.
    // HEADER_* keys match case-insensitively because HTTP header names are:
    // HEADER_authorization next to HEADER_Authorization is two keys for one
    // header, which the backend rejects on save. QUERY_* stays exact — query
    // strings are case-sensitive.
    const matchingKey = (keys: string[], key: string): string | undefined => {
      if (keys.includes(key)) return key;
      if (!key.startsWith("HEADER_")) return undefined;
      const lowered = key.toLowerCase();
      return keys.find((k) => k.toLowerCase() === lowered);
    };
    const envAdditions: Record<string, string> = {};
    // env is connector-wide, so a second paste would otherwise overwrite the
    // first one's tenant id or api key without a word — and the value it
    // replaces may be the only copy the user has. Existing values win; the
    // user is told which ones differed so they can reconcile by hand.
    const collisions: string[] = [];
    // The same fold applies within one paste: a single command can carry the
    // same header twice in different cases (curl sends both, the server reads
    // one), and letting both through produced an unexplained 422 from the
    // backend's duplicate-header check on save.
    const duplicateHeaders: string[] = [];
    // One verdict per pasted value, decided against the saved env first: doing
    // it in two passes let one header be reported as both kept and discarded,
    // and could drop a differing value before it was ever compared to env.
    const offer = (key: string, value: string) => {
      const saved = matchingKey(Object.keys(env), key);
      if (saved !== undefined) {
        // Identical values need no telling; only a real disagreement does,
        // because then the discarded one may be the credential that works.
        if (env[saved] !== value) collisions.push(saved);
        return;
      }
      const pending = matchingKey(Object.keys(envAdditions), key);
      if (pending !== undefined) {
        if (envAdditions[pending] !== value) duplicateHeaders.push(pending);
        return;
      }
      envAdditions[key] = value;
    };
    for (const [k, v] of Object.entries(parsed.headers)) {
      if (isImportableHeader(k)) offer(`HEADER_${k}`, v);
    }
    for (const [k, v] of Object.entries(parsed.query)) offer(`QUERY_${k}`, v);
    const storedQueryKeys = Object.keys(envAdditions)
      .filter((k) => k.startsWith("QUERY_"))
      .map((k) => k.slice("QUERY_".length));
    const name = suggestOperationName(parsed.method, requestPath, operationNames);
    const method = _KNOWN_OPERATION_METHODS.has(parsed.method) ? (parsed.method as ApiOperation["method"]) : "GET";
    // The captured body is a literal example payload (real values, no
    // {placeholder} tokens) — usable as-is only when it's actually valid
    // JSON and the method carries a payload; -F/--form and
    // --data-urlencode content is deliberately never captured (see
    // parseCurlCommand), and non-JSON -d content (e.g. raw form-encoded
    // text) can't be reused as a JSON body template either.
    let body = "";
    let bodyImported = true;
    let bodyPlaceholders: string[] = [];
    if (parsed.hasBody && (method === "POST" || method === "PUT" || method === "PATCH")) {
      // A literal {word} in the pasted payload — e.g. {"text": "Hello {name}"}
      // from a notification API's example — collides with the body-template
      // placeholder syntax. Importing it verbatim makes the operation
      // unsaveable: the backend demands a declared body parameter per
      // placeholder, and declaring one substitutes json.dumps() INSIDE the
      // surrounding quotes, which then fails the JSON check. Neither error
      // tells the user what to do, so drop the body and say so instead.
      bodyPlaceholders = [...parsed.body.matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g)].map((m) => m[1]);
      if (bodyPlaceholders.length) {
        bodyImported = false;
      } else {
        try {
          JSON.parse(parsed.body);
          body = parsed.body;
        } catch {
          bodyImported = false;
        }
      }
    } else if (parsed.hasBody) {
      bodyImported = false;
    }
    const operation: ApiOperation = { name, method, path, description: "", parameters, body };
    onImport({ origin: parsed.origin, envAdditions, operation });
    toast({ body: t("settings.connectors.curlImportSuccess", { name }), type: "info", autoHideDuration: 3000 });
    // One import can raise up to five follow-ups; the toast viewport only keeps
    // the newest few, so they are grouped into one toast per severity instead of
    // pushing each other — and the success message — out of view.
    const problems: string[] = [];
    const notices: string[] = [];
    if (parsed.hasBody && !bodyImported) {
      // Not a failure — the operation above was still added successfully —
      // just a heads-up that the request body couldn't be reused verbatim
      // and needs to be added by hand in the operation's body field.
      notices.push(
        bodyPlaceholders.length
          ? t("settings.connectors.curlImportBodyPlaceholderWarning", {
              keys: [...new Set(bodyPlaceholders)].join(", "),
            })
          : t("settings.connectors.curlImportBodyWarning")
      );
    }
    if (body && bodyLooksLikeItHoldsACredential(body)) {
      problems.push(t("settings.connectors.curlImportBodySecretWarning"));
    }
    // Deduped: three case variants of one header name it twice over, and the
    // key is what the user has to go find — repeating it reads as two problems.
    if (duplicateHeaders.length) {
      problems.push(
        t("settings.connectors.curlImportDuplicateHeader", {
          keys: [...new Set(duplicateHeaders)].join(", "),
        })
      );
    }
    if (collisions.length) {
      problems.push(
        t("settings.connectors.curlImportEnvCollision", {
          keys: [...new Set(collisions)].join(", "),
        })
      );
    }
    if (storedQueryKeys.length) {
      notices.push(
        t("settings.connectors.curlImportQueryNotice", { keys: storedQueryKeys.join(", ") })
      );
    }
    if (problems.length) toast({ body: toastLines(problems), type: "error" });
    if (notices.length) toast({ body: toastLines(notices), type: "info" });
    setText("");
    setOpen(false);
  };

  return (
    <div className="claw-curl-import">
      <button
        type="button"
        className="claw-curl-import-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <Icon icon={open ? ChevronDown : ChevronRight} size="xsm" color="secondary" />
        <Text size="sm" color="secondary">
          {t("settings.connectors.curlImportToggle")}
        </Text>
      </button>
      {open && (
        <div className="claw-curl-import-body">
          <TextArea
            label={t("settings.connectors.curlImportLabel")}
            description={t("settings.connectors.curlImportDesc")}
            placeholder="curl https://api.example.com/users/123 -H 'Authorization: Bearer TOKEN'"
            value={text}
            onChange={setText}
            rows={3}
          />
          <div className="claw-row">
            <Button
              label={t("settings.connectors.curlImportAction")}
              size="sm"
              variant="secondary"
              clickAction={handleParse}
              isDisabled={!text.trim()}
            />
          </div>
        </div>
      )}
    </div>
  );
}

const EMPTY_API_OPERATION: ApiOperation = {
  name: "",
  method: "GET",
  path: "/",
  description: "",
  parameters: [],
  body: "",
};
const EMPTY_API_PARAM: ApiOperationParam = {
  name: "",
  location: "query",
  type: "string",
  required: false,
  description: "",
};

// Repeatable operation/parameter list for a kind="api" connector — shared by
// ConnectorsPanel's (per-user) and Admin.tsx's PrebuiltConnectorsPanel's
// (admin-global) "Add custom" forms, since both save the same ConnectorInfo
// shape via api.saveConnector/admin.connectors.save.
export function ApiOperationsEditor({
  operations,
  onChange,
}: {
  operations: ApiOperation[];
  onChange: (ops: ApiOperation[]) => void;
}) {
  const t = useT();

  const updateOp = (i: number, patch: Partial<ApiOperation>) => {
    onChange(operations.map((op, idx) => (idx === i ? { ...op, ...patch } : op)));
  };
  const removeOp = (i: number) => onChange(operations.filter((_, idx) => idx !== i));
  const addOp = () => onChange([...operations, { ...EMPTY_API_OPERATION }]);

  const updateParam = (opIdx: number, paramIdx: number, patch: Partial<ApiOperationParam>) => {
    updateOp(opIdx, {
      parameters: operations[opIdx].parameters.map((p, idx) => (idx === paramIdx ? { ...p, ...patch } : p)),
    });
  };
  const removeParam = (opIdx: number, paramIdx: number) => {
    updateOp(opIdx, { parameters: operations[opIdx].parameters.filter((_, idx) => idx !== paramIdx) });
  };
  const addParam = (opIdx: number) => {
    updateOp(opIdx, { parameters: [...operations[opIdx].parameters, { ...EMPTY_API_PARAM }] });
  };

  return (
    <div className="claw-api-operations">
      <Text type="label">{t("settings.connectors.operationsLabel")}</Text>
      {operations.length === 0 && (
        <Text size="sm" color="secondary">
          {t("settings.connectors.operationsEmpty")}
        </Text>
      )}
      {operations.map((op, i) => (
        <Card key={i} className="claw-api-operation-row">
          <div className="claw-row claw-row-between">
            <TextInput
              label={t("settings.connectors.operationNameLabel")}
              placeholder="get_user"
              value={op.name}
              onChange={(v) => updateOp(i, { name: slugifyConnectorName(v).replace(/-/g, "_") })}
            />
            <Button
              variant="ghost"
              size="sm"
              icon={<Icon icon={Trash2} size="sm" />}
              clickAction={() => removeOp(i)}
              label={t("settings.connectors.removeOperation")}
            />
          </div>
          <SegmentedControl
            value={op.method}
            onChange={(v) => updateOp(i, { method: v as ApiOperation["method"] })}
            label={t("settings.connectors.operationMethodLabel")}
          >
            <SegmentedControlItem value="GET" label="GET" />
            <SegmentedControlItem value="POST" label="POST" />
            <SegmentedControlItem value="PUT" label="PUT" />
            <SegmentedControlItem value="PATCH" label="PATCH" />
            <SegmentedControlItem value="DELETE" label="DELETE" />
          </SegmentedControl>
          <TextInput
            label={t("settings.connectors.operationPathLabel")}
            placeholder="/users/{id}"
            value={op.path}
            onChange={(v) => updateOp(i, { path: v })}
          />
          <TextArea
            label={t("settings.connectors.operationDescLabel")}
            value={op.description}
            onChange={(v) => updateOp(i, { description: v })}
            rows={1}
          />
          {(op.method === "POST" || op.method === "PUT" || op.method === "PATCH") && (
            <TextArea
              label={t("settings.connectors.operationBodyLabel")}
              description={t("settings.connectors.operationBodyDesc")}
              placeholder={'{"limit": {limit}}'}
              value={op.body}
              onChange={(v) => updateOp(i, { body: v })}
              rows={3}
            />
          )}
          <Divider />
          <Text type="label" size="sm">
            {t("settings.connectors.parametersLabel")}
          </Text>
          {op.parameters.map((p, pi) => (
            <div key={pi} className="claw-row claw-api-param-row">
              <TextInput
                label={t("settings.connectors.paramNameLabel")}
                value={p.name}
                onChange={(v) => updateParam(i, pi, { name: sanitizeApiParamName(v, p.location) })}
              />
              <SegmentedControl
                value={p.location}
                onChange={(v) => {
                  const location = v as ApiOperationParam["location"];
                  // A path or body parameter that's missing from a call
                  // silently substitutes as "" (path) or a literal "{name}"
                  // token (body), corrupting the request (see
                  // claw/tools/api.py) — the backend rejects either as
                  // required=false, so force it here too.
                  updateParam(
                    i,
                    pi,
                    location === "path" || location === "body"
                      ? // Also re-narrows the name: "filter.name" is a valid
                        // header/query parameter but never a path/body one.
                        { location, required: true, name: sanitizeApiParamName(p.name, location) }
                      : { location },
                  );
                }}
                label={t("settings.connectors.paramLocationLabel")}
              >
                <SegmentedControlItem value="path" label={t("settings.connectors.paramLocationPath")} />
                <SegmentedControlItem value="query" label={t("settings.connectors.paramLocationQuery")} />
                <SegmentedControlItem value="header" label={t("settings.connectors.paramLocationHeader")} />
                <SegmentedControlItem value="body" label={t("settings.connectors.paramLocationBody")} />
              </SegmentedControl>
              <SegmentedControl
                value={p.type}
                onChange={(v) => updateParam(i, pi, { type: v as ApiOperationParam["type"] })}
                label={t("settings.connectors.paramTypeLabel")}
              >
                <SegmentedControlItem value="string" label="string" />
                <SegmentedControlItem value="number" label="number" />
                <SegmentedControlItem value="boolean" label="boolean" />
              </SegmentedControl>
              <Switch
                value={p.required}
                label={t("settings.connectors.paramRequiredLabel")}
                changeAction={(checked) => updateParam(i, pi, { required: checked })}
                isDisabled={p.location === "path" || p.location === "body"}
              />
              <Button
                variant="ghost"
                size="sm"
                icon={<Icon icon={Trash2} size="sm" />}
                clickAction={() => removeParam(i, pi)}
                label={t("settings.connectors.removeParameter")}
                isIconOnly
              />
            </div>
          ))}
          <Button
            label={t("settings.connectors.addParameter")}
            variant="secondary"
            size="sm"
            clickAction={() => addParam(i)}
          />
        </Card>
      ))}
      <Button
        label={t("settings.connectors.addOperation")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={Plus} size="sm" />}
        clickAction={addOp}
      />
    </div>
  );
}

function ConnectorsPanel() {
  const t = useT();
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [globalConnectors, setGlobalConnectors] = useState<ConnectorGlobalSummary[]>([]);
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
    api.listGlobalConnectors().then(setGlobalConnectors).catch(() => setGlobalConnectors([]));
  }, [reload]);

  const installedByName = new Map(connectors.map((c) => [c.name.toLowerCase(), c]));

  // Group presets into ordered categories for the catalog grid.
  const categoryOrder = [
    "Productivity",
    "Communication",
    "CRM",
    "Search",
    "Finance",
    "Automation",
    "Softnix",
    "Other",
  ];
  const grouped = new Map<string, ConnectorPreset[]>();
  for (const p of presets) {
    // Kept as the raw English literal (not t()) so it stays a stable grouping/sort
    // key and matches categoryOrder below — sibling categories (Productivity, etc.)
    // are backend-provided and untranslated for the same reason.
    const cat = p.category || "Other";
    (grouped.get(cat) ?? grouped.set(cat, []).get(cat)!).push(p);
  }
  const categories = [...grouped.keys()].sort(
    (a, b) => (categoryOrder.indexOf(a) + 1 || 99) - (categoryOrder.indexOf(b) + 1 || 99),
  );

  if (editing) {
    const transport = editing.transport ?? "http";
    const kind = editing.kind ?? "mcp";
    return (
      <div className="claw-panel">
        <TextInput
          ref={nameInputRef}
          label={t("settings.connectors.nameLabel")}
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
          label={t("settings.connectors.descOptional")}
          value={editing.description ?? ""}
          onChange={(v) => setEditing({ ...editing, description: v })}
          rows={2}
        />
        {/* Existing connectors keep their kind fixed — switching an already-
            saved connector between "speaks MCP" and "plain REST" would orphan
            whatever's already registered under its current tool names. */}
        {!editing.id && (
          <div className="claw-row">
            <Button
              label={t("settings.connectors.kindMcp")}
              size="sm"
              variant={kind === "mcp" ? "primary" : "secondary"}
              clickAction={() => setEditing({ ...editing, kind: "mcp" })}
            />
            <Button
              label={t("settings.connectors.kindApi")}
              size="sm"
              variant={kind === "api" ? "primary" : "secondary"}
              clickAction={() => setEditing({ ...editing, kind: "api", transport: "http" })}
            />
          </div>
        )}
        {kind === "api" ? (
          <>
            <TextInput
              label={t("settings.connectors.apiBaseUrlLabel")}
              value={editing.url ?? ""}
              onChange={(v) => setEditing({ ...editing, url: v })}
            />
            <CurlImportPanel
              operationNames={(editing.operations ?? []).map((op) => op.name)}
              baseUrl={editing.url ?? ""}
              env={editing.env ?? {}}
              onImport={({ origin, envAdditions, operation }) =>
                setEditing({
                  ...editing,
                  // Never overwrite a base URL the user already set — only
                  // prefill it the first time (e.g. before any operation exists).
                  url: editing.url && editing.url.trim() ? editing.url : origin,
                  env: { ...(editing.env ?? {}), ...envAdditions },
                  operations: [...(editing.operations ?? []), operation],
                })
              }
            />
            <ApiOperationsEditor
              operations={editing.operations ?? []}
              onChange={(operations) => setEditing({ ...editing, operations })}
            />
          </>
        ) : (
          <>
            <div className="claw-row">
              {/* stdio spawns a real subprocess on the server (unsandboxed) —
                  only an admin may pick it for a NEW connector. An existing
                  stdio connector (a built-in preset the user installed)
                  still shows so they can manage it, but see the disabled
                  Command field below. */}
              {(isAdmin || transport === "stdio") && (
                <Button
                  label={t("settings.connectors.transportStdio")}
                  size="sm"
                  variant={transport === "stdio" ? "primary" : "secondary"}
                  clickAction={() => setEditing({ ...editing, transport: "stdio" })}
                />
              )}
              <Button
                label={t("settings.connectors.transportHttp")}
                size="sm"
                variant={transport === "http" ? "primary" : "secondary"}
                clickAction={() => setEditing({ ...editing, transport: "http" })}
              />
            </div>
            {transport === "stdio" ? (
              <>
                <TextInput
                  label={t("settings.connectors.commandLabel")}
                  value={editing.command ?? ""}
                  onChange={(v) => setEditing({ ...editing, command: v })}
                  isDisabled={!isAdmin}
                />
                {!isAdmin && (
                  <Text size="sm" color="secondary">
                    {t("settings.connectors.commandAdminOnly")}
                  </Text>
                )}
              </>
            ) : (
              <TextInput
                label={t("settings.connectors.urlLabel")}
                value={editing.url ?? ""}
                onChange={(v) => setEditing({ ...editing, url: v })}
              />
            )}
          </>
        )}
        <TextArea
          label={t("settings.connectors.envLabel")}
          value={formatEnvText(editing.env)}
          onChange={(v) => setEditing({ ...editing, env: parseEnvText(v) })}
          rows={3}
        />
        <TextInput
          label={t("settings.connectors.timeoutLabel")}
          placeholder={t("settings.connectors.timeoutPlaceholder")}
          description={t("settings.connectors.timeoutDesc")}
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
          label={t("settings.connectors.enableServer")}
          changeAction={(checked) => setEditing({ ...editing, enabled: checked })}
        />
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label={t("settings.connectors.saveConnector")}
            icon={<Icon icon="check" size="sm" />}
            clickAction={() =>
              guard(async () => {
                const duplicates = duplicateHeaderKeys(editing.env ?? {});
                if (duplicates.length) {
                  throw new Error(
                    t("settings.connectors.duplicateHeaderKeys", { keys: duplicates.join("; ") })
                  );
                }
                await api.saveConnector({
                  name: (editing.name ?? "").trim(),
                  description: editing.description ?? "",
                  kind,
                  transport,
                  command: editing.command ?? "",
                  url: editing.url ?? "",
                  env: editing.env ?? {},
                  operations: editing.operations ?? [],
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
          <Button label={t("settings.common.cancel")} variant="ghost" clickAction={() => setEditing(null)} />
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
        <Text color="secondary">{t("settings.connectors.intro")}</Text>
        <Button
          label={t("settings.connectors.addCustom")}
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
                  ? [{ label: t("settings.connectors.viewDocs"), icon: ExternalLink, onClick: () => window.open(p.docs, "_blank", "noopener") }]
                  : []),
                ...(installed
                  ? [
                      { label: t("settings.common.edit"), icon: Pencil, onClick: () => setEditing(installed) },
                      { type: "divider" as const },
                      {
                        label: t("settings.connectors.remove"),
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
                          label={t("settings.connectors.toolsCount", { count: String(installed.runtime.tools ?? 0) })}
                        />
                      ) : installed?.runtime.status === "error" ? (
                        <Badge variant="error" icon={<Icon icon="error" size="xsm" />} label={t("settings.connectors.error")} />
                      ) : (
                        (() => {
                          const hint = presetAuthHint(p, t);
                          return hint ? <span className="claw-connector-auth">{hint}</span> : null;
                        })()
                      )}
                    </div>
                  </div>
                  <div className="claw-connector-actions">
                    {installed ? (
                      <Button
                        label={t("settings.connectors.manage")}
                        size="sm"
                        variant="secondary"
                        clickAction={() => setEditing(installed)}
                      />
                    ) : (
                      <Button
                        label={t("settings.connectors.add")}
                        size="sm"
                        variant="primary"
                        clickAction={() => setSetupPreset(p)}
                      />
                    )}
                    {menuItems.length > 0 && (
                      <MoreMenu label={t("settings.connectors.optionsFor", { label: p.label })} size="sm" items={menuItems} />
                    )}
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
            {t("settings.connectors.yourConnectors")}
          </Text>
          <Divider />
          {connectors.map((c) => (
            <Card key={c.id} padding={2}>
              <div className="claw-row claw-row-between">
                <div>
                  <div className="claw-row">
                    <Text weight="semibold">{c.name}</Text>
                    <Badge variant="neutral" label={c.kind === "api" ? "api" : c.transport} />
                    {c.runtime.status === "connected" && (
                      <Badge
                        variant="success"
                        icon={<Icon icon="check" size="xsm" />}
                        label={t("settings.connectors.toolsCount", { count: String(c.runtime.tools) })}
                      />
                    )}
                    {c.runtime.status === "error" && (
                      <Badge variant="error" icon={<Icon icon="error" size="xsm" />} label={t("settings.connectors.error")} />
                    )}
                  </div>
                  <Text size="sm" color="secondary" as="p">
                    {c.kind === "api" ? c.url : c.transport === "stdio" ? c.command : c.url}
                  </Text>
                  {(c.runtime.tool_names?.length ?? 0) > 0 && (
                    <ConnectorToolNames names={c.runtime.tool_names!} />
                  )}
                  {(c.runtime.shadowed_tools?.length ?? 0) > 0 && (
                    <ConnectorShadowedTools names={c.runtime.shadowed_tools!} />
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
                    label={t("settings.common.enable", { name: c.name })}
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
                    label={t("settings.common.edit")}
                    icon={<Icon icon={Pencil} size="sm" />}
                    size="sm"
                    variant="ghost"
                    clickAction={() => setEditing(c)}
                  />
                  <Button
                    label={t("settings.common.delete")}
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

      {globalConnectors.length > 0 && (
        <div className="claw-connector-category">
          <Text type="label" color="secondary" className="claw-connector-cat-title">
            {t("settings.connectors.globalTitle")}
          </Text>
          <Divider />
          <Text size="sm" color="secondary" as="p">
            {t("settings.connectors.globalDesc")}
          </Text>
          {globalConnectors.map((c) => (
            <Card key={c.id} padding={2}>
              <div className="claw-row">
                <Text weight="semibold">{c.name}</Text>
                <Badge variant="neutral" label={c.kind === "api" ? "api" : c.transport} />
                {c.runtime.status === "connected" && (
                  <Badge
                    variant="success"
                    icon={<Icon icon="check" size="xsm" />}
                    label={t("settings.connectors.toolsCount", { count: String(c.runtime.tools ?? 0) })}
                  />
                )}
                {c.runtime.status === "error" && (
                  <Badge variant="error" icon={<Icon icon="error" size="xsm" />} label={t("settings.connectors.error")} />
                )}
              </div>
              {c.description && (
                <Text size="sm" color="secondary" as="p">
                  {c.description}
                </Text>
              )}
              {(c.runtime.tool_names?.length ?? 0) > 0 && <ConnectorToolNames names={c.runtime.tool_names!} />}
              {(c.runtime.shadowed_tools?.length ?? 0) > 0 && (
                <ConnectorShadowedTools names={c.runtime.shadowed_tools!} />
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Schedules

function SchedulesPanel() {
  const t = useT();
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
          label={t("settings.schedules.name")}
          value={editing.name ?? ""}
          onChange={(v) => setEditing({ ...editing, name: v })}
        />
        <TextArea
          label={t("settings.schedules.prompt")}
          value={editing.prompt ?? ""}
          onChange={(v) => setEditing({ ...editing, prompt: v })}
          rows={4}
        />
        <TextInput
          label={t("settings.schedules.cron")}
          value={editing.cron ?? ""}
          onChange={(v) => setEditing({ ...editing, cron: v })}
        />
        <TextInput
          label={t("settings.schedules.interval")}
          value={String(Math.round((editing.interval_seconds ?? 0) / 60))}
          onChange={(v) =>
            setEditing({ ...editing, interval_seconds: Math.max(0, parseInt(v) || 0) * 60 })
          }
        />
        {error && <ErrorText>{error}</ErrorText>}
        <div className="claw-row">
          <Button
            label={t("settings.schedules.save")}
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
          <Button label={t("settings.common.cancel")} variant="ghost" clickAction={() => setEditing(null)} />
        </div>
      </div>
    );
  }

  return (
    <div className="claw-panel">
      <div className="claw-row claw-row-between">
        <Text color="secondary">{t("settings.schedules.intro")}</Text>
        <Button
          label={t("settings.schedules.new")}
          icon={<Icon icon={Plus} size="sm" />}
          size="sm"
          clickAction={() => setEditing({ enabled: true, interval_seconds: 0 })}
        />
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {schedules.length === 0 ? (
        <EmptyState
          title={t("settings.schedules.emptyTitle")}
          description={t("settings.schedules.emptyDesc")}
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
                      (s.interval_seconds
                        ? t("settings.schedules.every", { minutes: String(Math.round(s.interval_seconds / 60)) })
                        : t("settings.schedules.once"))
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
                  {s.next_run_at
                    ? t("settings.schedules.nextRun", { time: new Date(s.next_run_at).toLocaleString() })
                    : ""}
                </Text>
              </div>
              <div className="claw-row">
                <Switch
                  value={s.enabled}
                  label={t("settings.common.enable", { name: s.name })}
                  isLabelHidden
                  changeAction={(checked) =>
                    guard(async () => {
                      await api.updateSchedule(s.id, { ...s, enabled: checked });
                      await reload();
                    })
                  }
                />
                <Button
                  label={t("settings.schedules.runNow")}
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
                  label={t("settings.common.edit")}
                  icon={<Icon icon={Pencil} size="sm" />}
                  size="sm"
                  variant="ghost"
                  clickAction={() => setEditing(s)}
                />
                <Button
                  label={t("settings.common.delete")}
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

// `labelKey`, not a pre-resolved label — this array is module-level (can't
// call useT()); HeartbeatPanel resolves it via t(p.labelKey) at render time.
const HEARTBEAT_PRESETS = [
  { labelKey: "settings.heartbeat.off", minutes: 0 },
  { labelKey: "settings.heartbeat.every30", minutes: 30 },
  { labelKey: "settings.heartbeat.hourly", minutes: 60 },
  { labelKey: "settings.heartbeat.every4h", minutes: 240 },
  { labelKey: "settings.heartbeat.daily", minutes: 1440 },
];

function HeartbeatPanel() {
  const t = useT();
  const [state, setState] = useState<{ interval_minutes: number; enabled: boolean; next_run_at: string | null } | null>(
    null,
  );
  const { error, guard } = useAsyncError();

  useEffect(() => {
    api.getHeartbeat().then(setState);
  }, []);

  if (!state) return <Text color="secondary">{t("settings.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("settings.heartbeat.intro")}</Text>
      <div className="claw-row">
        {HEARTBEAT_PRESETS.map((p) => (
          <Button
            key={p.minutes}
            label={t(p.labelKey)}
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
            label={t("settings.heartbeat.onEvery", { minutes: String(state.interval_minutes) })}
          />
        ) : (
          <Badge variant="neutral" label={t("settings.heartbeat.off")} />
        )}
        {state.next_run_at && (
          <Text size="sm" color="secondary">
            {t("settings.heartbeat.nextCheckIn", { time: new Date(state.next_run_at).toLocaleString() })}
          </Text>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Telegram

function TelegramPanel() {
  const t = useT();
  const [status, setStatus] = useState<{ enabled: boolean; linked: boolean; bot_username: string } | null>(
    null,
  );
  const [code, setCode] = useState("");
  const { error, guard } = useAsyncError();

  const reload = useCallback(() => api.getTelegramStatus().then(setStatus), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  if (!status) return <Text color="secondary">{t("settings.common.loading")}</Text>;

  if (!status.enabled) {
    return (
      <div className="claw-panel">
        <Text color="secondary">{t("settings.telegram.notSetUp")}</Text>
      </div>
    );
  }

  const botHandle = status.bot_username ? `@${status.bot_username}` : t("settings.telegram.theBot");
  const botUrl = status.bot_username ? `https://t.me/${status.bot_username}` : "";

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("settings.telegram.intro")}</Text>
      <div className="claw-row">
        {status.linked ? (
          <Badge variant="success" icon={<Icon icon="check" size="xsm" />} label={t("settings.telegram.linked")} />
        ) : (
          <Badge variant="neutral" label={t("settings.telegram.notLinked")} />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {status.linked ? (
        <Button
          label={t("settings.telegram.unlink")}
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
            <Text weight="semibold">{t("settings.telegram.stepsTitle")}</Text>
            <ol className="claw-telegram-steps">
              <li>
                {t("settings.telegram.step1Prefix", { bot: botHandle })}
                <strong>Start</strong>
                {t("settings.telegram.step1Suffix")}
              </li>
              <li>{t("settings.telegram.step2")}</li>
              <li>{t("settings.telegram.step3")}</li>
            </ol>
          </Card>
          <div className="claw-row">
            {botUrl && (
              <Button
                label={t("settings.telegram.openInTelegram")}
                icon={<Icon icon={ExternalLink} size="sm" />}
                variant="secondary"
                href={botUrl}
                target="_blank"
                rel="noopener noreferrer"
              />
            )}
            <Button
              label={t("settings.telegram.generateCode")}
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
              <Text weight="semibold">{t("settings.telegram.yourCode")}</Text>
              <Text type="display-3">{code}</Text>
              <Text size="sm" color="secondary" as="p">
                {t("settings.telegram.sendAsMessage", { bot: botHandle })}
              </Text>
              <Text type="code">/link {code}</Text>
              <Text size="sm" color="secondary" as="p">
                {t("settings.telegram.expires")}
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
  const t = useT();
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
        <Text color="secondary">{t("settings.browserExt.mobileNotice")}</Text>
      </div>
    );
  }

  if (!status) return <Text color="secondary">{t("settings.common.loading")}</Text>;

  if (!status.client_extension_enabled) {
    return (
      <div className="claw-panel">
        <Text color="secondary">{t("settings.browserExt.notEnabled")}</Text>
      </div>
    );
  }

  const pairingText = pairing
    ? `Admin API: ${pairing.api_base}\nInstance: ${pairing.instance_id}\nTicket: ${pairing.pairing_ticket}`
    : "";

  return (
    <div className="claw-panel">
      <Text color="secondary">{t("settings.browserExt.intro")}</Text>

      <div className="claw-row">
        {status.paired ? (
          <Badge
            variant="success"
            icon={<Icon icon="check" size="xsm" />}
            label={t("settings.browserExt.paired")}
          />
        ) : (
          <Badge variant="neutral" label={t("settings.browserExt.notPaired")} />
        )}
        {status.paired &&
          (status.online ? (
            <Badge variant="success" label={t("settings.browserExt.online")} />
          ) : (
            <Badge variant="warning" label={t("settings.browserExt.offline")} />
          ))}
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      <Card padding={2} variant="muted">
        <Text weight="semibold">{t("settings.browserExt.step1Title")}</Text>
        <Text size="sm" color="secondary" as="p">
          {t("settings.browserExt.step1Desc")}
        </Text>
        <div className="claw-row">
          <Button
            label={t("settings.browserExt.download")}
            icon={<Icon icon={Download} size="sm" />}
            clickAction={() => {
              window.location.href = "/api/browser-extension/download";
            }}
          />
        </div>
      </Card>

      <Card padding={2} variant="muted">
        <Text weight="semibold">{t("settings.browserExt.step2Title")}</Text>
        <Text size="sm" color="secondary" as="p">
          {t("settings.browserExt.step2Desc")}
        </Text>
        <div className="claw-row">
          <Button
            label={t("settings.browserExt.generateDetails")}
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
                label={copied ? t("settings.browserExt.copied") : t("settings.browserExt.copyDetails")}
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
          label={t("settings.browserExt.unpair")}
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
  const t = useT();
  const otherGroups = groups.filter((g) => g.id !== myGroupId);
  return (
    <div className="claw-field-group">
      <Text size="sm" color="secondary">
        {t("settings.knowledge.visibility")}
      </Text>
      <div className="claw-row">
        <Button
          label={t("settings.knowledge.private")}
          size="sm"
          variant={visibility === "private" ? "primary" : "secondary"}
          clickAction={() => onChange("private")}
        />
        <Button
          label={t("settings.knowledge.group")}
          size="sm"
          variant={visibility === "group" ? "primary" : "secondary"}
          isDisabled={!myGroupId}
          clickAction={() => onChange("group")}
        />
        <Button
          label={t("settings.knowledge.public")}
          size="sm"
          variant={visibility === "public" ? "primary" : "secondary"}
          clickAction={() => onChange("public")}
        />
      </div>
      {!myGroupId && (
        <Text size="sm" color="secondary">
          {t("settings.knowledge.joinGroupFirst")}
        </Text>
      )}
      {visibility === "group" && myGroupId && (
        <>
          <Text size="sm" color="secondary">
            {t("settings.knowledge.defaultSharedWith", { group: myGroupName ?? "—" })}
          </Text>
          {otherGroups.length > 0 && (
            <>
              <Text size="sm" color="secondary">
                {t("settings.knowledge.alsoShareWith")}
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
  const t = useT();

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
          {t("settings.knowledge.intro")}
        </Text>
        {!creating && (
          <Button
            label={t("settings.knowledge.newBase")}
            variant="secondary"
            icon={<Icon icon={Plus} size="sm" />}
            onClick={() => setCreating(true)}
          >
            {t("settings.knowledge.newShort")}
          </Button>
        )}
      </div>

      {error && <ErrorText>{error}</ErrorText>}

      {creating && (
        <Card padding={3}>
          <div className="claw-kb-form">
            <TextInput
              label={t("settings.knowledge.name")}
              value={name}
              onChange={setName}
              placeholder={t("settings.knowledge.namePlaceholder")}
            />
            <TextInput
              label={t("settings.knowledge.description")}
              value={description}
              onChange={setDescription}
              placeholder={t("settings.knowledge.descPlaceholder")}
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
              <Button label={t("settings.knowledge.create")} variant="primary" onClick={create}>
                {t("settings.knowledge.create")}
              </Button>
              <Button label={t("settings.common.cancel")} variant="ghost" onClick={() => setCreating(false)}>
                {t("settings.common.cancel")}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {loading ? (
        <Text color="secondary">{t("settings.common.loading")}</Text>
      ) : bases.length === 0 && !creating ? (
        <EmptyState
          icon={<Icon icon={Library} size="lg" />}
          title={t("settings.knowledge.emptyTitle")}
          description={t("settings.knowledge.emptyDesc")}
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
  const t = useT();
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
      setPreviewText(t("settings.knowledge.previewUnavailable", { error: String(e) }));
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
      setPreviewText((prev) => prev + (r.text ?? ""));
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
        toast({
          body: t("settings.knowledge.queued", { count: String(res.ingested.length), name: kb.name }),
          type: "info",
          autoHideDuration: 2500,
        });
      }
      if (res.errors.length) {
        toast({ body: res.errors.join("; "), type: "error" });
      }
      loadDocs();
      setExpanded(true);
      onChanged();
    } catch (e) {
      toast({ body: t("settings.knowledge.uploadFailed", { error: String(e) }), type: "error" });
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
    if (!window.confirm(t("settings.knowledge.confirmDelete", { name: kb.name }))) return;
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
    const label =
      v === "public"
        ? t("settings.knowledge.public")
        : v === "group"
          ? t("settings.knowledge.group")
          : t("settings.knowledge.private");
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
      toast({ body: t("settings.knowledge.updateVisibilityFailed", { error: String(e) }), type: "error" });
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
                aria-label={t("settings.knowledge.editVisibility")}
                title={
                  kb.visibility === "group" && kb.owner_group_name
                    ? t("settings.knowledge.groupSharedTitle", { group: kb.owner_group_name })
                    : t("settings.knowledge.clickToEditVisibility")
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
                label={t("settings.common.save")}
                size="sm"
                variant="primary"
                isDisabled={visibilityBusy}
                clickAction={saveVisibility}
              />
              <Button
                label={t("settings.common.cancel")}
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
            <Icon icon={FileText} size="xsm" color="secondary" />{" "}
            {t("settings.knowledge.docsCount", { count: String(kb.docs), plural: kb.docs === 1 ? "" : "s" })}
          </span>
          {!kb.is_owner && <span className="claw-kb-shared">{t("settings.knowledge.shared")}</span>}
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
              label={uploading ? t("settings.knowledge.uploading") : t("settings.knowledge.upload")}
              variant="secondary"
              size="sm"
              isDisabled={uploading}
              icon={<Icon icon={Upload} size="sm" />}
              onClick={() => fileRef.current?.click()}
            />
          )}
          <Button
            label={expanded ? t("settings.knowledge.hideDocuments") : t("settings.knowledge.viewDocuments")}
            variant="ghost"
            size="sm"
            onClick={toggle}
          >
            {expanded ? t("settings.knowledge.hide") : t("settings.knowledge.documents")}
          </Button>
          {kb.is_owner && (
            <Button
              label={t("settings.knowledge.deleteBase")}
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
                {t("settings.common.loading")}
              </Text>
            ) : docs.length === 0 ? (
              <Text size="sm" color="secondary">
                {t("settings.knowledge.noDocsYet")}
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
                        {t("settings.knowledge.failed")}
                      </span>
                    ) : d.status === "pending" || d.status === "processing" ? (
                      <span className="claw-kb-doc-meta claw-kb-doc-processing">
                        {t("settings.knowledge.processing")}
                      </span>
                    ) : (
                      <span className="claw-kb-doc-meta">
                        {t("settings.knowledge.chunksCount", { count: String(d.chunks), plural: d.chunks === 1 ? "" : "s" })}
                      </span>
                    )}
                    {d.status === "ready" && (
                      <button
                        type="button"
                        className="claw-kb-doc-del"
                        aria-label={t("settings.knowledge.previewText")}
                        onClick={() => openPreview(d.id)}
                      >
                        <Icon icon={Eye} size="xsm" color={previewFor === d.id ? "primary" : "secondary"} />
                      </button>
                    )}
                    {kb.is_owner && (
                      <button
                        type="button"
                        className="claw-kb-doc-del"
                        aria-label={t("settings.knowledge.deleteDoc")}
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
                          {t("settings.knowledge.extractedText")}{" "}
                          {previewTotal > 0 &&
                            t("settings.knowledge.charsCount", {
                              loaded: previewLoadedChars.toLocaleString(),
                              total: previewTotal.toLocaleString(),
                            })}
                        </Text>
                        <button
                          type="button"
                          className="claw-kb-doc-expand"
                          aria-label={t("settings.knowledge.expandView")}
                          onClick={() => setPreviewExpanded(true)}
                        >
                          <Icon icon={Maximize2} size="xsm" color="secondary" />
                        </button>
                      </div>
                      <pre className="claw-kb-doc-preview-body">{previewText}</pre>
                      {previewMore && (
                        <Button
                          label={previewLoading ? t("settings.common.loading") : t("settings.knowledge.loadMore")}
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
                                ? t("settings.knowledge.charsExtracted", {
                                    loaded: previewLoadedChars.toLocaleString(),
                                    total: previewTotal.toLocaleString(),
                                  })
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
                                label={previewLoading ? t("settings.common.loading") : t("settings.knowledge.loadMore")}
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
