import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import { Download, Eye, EyeOff, Film, Music } from "lucide-react";
import { useEffect, useState } from "react";
import { useT } from "./branding";
import { SaveToBlueprintButton } from "./SaveToBlueprintButton";

/** Video and audio files the browser can usually play natively. */
export const PLAYABLE_MEDIA_RE = /\.(mp4|webm|mov|m4v|mp3|wav|ogg|m4a)$/i;

type Props = { sessionId: string; path: string; href: string };

/** Inline player for video/audio artifacts. Nothing is fetched until the user
 * opens it, and then only the metadata (`preload="metadata"`): the file route
 * answers Range requests, so seeking streams just the part being watched
 * instead of pulling a whole render into the page. */
export function MediaPreview({ sessionId, path, href }: Props) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  const name = path.split("/").pop() ?? path;
  const isVideo = /\.(mp4|webm|mov|m4v)$/i.test(path);

  useEffect(() => {
    setOpen(false);
    setFailed(false);
  }, [sessionId, path]);

  return <div className="claw-document-preview">
    <div className="claw-document-preview-head">
      <Icon icon={isVideo ? Film : Music} size="sm" color="secondary" />
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
        <span>{open ? t("chat.artifact.hidePreview") : t("chat.artifact.play")}</span>
      </button>
    </div>
    {open && (failed
      ? <div className="claw-document-preview-foot">
        <Text size="xsm" color="secondary">{t("chat.preview.mediaUnsupported")}</Text>
      </div>
      : isVideo
        ? <video className="claw-media-preview-video" controls playsInline preload="metadata"
          src={href} aria-label={name} onError={() => setFailed(true)} />
        : <audio className="claw-media-preview-audio" controls preload="metadata"
          src={href} aria-label={name} onError={() => setFailed(true)} />)}
  </div>;
}
