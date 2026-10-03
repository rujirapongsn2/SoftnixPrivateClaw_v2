import { useEffect, useRef, useState } from 'react';
import { getToken } from './api';
import { useT } from './branding';

type Zone = 'ai' | 'user' | 'scratch';
type FileEntry = { path: string; size: number; modified_at: number; zone: Zone; expires_at: number | null };
type TrashEntry = { id: string; path: string; at: number; actor: string; rule: string; size: number; expires_at: number | null };
type TrashListing = { files: TrashEntry[]; total: number; retention_days: number; cap_mb: number; permanent_delete_enabled: boolean };

const TRASH_LIMIT = 500;
const ZONE_LABEL: Record<Zone, string> = { ai: 'files.zone.ai', user: 'files.zone.user', scratch: 'files.zone.scratch' };

function daysUntil(epochSeconds: number) {
  return Math.max(0, Math.ceil((epochSeconds * 1000 - Date.now()) / 86_400_000));
}

/** File ownership is the authenticated user and mode, never a session id. */
export function FileManager({ mode, onClose }: { mode: 'privateclaw' | 'sbot'; onClose: () => void }) {
  const t = useT();
  const base = mode === 'sbot' ? '/modes/sbot/api/files' : '/api/files';
  const dialog = useRef<HTMLElement>(null);
  // Callers pass a new onClose each render; the key effect must not re-run and steal focus.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [trash, setTrash] = useState<TrashListing | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<'files' | 'trash'>('files');
  async function request(path: string, init?: RequestInit) {
    const response = await fetch(base + path, { ...init, headers: {
      'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}`,
    } });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || t('files.requestFailed', { status: String(response.status) }));
    }
    return response;
  }
  async function refresh() {
    const [live, deleted] = await Promise.all([
      request('').then(r => r.json()), request(`/trash?limit=${TRASH_LIMIT}`).then(r => r.json() as Promise<TrashListing>),
    ]);
    setFiles(live.files); setTruncated(live.truncated); setTrash(deleted);
  }
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, [base]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCloseRef.current();
      if (event.key === 'Tab') {
        const controls = dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
        if (!controls?.length) return;
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener('keydown', close);
    return () => { window.removeEventListener('keydown', close); previous?.focus(); };
  }, []);
  async function act(path: string, init?: RequestInit) {
    setBusy(true); setError('');
    try { await request(path, init); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function download(path: string) {
    setError('');
    try {
      const encoded = path.split('/').map(encodeURIComponent).join('/');
      const blob = await (await request('/download/' + encoded)).blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url;
      anchor.download = path.split('/').pop() || 'file'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(String(e)); }
  }
  const trashed = trash?.files ?? [];
  const trashTotal = trash?.total ?? 0;
  return <div className="claw-files-backdrop">
    <section ref={dialog} className="claw-files-dialog" role="dialog" aria-modal="true" aria-labelledby="files-title">
      <header><h2 id="files-title">{t('files.title')}</h2><button autoFocus onClick={onClose}>{t('files.close')}</button></header>
      <p>{t('files.intro')}</p>
      <nav aria-label={t('files.views')}>
        <button aria-pressed={tab === 'files'} onClick={() => setTab('files')}>{t('files.tabFiles', { count: String(files.length) })}</button>
        <button aria-pressed={tab === 'trash'} onClick={() => setTab('trash')}>{t('files.tabTrash', { count: String(trashTotal) })}</button>
      </nav>
      {error && <p role="alert">{error}</p>}
      {tab === 'files' ? <>
        {truncated && <p>{t('files.truncated', { count: files.length.toLocaleString() })}</p>}
        {!files.length && <p>{t('files.empty')}</p>}
        {files.map(file => { const days = file.expires_at == null ? null : daysUntil(file.expires_at); return <article key={file.path}>
          <span>{file.path}<small>
            <span className="claw-files-zone">{t(ZONE_LABEL[file.zone])}</span>
            {t('files.bytes', { size: file.size.toLocaleString() })}
            {days != null && <> · {days ? t('files.expiresIn', { days: String(days) }) : t('files.expiresToday')}</>}
          </small></span>
          <button disabled={busy} onClick={() => void download(file.path)}>{t('files.download')}</button>
          <button disabled={busy} onClick={() => {
            if (window.confirm(t('files.moveConfirm', { path: file.path })))
              void act('/trash', { method: 'POST', body: JSON.stringify({path: file.path}) });
          }}>{t('files.moveToTrash')}</button>
        </article>; })}
      </> : <>
        {trash && <p>{t('files.trashIntro', { days: String(trash.retention_days) })}</p>}
        {trashTotal > trashed.length && <p>{t('files.trashTruncated', { shown: trashed.length.toLocaleString(), total: trashTotal.toLocaleString() })}</p>}
        {!trashed.length && <p>{t('files.trashEmpty')}</p>}
        {trashed.map(file => <article key={file.id}>
          <span>{file.path}<small>
            {t('files.movedAt', { date: new Date(file.at * 1000).toLocaleDateString() })}
            {file.expires_at != null && <> · {t('files.purgeAt', { date: new Date(file.expires_at * 1000).toLocaleDateString() })}</>}
          </small></span>
          <button disabled={busy} onClick={() => void act(`/trash/${file.id}/restore`, {method: 'POST'})}>{t('files.restore')}</button>
          {trash?.permanent_delete_enabled && <button disabled={busy} onClick={() => {
            if (window.prompt(t('files.purgeConfirm', { path: file.path })) === file.path)
              void act(`/trash/${file.id}/purge`, {method: 'POST', body: JSON.stringify({confirm_permanent: true, expected_path: file.path})});
          }}>{t('files.deletePermanently')}</button>}
        </article>)}
      </>}
    </section>
  </div>;
}
