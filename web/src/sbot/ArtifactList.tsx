import { useEffect, useState, type ReactNode } from 'react';
import { fileFingerprint, visibleArtifacts } from './api';

/** Compare only potential duplicates, never collapse different content by name. */
export function ArtifactList({ sessionId, paths, render }: {
  sessionId: string; paths?: string[]; render: (path: string) => ReactNode;
}) {
  const candidates = visibleArtifacts(paths);
  const key = JSON.stringify([sessionId, candidates]);
  const [result, setResult] = useState<{ key: string; hidden: Set<string> } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const [, files] = JSON.parse(key) as [string, string[]];
    const groups = new Map<string, string[]>();
    for (const path of files) {
      const name = path.split('/').pop()!;
      groups.set(name, [...(groups.get(name) ?? []), path]);
    }
    void (async () => {
      const hidden = new Set<string>();
      for (const group of groups.values()) {
        if (group.length < 2) continue;
        const seen = new Set<string>();
        // Preserve immutable published copies in preference to working files.
        group.sort((a, b) => Number(b.startsWith('.deliveries/')) - Number(a.startsWith('.deliveries/')));
        for (const path of group) {
          if (controller.signal.aborted) return;
          try {
            const { sha256 } = await fileFingerprint(sessionId, path, controller.signal);
            if (sha256 && seen.has(sha256)) hidden.add(path);
            if (sha256) seen.add(sha256);
          } catch { /* A missing/unavailable preview must never hide a file. */ }
        }
      }
      if (!controller.signal.aborted) setResult({ key, hidden });
    })();
    return () => controller.abort();
  }, [key, sessionId]);
  return <div className="claw-artifacts">{candidates.filter(path => result?.key !== key || !result.hidden.has(path)).map(render)}</div>;
}
