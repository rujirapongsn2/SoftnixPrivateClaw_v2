/** Inline render of a `.a2ui` artifact (written by the present_ui /
 * openrouter_price_watch tools). The file is canonical A2UI v0.9 JSON; it lives
 * in the user's workspace, so the card survives a reload like any artifact. */
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { useEffect, useState } from "react";
import { A2uiCard } from "./A2uiCard";

export const A2UI_ARTIFACT_RE = /\.a2ui$/i;

type Props = { href: string; interactive: boolean; onAction?: (t: string) => void };

export function A2uiArtifact({ href, interactive, onAction }: Props) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    setText(null);
    setError("");
    fetch(href, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = await r.text();
        if (alive) setText(body);
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [href]);
  if (error) return <Text size="sm" color="secondary">โหลดการ์ดไม่ได้ ({error})</Text>;
  if (text === null) return <Spinner size="sm" />;
  return <A2uiCard source={text} interactive={interactive} onAction={onAction} language="json" />;
}
