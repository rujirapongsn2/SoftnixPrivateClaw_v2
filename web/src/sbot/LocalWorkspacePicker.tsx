import { useEffect, useState, useRef } from 'react';
import { Check, ChevronDown, Folder, FolderPlus, Trash2, X } from 'lucide-react';
import { getToken } from './api';

type Workspace = { id: string; name: string; path?: string | null; online: boolean; writable: boolean };
const base = '/modes/sbot/api/local-workspaces';
async function call(path: string, method = 'GET', body?: object) {
  const response = await fetch(base + path, {
    method, headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw Object.assign(new Error(error.detail || 'Workspace request failed'), { status: response.status });
  }
  return response.json();
}

export function LocalWorkspacePicker({ sessionId, ensureSession, disabled }: {
  sessionId: string | null;
  ensureSession?: () => Promise<{ id: string; isNew: boolean }>;
  disabled: boolean;
}) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState('');
  const [pairing, setPairing] = useState<{ code: string; expires_in: number } | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);
  const pairingSession = useRef<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  useEffect(() => {
    let live = true;
    const refresh = () => call('').then(items => { if (live) setWorkspaces(items); }).catch(e => { if (live) setError(String(e.message)); });
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => { live = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    let live = true;
    setSelected('');
    if (sessionId) void call(`/sessions/${encodeURIComponent(sessionId)}`).then(row => {
      if (live) setSelected(row.workspace_id);
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [sessionId]);
  const choose = async (id: string) => {
    setSaving(true); setError('');
    try {
      const sid = sessionId || (await ensureSession?.())?.id;
      if (!sid) throw new Error('Open a chat first');
      await call(`/sessions/${encodeURIComponent(sid)}`, 'PUT', { workspace_id: id });
      setSelected(id);
      setOpen(false);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };
  const active = workspaces.find(w => w.id === selected);
  const beginPairing = async () => {
    setError(''); setSaving(true);
    try {
      const sid = sessionId || (await ensureSession?.())?.id;
      if (!sid) throw new Error('Open a chat first');
      pairingSession.current = sid;
      const result = await call('/pair', 'POST');
      // Check API compatibility before launching the desktop app.
      try { await call('/pair/status', 'POST', { code: result.code }); }
      catch (e) {
        if ((e as Error & { status?: number }).status === 404) throw new Error('Local folder service needs a server update. Please restart the Sbot server and try again.');
        throw e;
      }
      setPairing(result);
      window.location.href = 'softnix-local-agent://connect?' + new URLSearchParams({ server: window.location.origin, code: result.code });
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };
  useEffect(() => {
    if (!pairing) return;
    let live = true;
    let busy = false;
    const deadline = Date.now() + pairing.expires_in * 1000;
    const complete = (workspaceId: string) => {
      // A folder is connected as soon as the Local Agent redeems the pairing
      // code. Close this transient menu immediately; selecting it for the chat
      // and refreshing the list may take longer (or wait for a running turn).
      setPairing(null);
      setOpen(false);
      setSelected(workspaceId);
      setError('');

      void (async () => {
        try {
          const sid = pairingSession.current;
          if (sid) await call(`/sessions/${encodeURIComponent(sid)}`, 'PUT', { workspace_id: workspaceId });
          const items = await call('');
          if (live) setWorkspaces(items);
        } catch (e) {
          // The connection is valid even when a chat is busy. Do not bring the
          // pairing panel back or make the user dismiss it by hand.
          if (live && (e as Error & { status?: number }).status !== 409) setError((e as Error).message);
        }
      })();
    };
    const checkPairing = async () => {
      if (busy) return;
      if (Date.now() >= deadline) { setPairing(null); setError('Folder connection expired. Try again.'); return; }
      busy = true;
      try {
        const result = await call('/pair/status', 'POST', { code: pairing.code });
        if (!live || !result.workspace_id) return;
        complete(result.workspace_id);
      } catch (e) { if (live) {
        if ((e as Error & { status?: number }).status === 404) { setPairing(null); setError('Local folder service needs a server update. Please restart the Sbot server and try again.'); }
        else setError((e as Error).message);
      } }
      finally { busy = false; }
    };
    // Check immediately, then again when the browser becomes active after the
    // Local Agent hands control back. Background tabs can throttle intervals.
    void checkPairing();
    const timer = setInterval(() => void checkPairing(), 1000);
    const whenVisible = () => { if (!document.hidden) void checkPairing(); };
    window.addEventListener('focus', checkPairing);
    document.addEventListener('visibilitychange', whenVisible);
    return () => {
      live = false;
      clearInterval(timer);
      window.removeEventListener('focus', checkPairing);
      document.removeEventListener('visibilitychange', whenVisible);
    };
  }, [pairing]);
  useEffect(() => {
    if (pairingSession.current && sessionId && pairingSession.current !== sessionId) setPairing(null);
  }, [sessionId]);
  const remove = async (id: string) => {
    setSaving(true); setError('');
    try {
      await call(`/${encodeURIComponent(id)}`, 'DELETE');
      setWorkspaces(items => items.filter(item => item.id !== id));
      if (selected === id) setSelected('');
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };
  const label = selected ? (active?.name ?? 'Local folder · Disconnected') : 'Work in a folder';
  const select = choose;
  return <div className="sbot-local-workspace" ref={root}>
    <button type="button" className="sbot-local-workspace-trigger" aria-expanded={open} disabled={disabled || saving} onClick={() => { setOpen(value => !value); setPairing(null); setError(''); }}>
      <Folder size={18} aria-hidden="true" />
      <span title={active?.path ?? 'Local path unavailable — reconnect this folder with the latest Local Agent'}>{label}</span>
      <ChevronDown size={16} aria-hidden="true" />
    </button>
    {open && <div className="sbot-local-workspace-menu" role="dialog" aria-label="Choose workspace">
      {pairing ? <div className="sbot-local-pairing">
        <div className="sbot-local-menu-heading"><span role="status">Choose a folder in Local Agent…</span><button type="button" aria-label="Cancel connection" onClick={() => setPairing(null)}><X size={16} /></button></div>
        <small>App not installed? Go to Settings → Client & Extention.</small>
      </div> : <>
        <div className="sbot-local-menu-heading">Workspace</div>
        <button type="button" className="sbot-local-workspace-option" disabled={saving || disabled} onClick={() => void select('')}>
          <Folder size={16} /><span>Cloud workspace</span>{!selected && <Check size={16} />}
        </button>
        {selected && !active && <button type="button" className="sbot-local-workspace-option" disabled><Folder size={16} /><span>Local folder · Disconnected</span></button>}
        {workspaces.map(w => <div key={w.id} className="sbot-local-workspace-row">
          <button type="button" className="sbot-local-workspace-option" disabled={saving || disabled} onClick={() => w.writable ? void select(w.id) : void beginPairing()}>
            <Folder size={16} /><span title={w.path ?? 'Local path unavailable — reconnect this folder with the latest Local Agent'}>{w.name}{w.path && <small className="sbot-local-workspace-path">{w.path}</small>}{(!w.online || !w.writable) && <small>{!w.writable ? 'Reconnect for read & write' : 'Offline'}</small>}</span>{selected === w.id && <Check size={16} />}
          </button>
          <button type="button" className="sbot-local-remove" title={`Remove ${w.name}`} aria-label={`Remove ${w.name}`} disabled={saving || disabled} onClick={() => void remove(w.id)}><Trash2 size={15} /></button>
        </div>)}
        <div className="sbot-local-menu-divider" />
        <button type="button" className="sbot-local-workspace-option sbot-local-workspace-add" disabled={saving || disabled} onClick={() => void beginPairing()}><FolderPlus size={16} /><span>Open local folder…</span></button>
      </>}
      {error && <p role="alert">{error}</p>}
    </div>}
  </div>;
}
