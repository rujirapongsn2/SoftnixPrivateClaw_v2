import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { SideNavItem, SideNavSection } from "@astryxdesign/core/SideNav";
import { Loader2, Plus, Users, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { BotAvatar, avatarVariantFor } from "./BotAvatar";
import { api, type BotGroupInfo, type BotInfo, type SessionInfo } from "./api";
import { useT } from "../branding";
import { botDisplayName } from "./botLabels";

export function BotGroupNav({ groups, sessions, active, done, onSelect, onCreate }: {
  groups: BotGroupInfo[]; sessions: SessionInfo[]; active: string | null; done: Set<string>;
  onSelect: (group: BotGroupInfo) => void; onCreate: () => void;
}) {
  return <SideNavSection
    title="Group Bots"
    endContent={
      <IconButton
        label="Add group"
        icon={<Icon icon={Plus} size="sm" />}
        variant="ghost"
        size="sm"
        clickAction={onCreate}
      />
    }
  >
    {groups.map(group => <div className="sbot-group-nav-row" key={group.id}>
      <SideNavItem label={group.name} icon={Users} isSelected={active === group.session_id}
        onClick={() => onSelect(group)} />
      {sessions.find(s => s.id === group.session_id)?.running
        ? <Loader2 size={14} className="sbot-group-spin" aria-label="Group working" />
        : done.has(group.session_id) && <span className="sbot-group-unread" aria-label="Unread messages" />}
    </div>)}
  </SideNavSection>;
}

export function BotGroupHeader({ group, bots, working, groupRunning, onEdit }: {
  group: BotGroupInfo; bots: BotInfo[]; working?: Set<string>; groupRunning?: boolean; onEdit: () => void;
}) {
  const t = useT();
  const leader = bots.find(b => b.id === group.leader_id);
  return <header className="sbot-group-header">
    <Users size={22} aria-hidden="true" />
    <div className="sbot-group-heading">
      <strong title={group.name}>{group.name}</strong>
      <span>{leader ? `Leader · ${botDisplayName(leader, t)}` : "Choose an active leader"}</span>
    </div>
    <button type="button" className="sbot-group-members-button" onClick={onEdit} aria-label={`Edit ${group.name} members`}>
      <span className="sbot-group-faces" aria-hidden="true">{group.member_ids.slice(0, 3).map(id =>
        <BotAvatar
          key={id}
          variant={avatarVariantFor(bots.find(b => b.id === id) ?? { id })}
          size={24}
          working={Boolean(working?.has(id) || (groupRunning && id === group.leader_id))}
        />
      )}</span>
      <span>Members · {group.member_ids.length}</span>
    </button>
  </header>;
}

export function BotGroupEditor({ group, bots, onClose, onSaved, onDeleted }: {
  group: BotGroupInfo | null; bots: BotInfo[]; onClose: () => void;
  onSaved: (group: BotGroupInfo) => void; onDeleted: (group: BotGroupInfo) => void;
}) {
  const t = useT();
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(group?.name ?? "");
  const [members, setMembers] = useState<string[]>(group?.member_ids ?? []);
  const [leader, setLeader] = useState(group?.leader_id ?? "");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
    dialog.current?.querySelector<HTMLInputElement>("#bot-group-name")?.focus();
  }, []);
  const activeIds = new Set(bots.map(b => b.id));
  const missing = members.filter(id => !activeIds.has(id));
  const valid = name.trim().length > 0 && members.length >= 2 && members.length <= 12
    && members.includes(leader) && activeIds.has(leader) && missing.length === 0;
  const toggle = (id: string) => {
    const next = members.includes(id) ? members.filter(item => item !== id) : [...members, id];
    setMembers(next);
    if (!next.includes(leader)) setLeader(next[0] ?? "");
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!valid || saving) return;
    setSaving(true); setError("");
    try {
      const data = { name: name.trim(), member_ids: members, leader_id: leader };
      const saved = group ? await api.updateBotGroup(group.id, data) : await api.createBotGroup(data);
      onSaved(saved);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save group"); setSaving(false); }
  };
  const remove = async () => {
    if (!group || saving || !window.confirm(`Delete “${group.name}” and its chat history?`)) return;
    setSaving(true); setError("");
    try { await api.deleteBotGroup(group.id); onDeleted(group); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not delete group"); setSaving(false); }
  };
  return <dialog ref={dialog} className="sbot-group-dialog" aria-labelledby="bot-group-title"
    onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={save}>
      <div className="sbot-group-dialog-head">
        <h2 id="bot-group-title">{group ? "Edit group" : "New group"}</h2>
        <IconButton label="Close group editor" icon={<Icon icon={X} size="sm" />} variant="ghost"
          isDisabled={saving} clickAction={onClose} />
      </div>
      <fieldset disabled={saving} className="sbot-group-fields">
        <label htmlFor="bot-group-name">Group name</label>
        <input id="bot-group-name" value={name} onChange={e => setName(e.target.value)} maxLength={64} required autoFocus />
        <div className="sbot-group-field-heading"><label htmlFor="bot-group-search">Members</label><span>{members.length}/12 · minimum 2</span></div>
        <input id="bot-group-search" type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search bots" />
        <div className="sbot-group-picker" role="group" aria-label="Select group members">
          {bots.filter(b => `${botDisplayName(b, t)} ${b.name} ${b.role_title}`.toLowerCase().includes(query.toLowerCase())).map(b =>
            <label key={b.id} className={`sbot-group-option${members.includes(b.id) ? " is-selected" : ""}`}>
              <input type="checkbox" checked={members.includes(b.id)} onChange={() => toggle(b.id)}
                disabled={!members.includes(b.id) && members.length >= 12} />
              <BotAvatar variant={avatarVariantFor(b)} size={32} />
              <span><strong>{botDisplayName(b, t)}</strong><small>{b.role_title}</small></span>
            </label>
          )}
          {bots.length === 0 && <p>Create at least two bots to start a group.</p>}
          {bots.length > 0 && !bots.some(b => `${botDisplayName(b, t)} ${b.name} ${b.role_title}`.toLowerCase().includes(query.toLowerCase())) && <p>No bots found.</p>}
          {missing.length > 0 && <div role="alert" className="sbot-group-error">{missing.length} member(s) are unavailable.
            <button type="button" onClick={() => { const next = members.filter(id => activeIds.has(id)); setMembers(next); if (!next.includes(leader)) setLeader(next[0] ?? ""); }}>Remove unavailable members</button>
          </div>}
        </div>
        <label htmlFor="bot-group-leader">Group leader</label>
        <select id="bot-group-leader" value={leader} onChange={e => setLeader(e.target.value)} required>
          <option value="" disabled>Select a member</option>
          {bots.filter(b => members.includes(b.id)).map(b => <option key={b.id} value={b.id}>{botDisplayName(b, t)}</option>)}
        </select>
      </fieldset>
      {group && <p className="sbot-group-note">Changes apply to the next message.</p>}
      {error && <p className="sbot-group-error" role="alert">{error}</p>}
      <footer className="sbot-group-actions">
        {group && <button type="button" className="sbot-group-delete" disabled={saving} onClick={remove}>Delete group</button>}
        <button type="button" disabled={saving} onClick={onClose}>Cancel</button>
        <button type="submit" className="sbot-group-primary" disabled={!valid || saving}>{saving ? "Saving…" : group ? "Save changes" : "Create group"}</button>
      </footer>
    </form>
  </dialog>;
}
