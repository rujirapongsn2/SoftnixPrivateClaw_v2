import { Icon } from "@astryxdesign/core/Icon";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { Download, Eye, EyeOff, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { fileDocumentPreview, type DocumentPreview as DocumentPreviewData } from "./api";
import { useT } from "../branding";
import { SaveToBlueprintButton } from "./SaveToBlueprintButton";

type Props = { sessionId: string; path: string; href: string };

/** Text preview for DOCX/PPTX/Markdown, and the browser's PDF viewer. The original
 * file remains available through the Download action beside the preview. */
export function DocumentPreview({ sessionId, path, href }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<DocumentPreviewData | null>(null);
  const [error, setError] = useState(false);
  const name = path.split("/").pop() ?? path;
  const isPdf = /\.pdf$/i.test(path);

  useEffect(() => {
    setOpen(false);
    setData(null);
    setError(false);
  }, [sessionId, path]);

  useEffect(() => {
    if (!open || isPdf || data || error) return;
    const control = new AbortController();
    fileDocumentPreview(sessionId, path, control.signal).then(setData).catch(() => setError(true));
    return () => control.abort();
  }, [open, isPdf, data, error, sessionId, path]);

  return <div className="claw-document-preview">
    <div className="claw-document-preview-head">
      <Icon icon={FileText} size="sm" color="secondary" />
      <span className="claw-document-preview-name" title={name}>{name}</span>
      <a className="claw-document-preview-download" href={href} download={name}
        title={t("chat.artifact.download", { name })}>
        <Icon icon={Download} size="xsm" color="secondary" />
        <span>{t("chat.artifact.downloadAction")}</span>
      </a>
      <SaveToBlueprintButton sessionId={sessionId} path={path} />
      <button type="button" className="claw-document-preview-toggle"
        onClick={() => setOpen(value => !value)} aria-expanded={open}>
        <Icon icon={open ? EyeOff : Eye} size="xsm" />
        <span>{open ? t("chat.artifact.hidePreview") : t("chat.artifact.preview")}</span>
      </button>
    </div>
    {open && (
      isPdf ? <iframe src={href} title={name} className="claw-document-preview-pdf" />
        : !data && !error ? <div className="claw-document-preview-loading"><Spinner size="sm" /></div>
        : error ? <div className="claw-document-preview-foot"><Text size="xsm" color="secondary">{t("chat.preview.unavailable")}</Text></div>
        : data?.text.trim() === "" ? <div className="claw-document-preview-foot"><Text size="xsm" color="secondary">{t("chat.preview.noText")}</Text></div>
        : <>
          <pre className="claw-document-preview-body">{data?.text}</pre>
          {data?.truncated && <div className="claw-document-preview-foot"><Text size="xsm" color="secondary">{t("chat.preview.textTruncated")}</Text></div>}
        </>
    )}
  </div>;
}
