"""Bounded server-side previews for files in a user's workspace: CSV/XLSX as
parsed tables, HTML as bounded source for a sandboxed render.

Parsing happens here rather than in the browser on purpose: a 500k-row export
must never be shipped over the wire just so the UI can show the first screenful.
Every limit below bounds the work done *before* anything is returned, so preview
cost stays independent of how large the underlying file is.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

MAX_ROWS = 200
MAX_COLS = 40
MAX_CELL_CHARS = 300

# CSV is streamed, so this caps memory, not the file we accept: we decode at
# most this many bytes and parse whatever complete rows fit.
CSV_READ_BYTES = 2 * 1024 * 1024

# Deliberately below routes._MAX_ATTACHMENT_BYTES (20_000_000): a user can
# upload an arbitrary .xlsx as a chat attachment and then ask to preview it, so
# a cap above the upload limit would be unreachable and guard nothing.
XLSX_MAX_BYTES = 8 * 1024 * 1024

# openpyxl must open the whole zip container (and materialize sharedStrings.xml
# in full) before the first row is readable, so the MAX_ROWS break below cannot
# bound a decompression bomb — the cost is already paid inside load_workbook().
# These two caps are what actually contain it.
XLSX_MAX_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
XLSX_MAX_ENTRIES = 512

PREVIEWABLE_SUFFIXES = (".csv", ".tsv", ".xlsx")

# An HTML report is rendered, not parsed, so the browser needs the real markup —
# there is no reduced server-side shape to send instead, the way there is for a
# table. This cap is what keeps that from meaning "ship whatever the agent
# wrote": the read stops here, the card says it was cut, and the untruncated
# file stays one click away on the download link.
HTML_MAX_BYTES = 2 * 1024 * 1024

PREVIEWABLE_HTML_SUFFIXES = (".html", ".htm", ".svg")

# Office files are zip containers. A DOCX layout needs a browser-grade renderer
# to reproduce faithfully, but extracting its paragraphs gives a fast, safe
# preview that answers the useful question before a download: "is this the
# report I asked for?" Legacy .doc is binary and intentionally remains a
# download-only format.
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
DOCUMENT_XML_MAX_BYTES = 8 * 1024 * 1024
DOCUMENT_TEXT_MAX_CHARS = 120_000
PREVIEWABLE_DOCUMENT_SUFFIXES = (".docx", ".pptx", ".md", ".txt", ".json", ".xml", ".yaml", ".yml", ".log", ".py", ".js", ".ts", ".tsx", ".css", ".sql", ".sh", ".ini", ".toml")

# Sandbox and CSP cover different halves, and neither substitutes for the other.
#
# What only the CSP stops: subresource loads. There is no sandbox flag for them,
# so without this policy an agent-authored report phones home through <img
# src=https://...>, a CSS url() or a webfont, leaking the reader's IP and a
# read-receipt on scroll alone. srcdoc inherits the embedder's CSP, but this app
# serves none, so the document has to carry its own.
#
# What only the sandbox stops: everything that *navigates*. No CSP directive
# governs a frame navigating itself — navigate-to was dropped from the spec and
# never shipped, and default-src does not apply to navigations. A bare `sandbox`
# is what blocks <meta http-equiv=refresh>, via the sandboxed-automatic-features
# flag that only allow-scripts relaxes (HTML "shared declarative refresh steps").
# So granting allow-scripts — say, to make a chart library render — would open a
# phone-home channel that NOTHING here catches. Do not add it.
#
# form-action is belt-and-braces: sandbox already blocks form submission without
# allow-forms, but form-action does not fall back to default-src, so stating it
# means the policy still says no if that flag is ever added.
#
# Mirrors what _ACTIVE_CONTENT_CSP gives the /files/ download route for the same
# bytes; 'unsafe-inline' styles and data: images keep real reports rendering.
_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'\">"
)

# The app itself is light-only (styles.css forces `color-scheme: light`), so a
# report that renders dark inside the card looks like a different product. The
# agent is told to write light documents (the html-report built-in skill), but
# that only fixes files written from here on, and it does not cover the specific
# case where a light-looking document still carries a
# `@media (prefers-color-scheme: dark)` block — that block keys off the READER's
# OS setting, so the same file renders light for the author and dark for
# everyone in dark mode.
#
# Declaring the frame's color scheme is what neutralizes that: browsers evaluate
# prefers-color-scheme against the document's own used color scheme, so a
# document restricted to light no longer matches the dark branch. Emitted as a
# meta rather than an injected <style> because meta behaves as a declaration at
# the *start* of the author origin, so it changes nothing a document states for
# itself in CSS.
#
# Only affects the preview. The /files download route serves the file's real
# bytes, so what the user opens in a tab is still exactly what was written. That
# divergence is the point for an accidentally-dark report, but it would be a bug
# for a deliberately dark one, which is why _declares_color_scheme() below backs
# out entirely rather than relying on the CSS override: browsers honour the
# FIRST name=color-scheme meta in tree order, and ours is injected ahead of the
# document, so a report that declares its own would otherwise have no way to win.
_COLOR_SCHEME_META = '<meta name="color-scheme" content="light">'

# Whether the document already says what scheme it wants — its own
# <meta name="color-scheme">, or a `color-scheme:` CSS declaration anywhere in
# it. Either means a human asked for this (the agent is told to state it
# explicitly when a dark report was requested), so the preview leaves it alone
# and the card matches what the download route serves.
#
# The lookbehind is load-bearing: without it `prefers-color-scheme: dark` — the
# exact pattern this whole mechanism exists to defuse — matches as a declaration
# and disables the injection in precisely the case that needs it.
_DECLARES_COLOR_SCHEME_RE = re.compile(
    r"""<meta\s[^>]*name\s*=\s*["']?color-scheme\b|(?<![-\w])color-scheme\s*:""",
    re.IGNORECASE,
)


def _declares_color_scheme(markup: str) -> bool:
    return _DECLARES_COLOR_SCHEME_RE.search(markup) is not None

# Keep the doctype first. A <meta> ahead of it makes the parser discard the
# doctype token, which for an ordinary document means quirks mode \u2014 not here,
# since srcdoc documents are explicitly exempt from that clause of the "initial"
# insertion mode, but the preview should not depend on staying in an iframe to
# render the way the same bytes do on the /files download route. Anywhere in
# this leading position still lands in <head>: the tree builder implicitly opens
# one, and a later explicit <html>/<head> merges into it rather than restarting.
_DOCTYPE_RE = re.compile(r"\A\ufeff?\s*<!doctype[^>]*>", re.IGNORECASE)


def _with_csp(markup: str) -> str:
    """Inject the preview's own Content-Security-Policy at the front of `markup`,
    plus a light color scheme unless the document already picks one."""
    head = _CSP_META if _declares_color_scheme(markup) else _CSP_META + _COLOR_SCHEME_META
    doctype = _DOCTYPE_RE.match(markup)
    if doctype:
        return markup[: doctype.end()] + head + markup[doctype.end() :]
    return head + markup


class PreviewError(Exception):
    """Raised with a user-safe message when a file can't be previewed."""


# openpyxl surfaces structural damage as KeyError/ValueError/TypeError from deep
# inside its readers; zipfile/zlib raise their own for a corrupt container or a
# corrupt compressed stream inside an otherwise well-formed one. Caught only
# around the actual openpyxl/zip calls below (not the whole preview_table call,
# the way this used to be caught at the router) so a genuine bug in this
# module's own code still surfaces as a 500 instead of a misleading 400.
_XLSX_PARSE_ERRORS = (KeyError, ValueError, TypeError, zipfile.BadZipFile, zlib.error)


# --- Encoding detection -------------------------------------------------------

# Thai combining vowels and tone marks. In correct Thai these always attach to a
# preceding Thai letter or vowel; landing after an ASCII letter means we guessed
# the codepage wrong.
_THAI_MARKS = frozenset("ัิีึืฺุู็่้๊๋์ํ๎")

# A Thai word is several characters long, so real Thai text always contains runs
# of Thai characters. Mis-decoded Western text does not: each accented byte or
# symbol becomes exactly ONE isolated Thai character surrounded by ASCII.
_MIN_THAI_RUN = 3


def _is_thai(ch: str) -> bool:
    return "ก" <= ch <= "๛"


def _looks_like_thai(text: str) -> bool:
    """Whether a cp874 decode is plausibly real Thai rather than mis-decoded
    Western text.

    cp874 and cp1252 overlap almost completely (cp874's undecodable bytes are a
    strict superset of cp1252's), so "it decoded without raising" tells us
    nothing — Windows-1252 text decodes "successfully" into Thai gibberish and
    vice versa. Ordering the two codecs is therefore not a fix in either
    direction; the decode has to be judged on whether the result reads as Thai.

    The judgement weighs evidence rather than vetoing on any single oddity,
    because one file gets one codepage for all of its cells: a stray character
    must not flip a whole Thai document into mojibake, nor one Thai-looking byte
    flip a whole Western one. Evidence *for* Thai is a run of at least
    _MIN_THAI_RUN Thai characters — a real syllable, which mis-decoding
    essentially never produces, since each Western byte yields a single isolated
    Thai character. Evidence *against* is a tone/vowel mark with no Thai letter
    to attach to, which is exactly what mis-decoding does produce ("café" ->
    "caf" + a stray tone mark). No evidence either way (a lone "£" or "®")
    means not Thai, which is what keeps punctuation-only Western files out of
    cp874.

    A single qualifying run is not enough evidence on its own: a run of
    several *consecutive* accented Latin-1 characters with no ASCII in between
    (e.g. "ÀÈÌÒÙ", an all-caps European acronym or a decorative header) can
    decode via cp874 into a syllable-shaped Thai run with no orphan marks,
    since cp874's Thai vowel/tone signs happen to sit at the same byte offsets
    as several cp1252 accented letters. Real Thai CSV content — this is judged
    over the whole file, not one cell — essentially always produces more than
    one such run (a header cell plus data cells, at minimum), so requiring at
    least two independently raises the bar against a single decorative
    coincidence without costing real Thai files anything.

    Deliberately scans the whole string. Sampling a prefix instead is not a
    cheaper approximation of the same answer: a report whose head is a large
    inline <style> block or a base64 chart, or a CSV whose leading columns are
    ids and timestamps, is pure ASCII for far longer than any prefix worth
    scanning. Finding no Thai there sends the decode on to cp1252, which accepts
    the bytes without raising, so the *entire* document turns to mojibake rather
    than just the unscanned tail. A full pass over the 2 MB cap measures ~70 ms.
    """
    words = 0
    orphan_marks = 0
    run = 0
    prev = ""
    for ch in text:
        if _is_thai(ch):
            if ch in _THAI_MARKS and not ("ก" <= prev <= "๎"):
                orphan_marks += 1
            run += 1
        else:
            words += run >= _MIN_THAI_RUN
            run = 0
        prev = ch
    words += run >= _MIN_THAI_RUN
    return words >= 2 and words > orphan_marks


def decode_text_bytes(raw: bytes) -> str:
    """Decode text bytes, tolerating the regional codepages Excel still exports
    (Windows-1252, Thai TIS-620/cp874) without silently mangling either.

    UTF-8 is tried strictly first: a multi-byte UTF-8 sequence is structurally
    self-validating, so a clean strict decode is effectively never a false
    positive. Only the ambiguous single-byte codepages need the heuristic above.
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    try:
        thai = raw.decode("cp874")
    except UnicodeDecodeError:
        thai = None
    if thai is not None and _looks_like_thai(thai):
        return thai

    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        # cp1252's undecodable bytes are a strict subset of cp874's, so reaching
        # here means neither codepage fits (`thai` is None too). Re-read as utf-8
        # and let U+FFFD mark the bad bytes — with -sig, because a leading BOM
        # would otherwise survive into the first header cell, where str.strip()
        # does not remove it and the admin importer's column matching then fails.
        return raw.decode("utf-8-sig", errors="replace")


# --- Shared shaping -----------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        text = value.isoformat()
    else:
        text = str(value)
    return text[:MAX_CELL_CHARS]


def _shape(rows: list[list[str]], *, truncated: bool, truncated_columns: bool) -> dict:
    """Split parsed rows into a header + body, padded to a rectangle so the UI
    can render a table without per-row length checks. Rows arrive already
    clipped to MAX_COLS by the parsers."""
    width = max((len(r) for r in rows), default=0)
    padded = [r + [""] * (width - len(r)) for r in rows]
    header = padded[0] if padded else []
    return {
        "columns": [c or f"#{i + 1}" for i, c in enumerate(header)],
        "rows": padded[1:],
        "truncated": truncated,
        "truncated_columns": truncated_columns,
    }


# csv.reader rejects any field over 131072 chars by default, which would abort a
# whole parse over a single long note column — even though every consumer here
# clips values anyway (_cell() to MAX_CELL_CHARS, read_csv to _MAX_CHARS).
#
# Raised once, process-wide, rather than saved/restored around each parse.
# csv.field_size_limit() is a single global, so the old scoped version needed a
# lock to stop one thread's restore stomping another's raise mid-parse — and
# because csv.reader consults the limit lazily per field, that lock had to be
# held across the entire read. Three call sites shared it (here, the read_csv
# tool, and admin bulk import), so one slow CSV serialized all of them on a
# 2-worker pool while still occupying a worker. Every call site wanted this
# same raised value and none wants the default, so there is nothing to scope:
# setting it once removes the contention and the race together. Memory stays
# bounded by how much each caller reads, which is already capped.
csv.field_size_limit(CSV_READ_BYTES)


# --- Parsers ------------------------------------------------------------------


def _preview_csv(path: Path, delimiter: str) -> dict:
    with path.open("rb") as fh:
        raw = fh.read(CSV_READ_BYTES)
        capped = len(fh.read(1)) > 0

    if capped:
        # Cut back to the last newline. Slicing at an arbitrary byte offset can
        # land mid-UTF-8-sequence, which makes the strict utf-8 attempt raise
        # and silently demotes the WHOLE file to a single-byte codepage — the
        # entire table would render as mojibake, not just the final row.
        # 0x0A is unambiguous in utf-8 and in both codepages, so it is a safe
        # boundary.
        newline = raw.rfind(b"\n")
        if newline == -1:
            raise PreviewError("the first row is too large to preview")
        raw = raw[: newline + 1]

    text = decode_text_bytes(raw)
    rows: list[list[str]] = []
    truncated = capped
    truncated_columns = False
    row_cap_hit = False
    try:
        for row in csv.reader(io.StringIO(text), delimiter=delimiter):
            # rows[0] is the header, so the limit is MAX_ROWS *body* rows.
            if len(rows) > MAX_ROWS:
                truncated = True
                row_cap_hit = True
                break
            truncated_columns = truncated_columns or len(row) > MAX_COLS
            rows.append([_cell(c) for c in row[:MAX_COLS]])
    except csv.Error as exc:
        raise PreviewError("file could not be parsed") from exc

    # Trimming to a newline guarantees whole lines, but a quoted field spanning
    # newlines can still straddle the cut. An odd quote count means the last
    # record was left unterminated and swallowed the remaining buffer, so drop
    # it — and only then, or a cap would needlessly discard a good row. But
    # that quote count is over the WHOLE capped buffer, not just what
    # csv.reader consumed: if MAX_ROWS ended the loop first (common for any
    # file with more than MAX_ROWS short rows in its first CSV_READ_BYTES),
    # the last row in `rows` was fully parsed and unrelated to whatever is in
    # the untouched remainder — popping it would silently drop a good row.
    if capped and not row_cap_hit and text.count('"') % 2 and len(rows) > 1:
        rows.pop()

    return {**_shape(rows, truncated=truncated, truncated_columns=truncated_columns), "sheet": None}


def _assert_workbook_within_budget(path: Path) -> None:
    """Reject decompression bombs before handing the file to openpyxl.

    Declared sizes in the zip central directory are attacker-controlled, so this
    measures the real decompressed output instead, stopping as soon as it
    exceeds the budget — the check itself is bounded by the budget, not by what
    the archive claims."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > XLSX_MAX_ENTRIES:
                raise PreviewError("file is too large to preview")
            total = 0
            for entry in entries:
                with archive.open(entry) as stream:
                    while chunk := stream.read(262144):
                        total += len(chunk)
                        if total > XLSX_MAX_UNCOMPRESSED_BYTES:
                            raise PreviewError("file is too large to preview")
    except (zipfile.BadZipFile, zlib.error):
        # BadZipFile covers a broken container (bad central directory etc.);
        # zlib.error is what a valid container with a corrupt DEFLATE stream
        # actually raises here, from inside stream.read() above.
        raise PreviewError("file could not be parsed") from None


def _formula_fallback_rows(path: Path, sheet_title: str, limit: int) -> list[tuple]:
    """Up to `limit` rows of formula text for cells the data_only=True read
    left blank.

    openpyxl's read-only parser captures either the cached <v> result or the
    <f> formula text per load, never both, so recovering a formula that was
    never opened in Excel — and so never got a cached result — needs a second
    pass. Bounded the same way the primary read is, so the extra pass stays
    cheap even on a wide/tall sheet. Best-effort: any trouble here just means
    those particular cells stay blank, exactly as before this existed — it
    must never fail a preview that already succeeded without it."""
    from openpyxl import load_workbook

    try:
        fwb = load_workbook(path, read_only=True, data_only=False)
    except _XLSX_PARSE_ERRORS:
        return []
    try:
        sheet = fwb[sheet_title] if sheet_title in fwb.sheetnames else fwb.worksheets[0]
        return list(
            itertools.islice(
                sheet.iter_rows(max_col=MAX_COLS, values_only=True),
                limit,
            )
        )
    except _XLSX_PARSE_ERRORS:
        return []
    finally:
        fwb.close()


def _with_formula_fallback(row: tuple, formula_row: tuple | None) -> tuple:
    """Swap a None cell for its formula text, but only where data_only=True
    actually lost information (a formula with no cached result) rather than a
    genuinely empty cell — which reads as None in both passes and is left
    alone. Never widens `row`: the formula pass reads with max_col=MAX_COLS,
    which pads short sheets out to that width, and zip-style pairing would
    otherwise leak those pad columns into a sheet with far fewer real ones."""
    if formula_row is None:
        return row
    out = list(row)
    for i in range(min(len(row), len(formula_row))):
        f = formula_row[i]
        if row[i] is None and isinstance(f, str) and f.startswith("="):
            out[i] = f
    return tuple(out)


def _preview_xlsx(path: Path) -> dict:
    if path.stat().st_size > XLSX_MAX_BYTES:
        raise PreviewError("file is too large to preview")
    _assert_workbook_within_budget(path)

    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except _XLSX_PARSE_ERRORS as exc:
        raise PreviewError("file could not be parsed") from exc
    try:
        if not workbook.worksheets:
            raise PreviewError("workbook has no sheets")
        sheet = workbook.worksheets[0]
        raw_rows: list[tuple] = []
        truncated = False
        truncated_columns = False
        for row in sheet.iter_rows(values_only=True):
            if len(raw_rows) > MAX_ROWS:
                truncated = True
                break
            # Clip BEFORE building cells: a 5000-column sheet otherwise costs
            # ~1M _cell() calls to return a 40-column preview, and these parses
            # run in a shared thread pool where that CPU is stolen from every
            # other tenant.
            truncated_columns = truncated_columns or len(row) > MAX_COLS
            raw_rows.append(row[:MAX_COLS])
        # Only pay for the second pass when the first one can actually have lost
        # something. A formula with no cached result reads as None here, so no
        # None in the preview window means no formula to recover — and that pass
        # costs a full second parse of the workbook, on a 2-worker pool.
        formula_rows: list[tuple] = []
        if any(c is None for row in raw_rows for c in row):
            formula_rows = _formula_fallback_rows(path, sheet.title, len(raw_rows))
        rows: list[list[str]] = [
            [
                _cell(c)
                for c in _with_formula_fallback(row, formula_rows[i] if i < len(formula_rows) else None)
            ]
            for i, row in enumerate(raw_rows)
        ]
        return {
            **_shape(rows, truncated=truncated, truncated_columns=truncated_columns),
            "sheet": sheet.title,
            "sheets": [ws.title for ws in workbook.worksheets],
        }
    except _XLSX_PARSE_ERRORS as exc:
        raise PreviewError("file could not be parsed") from exc
    finally:
        # read_only mode holds the zip handle open until closed explicitly.
        workbook.close()


def preview_html(path: str | Path) -> dict:
    """Read at most HTML_MAX_BYTES of an .html/.htm file as markup for the UI to
    render in a sandboxed iframe. Blocking — call via a thread.

    Nothing here sanitizes the markup, deliberately. Stripping tags server-side
    would both mangle legitimate reports and invite trusting the result. Safety
    comes from two things the caller must preserve: the iframe's bare `sandbox`
    attribute (opaque origin, no scripts, no forms) and the _CSP_META injected
    below, which is what actually stops the document reaching the network —
    `sandbox` alone does not.
    """
    resolved = Path(path)
    if resolved.suffix.lower() not in PREVIEWABLE_HTML_SUFFIXES:
        raise PreviewError("preview is not supported for this file type")

    with resolved.open("rb") as fh:
        raw = fh.read(HTML_MAX_BYTES)
        truncated = len(fh.read(1)) > 0

    if truncated:
        # Cut back to the last tag boundary. Slicing at an arbitrary byte offset
        # can land mid-UTF-8-sequence, which makes the strict utf-8 attempt raise
        # and silently demotes the WHOLE document to a single-byte codepage — the
        # entire page would render as mojibake, not just the dropped tail. ">" is
        # unambiguous in utf-8 and in both codepages, so it is a safe boundary.
        end = raw.rfind(b">")
        if end == -1:
            raise PreviewError("the first HTML tag is too large to preview")
        raw = raw[: end + 1]

    markup = decode_text_bytes(raw)
    if resolved.suffix.lower() == '.svg':
        # XML declarations/DTDs do not belong in an HTML srcdoc. Never resolve entities.
        if re.search(r'<!DOCTYPE|<!ENTITY', markup, re.I):
            raise PreviewError("SVG declarations are not supported")
        markup = re.sub(r'<\?xml.*?\?>', '', markup, flags=re.S)
        markup = '<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}svg{display:block;max-width:100%;height:auto}</style></head><body>' + markup + '</body></html>'
    # A blank document needs no policy, and returning one would leave the UI
    # unable to tell "empty report" from "report that starts with our own meta"
    # — it would render a blank frame captioned as a successful preview.
    return {"html": _with_csp(markup) if markup.strip() else "", "truncated": truncated}


def _preview_text_document(path: Path) -> dict:
    with path.open("rb") as fh:
        raw = fh.read(DOCUMENT_TEXT_MAX_CHARS + 1)
    truncated = len(raw) > DOCUMENT_TEXT_MAX_CHARS
    return {"text": decode_text_bytes(raw[:DOCUMENT_TEXT_MAX_CHARS]), "truncated": truncated}


def _preview_docx(path: Path) -> dict:
    if path.stat().st_size > DOCUMENT_MAX_BYTES:
        raise PreviewError("file is too large to preview")
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                document = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise PreviewError("file could not be parsed") from exc
            if document.file_size > DOCUMENT_XML_MAX_BYTES:
                raise PreviewError("file is too large to preview")
            raw = archive.read(document)
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, zipfile.BadZipFile, zlib.error) as exc:
        raise PreviewError("file could not be parsed") from exc

    # Text in Word's body and table cells is represented by the same w:t
    # elements. Preserve paragraph boundaries while skipping formatting markup.
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    used = 0
    truncated = False
    for paragraph in root.iter(f"{ns}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{ns}t")).strip()
        if not text:
            continue
        next_size = used + len(text) + (1 if paragraphs else 0)
        if next_size > DOCUMENT_TEXT_MAX_CHARS:
            remaining = max(0, DOCUMENT_TEXT_MAX_CHARS - used - (1 if paragraphs else 0))
            if remaining:
                paragraphs.append(text[:remaining])
            truncated = True
            break
        paragraphs.append(text)
        used = next_size
    return {"text": "\n".join(paragraphs), "truncated": truncated}


def _preview_pptx(path: Path) -> dict:
    """Extract bounded slide text; retain slide order from the presentation relationships."""
    if path.stat().st_size > DOCUMENT_MAX_BYTES:
        raise PreviewError('file is too large to preview')
    import posixpath

    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > 2048:
                raise PreviewError('presentation has too many parts to preview')
            consumed = 0

            def read_xml(name):
                nonlocal consumed
                entry = archive.getinfo(name)
                consumed += entry.file_size
                if consumed > DOCUMENT_XML_MAX_BYTES:
                    raise PreviewError('file is too large to preview')
                return ElementTree.fromstring(archive.read(entry))

            presentation = read_xml('ppt/presentation.xml')
            relationships = read_xml('ppt/_rels/presentation.xml.rels')
            targets = {r.get('Id'): r.get('Target', '') for r in relationships if r.get('TargetMode') != 'External'}
            pns = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
            rns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
            ans = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
            parts = []
            for index, slide in enumerate(presentation.iter(pns + 'sldId'), 1):
                target = targets[slide.get(rns + 'id')]
                name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'ppt/' + target)
                if not name.startswith('ppt/slides/'):
                    raise PreviewError('invalid slide reference')
                root = read_xml(name)
                paragraphs = [''.join(t.text or '' for t in p.iter(ans + 't')) for p in root.iter(ans + 'p')]
                parts.append(f'Slide {index}\n' + '\n'.join(paragraphs))
                text = '\n\n'.join(parts)
                if len(text) > DOCUMENT_TEXT_MAX_CHARS:
                    return {'text': text[:DOCUMENT_TEXT_MAX_CHARS], 'truncated': True}
            return {'text': '\n\n'.join(parts), 'truncated': False}
    except (KeyError, ElementTree.ParseError, zipfile.BadZipFile, zlib.error) as exc:
        raise PreviewError('file could not be parsed') from exc


def preview_document(path: str | Path) -> dict:
    """Extract a bounded, non-executable text preview from a DOCX/MD/TXT file.

    This is intentionally a content preview, not an Office renderer. The real,
    unchanged file remains available through the authenticated download URL.
    """
    resolved = Path(path)
    suffix = resolved.suffix.lower()
    if suffix not in PREVIEWABLE_DOCUMENT_SUFFIXES:
        raise PreviewError("preview is not supported for this file type")
    if suffix == ".docx":
        return _preview_docx(resolved)
    if suffix == '.pptx':
        return _preview_pptx(resolved)
    return _preview_text_document(resolved)


def preview_table(path: str | Path) -> dict:
    """Parse a bounded preview of a CSV/TSV/XLSX file. Blocking — call via
    asyncio.to_thread."""
    resolved = Path(path)
    suffix = resolved.suffix.lower()
    if suffix == ".xlsx":
        return _preview_xlsx(resolved)
    if suffix in (".csv", ".tsv"):
        return _preview_csv(resolved, "\t" if suffix == ".tsv" else ",")
    raise PreviewError("preview is not supported for this file type")


def preview_fingerprint(path: str | Path) -> dict:
    """Bounded content identity for collapsing duplicate attachment cards."""
    import hashlib
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > DOCUMENT_MAX_BYTES:
                return {'sha256': None}
            digest.update(chunk)
    return {'sha256': digest.hexdigest()}
