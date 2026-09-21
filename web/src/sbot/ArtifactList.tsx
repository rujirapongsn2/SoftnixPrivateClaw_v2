import { ChevronDown, ChevronUp } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { fileFingerprint, visibleArtifacts } from './api';
import { useT } from '../branding';
import { isArtifactListExpanded, toggleArtifactList } from './artifact-list-state';

const INITIAL_ARTIFACTS = 3;

function artifactPriority(path: string): number {
  if (path.startsWith('.deliveries/')) return 0;
  const name = path.split('/').pop()?.toLowerCase() ?? path.toLowerCase();
  // Keep probable scratch/recovery files accessible, but move them behind
  // human-named results. This is deliberately ordering, not filtering: names
  // are not a reliable enough contract to make a user's file disappear.
  if (/^_|(?:^|[._-])(?:tmp|temp|test|backup|orig|draft)(?:[._-]|$)/.test(name)) return 2;
  return 1;
}

/** Compare only potential duplicates, never collapse different content by name. */
export function ArtifactList({ sessionId, paths, render }: {
  sessionId: string; paths?: string[]; render: (path: string) => ReactNode;
}) {
  const t = useT();
  const [expansion, setExpansion] = useState({ key: '', expanded: false });
  const candidates = visibleArtifacts(paths)
    .map((path, index) => ({ path, index }))
    .sort((a, b) => artifactPriority(a.path) - artifactPriority(b.path) || a.index - b.index)
    .map(({ path }) => path);
  const key = JSON.stringify([sessionId, candidates]);
  // Derive this during render so an expanded list never leaks into a new
  // session/list for the frame before an effect can reset local state.
  const expanded = isArtifactListExpanded(expansion, key);
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
  const files = candidates.filter(path => result?.key !== key || !result.hidden.has(path));
  const shown = expanded ? files : files.slice(0, INITIAL_ARTIFACTS);
  const remaining = Math.max(0, files.length - INITIAL_ARTIFACTS);
  return <div className="claw-artifacts">
    {shown.map(render)}
    {remaining > 0 && <button
      type="button"
      className="sbot-artifacts-more"
      aria-expanded={expanded}
      onClick={() => setExpansion(value => toggleArtifactList(value, key))}
    >
      {expanded ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
      {expanded
        ? t('chat.artifact.showLess')
        : t('chat.artifact.showMore', { count: String(remaining) })}
    </button>}
  </div>;
}
