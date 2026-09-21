import { Icon } from "@astryxdesign/core/Icon";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { Download, Eye, FileSpreadsheet, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { fileTablePreview, previewRetryDelay, type TablePreview as TablePreviewData } from "./api";
import { useT } from "../branding";
import { SaveToBlueprintButton } from "./SaveToBlueprintButton";

type Props = {
  sessionId: string;
  /** Workspace-relative artifact path. */
  path: string;
  /** Direct download/open URL for the untruncated file. */
  href: string;
};

/** Inline table view of a CSV/TSV/XLSX artifact.
 *
 * Fetching is deferred until the card scrolls into view: a long transcript can
 * hold many spreadsheet artifacts, and eagerly previewing all of them would
 * fire one request per artifact on every session load. The server already caps
 * rows/columns, so each response is small once it does happen. */
export function TablePreview({ sessionId, path, href }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<TablePreviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const name = path.split("/").pop() ?? path;

  // Chat.tsx keys transcript rows by index and artifacts by bare path, so a
  // session switch can hand this instance a new sessionId; without resetting,
  // the old table (or a latched error) would carry over into the new session.
  const key = `${sessionId} ${path}`;
  const loadedKey = useRef(key);
  if (loadedKey.current !== key) {
    loadedKey.current = key;
    if (open) setOpen(false);
    if (data) setData(null);
    if (error) setError(null);
    if (attempt) setAttempt(0);
    // `visible` latches true below and is never cleared by the observer, so it
    // has to be cleared here: the new artifact is a different card and has not
    // been seen yet. Carrying the old true through would fetch it the instant
    // the session switches, whatever the scroll position — exactly the eager
    // burst the deferral exists to prevent. Safe to reset because the observer
    // effect keys on `visible`, so clearing it rebuilds the observer and a
    // fresh observe() re-reports a card that really is on screen.
    if (visible) setVisible(false);
  }

  useEffect(() => {
    const host = hostRef.current;
    if (!open || !host || visible) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setVisible(true);
      },
      { rootMargin: "200px" },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, [open, visible]);

  // The `data`/`error` guard is load-bearing, not just an optimization. This
  // component latches `visible` permanently true, so without it a card that had
  // been shed once went on polling every few seconds for the life of the
  // transcript — including long after it scrolled out of view.
  useEffect(() => {
    if (!open || !visible || data || error) return;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const control = new AbortController();
    fileTablePreview(sessionId, path, control.signal)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        if (attempt) setAttempt(0);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // 503 is the gate shedding load, which the server pairs with a
        // Retry-After. Bounded and jittered by the shared policy so a burst of
        // shed cards cannot re-fire as a burst against an endpoint at capacity.
        const delay = previewRetryDelay(e, attempt);
        if (delay === null) {
          setError(e instanceof Error ? e.message : String(e));
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

  // A failed preview must not hide the file: fall through to the plain chip so
  // the user can still download it.
  if (error) {
    return (
      <a
        className="claw-artifact-chip"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={t("chat.artifact.open", { name })}
      >
        <Icon icon={FileSpreadsheet} size="sm" color="secondary" />
        <span className="claw-artifact-name">{name}</span>
        <Icon icon={Download} size="xsm" color="secondary" />
      </a>
    );
  }

  return (
    <div className="claw-table-preview" ref={hostRef}>
      <div className="claw-table-preview-head">
        <Icon icon={FileSpreadsheet} size="sm" color="secondary" />
        <span className="claw-table-preview-name" title={name}>
          {name}
        </span>
        {data?.sheet && <span className="claw-table-preview-sheet">{data.sheet}</span>}
        <a
          className="claw-table-preview-download"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={t("chat.artifact.open", { name })}
        >
          <Icon icon={Download} size="xsm" color="secondary" />
        </a>
        <SaveToBlueprintButton sessionId={sessionId} path={path} />
        <button className="claw-preview-action" type="button" onClick={() => setOpen(value => !value)} aria-expanded={open} aria-label={open ? t("chat.artifact.hidePreview") : t("chat.artifact.preview")} title={open ? t("chat.artifact.hidePreview") : t("chat.artifact.preview")}><Icon icon={open ? X : Eye} size="sm" /></button>
      </div>

      {open && (!data ? (
        <div className="claw-table-preview-loading">
          <Spinner size="sm" />
        </div>
      ) : data.columns.length === 0 ? (
        <div className="claw-table-preview-foot">
          <Text size="xsm" color="secondary">
            {t("chat.preview.empty")}
          </Text>
        </div>
      ) : (
        <>
          <div className="claw-table-preview-scroll">
            <table className="claw-table-preview-table">
              <thead>
                <tr>
                  {data.columns.map((c, i) => (
                    <th key={i} title={c}>
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c} title={cell}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(data.truncated || data.truncated_columns) && (
            <div className="claw-table-preview-foot">
              <Text size="xsm" color="secondary">
                {/* Both flags can be true at once — a wide report is the normal
                    case — so "both" is its own message rather than a join of
                    two sentences, which would read redundantly in any language. */}
                {data.truncated && data.truncated_columns
                  ? t("chat.preview.truncatedBoth", {
                      rows: String(data.rows.length),
                      cols: String(data.columns.length),
                    })
                  : data.truncated
                    ? t("chat.preview.truncated", { rows: String(data.rows.length) })
                    : t("chat.preview.truncatedColumns", { cols: String(data.columns.length) })}
              </Text>
            </div>
          )}
        </>
      ))}
    </div>
  );
}
