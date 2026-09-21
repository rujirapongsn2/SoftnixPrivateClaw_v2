import { Icon } from "@astryxdesign/core/Icon";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { Eye, ExternalLink, FileCode, Maximize2, Minimize2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { fileHtmlPreview, filePngExport, fileUrl, type HtmlPreview as HtmlPreviewData, previewRetryDelay } from "./api";
import { useT } from "../branding";

type Props = {
  sessionId: string;
  /** Workspace-relative artifact path. */
  path: string;
  /** Direct open URL for the untruncated file. */
  href: string;
};

/** Inline render of an .html artifact, in an iframe that can do nothing.
 *
 * The `sandbox` attribute is bare on purpose — no allow-scripts, and above all
 * no allow-same-origin. That gives the document an opaque origin, so even
 * though the markup is agent-authored and unsanitized it cannot reach the app's
 * cookies, localStorage or API, or submit a form. Anything that widens this
 * attribute turns a stored report into stored XSS.
 *
 * `sandbox` does NOT block subresource loads, though — there is no such flag —
 * so it is the CSP that claw.api.file_preview injects into the markup that stops
 * an <img src=https://...> or a CSS url() phoning home on scroll. Both halves
 * are load-bearing; neither is sufficient alone.
 *
 * srcdoc rather than src: the markup already arrived over the authenticated
 * JSON API, so there is no second request to attach a token to, and no URL a
 * user could paste to get the same document rendered *without* the sandbox.
 *
 * Height is fixed rather than fitted to the content — measuring it would need
 * allow-same-origin, which is exactly what must not be granted. */
export function HtmlPreview({ sessionId, path, href }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [png, setPng] = useState<string | null>(null);
  const [data, setData] = useState<HtmlPreviewData | null>(null);
  const [error, setError] = useState(false);
  const [visible, setVisible] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const observerRef = useRef<IntersectionObserver | null>(null);
  const name = path.split("/").pop() ?? path;

  // Identity of what is currently loaded. Chat.tsx keys transcript rows by index
  // and artifacts by bare path, so switching sessions can hand this very
  // instance a new sessionId — without this the iframe would keep showing the
  // previously loaded document, and a latched error would follow it across.
  const key = `${sessionId}\u0000${path}`;
  const loadedKey = useRef(key);
  if (loadedKey.current !== key) {
    loadedKey.current = key;
    if (open) setOpen(false);
    if (data) setData(null);
    setPng(null);
    setExportError(null);
    if (error) setError(false);
    // A fresh artifact starts with a fresh retry budget; inheriting the count
    // would drop the new card straight into a long backoff, or straight into
    // the chip fallback if the previous one had already exhausted its attempts.
    if (attempt) setAttempt(0);
  }

  // Unlike the table preview — whose payload the server reduces to a screenful
  // of cells — this one carries up to HTML_MAX_BYTES of markup plus a live
  // document per card. Re-observing after load, rather than latching `visible`,
  // is what lets a card that scrolls far away drop both again, so memory tracks
  // what is on screen instead of how far the user has scrolled.
  //
  // A callback ref rather than a mount-time effect, because the host div is not
  // permanent: it unmounts whenever `error` swaps in the fallback chip. An
  // effect would go on observing that detached node, and since this callback
  // *assigns* visibility rather than latching it, the removal itself reports
  // false — after which nothing could set it true again and the card would spin
  // forever with no request in flight, until a full page reload.
  const hostRef = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node || !open) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => setVisible(entries.some((e) => e.isIntersecting)),
      { rootMargin: "400px" },
    );
    observer.observe(node);
    observerRef.current = observer;
  }, [open]);

  useEffect(() => {
    if (!open || !visible || data || error) return;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // Aborting on unmount is about the download, not the server: an HTML preview
    // can be the full 2 MB cap, and scrolling a transcript past several of them
    // otherwise leaves those bodies streaming into responses nobody will read.
    // The parse still finishes server-side — see fileHtmlPreview.
    const control = new AbortController();
    fileHtmlPreview(sessionId, path, control.signal)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        // The card is dropped and refetched on every scroll cycle, so the
        // budget has to reset on success — otherwise attempts accumulate across
        // unrelated visits and a card that was shed once early gives up for good.
        if (attempt) setAttempt(0);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // A 503 is the gate shedding load, the one failure the server designs
        // as recoverable. The retry has to be bounded and decorrelated though,
        // so the policy lives in one place shared with the table preview.
        const delay = previewRetryDelay(e, attempt);
        if (delay === null) {
          setError(true);
          return;
        }
        retry = setTimeout(() => setAttempt((n) => n + 1), delay);
      });
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      control.abort();
    };
  }, [open, visible, sessionId, path, data, error, attempt]);

  // Grace period rather than an immediate drop: without it, jitter across the
  // observer's own margin would refetch the document repeatedly while the user
  // is only nudging the scrollbar.
  useEffect(() => {
    if (visible || !data) return;
    const timer = setTimeout(() => setData(null), 10_000);
    return () => clearTimeout(timer);
  }, [visible, data]);

  // A failed preview must not hide the file: fall through to the plain chip so
  // the user can still open it.
  if (error) {
    return (
      <a
        className="claw-artifact-chip"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={t("chat.artifact.open", { name })}
      >
        <Icon icon={FileCode} size="sm" color="secondary" />
        <span className="claw-artifact-name">{name}</span>
        <Icon icon={ExternalLink} size="xsm" color="secondary" />
      </a>
    );
  }

  return (
    <div className="claw-html-preview" ref={hostRef}>
      <div className="claw-html-preview-head">
        <Icon icon={FileCode} size="sm" color="secondary" />
        <span className="claw-html-preview-name" title={name}>
          {name}
        </span>
        <a
          className="claw-html-preview-open"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={t("chat.artifact.open", { name })}
        >
          <Icon icon={ExternalLink} size="xsm" color="secondary" />
        </a>
        <button className="claw-preview-action" type="button" onClick={() => setOpen(value => !value)} aria-expanded={open} aria-label={open ? t("chat.artifact.hidePreview") : t("chat.artifact.preview")} title={open ? t("chat.artifact.hidePreview") : t("chat.artifact.preview")}><Icon icon={open ? X : Eye} size="sm" /></button>
        {open && <button className="claw-preview-action" type="button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-label={t("chat.preview.resize")} title={t("chat.preview.resize")}><Icon icon={expanded ? Minimize2 : Maximize2} size="sm" /></button>}
        {open && data && /<svg\b/i.test(data.html) && <button className="claw-preview-action" title={t("chat.preview.exportPng")} aria-label={t("chat.preview.exportPng")} type="button" disabled={exportBusy} onClick={async () => {
          const exportKey = key;
          setExportBusy(true); setExportError(null);
          try {
            const result = await filePngExport(sessionId, path);
            if (loadedKey.current === exportKey) { setPng(result.path); setExportError(result.warnings.join("; ") || null); }
          } catch { if (loadedKey.current === exportKey) setExportError(t("chat.preview.pngError")); }
          finally { setExportBusy(false); }
        }}>PNG{exportBusy ? "…" : ""}</button>}
        {png && <a href={fileUrl(sessionId, png)} target="_blank" rel="noopener noreferrer" download>{t("chat.preview.downloadPng")}</a>}
      </div>
      {open && exportError && <div className="claw-html-preview-foot" role="status">{exportError}</div>}

      {open && (!data ? (
        <div className="claw-html-preview-loading">
          <Spinner size="sm" />
        </div>
      ) : data.html.trim() === "" ? (
        // An agent's write step can fail partway and leave a 0-byte report.
        // Rendering that as a blank frame captioned "scripts don't run here"
        // reads as a successful preview of nothing.
        <div className="claw-html-preview-foot">
          <Text size="xsm" color="secondary">
            {t("chat.preview.empty")}
          </Text>
        </div>
      ) : (
        <>
          <iframe
            className="claw-html-preview-frame"
            style={expanded ? { height: "80vh" } : undefined}
            sandbox=""
            srcDoc={data.html}
            title={name}
            loading="lazy"
            referrerPolicy="no-referrer"
          />
          <div className="claw-html-preview-foot">
            <Text size="xsm" color="secondary">
              {data.truncated ? t("chat.preview.htmlTruncated") : t("chat.preview.htmlNoScripts")}
            </Text>
          </div>
        </>
      ))}
    </div>
  );
}
