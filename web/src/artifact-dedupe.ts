/** Pure helpers for collapsing duplicate artifact cards in one chat turn.
 *
 * publish_artifact copies a workspace file to `.deliveries/<hash>/<name>`, and a
 * turn's artifact list then carries BOTH paths, so the same workbook/PDF showed
 * as two previews. Only same-basename paths are compared, and only identical
 * content (server sha256) is collapsed — a name alone never hides a file. Kept
 * React-free so `npm test` can run it under node --test. Mirrors
 * sbot/ArtifactList.tsx. */

export const isDelivery = (path: string): boolean => path.startsWith(".deliveries/");

const baseName = (path: string): string => path.split("/").pop() ?? path;

/** Same-basename groups worth fingerprinting, published copies first so the
 * immutable `.deliveries/` copy is the one kept. Exact repeats are dropped. */
export function duplicateCandidateGroups(paths: string[]): string[][] {
  const groups = new Map<string, string[]>();
  for (const path of new Set(paths)) {
    const name = baseName(path);
    groups.set(name, [...(groups.get(name) ?? []), path]);
  }
  return [...groups.values()]
    .filter((group) => group.length > 1)
    .map((group) => [...group].sort((a, b) => Number(isDelivery(b)) - Number(isDelivery(a))));
}

/** Paths to hold back while fingerprints load: a working file whose published
 * copy is in the same turn. Avoids a flash of two cards; anything that fails to
 * fingerprint is shown again. */
export function provisionallyHidden(paths: string[]): Set<string> {
  const hidden = new Set<string>();
  for (const group of duplicateCandidateGroups(paths)) {
    if (!group.some(isDelivery)) continue;
    for (const path of group) if (!isDelivery(path)) hidden.add(path);
  }
  return hidden;
}

/** Given fingerprints (null/missing = unknown), the paths to hide: every later
 * member of a group whose content was already seen. */
export function hiddenDuplicates(
  paths: string[],
  sha256: ReadonlyMap<string, string | null | undefined>,
): Set<string> {
  const hidden = new Set<string>();
  for (const group of duplicateCandidateGroups(paths)) {
    const seen = new Set<string>();
    for (const path of group) {
      const digest = sha256.get(path);
      if (!digest) continue;
      if (seen.has(digest)) hidden.add(path);
      seen.add(digest);
    }
  }
  return hidden;
}

/** Final list in original order: exact repeats dropped, duplicates hidden. */
export function dedupeArtifacts(paths: string[], hidden: ReadonlySet<string>): string[] {
  return [...new Set(paths)].filter((path) => !hidden.has(path));
}
