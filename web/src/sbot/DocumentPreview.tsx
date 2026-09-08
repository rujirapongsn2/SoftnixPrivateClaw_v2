import { Markdown } from '@astryxdesign/core/Markdown';
import { Download, Eye, FileText, Maximize2, X, Copy, Check, RotateCcw, WrapText } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { fileDocumentPreview, type DocumentPreview as PreviewData } from './api';
import { useT } from '../branding';
import { SaveToBlueprintButton } from './SaveToBlueprintButton';

type Props = { sessionId: string; path: string; href: string };
export function DocumentPreview(props: Props) {
  // A new file/session must never inherit loaded content or pending requests.
  return <PreviewContent key={`${props.sessionId}:${props.path}`} {...props} />;
}
function PreviewContent({ sessionId, path, href }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<PreviewData | null>(null);
  const [error, setError] = useState(false);
  const [mediaError, setMediaError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [raw, setRaw] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const restoreFocus = useRef<HTMLElement | null>(null);
  const name = path.split('/').pop() ?? path;
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  const pdf = ext === 'pdf';
  const audio = /^(mp3|wav|ogg|m4a)$/.test(ext);
  const video = /^(mp4|webm|mov)$/.test(ext);
  const media = pdf || audio || video;

  useEffect(() => {
    if (!open || media) return;
    const controller = new AbortController();
    setError(false);
    fileDocumentPreview(sessionId, path, controller.signal)
      .then(result => { if (!controller.signal.aborted) setData(result); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [open, media, sessionId, path, attempt]);
  useEffect(() => {
    const element = dialog.current;
    if (expanded) {
      if (element && !element.open) element.showModal();
      return;
    }
    if (element?.open) element.close();
    // Native dialogs can leave focus on the dialog element after close. That
    // makes Chromium paint a blue focus ring around the old modal bounds.
    // Return focus to the control that opened it, after the close transaction.
    const target = restoreFocus.current;
    restoreFocus.current = null;
    if (target && document.contains(target)) requestAnimationFrame(() => target.focus());
  }, [expanded]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);

  let text = data?.text ?? '';
  if (ext === 'json' && !raw) {
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* Preserve invalid/truncated JSON. */ }
  }
  const content = () => <>
    {!media && <div className="sbot-preview-toolbar">
      {['md', 'json'].includes(ext) && <div className="sbot-preview-tabs" aria-label="Display mode">
        <button type="button" aria-pressed={!raw} onClick={() => setRaw(false)}>{ext === 'md' ? 'Preview' : 'Formatted'}</button>
        <button type="button" aria-pressed={raw} onClick={() => setRaw(true)}>Source</button>
      </div>}
      {(ext !== 'md' || raw) && <button type="button" title="Wrap lines" aria-label="Wrap lines" aria-pressed={wrap} onClick={() => setWrap(!wrap)}><WrapText size={16} /></button>}
      <span className="sbot-preview-spacer" />
      {data && <button type="button" onClick={async () => {
        try { await navigator.clipboard.writeText(text); setCopied(true); setCopyError(false); }
        catch { setCopyError(true); }
      }} aria-label={copied ? 'Copied' : 'Copy content'} title={copied ? 'Copied' : 'Copy content'}>{copied ? <Check size={16} /> : <Copy size={16} />}</button>}
      {copyError && <span role="status">Could not copy</span>}
    </div>}
    {pdf ? <iframe src={href} title={name} className="claw-document-preview-pdf" />
      : audio ? <audio controls preload="metadata" src={href} aria-label={name} onError={() => setMediaError(true)} />
      : video ? <video controls preload="metadata" src={href} aria-label={name} onError={() => setMediaError(true)} />
      : error ? <div className="sbot-preview-empty" role="status">{t('chat.preview.unavailable')}<button type="button" onClick={() => setAttempt(n => n + 1)}><RotateCcw size={15} />Retry</button></div>
      : !data ? <div className="sbot-preview-empty" role="status">Loading preview…</div>
      : !text.trim() ? <div className="sbot-preview-empty">{t('chat.preview.noText')}</div>
      : ext === 'md' && !raw ? <div className="sbot-preview-reading"><Markdown>{text}</Markdown></div>
      : <pre className={`sbot-preview-text${wrap ? ' is-wrapped' : ''}`}>{text}</pre>}
    {mediaError && <div className="claw-document-preview-foot" role="status">This browser cannot play this file. Download to open it on your device.</div>}
    {data?.truncated && <div className="claw-document-preview-foot">{t('chat.preview.textTruncated')}</div>}
    {['docx', 'pptx'].includes(ext) && open && <div className="claw-document-preview-foot">Text preview · Download for original layout</div>}
  </>;
  return <div className="claw-document-preview">
    <div className="claw-document-preview-head">
      <FileText size={18} aria-hidden="true" />
      <button type="button" className="sbot-preview-file" onClick={() => setOpen(!open)} aria-expanded={open} title={path}>
        <span>{name}</span><small>{ext.toUpperCase()}</small>
      </button>
      <button type="button" className="claw-document-preview-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={open ? 'Close preview' : 'Preview file'} title={open ? 'Close preview' : 'Preview file'}>{open ? <X size={17} /> : <Eye size={17} />}</button>
      <a className="claw-document-preview-download" href={href} download={name} aria-label={`Download ${name}`} title={`Download ${name}`}><Download size={17} /></a>
      <SaveToBlueprintButton sessionId={sessionId} path={path} />
      {open && <button type="button" className="claw-document-preview-toggle" onClick={(event) => {
        // Preserve a visible focus destination for keyboard users. Pointer
        // activation should not manufacture a focus ring after the modal closes.
        restoreFocus.current = event.detail === 0 ? event.currentTarget : null;
        setExpanded(true);
      }} aria-label="Expand preview" title="Expand preview"><Maximize2 size={17} /></button>}
    </div>
    {open && !expanded && content()}
    <dialog ref={dialog} className="sbot-preview-dialog" onCancel={() => setExpanded(false)} onClose={() => setExpanded(false)} aria-label={`Preview ${name}`}>
      <div className="claw-document-preview-head"><strong className="sbot-preview-file">{name}</strong><a className="claw-document-preview-download" href={href} download={name} aria-label={`Download ${name}`} title="Download"><Download size={18} /></a><button type="button" className="claw-document-preview-toggle" onClick={() => setExpanded(false)} aria-label="Close expanded preview" autoFocus><X size={20} /></button></div>
      {expanded && content()}
    </dialog>
  </div>;
}
