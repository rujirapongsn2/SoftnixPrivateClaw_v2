import { IconButton } from "@astryxdesign/core/IconButton";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { FilePlus2 } from "lucide-react";
import { useState } from "react";
import { api } from "./shared-api";
import { useT } from "./branding";

const BLUEPRINT_FILE_RE = /\.(docx|xlsx|pptx)$/i;

export function SaveToBlueprintButton({ sessionId, path }: { sessionId: string; path: string }) {
  const t = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!BLUEPRINT_FILE_RE.test(path)) return null;
  const filename = path.split("/").pop() ?? path;
  const name = filename.replace(/\.[^.]+$/, "");

  const save = async () => {
    setBusy(true);
    try {
      await api.saveArtifactAsBlueprint(sessionId, path, { name, visibility: "private" });
      toast({ body: t("chat.artifact.savedToBlueprint"), type: "info", autoHideDuration: 2500 });
    } catch (e) {
      toast({ body: `${t("chat.artifact.blueprintSaveFailed")}: ${String(e)}`, type: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <IconButton
      label={t("chat.artifact.saveToBlueprint")}
      icon={<Icon icon={FilePlus2} size="xsm" />}
      variant="ghost"
      size="sm"
      isDisabled={busy}
      clickAction={() => void save()}
    />
  );
}
