import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Loader2, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { BOT_AVATARS, BotAvatar, avatarVariantFor, type BotAvatarVariant } from "./BotAvatar";
import { api, type BotInfo, type SkillInfo } from "./api";

const BOT_TOOLS = [
  ["project", "Project environment"],
  ["read_file", "Read files"],
  ["write_file", "Write files"],
  ["edit_file", "Edit files"],
  ["list_dir", "List directories"],
  ["exec", "Run sandbox commands"],
  ["web_fetch", "Fetch web pages"],
  ["web_search", "Search the web"],
] as const;

type SkillScope = "all" | "selected" | "none";

function initialSkillScope(skillIds: string[] | null | undefined): SkillScope {
  if (skillIds == null) return "all";
  return skillIds.length > 0 ? "selected" : "none";
}

export function BotEditor({ bot, onClose, onSaved, onDeleted }: {
  bot: BotInfo;
  onClose: () => void;
  onSaved: (bot: BotInfo) => void;
  onDeleted: (bot: BotInfo) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(bot.name);
  const [roleTitle, setRoleTitle] = useState(bot.role_title);
  const [charter, setCharter] = useState(bot.charter);
  const [model, setModel] = useState(bot.model ?? "");
  const [avatar, setAvatar] = useState<BotAvatarVariant>(avatarVariantFor(bot));
  const [restricted, setRestricted] = useState(bot.tool_allowlist !== null);
  const [tools, setTools] = useState<string[]>(bot.tool_allowlist ?? []);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillIds, setSkillIds] = useState<string[]>(bot.skill_ids ?? []);
  const [skillScope, setSkillScope] = useState<SkillScope>(() => initialSkillScope(bot.skill_ids));
  const [loadingSkills, setLoadingSkills] = useState(true);
  const [skillsLoadFailed, setSkillsLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    dialog.current?.showModal();
    dialog.current?.querySelector<HTMLInputElement>("#bot-name")?.focus();
    let cancelled = false;
    api.listSkills().then(items => {
      if (!cancelled) setSkills(items.filter(item => item.enabled && (!item.read_only || item.subscription_enabled)));
    }).catch(() => {
      if (!cancelled) {
        setSkillsLoadFailed(true);
        setError("Could not load skills. Use all enabled skills or no skill shortlist before saving.");
      }
    }).finally(() => {
      if (!cancelled) setLoadingSkills(false);
    });
    return () => { cancelled = true; };
  }, []);

  const selectedSkillSet = useMemo(() => new Set(skillIds), [skillIds]);
  const hasSelectedEnabledSkill = useMemo(
    () => skills.some(skill => selectedSkillSet.has(skill.id) || selectedSkillSet.has(skill.name)),
    [skills, selectedSkillSet],
  );
  const toggleTool = (id: string) => setTools(previous => previous.includes(id)
    ? previous.filter(item => item !== id)
    : [...previous, id]);
  const toggleSkill = (skill: SkillInfo) => setSkillIds(previous => {
    const aliases = new Set([skill.id, skill.name]);
    return previous.some(item => aliases.has(item))
      ? previous.filter(item => !aliases.has(item))
      : [...previous, skill.id];
  });

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || !name.trim() || !roleTitle.trim()) return;
    if (skillScope === "selected") {
      if (loadingSkills) {
        setError("Wait for skills to finish loading before saving selected skills.");
        return;
      }
      if (skillsLoadFailed) {
        setError("Could not verify selected skills. Use all enabled skills or no skill shortlist.");
        return;
      }
      if (!hasSelectedEnabledSkill) {
        setError("Choose at least one enabled skill, or use all enabled skills.");
        return;
      }
    }
    setSaving(true);
    setError("");
    const savedSkillIds = skillScope === "all" ? null : skillScope === "none" ? [] : skillIds;
    try {
      await api.updateBot(bot.id, {
        name: name.trim(),
        role_title: roleTitle.trim(),
        charter: charter.trim(),
        model: model.trim() || null,
        avatar: { ...(bot.avatar || {}), variant: avatar },
        skill_ids: savedSkillIds,
        tool_allowlist: restricted ? tools : null,
      });
      onSaved(await api.getBot(bot.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save bot");
      setSaving(false);
    }
  };

  const remove = async () => {
    if (bot.kind === "chief_of_staff" || saving) return;
    if (!window.confirm(`Delete “${bot.name}”? It will be removed from the bot list. Chat history will remain available.`)) return;
    setSaving(true);
    setError("");
    try {
      await api.deleteBot(bot.id);
      onDeleted(bot);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete bot");
      setSaving(false);
    }
  };

  return <dialog ref={dialog} className="sbot-bot-dialog" aria-labelledby="bot-editor-title"
    onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}>
    <form method="dialog" className="sbot-bot-editor" onSubmit={save}>
      <div className="sbot-bot-dialog-head">
        <div>
          <h2 id="bot-editor-title">Edit bot</h2>
          <p>Persona and capabilities</p>
        </div>
        <IconButton label="Close" icon={<Icon icon={X} size="sm" />} variant="ghost" size="sm"
          clickAction={() => { if (!saving) onClose(); }} />
      </div>

      <div className="sbot-bot-editor-grid">
        <label>Bot name<input id="bot-name" value={name} maxLength={64} onChange={e => setName(e.target.value)} /></label>
        <label>Role title<input value={roleTitle} maxLength={64} onChange={e => setRoleTitle(e.target.value)} /></label>
        <label className="sbot-bot-field-wide">Persona / charter
          <textarea value={charter} rows={10} placeholder="Describe identity, mission, principles, workflow and boundaries..."
            onChange={e => setCharter(e.target.value)} />
        </label>
        <label>Model override<input value={model} placeholder="Use server default" onChange={e => setModel(e.target.value)} /></label>
      </div>

      <fieldset className="sbot-bot-avatar-field">
        <legend>Avatar</legend>
        <div className="sbot-bot-avatar-options">
          {BOT_AVATARS.map(variant => <button key={variant} type="button"
            className={`sbot-bot-avatar-option${avatar === variant ? " sbot-bot-avatar-option--selected" : ""}`}
            aria-label={`Use ${variant} avatar`} aria-pressed={avatar === variant}
            onClick={() => setAvatar(variant)}><BotAvatar variant={variant} size={42} /></button>)}
        </div>
      </fieldset>

      <fieldset className="sbot-bot-tools-field">
        <legend>Tools</legend>
        <label className="sbot-bot-check sbot-bot-tools-toggle"><input type="checkbox" checked={restricted}
          onChange={e => setRestricted(e.target.checked)} /> Limit this bot to selected tools</label>
        {restricted && <div className="sbot-bot-check-grid">{BOT_TOOLS.map(([id, label]) => <label key={id} className="sbot-bot-check">
          <input type="checkbox" checked={tools.includes(id)} onChange={() => toggleTool(id)} /> {label}
        </label>)}</div>}
      </fieldset>

      <fieldset className="sbot-bot-tools-field">
        <legend>Skills</legend>
        <div className="sbot-bot-skill-scopes" role="radiogroup" aria-label="Skill scope">
          <label className={`sbot-bot-skill-scope${skillScope === "all" ? " sbot-bot-skill-scope--selected" : ""}`}>
            <input type="radio" name="skill-scope" checked={skillScope === "all"} onChange={() => setSkillScope("all")} />
            <span><strong>Use all enabled skills</strong><small>Default. The bot can discover every available skill.</small></span>
          </label>
          <label className={`sbot-bot-skill-scope${skillScope === "selected" ? " sbot-bot-skill-scope--selected" : ""}`}>
            <input type="radio" name="skill-scope" checked={skillScope === "selected"} onChange={() => setSkillScope("selected")} />
            <span><strong>Use selected skills</strong><small>Keep the bot focused on the skills below.</small></span>
          </label>
          <label className={`sbot-bot-skill-scope${skillScope === "none" ? " sbot-bot-skill-scope--selected" : ""}`}>
            <input type="radio" name="skill-scope" checked={skillScope === "none"} onChange={() => setSkillScope("none")} />
            <span><strong>No skill shortlist</strong><small>Do not preload skill guidance for this bot.</small></span>
          </label>
        </div>
        {skillScope === "selected" && (
          loadingSkills ? <span className="sbot-bot-muted"><Loader2 size={14} className="sbot-bot-spin" /> Loading skills…</span>
            : skills.length === 0 ? <span className="sbot-bot-muted">No enabled skills</span>
            : <div className="sbot-bot-check-grid">{skills.map(skill => <label key={skill.id} className="sbot-bot-check">
              <input type="checkbox" checked={selectedSkillSet.has(skill.id) || selectedSkillSet.has(skill.name)} onChange={() => toggleSkill(skill)} />
              <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
            </label>)}</div>
        )}
      </fieldset>

      {error && <p className="sbot-bot-editor-error" role="alert">{error}</p>}
      <div className="sbot-bot-dialog-actions">
        {bot.kind !== "chief_of_staff" && <button type="button" className="sbot-bot-danger" onClick={() => void remove()} disabled={saving}><Trash2 size={15} /> Delete bot</button>}
        <span className="sbot-bot-actions-spacer" />
        <button type="button" className="sbot-bot-secondary" onClick={onClose} disabled={saving}>Cancel</button>
        <button type="submit" className="sbot-bot-primary" disabled={saving || !name.trim() || !roleTitle.trim()}>{saving ? "Saving…" : "Save changes"}</button>
      </div>
    </form>
  </dialog>;
}
