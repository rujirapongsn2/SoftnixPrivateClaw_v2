"""Built-in ("pre-built") skills shipped with the platform.

Unlike user skills (rows in the ``skills`` table), these are defined in code so
they're always available, version-controlled, and can't be edited or deleted.
They're merged into the agent's skills list — the system-prompt summary and the
``read_skill`` tool — and surfaced read-only in Settings → Skills.

``skill-creator`` teaches the agent how to author new user skills correctly via
the ``manage_skill`` tool (writing workspace files does not create a skill,
which is the trap that made agent-"created" skills never show up). The
``pptx``/``xlsx``/``pdf``/``docx`` document skills are recipes for the exec
tool: they teach techniques for the document/data stack already baked into the
sandbox image (docker/sandbox.Dockerfile) — python-pptx, openpyxl, PyMuPDF,
python-docx, reportlab, pandas, etc. — rather than introducing new in-process
tools, since document creation/editing already happens by shelling out to the
sandbox.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Stable timestamp so the API payload is deterministic across restarts.
_BUILTIN_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class BuiltinSkill:
    """A code-defined skill; duck-types the ORM ``Skill`` for the summary/API."""

    name: str
    description: str
    content: str
    enabled: bool = True
    # Optional "capabilities covered" — shown as a 2x2 card grid in the
    # Settings → Skills detail view. Empty for skills with no such UI (e.g.
    # skill-creator), which fall back to the plain raw-content view instead.
    capabilities: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    summary: str = ""

    @property
    def id(self) -> str:
        return f"builtin:{self.name}"

    @property
    def updated_at(self) -> datetime:
        return _BUILTIN_TS


_SKILL_CREATOR_CONTENT = """\
A skill is a reusable procedure stored in the system and offered to you in every
future chat. A skill is NOT a workspace file: writing a `.py`/`.md` file does NOT
create a skill and it will never appear in Settings → Skills. The ONLY way to
create or update a skill is the `manage_skill` tool.

## Create a skill (manage_skill, action="save")
1. name — short, kebab-case, unique per user (e.g. `stock-analysis`, `weekly-report`).
2. description — ONE line, shown to you in every chat's system prompt. This is how
   future-you decides to use the skill, so make it specific and trigger-oriented:
   what it does AND when to use it. e.g. "Analyse a Thai/US stock over N months
   (price trend, dividend yield, PE) — use when the user asks to analyse a stock."
3. content — the full instructions, loaded on demand when you `read_skill`. Keep it
   procedural and focused:
   - The exact steps, in order.
   - Which tools to use at each step (web_search, mcp_* connectors, exec for
     computation/charts, write_file for outputs).
   - The output the user expects (table, chart PNG, PDF) and its format.
   - Sensible defaults and edge cases (default period, currency, data source).
4. Call `manage_skill` with action="save" and those fields.

## What makes a good skill
- One skill = one clear procedure. Don't bundle unrelated tasks.
- Capture the reusable *method*, not one-off data from this chat.
- Name the tools you'll call so future-you needs no guesswork.
- Keep content concise — you pay tokens to read it; link steps, don't pad.
- After saving, tell the user it's saved and now appears in Settings → Skills
  (enabled by default).

## Manage existing skills
- action="list" — list your saved skills.
- action="save" — create, or overwrite one with the same name.
- action="delete" — remove a skill by name.

Never try to "install" a skill by writing files or editing the database — always
go through `manage_skill`.
"""

_PPTX_CONTENT = """\
Build and edit PowerPoint decks with `python-pptx` (already installed in the sandbox)
via the `exec` tool. Write the .pptx to /workspace so it's downloadable when done.

## Presentation Creation
For a new deck, don't hand-place shapes by guessing coordinates — design the slide as
an HTML/CSS layout first (absolute-positioned divs at the deck's exact pixel size,
e.g. 1280x720 for 16:9), render it to a PNG with the tools already in the sandbox
(`weasyprint` HTML→PDF, then PyMuPDF `fitz` to rasterize page 0 to PNG), and look at
the image before committing to it. This catches overlap/overflow far faster than
reasoning about EMU coordinates blind. Once the layout is right, translate each div's
`(left, top, width, height)` in pixels to inches (`px / 96`) and build the real shapes:

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)  # 16:9
slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout — full control

PALETTE = {"primary": RGBColor(0x1F, 0x4E, 0x79), "accent": RGBColor(0xED, 0x7D, 0x31)}

box = slide.shapes.add_textbox(Inches(0.5), Inches(0.4), Inches(9), Inches(1))
p = box.text_frame.paragraphs[0]
p.text = "Q3 Supplier Review"
p.font.size, p.font.bold, p.font.color.rgb = Pt(32), True, PALETTE["primary"]

table = slide.shapes.add_table(rows=4, cols=3, left=Inches(0.5), top=Inches(1.6),
                                width=Inches(9), height=Inches(2.5)).table

chart_data = CategoryChartData()
chart_data.categories = ["Q1", "Q2", "Q3"]
chart_data.add_series("Revenue", (19.2, 21.4, 25.0))
slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(4.3),
                        Inches(9), Inches(2.8), chart_data)
prs.save("/workspace/review.pptx")
```

## Template Reuse
To reuse an existing deck as a template (matching layouts, duplicating a slide, then
replacing its content) rather than building from scratch, `python-pptx` has no native
"duplicate slide" API — build it via `copy.deepcopy` on the slide's XML:

```python
import copy
from pptx.oxml.ns import qn

def duplicate_slide(prs, index):
    source = prs.slides[index]
    new_slide = prs.slides.add_slide(source.slide_layout)
    # A layout-based add_slide() pre-populates inherited placeholders — strip them
    # before copying the source's own shapes over, or they collide/duplicate.
    for shape in list(new_slide.shapes):
        shape._element.getparent().remove(shape._element)
    for shape in source.shapes:
        new_slide.shapes._spTree.append(copy.deepcopy(shape._element))
    return new_slide

new_slide = duplicate_slide(prs, 0)
for shape in new_slide.shapes:
    if shape.has_text_frame and "Q3" in shape.text_frame.text:
        shape.text_frame.paragraphs[0].runs[0].text = "Q4 Supplier Review"
```
Match layouts by reusing `source.slide_layout` (not a fresh blank layout) so fonts,
placeholder positions, and theme colors carry over automatically.

## Slide Editing
For edits `python-pptx`'s object model doesn't expose directly (custom XML attributes,
some line/fill effects), drop to its OOXML tree via `shape._element` /
`slide.shapes._spTree` and `lxml` — every shape is backed by a real `oxml` element.
Prefer the high-level API (`shape.text_frame`, `shape.fill.solid()`,
`shape.left`/`.top`/`.width`/`.height`) whenever it covers what you need; reach for
raw XML only when it doesn't.

## Thumbnails & Analysis
There is no slide-rendering engine in the sandbox (no LibreOffice/PowerPoint) — do
NOT claim a pixel-perfect visual render. Instead:
- **Structural analysis** (the reliable part): iterate `slide.shapes` for shape
  counts, `shape.shape_type`, positions/sizes, `shape.has_chart`/`has_table`, and
  `shape.text_frame.text` for all text content — this is exact, not approximate.
- **Schematic thumbnail** (a wireframe, not a real render): draw each shape's
  bounding box and a text snippet with `Pillow` (`ImageDraw.rectangle` +
  `ImageDraw.text`, scaled from EMU to pixels). Label the output clearly as a
  schematic/wireframe overview when you show it to the user — it will not match
  the deck's actual fonts, colors, or chart rendering.
"""

_XLSX_CONTENT = """\
Build, analyze, and visualize Excel workbooks with `openpyxl` and `pandas` (already
installed in the sandbox) via the `exec` tool. Write outputs to /workspace.

## Spreadsheet Creation
Write two small local helper functions at the top of the script — `build_table` and
`build_chart` — parameterized by a `theme` (a dict of header/accent colors) so every
sheet in the workbook looks consistent without repeating style code:

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, Reference

THEMES = {
    "corporate": {"header_fill": "1F4E79", "header_font": "FFFFFF", "accent": "2E75B6"},
    "warm":      {"header_fill": "C0504D", "header_font": "FFFFFF", "accent": "E8A33D"},
}

def build_table(ws, rows, start_row=1, theme="corporate"):
    t = THEMES[theme]
    header_fill = PatternFill("solid", fgColor=t["header_fill"])
    header_font = Font(bold=True, color=t["header_font"])
    for c, header in enumerate(rows[0], start=1):
        cell = ws.cell(row=start_row, column=c, value=header)
        cell.fill, cell.font = header_fill, header_font
    for r, row in enumerate(rows[1:], start=start_row + 1):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)
    return start_row + len(rows)  # next free row

def build_chart(ws, data_range, cats_range, anchor, title, theme="corporate", kind="bar"):
    chart = BarChart() if kind == "bar" else BarChart()  # swap for LineChart/AreaChart as needed
    chart.title = title
    chart.add_data(data_range, titles_from_data=True)
    chart.set_categories(cats_range)
    ws.add_chart(chart, anchor)

wb = Workbook()
ws = wb.active
next_row = build_table(ws, [["Supplier", "Q1", "Q2", "Q3"], ["Acme", 100, 120, 115]])
# Formula cells: write the formula STRING, not a computed value — Excel/LibreOffice
# calculates it on open. openpyxl never evaluates formulas itself.
ws["E2"] = "=SUM(B2:D2)"
build_chart(ws, Reference(ws, min_col=2, min_row=1, max_col=4, max_row=2),
            Reference(ws, min_col=1, min_row=2, max_row=2), "G2", "Quarterly spend")
wb.save("/workspace/suppliers.xlsx")
```
For **line/area/combo charts** swap `BarChart` for `openpyxl.chart.LineChart` /
`AreaChart`, or add a second series with `chart2.y_axis.axId = chart.y_axis.axId` and
`chart += chart2` for a combo. Configure axis titles via `chart.x_axis.title` /
`chart.y_axis.title`, and legend position via `chart.legend.position`.

## Data Analysis
Use `pandas` for anything beyond simple rows: `pd.read_excel`/`read_csv` to load,
`.groupby()`/`.describe()`/`.pivot_table()` for statistics, then either
`df.to_excel(path, sheet_name=...)` for a quick dump, or `df.values.tolist()` fed
into `build_table` above when you need the openpyxl styling/formulas on top.

## Financial Models
For DCF / LBO / three-statement models, the point is that every number the reader
might question should be a **formula referencing another cell**, not a hardcoded
literal — so changing one assumption recalculates the whole model:
- Put assumptions (discount rate, growth rate, tax rate, leverage ratio) in one
  labelled "Assumptions" block/sheet, each in its own cell.
- Every downstream calculation references those cells (`=B5*(1+$B$2)`), never
  repeats the number.
- **Source annotations**: add a cell comment (`ws["B2"].comment = Comment("Source: ...", "Claw")`)
  or a footnote row citing where each assumption came from.
- **Scenario analysis**: put 2-3 named scenarios (Base/Upside/Downside) as columns
  of assumption values on a separate sheet, and have the model sheet's assumption
  cells reference one scenario column at a time (or use Excel Data Tables /
  `CHOOSE()` if the target is manual toggling after you hand off the file).
"""

_PDF_CONTENT = """\
Extract, create, merge, and fill PDFs with `pypdf` and `PyMuPDF` (`fitz`) — both
pre-installed in the sandbox — via the `exec` tool. Prefer `pypdf` for
merge/split/form-fill and `PyMuPDF` for extraction-with-layout and form-field
creation; both read/write the same PDF files so mix freely.

## Text & Table Extraction
`PyMuPDF` preserves layout far better than plain text scraping:

```python
import fitz  # PyMuPDF

doc = fitz.open("/workspace/contract.pdf")
for page in doc:
    text = page.get_text("text")       # reading-order plain text
    words = page.get_text("words")     # (x0, y0, x1, y1, word, block, line, word_no)
    tables = page.find_tables()        # page.find_tables().tables -> structured cells
    for table in tables.tables:
        rows = table.extract()         # list[list[str]] — feed straight into pandas/csv
```
Use `words`/block positions when you need to preserve columns or find text near a
specific location (e.g. "the clause after this heading"), not just concatenated text.

## PDF Creation
For a **document from scratch** (reports, letters), `reportlab` is more direct for
flowing text/tables (`SimpleDocTemplate` + `Paragraph`/`Table` flowables). For
**precise, coordinate-based placement** (a branded one-pager, an overlay on an
existing template), build pages with `fitz`:

```python
doc = fitz.open()
page = doc.new_page(width=595, height=842)  # A4 in points
page.insert_text((72, 72), "Purchase Order #4471", fontsize=18, fontname="helv")
page.draw_rect(fitz.Rect(72, 100, 523, 101), color=(0.2, 0.3, 0.5), fill=(0.2, 0.3, 0.5))
doc.save("/workspace/po.pdf")
```
Both libraries support multi-page docs (call the page-adding API in a loop) and
embedding chart images (render the chart with matplotlib/openpyxl to a PNG first,
then `page.insert_image(rect, filename=...)` or reportlab's `Image` flowable).

## Merge & Split
```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
writer.append("/workspace/cover.pdf")
writer.append("/workspace/contract.pdf")
writer.write("/workspace/combined.pdf")

reader = PdfReader("/workspace/combined.pdf")
writer = PdfWriter()
writer.add_page(reader.pages[2].rotate(90))       # rotate a specific page
writer.add_page(reader.pages[0])                  # reorder — page 0 goes last
writer.write("/workspace/reordered.pdf")
```

## Form Filling
Create a fillable field with `PyMuPDF`, then fill it with `pypdf` (the two libraries
round-trip AcroForm fields cleanly):

```python
doc = fitz.open("/workspace/template.pdf")
widget = fitz.Widget()
widget.field_name, widget.field_type = "supplier_name", fitz.PDF_WIDGET_TYPE_TEXT
widget.rect = fitz.Rect(150, 700, 400, 720)
doc[0].add_widget(widget)
doc.save("/workspace/form.pdf")

reader = PdfReader("/workspace/form.pdf")
writer = PdfWriter()
writer.append(reader)
writer.update_page_form_field_values(writer.pages[0], {"supplier_name": "Acme Co."})
writer.write("/workspace/filled.pdf")
```
Before filling an unfamiliar PDF, first detect its existing fields with
`reader.get_fields()` (pypdf) to confirm exact field names, then validate required
fields got a non-empty value before handing the filled PDF back to the user.
"""

_DOCX_CONTENT = """\
Create and edit Word documents with `python-docx` (already installed in the sandbox)
via the `exec` tool. Write outputs to /workspace.

## Document Creation
```python
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
doc.styles["Normal"].font.name, doc.styles["Normal"].font.size = "Calibri", Pt(11)

h = doc.add_heading("Request for Quotation", level=1)
p = doc.add_paragraph("Dear Supplier,")
table = doc.add_table(rows=1, cols=3)
table.style = "Light Grid Accent 1"
table.rows[0].cells[0].text = "Item"

section = doc.sections[0]
section.header.paragraphs[0].text = "Acme Procurement — Confidential"
doc.save("/workspace/rfq.docx")
```
For professional typesetting: set styles at the document level (above) rather than
per-run, use built-in heading styles (`add_heading`) so a generated table of contents
picks them up, and `section.page_width`/`page_height`/margins for layout control.

## Document Editing
When editing an EXISTING document, preserve its formatting: edit `run.text` on the
specific run that needs to change rather than replacing `paragraph.text` (which
destroys any bold/italic/color split across runs in that paragraph). For structural
edits python-docx's API doesn't expose (custom OOXML attributes), drop to
`paragraph._p` (the underlying `lxml` element) directly — see Tracked Changes below
for the pattern.

## Tracked Changes
`python-docx` has **no native tracked-changes API** — Word's revision marks
(`w:ins`/`w:del`) must be built as raw OOXML and inserted via the low-level
`oxml`/`lxml` layer:

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

def insert_tracked_insertion(paragraph, text, author="Claw"):
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), "1")
    ins.set(qn("w:author"), author)
    ins.set(qn("w:date"), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    ins.append(run)
    paragraph._p.append(ins)

def mark_tracked_deletion(run, author="Claw"):
    # Wrap the run's text as w:delText inside a w:del, replacing the plain w:r.
    del_el = OxmlElement("w:del")
    del_el.set(qn("w:id"), "2")
    del_el.set(qn("w:author"), author)
    del_el.set(qn("w:date"), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    new_run = OxmlElement("w:r")
    del_text = OxmlElement("w:delText")
    del_text.set(qn("xml:space"), "preserve")
    del_text.text = run.text
    new_run.append(del_text)
    del_el.append(new_run)
    run._element.getparent().replace(run._element, del_el)
```
This is exactly what Word renders as "tracked changes" in Review mode — suitable for
legal/academic/business review workflows where the user needs to see (and accept or
reject) each change rather than a silently-edited document. Review comments follow
the same low-level pattern via the `w:commentRangeStart`/`w:comment` parts.

## Content Extraction
```python
doc = Document("/workspace/contract.docx")
text = "\\n".join(p.text for p in doc.paragraphs)
tables = [[[cell.text for cell in row.cells] for row in t.rows] for t in doc.tables]
metadata = doc.core_properties  # .author, .title, .created, ...

# Embedded images live in the document part's relationships:
for rel in doc.part.rels.values():
    if "image" in rel.reltype:
        image_bytes = rel.target_part.blob
```
Comments require the same low-level `oxml` route as tracked changes (there's no
`doc.comments` on python-docx) — read the `word/comments.xml` part via
`doc.part.package.part_related_by(...)` if the document has one. Support multiple
output formats (plain text, markdown, JSON) by building a plain dict/list from the
above and letting the caller decide the serialization.
"""

_BUILTIN_SKILLS: tuple[BuiltinSkill, ...] = (
    BuiltinSkill(
        name="skill-creator",
        description=(
            "How to author a new reusable skill correctly — use whenever the user asks you to "
            "create, build, or save a skill."
        ),
        content=_SKILL_CREATOR_CONTENT,
    ),
    BuiltinSkill(
        name="pptx",
        description=(
            "Create and edit PowerPoint presentations — HTML-previewed layouts, template reuse, "
            "charts/tables, and slide analysis. Use whenever the user asks for a .pptx/slide deck."
        ),
        content=_PPTX_CONTENT,
        capabilities=(
            (
                "Presentation Creation",
                "HTML-to-PPTX workflow with custom color palettes, charts, tables, and visual design",
            ),
            (
                "Template Reuse",
                "automatically matching layouts, duplicating slides, and replacing content",
            ),
            (
                "Slide Editing",
                "OOXML manipulation, modifying text, layouts, and design elements",
            ),
            (
                "Thumbnails & Analysis",
                "slide thumbnail grids, extract text content, and analyze layout structure",
            ),
        ),
        summary=(
            "Build PowerPoint presentations for supplier reviews, data summaries, and strategy "
            "proposals with structured layouts and chart integration."
        ),
    ),
    BuiltinSkill(
        name="xlsx",
        description=(
            "Create, analyze, and visualize Excel spreadsheets — themed tables/charts, pandas "
            "analysis, and financial models. Use whenever the user asks for a .xlsx/spreadsheet."
        ),
        content=_XLSX_CONTENT,
        capabilities=(
            (
                "Spreadsheet Creation",
                "build_table/build_chart with multiple theme styles and automatic formula calculation",
            ),
            (
                "Data Analysis",
                "pandas processing, statistical calculations, and structured output",
            ),
            (
                "Chart Visualization",
                "bar, line, area, and combo charts with themed colors, axis configuration, and legend layouts",
            ),
            (
                "Financial Models",
                "DCF, LBO, three-statement models with formula linking, source annotations, and scenario analysis",
            ),
        ),
        summary=(
            "Create, edit, analyze, and visualize Excel spreadsheets with formulas, formatting, "
            "charts, and data cleaning — from supplier comparison tables to financial models."
        ),
    ),
    BuiltinSkill(
        name="pdf",
        description=(
            "Extract, create, merge/split, and fill PDFs — layout-preserving extraction, branded "
            "document generation, and AcroForm filling. Use whenever the user asks for a .pdf."
        ),
        content=_PDF_CONTENT,
        capabilities=(
            (
                "Text & Table Extraction",
                "layout preservation and structured output",
            ),
            (
                "PDF Creation",
                "using reportlab or PyMuPDF, supporting multi-page, charts, and custom styling",
            ),
            (
                "Merge & Split",
                "supporting page rotation and reordering",
            ),
            (
                "Form Filling",
                "field detection, auto-fill, and validation",
            ),
        ),
        summary=(
            "Generate branded PDF documents, extract key clauses from contracts, and merge or "
            "split PDF files for procurement and business use cases."
        ),
    ),
    BuiltinSkill(
        name="docx",
        description=(
            "Create and edit Word documents — professional typesetting, tracked changes/review "
            "comments, and content extraction. Use whenever the user asks for a .docx/Word document."
        ),
        content=_DOCX_CONTENT,
        capabilities=(
            (
                "Document Creation",
                "using python-docx, supporting paragraphs, tables, headers/footers, and professional typesetting",
            ),
            (
                "Document Editing",
                "OOXML manipulation, preserving original formatting, styles, and formulas",
            ),
            (
                "Tracked Changes",
                "apply tracked changes and review comments, suitable for legal, academic, and business document review",
            ),
            (
                "Content Extraction",
                "document text, metadata, embedded images, and comments, supporting multiple output formats",
            ),
        ),
        summary=(
            "Create and edit Word documents including RFQ letters, supplier evaluation reports, "
            "SOPs, and contract templates with professional formatting."
        ),
    ),
)


def builtin_skills() -> list[BuiltinSkill]:
    """All built-in skills (always enabled)."""
    return list(_BUILTIN_SKILLS)


def get_builtin_skill(name: str) -> BuiltinSkill | None:
    for skill in _BUILTIN_SKILLS:
        if skill.name == name:
            return skill
    return None
