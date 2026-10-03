import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { useT } from "./branding";

/** A chat artifact whose file the lifecycle sweep moved to trash. */
export function ArchivedArtifactCard({ path, trashId, onRestored }: { path: string; trashId: string; onRestored: () => void }) {
  const t = useT();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const name = path.split("/").pop() ?? path;

  const restore = async () => {
    setBusy(true);
    try {
      await api.restoreTrashedFile(trashId);
      onRestored();
    } catch (e) {
      toast({ body: `${t("chat.artifact.restoreFailed")}: ${String(e)}`, type: "error" });
      setBusy(false);
    }
  };

  return (
    <div className="claw-artifact-card claw-artifact-card--archived" title={path}>
      <span className="claw-artifact-card-icon">
        <Icon icon={Trash2} size="md" color="secondary" />
      </span>
      <span className="claw-artifact-card-body">
        <span className="claw-artifact-name">{name}</span>
        <span className="claw-artifact-card-type">{t("chat.artifact.inTrash")}</span>
      </span>
      <Button label={t("chat.artifact.restore")} variant="ghost" size="sm" isDisabled={busy} clickAction={() => void restore()} />
    </div>
  );
}
