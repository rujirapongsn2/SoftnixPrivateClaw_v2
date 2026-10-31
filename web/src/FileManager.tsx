import { File, FileCode, FileSpreadsheet, FileText, Film, Image, LayoutDashboard, LayoutGrid, List, Presentation, Search } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { getToken } from './api';
import { useT } from './branding';

type Zone = 'ai' | 'user' | 'scratch';
type Kind = 'document' | 'spreadsheet' | 'presentation' | 'web' | 'image' | 'card' | 'media' | 'other';
type Sort = 'recent' | 'name' | 'size';
type View = 'grid' | 'list';
type Period = '' | 'today' | 'week' | 'month';
type FileEntry = { path: string; size: number; modified_at: number; zone: Zone; expires_at: number | null; kind: Kind };
type FilePage = { files: FileEntry[]; total: number; counts: Partial<Record<Kind, number>>; next_offset: number | null; truncated: boolean; walk_limit: number };
type TrashEntry = { id: string; path: string; at: number; actor: string; rule: string; size: number; expires_at: number | null };
type TrashListing = { files: TrashEntry[]; total: number; retention_days: number; cap_mb: number; permanent_delete_enabled: boolean };

const TRASH_LIMIT = 500;
const PAGE_SIZE = 40;
const SEARCH_DELAY_MS = 250;
const VIEW_KEY = 'claw.files.view';
const KINDS: Kind[] = ['document', 'spreadsheet', 'presentation', 'web', 'image', 'card', 'media', 'other'];
const KIND_ICON = {
  document: FileText, spreadsheet: FileSpreadsheet, presentation: Presentation, web: FileCode,
  image: Image, card: LayoutDashboard, media: Film, other: File,
} satisfies Record<Kind, unknown>;

/** Start of the period in epoch seconds, measured in the viewer's own day. */
function periodStart(period: Period): number | null {
  if (!period) return null;
  if (period === 'today') { const day = new Date(); day.setHours(0, 0, 0, 0); return day.getTime() / 1000; }
  return Date.now() / 1000 - (period === 'week' ? 7 : 30) * 86_400;
}

function savedView(): View {
  try { return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'grid'; } catch { return 'grid'; }
}

function encodePath(path: string) {
  return path.split('/').map(encodeURIComponent).join('/');
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
const ZONE_LABEL: Record<Zone, string> = { ai: 'files.zone.ai', user: 'files.zone.user', scratch: 'files.zone.scratch' };

function daysUntil(epochSeconds: number) {
  return Math.max(0, Math.ceil((epochSeconds * 1000 - Date.now()) / 86_400_000));
}

/** An image thumbnail fetched with the Authorization header once it scrolls
 * near view, so the session token never appears in an image URL (and so in
 * access logs or browser history). */
function Thumbnail({ url }: { url: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const node = host.current;
    if (!node) return;
    if (typeof IntersectionObserver === 'undefined') { setNear(true); return; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) { setNear(true); observer.disconnect(); }
    }, { rootMargin: '200px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!near) return;
    const control = new AbortController();
    let objectUrl: string | null = null;
    fetch(url, { headers: { Authorization: `Bearer ${getToken()}` }, signal: control.signal })
      .then(r => (r.ok ? r.blob() : null))
      .then(blob => { if (blob) { objectUrl = URL.createObjectURL(blob); setSrc(objectUrl); } })
      .catch(() => {});
    return () => { control.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [near, url]);
  return <div ref={host} className="claw-files-thumb-image">
    {src ? <img src={src} alt="" decoding="async" /> : <Image size={36} aria-hidden="true" />}
  </div>;
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
  const [page, setPage] = useState<Omit<FilePage, 'files'> | null>(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [kind, setKind] = useState<Kind | ''>('');
  const [sort, setSort] = useState<Sort>('recent');
  const [period, setPeriod] = useState<Period>('');
  const [zone, setZone] = useState<'' | Zone>('');
  const [view, setView] = useState<View>(savedView);
  const [loadingMore, setLoadingMore] = useState(false);
  const fresh = useRef(true);
  // Close only when the press and the release both land on the backdrop, so
  // dragging a text selection out of the dialog does not dismiss it.
  const pressedBackdrop = useRef(false);
  // Each new search/filter/sort bumps this, so a slower earlier response cannot overwrite a newer one.
  const generation = useRef(0);
  // The generation the shown list came from; paging is only allowed from that one.
  const shownGeneration = useRef(-1);
  const sentinel = useRef<HTMLDivElement>(null);
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
  function listUrl(offset: number) {
    const params = new URLSearchParams({ q: search, kind, sort, zone, offset: String(offset), limit: String(PAGE_SIZE) });
    const since = periodStart(period);
    if (since != null) params.set('since', String(Math.floor(since)));
    // The first listing after opening walks the disk; later pages, searches and
    // filters reuse that walk on the server for a few seconds.
    if (fresh.current) { params.set('refresh', 'true'); fresh.current = false; }
    return '?' + params.toString();
  }
  async function refresh() {
    const current = ++generation.current;
    const [live, deleted] = await Promise.all([
      request(listUrl(0)).then(r => r.json() as Promise<FilePage>),
      request(`/trash?limit=${TRASH_LIMIT}`).then(r => r.json() as Promise<TrashListing>),
    ]);
    if (current !== generation.current) return;
    const { files: first, ...rest } = live;
    shownGeneration.current = current;
    setFiles(first); setPage(rest); setTrash(deleted);
  }
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, [base, search, kind, sort, period, zone]);
  const loadMore = useCallback(async () => {
    if (loadingMore || page?.next_offset == null || shownGeneration.current !== generation.current) return;
    setLoadingMore(true);
    const current = generation.current;
    try {
      const next = await request(listUrl(page.next_offset)).then(r => r.json() as Promise<FilePage>);
      if (current !== generation.current) return;
      const { files: more, ...rest } = next;
      // The server may have re-walked the workspace between pages; never show a file twice.
      setFiles(previous => {
        const seen = new Set(previous.map(f => f.path));
        return [...previous, ...more.filter(f => !seen.has(f.path))];
      });
      setPage(rest);
    } catch (e) { setError(String(e)); }
    finally { setLoadingMore(false); }
  }, [loadingMore, page, search, kind, sort, period, zone]);
  useEffect(() => {
    const node = sentinel.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) void loadMore(); });
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadMore, tab, files.length]);
  function chooseView(next: View) {
    setView(next);
    try { localStorage.setItem(VIEW_KEY, next); } catch { /* per-viewer convenience only */ }
  }
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
  async function trashFile(file: FileEntry) {
    setBusy(true); setError('');
    try {
      await request('/trash', { method: 'POST', body: JSON.stringify({ path: file.path }) });
      // Drop the row where it is, so the user keeps the pages they loaded and their place in them.
      setFiles(previous => previous.filter(f => f.path !== file.path));
      setPage(previous => previous && {
        ...previous,
        total: Math.max(0, previous.total - 1),
        next_offset: previous.next_offset == null ? null : Math.max(0, previous.next_offset - 1),
        counts: { ...previous.counts, [file.kind]: Math.max(0, (previous.counts[file.kind] ?? 1) - 1) },
      });
      setTrash(await request(`/trash?limit=${TRASH_LIMIT}`).then(r => r.json() as Promise<TrashListing>));
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function download(path: string) {
    setError('');
    try {
      const blob = await (await request('/download/' + encodePath(path))).blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url;
      anchor.download = path.split('/').pop() || 'file'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(String(e)); }
  }
  const trashed = trash?.files ?? [];
  const trashTotal = trash?.total ?? 0;
  return <div className="claw-files-backdrop"
    onMouseDown={e => { pressedBackdrop.current = e.target === e.currentTarget; }}
    onClick={e => { if (pressedBackdrop.current && e.target === e.currentTarget) onClose(); pressedBackdrop.current = false; }}>
    <section ref={dialog} className="claw-files-dialog" role="dialog" aria-modal="true" aria-labelledby="files-title">
      <header><h2 id="files-title">{t('files.title')}</h2><button autoFocus onClick={onClose}>{t('files.close')}</button></header>
      <p>{t('files.intro')}</p>
      <nav aria-label={t('files.views')}>
        <button aria-pressed={tab === 'files'} onClick={() => setTab('files')}>{t('files.tabFiles', { count: String(page?.total ?? files.length) })}</button>
        <button aria-pressed={tab === 'trash'} onClick={() => setTab('trash')}>{t('files.tabTrash', { count: String(trashTotal) })}</button>
      </nav>
      {error && <p role="alert">{error}</p>}
      {tab === 'files' ? <>
        <div className="claw-files-toolbar">
          <label className="claw-files-search">
            <Search size={16} aria-hidden="true" />
            <input type="search" value={query} onChange={e => setQuery(e.target.value)}
              placeholder={t('files.searchPlaceholder')} aria-label={t('files.searchPlaceholder')} maxLength={200} />
          </label>
          <select value={sort} onChange={e => setSort(e.target.value as Sort)} aria-label={t('files.sortLabel')}>
            <option value="recent">{t('files.sort.recent')}</option>
            <option value="name">{t('files.sort.name')}</option>
            <option value="size">{t('files.sort.size')}</option>
          </select>
          <select value={period} onChange={e => setPeriod(e.target.value as Period)} aria-label={t('files.periodLabel')}>
            <option value="">{t('files.period.any')}</option>
            <option value="today">{t('files.period.today')}</option>
            <option value="week">{t('files.period.week')}</option>
            <option value="month">{t('files.period.month')}</option>
          </select>
          <select value={zone} onChange={e => setZone(e.target.value as '' | Zone)} aria-label={t('files.zoneLabel')}>
            <option value="">{t('files.zone.any')}</option>
            <option value="ai">{t('files.zone.ai')}</option>
            <option value="user">{t('files.zone.user')}</option>
            <option value="scratch">{t('files.zone.scratch')}</option>
          </select>
          <div className="claw-files-viewtoggle" role="group" aria-label={t('files.viewLabel')}>
            <button aria-pressed={view === 'grid'} onClick={() => chooseView('grid')} title={t('files.view.grid')} aria-label={t('files.view.grid')}><LayoutGrid size={16} /></button>
            <button aria-pressed={view === 'list'} onClick={() => chooseView('list')} title={t('files.view.list')} aria-label={t('files.view.list')}><List size={16} /></button>
          </div>
        </div>
        <div className="claw-files-kinds" role="group" aria-label={t('files.kindLabel')}>
          <button aria-pressed={kind === ''} onClick={() => setKind('')}>{t('files.kind.all')}</button>
          {KINDS.filter(k => page?.counts[k] || kind === k).map(k => (
            <button key={k} aria-pressed={kind === k} onClick={() => setKind(kind === k ? '' : k)}>
              {t(`files.kind.${k}`)} <span>{page?.counts[k] ?? 0}</span>
            </button>
          ))}
        </div>
        {page?.truncated && <p>{t('files.truncated', { count: page.walk_limit.toLocaleString() })}</p>}
        {page && !files.length && <p>{search || kind || period || zone ? t('files.noMatches') : t('files.empty')}</p>}
        <div className={view === 'grid' ? 'claw-files-grid' : 'claw-files-list'}>
          {files.map(file => {
            const days = file.expires_at == null ? null : daysUntil(file.expires_at);
            const name = file.path.split('/').pop() || file.path;
            const folder = file.path.slice(0, file.path.length - name.length).replace(/\/$/, '');
            const KindIcon = KIND_ICON[file.kind] ?? File;
            return <article key={file.path} className={`claw-files-item claw-files-kind-${file.kind}`}>
              {view === 'grid' && <div className="claw-files-thumb">
                {file.kind === 'image'
                  ? <Thumbnail url={`${base}/download/${encodePath(file.path)}`} />
                  : <KindIcon size={36} aria-hidden="true" />}
              </div>}
              <span className="claw-files-meta">
                <span className="claw-files-name" title={file.path}>{view === 'list' && <KindIcon size={16} aria-hidden="true" />}<span className="claw-files-name-text">{name}</span></span>
                <small>
                  {folder && <>{folder} · </>}
                  {new Date(file.modified_at * 1000).toLocaleDateString()} · {formatSize(file.size)}
                </small>
                <small>
                  <span className="claw-files-zone">{t(ZONE_LABEL[file.zone])}</span>
                  {days != null && <>{days ? t('files.expiresIn', { days: String(days) }) : t('files.expiresToday')}</>}
                </small>
              </span>
              <span className="claw-files-actions">
                <button disabled={busy} onClick={() => void download(file.path)}>{t('files.download')}</button>
                <button disabled={busy} onClick={() => {
                  if (window.confirm(t('files.moveConfirm', { path: file.path }))) void trashFile(file);
                }}>{t('files.moveToTrash')}</button>
              </span>
            </article>;
          })}
        </div>
        {page?.next_offset != null && <div ref={sentinel} className="claw-files-more">
          <button disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? t('files.loading') : t('files.loadMore')}</button>
        </div>}
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
