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

/**
 * Reduce assistant markdown to plain, speakable text for the "read aloud"
 * (text-to-speech) button — strips syntax a TTS engine would otherwise read
 * literally (e.g. "asterisk asterisk bold asterisk asterisk").
 */
export function stripMarkdownForSpeech(text: string): string {
  if (!text) return text;
  return text
    .replace(/```[\s\S]*?```/g, " ") // fenced code blocks
    .replace(/`([^`\n]*)`/g, "$1") // inline code
    .replace(/!\[[^\]]*]\([^)]*\)/g, "") // images
    .replace(/\[([^\]]*)]\([^)]*\)/g, "$1") // links -> link text
    .replace(/^#{1,6}\s+/gm, "") // heading markers
    .replace(/^>\s?/gm, "") // blockquote markers
    .replace(/^\s*[-*+]\s+/gm, "") // bullet list markers
    .replace(/^\s*\d+[.)]\s+/gm, "") // numbered list markers
    .replace(/^\s*(?:---|\*\*\*|___)\s*$/gm, "") // horizontal rules
    // Emphasis markers: require a non-whitespace char flanking the inside of
    // each pair (real markdown emphasis never wraps whitespace) so this can't
    // lazily jump to some unrelated later delimiter on the same line (e.g.
    // "3 * 4 = 12 and 5 * 6 = 30" — spaced multiplication, not italics).
    // Underscore variants additionally require a non-word char (or
    // start/end of string) outside each delimiter, since CommonMark treats
    // "_" as emphasis only outside word boundaries — otherwise this would
    // wreck snake_case identifiers and ENV__VAR names.
    .replace(/\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*/g, "$1") // bold+italic (*)
    .replace(/(?<!\w)___(?!\s)(.+?)(?<!\s)___(?!\w)/g, "$1") // bold+italic (_)
    .replace(/\*\*(?!\s)(.+?)(?<!\s)\*\*/g, "$1") // bold (*)
    .replace(/(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)/g, "$1") // bold (_)
    .replace(/(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)/g, "$1") // italic (_)
    .replace(/\*(?!\s)(.+?)(?<!\s)\*/g, "$1") // italic (*)
    .replace(/~~(.+?)~~/g, "$1") // strikethrough
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}
