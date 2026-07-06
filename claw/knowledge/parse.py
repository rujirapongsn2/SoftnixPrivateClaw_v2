"""Extract plain text from uploaded knowledge documents.

Supports pdf, docx, html, markdown, and plain text. Parsing libraries are
imported lazily so a missing optional dep degrades to a clear error instead of
crashing import. The caller handles the returned error string.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, skipping script/style/head noise."""

    _SKIP = {"script", "style", "head", "meta", "link", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _clean(text: str) -> str:
    # Collapse runs of blank lines / trailing whitespace so chunks stay tidy.
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip():
            blanks = 0
            out.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def extract_text(data: bytes, filename: str, mime: str) -> tuple[str, str]:
    """Return (text, error). On success error is ""; on failure text is ""."""
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    try:
        if ext == "pdf" or mime == "application/pdf":
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
            return _clean("\n\n".join(pages)), ""

        if ext == "docx" or "wordprocessingml" in mime:
            import io

            import docx

            document = docx.Document(io.BytesIO(data))
            paras = [p.text for p in document.paragraphs]
            # Include table cell text too — tables often hold the real content.
            for table in document.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            return _clean("\n".join(paras)), ""

        if ext in ("html", "htm") or mime in ("text/html", "application/xhtml+xml"):
            parser = _TextExtractor()
            parser.feed(data.decode("utf-8", "replace"))
            return _clean(parser.text()), ""

        if ext in ("md", "markdown", "txt", "text", "csv", "log") or mime.startswith("text/"):
            return _clean(data.decode("utf-8", "replace")), ""

        # Fall back to a best-effort UTF-8 decode for unknown types.
        text = _clean(data.decode("utf-8", "replace"))
        if text:
            return text, ""
        return "", f"unsupported file type: {filename or mime or 'unknown'}"
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the user
        return "", f"could not read {filename}: {exc}"


def chunk_text(text: str, *, size: int = 900, overlap: int = 150) -> list[str]:
    """Split text into ~`size`-char chunks on paragraph boundaries with overlap,
    so a retrieved chunk carries enough surrounding context to answer from."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > size:
            # Flush what we have, then hard-split the oversized paragraph.
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), size - overlap):
                chunks.append(para[i : i + size])
            continue
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            # Carry a tail of the previous chunk for continuity.
            tail = current[-overlap:] if overlap else ""
            current = (tail + "\n\n" + para).strip()
        else:
            current = (current + "\n\n" + para).strip() if current else para
    if current:
        chunks.append(current)
    return chunks
