import { useEffect, useState, type ReactNode } from "react";
import { fileFingerprint } from "./api";
import {
  dedupeArtifacts,
  duplicateCandidateGroups,
  hiddenDuplicates,
  provisionallyHidden,
} from "./artifact-dedupe";

/** Renders one turn's artifacts with a working file and its identical
 * `.deliveries/<hash>` published copy shown once (the published copy wins). */
export function DedupedArtifacts({ sessionId, paths, render }: {
  sessionId: string;
  paths: string[];
  render: (path: string) => ReactNode;
}) {
  const key = JSON.stringify([sessionId, paths]);
  const [result, setResult] = useState<{ key: string; hidden: Set<string> } | null>(null);
  useEffect(() => {
    const groups = duplicateCandidateGroups(paths);
    if (groups.length === 0) {
      setResult({ key, hidden: new Set() });
      return;
    }
    const controller = new AbortController();
    void (async () => {
      const digests = new Map<string, string | null>();
      for (const path of groups.flat()) {
        if (controller.signal.aborted) return;
        try {
          digests.set(path, (await fileFingerprint(sessionId, path, controller.signal)).sha256);
        } catch {
          /* A missing/unavailable fingerprint must never hide a file. */
        }
      }
      if (!controller.signal.aborted) setResult({ key, hidden: hiddenDuplicates(paths, digests) });
    })();
    return () => controller.abort();
    // `key` captures sessionId + paths by value; the arrays are rebuilt every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const hidden = result?.key === key ? result.hidden : provisionallyHidden(paths);
  return <>{dedupeArtifacts(paths, hidden).map(render)}</>;
}
