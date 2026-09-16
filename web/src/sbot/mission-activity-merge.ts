/** Merge durable observation rows without moving existing live chat/tool rows. */
export function mergeMissionActivity<T extends {
  kind: string;
  deliveryId?: string;
  seq?: number;
  missionActivity?: { revision: number; status?: string };
}>(previous: T[], incoming: T[]): T[] {
  const replacements = new Map(incoming.map(row => [row.deliveryId, row]));
  const next = previous.map(item => {
    if (item.kind !== 'message' || !item.deliveryId) return item;
    const replacement = replacements.get(item.deliveryId);
    replacements.delete(item.deliveryId);
    if (!replacement) return item;
    return replacement.missionActivity && item.missionActivity
      && replacement.missionActivity.revision === item.missionActivity.revision
      && replacement.missionActivity.status === item.missionActivity.status
      ? item
      : replacement;
  });
  const sequences = previous.flatMap(item => item.kind === 'message' && item.seq != null ? [item.seq] : []);
  const oldest = sequences.length ? Math.min(...sequences) : 0;
  for (const item of replacements.values()) {
    // Older pages remain behind their pagination cursor, never appended at the bottom.
    if (item.kind !== 'message' || (item.seq ?? 0) < oldest) continue;
    const index = next.findIndex(other => other.kind === 'message' && other.seq != null && other.seq > (item.seq ?? 0));
    if (index < 0) next.push(item); else next.splice(index, 0, item);
  }
  return next;
}
