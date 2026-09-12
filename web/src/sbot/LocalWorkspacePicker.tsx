import { useEffect, useState, useRef } from 'react';
import { Check, ChevronDown, CloudOff, Folder, FolderPlus, Trash2, X } from 'lucide-react';
import { getToken } from './api';

type Workspace = { id: string; name: string; path?: string | null; online: boolean; writable: boolean; pending: number; failed: number };
type Delivery = { id: string; workspace: string; name: string; dest: string; status: string; detail: string };
const VISIBLE_DELIVERIES = 5;
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
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
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
  // The workspace list already carries pending/failed counts, so the queue
  // itself is only fetched while the menu is open.
  useEffect(() => {
    if (!open) return;
    let live = true;
    const refresh = () => call('/deliveries').then(items => { if (live) setDeliveries(items); }).catch(() => undefined);
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [open]);
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
  const cancelDelivery = async (id: string) => {
    setError('');
    try {
      await call(`/deliveries/${encodeURIComponent(id)}`, 'DELETE');
      setDeliveries(items => items.filter(item => item.id !== id));
      void call('').then(setWorkspaces).catch(() => undefined);
    } catch (e) { setError((e as Error).message); }
  };
  const status = (w: Workspace) => {
    if (!w.writable) return 'Reconnect for read & write';
    if (!w.online) return w.pending ? `Offline · ${w.pending} file(s) waiting to be delivered` : 'Offline · not reachable right now';
    if (w.pending) return `Delivering ${w.pending} queued file(s)…`;
    return '';
  };
  const label = selected
    ? active ? (active.online ? active.name : `${active.name} · Offline`) : 'Local folder · Disconnected'
    : 'Work in a folder';
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
            {w.online ? <Folder size={16} /> : <CloudOff size={16} aria-label="Offline" />}
            <span title={w.path ?? 'Local path unavailable — reconnect this folder with the latest Local Agent'}>{w.name}{w.path && <small className="sbot-local-workspace-path">{w.path}</small>}{status(w) && <small className={w.online ? undefined : 'sbot-local-offline'}>{status(w)}</small>}{w.failed > 0 && <small className="sbot-local-failed">{w.failed} delivery(s) failed</small>}</span>{selected === w.id && <Check size={16} />}
          </button>
          <button type="button" className="sbot-local-remove" title={`Remove ${w.name}`} aria-label={`Remove ${w.name}`} disabled={saving || disabled} onClick={() => void remove(w.id)}><Trash2 size={15} /></button>
        </div>)}
        {active && !active.online && <p className="sbot-local-offline-note" role="status">
          {active.name} is offline. New files are queued and written automatically when the Local Agent reconnects; nothing is saved to your computer until then.
        </p>}
        {deliveries.length > 0 && <>
          <div className="sbot-local-menu-divider" />
          <div className="sbot-local-menu-heading">Waiting to be delivered</div>
          {deliveries.slice(0, VISIBLE_DELIVERIES).map(d => <div key={d.id} className="sbot-local-workspace-row">
            <span className="sbot-local-delivery" title={d.detail || d.dest}>
              {d.dest}
              <small className={d.status === 'failed' ? 'sbot-local-failed' : undefined}>
                {d.status === 'failed' ? d.detail || 'Delivery failed' : d.status === 'running' ? 'Delivering…' : 'Waiting for the folder'}
              </small>
            </span>
            <button type="button" className="sbot-local-remove" title={`Cancel ${d.dest}`} aria-label={`Cancel ${d.dest}`} disabled={d.status === 'running'} onClick={() => void cancelDelivery(d.id)}><X size={15} /></button>
          </div>)}
          {deliveries.length > VISIBLE_DELIVERIES && <small className="sbot-local-more">+{deliveries.length - VISIBLE_DELIVERIES} more</small>}
        </>}
        <div className="sbot-local-menu-divider" />
        <button type="button" className="sbot-local-workspace-option sbot-local-workspace-add" disabled={saving || disabled} onClick={() => void beginPairing()}><FolderPlus size={16} /><span>Open local folder…</span></button>
      </>}
      {error && <p role="alert">{error}</p>}
    </div>}
  </div>;
}
