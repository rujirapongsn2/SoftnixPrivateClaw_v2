"""Document reading tools: Excel, CSV, PDF, Word — over workspace files.

Parsing libraries are optional (dependency group 'documents'); a tool returns a
clear error if its library isn't installed rather than crashing the agent.
"""

import asyncio
import csv
import io
from pathlib import Path
from typing import Any

from sbot.api.file_preview import CSV_READ_BYTES
from sbot.tools.base import Tool

# Same process-global raise the preview path applies, restated here rather than
# inherited: it is set as an import side effect over there, so relying on that
# would silently reimpose csv's default 131072-char field limit on this tool in
# any process that never happens to import the preview module. The call is
# idempotent, so import order does not matter.
csv.field_size_limit(CSV_READ_BYTES)

_MAX_CHARS = 40_000


class _WorkspaceDocTool(Tool):
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def _resolve(self, raw: str) -> Path | None:
        try:
            p = (self.workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            p.relative_to(self.workspace)
        except (ValueError, OSError):
            return None
        return p if p.is_file() else None

    @staticmethod
    def _cap(text: str) -> str:
        return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS] + "\n... (truncated)"


class ReadExcelTool(_WorkspaceDocTool):
    name = "read_excel"
    description = "Read an Excel workbook (.xlsx) from the workspace; returns each sheet as rows."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "sheet": {"type": "string", "description": "Optional sheet name; default all"},
            "max_rows": {"type": "integer", "description": "Rows per sheet (default 200)"},
        },
        "required": ["path"],
    }

    async def execute(self, path: str, sheet: str = "", max_rows: int = 200, **_: Any) -> str:
        target = self._resolve(path)
        if target is None:
            return f"Error: file not found: {path}"
        try:
            from openpyxl import load_workbook
        except ImportError:
            return "Error: Excel support is not installed (pip install openpyxl)."
        wb = load_workbook(target, read_only=True, data_only=True)
        names = [sheet] if sheet else wb.sheetnames
        out: list[str] = []
        for name in names:
            if name not in wb.sheetnames:
                out.append(f"[sheet '{name}' not found]")
                continue
            ws = wb[name]
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    rows.append("... (more rows)")
                    break
                rows.append(" | ".join("" if c is None else str(c) for c in row))
            out.append(f"## Sheet: {name}\n" + "\n".join(rows))
        wb.close()
        return self._cap("\n\n".join(out))


class ReadCsvTool(_WorkspaceDocTool):
    name = "read_csv"
    description = "Read a CSV/TSV file from the workspace as delimited rows."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "max_rows": {"type": "integer"}},
        "required": ["path"],
    }

    @staticmethod
    def _parse(target: Path, max_rows: int) -> str:
        text = target.read_text("utf-8", "replace")
        delimiter = "\t" if target.suffix.lower() == ".tsv" else ","
        lines = []
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for i, row in enumerate(reader):
            if i >= max_rows:
                lines.append("... (more rows)")
                break
            lines.append(" | ".join(row))
        return "\n".join(lines)

    async def execute(self, path: str, max_rows: int = 500, **_: Any) -> str:
        target = self._resolve(path)
        if target is None:
            return f"Error: file not found: {path}"
        # Off the event loop: reading and parsing the file is synchronous CPU
        # work that would otherwise stall every other request in this process.
        return self._cap(await asyncio.to_thread(self._parse, target, max_rows))


class ReadPdfTool(_WorkspaceDocTool):
    name = "read_pdf"
    description = "Extract text from a PDF file in the workspace."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str, **_: Any) -> str:
        target = self._resolve(path)
        if target is None:
            return f"Error: file not found: {path}"
        try:
            from pypdf import PdfReader
        except ImportError:
            return "Error: PDF support is not installed (pip install pypdf)."
        reader = PdfReader(str(target))
        parts = [(page.extract_text() or "") for page in reader.pages]
        return self._cap("\n\n".join(parts).strip() or "(no extractable text)")


class ReadDocxTool(_WorkspaceDocTool):
    name = "read_docx"
    description = "Read Word paragraphs and tables in document order, with character paging for long files."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0, "description": "Character offset; initially 0"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10000},
    }, "required": ["path"]}

    async def execute(self, path: str, offset: int = 0, limit: int = 10000, **_: Any) -> str:
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 10000:
            return "Error: offset must be nonnegative and limit must be 1–10000."
        target = self._resolve(path)
        if target is None:
            return f"Error: file not found: {path}"
        try:
            import docx
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError:
            return "Error: Word support is not installed (pip install python-docx)."
        document = docx.Document(str(target))
        blocks = []
        for element in document.element.body.iterchildren():
            if element.tag.endswith('}p'):
                blocks.append(Paragraph(element, document).text)
            elif element.tag.endswith('}tbl'):
                table = Table(element, document)
                blocks.append("[Table]\n" + "\n".join(
                    " | ".join(cell.text for cell in row.cells) for row in table.rows))
        text = "\n".join(blocks).strip() or "(empty document)"
        end = min(len(text), offset + limit)
        page = text[offset:end]
        return page + (f"\n[Next offset: {end}; total characters: {len(text)}]" if end < len(text) else "\n[End of document]")


def build_document_tools(workspace: Path) -> list[Tool]:
    return [
        ReadExcelTool(workspace),
        ReadCsvTool(workspace),
        ReadPdfTool(workspace),
        ReadDocxTool(workspace),
    ]
