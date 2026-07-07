// Markdown cleanup for model output.
//
// Weaker open models (e.g. gemma) sometimes emit an entire bullet list inline
// with NO line breaks, e.g. "intro: * **A:** … * **B:** …". With no newlines
// the `*` bullet markers sit next to `**bold**` labels, and the parser pairs
// the stray asterisks into emphasis — rendering large runs as italic (a wall of
// slanted, hard-to-read text, especially in Thai). We restore the line breaks
// the model omitted so each bullet parses as a real list item instead.
//
// Only inline, space-flanked `*` markers are touched, and only when there are
// several of them (a real list). A properly formatted answer keeps its bullets
// at line start (preceded by a newline, not a space), so this never rewrites
// well-behaved markdown — the existing rendering is unaffected.

// A lone `*` surrounded by spaces/tabs mid-line: an inline bullet marker. `**`
// never matches (its asterisks are adjacent, with no space between them).
const INLINE_BULLET_RE = /[ \t]+\*[ \t]+/g;

// A numbered bold "section header" sitting mid-line, e.g. "… SHA256) **2. Foo:**".
// The lookbehind requires a non-newline char + space before it, so a header
// already at line start (well-formatted output) is left untouched.
const INLINE_HEADING_RE = /(?<=[^\n])[ \t]+(\*\*\d+[.)]\s)/g;

// Private-use sentinels wrap stashed code spans so restoring them can't collide
// with digits/content. Built via fromCharCode to keep the source plain ASCII.
const OPEN = String.fromCharCode(0xe000);
const CLOSE = String.fromCharCode(0xe001);
const RESTORE_RE = new RegExp(`${OPEN}(\\d+)${CLOSE}`, "g");

/**
 * Restore omitted line breaks in under-formatted assistant markdown so an
 * inline bullet list parses (and renders) as a real list instead of collapsing
 * into italic runs. Returns the input unchanged when there's nothing to fix.
 */
export function sanitizeModelMarkdown(text: string): string {
  if (!text || !text.includes("*")) return text;

  // Protect code (fenced + inline) so asterisks inside code are never rewritten.
  const code: string[] = [];
  let work = text
    .replace(/```[\s\S]*?```/g, (m) => `${OPEN}${code.push(m) - 1}${CLOSE}`)
    .replace(/`[^`\n]*`/g, (m) => `${OPEN}${code.push(m) - 1}${CLOSE}`);

  // Two or more inline bullets ⇒ the model emitted a list without line breaks.
  // Put each bullet on its own line, and break inline numbered headers onto
  // their own line too. (Real, already-formatted bullets/headers sit at line
  // start — preceded by `\n`, not a space — so they don't match here.)
  const bullets = work.match(INLINE_BULLET_RE);
  if (bullets && bullets.length >= 2) {
    work = work.replace(INLINE_HEADING_RE, "\n\n$1").replace(INLINE_BULLET_RE, "\n* ");
    work = work.replace(/\n{3,}/g, "\n\n"); // collapse any runs we introduced
  }

  return work.replace(RESTORE_RE, (_s, i) => code[Number(i)]);
}
