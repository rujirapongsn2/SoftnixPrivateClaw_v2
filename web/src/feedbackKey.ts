// Identity of an assistant reply for 👍/👎. Live replies have no DB id yet, so the
// key is derived from the content: length plus a 32-bit hash of its head and tail
// (cheap enough to run on every render of a long transcript).
export function messageKey(content: string): string {
  const sample = content.length > 400 ? content.slice(0, 200) + content.slice(-200) : content;
  let h = 5381;
  for (let i = 0; i < sample.length; i++) h = ((h << 5) + h + sample.charCodeAt(i)) | 0;
  return `${content.length}:${(h >>> 0).toString(36)}`;
}
