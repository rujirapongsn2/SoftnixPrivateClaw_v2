"""Docker integration: real offline work inside a built sandbox image.

Runs only when CLAW_SANDBOX_TEST_IMAGE names an image (for example
claw-sandbox:dev-validation) and docker is available. Every command goes
through EphemeralSandbox exactly as the exec tool sends it: network=none, a
read-only root, a fresh /tmp, all capabilities dropped, no-new-privileges, a
non-root user and the production CPU/memory/pids limits. All inputs are
synthetic and are generated inside the run.
"""

import asyncio
import os
import shutil
import textwrap

import pytest

from claw.config import SandboxSettings
from claw.sandbox.capabilities import check_image
from claw.sandbox.ephemeral import EphemeralSandbox

IMAGE = os.environ.get("CLAW_SANDBOX_TEST_IMAGE", "")

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason="set CLAW_SANDBOX_TEST_IMAGE to a built sandbox image to run the docker integration tests",
)


@pytest.fixture
def sandbox():
    settings = SandboxSettings(
        enabled=True, image=IMAGE, network="none", cpu_limit=1.0, memory_limit="1g", timeout_seconds=120
    )
    return EphemeralSandbox(settings)


def run_py(sandbox, workspace, source: str):
    script = workspace / "job.py"
    script.write_text(textwrap.dedent(source))
    result = asyncio.run(sandbox.run("python /workspace/job.py", workspace))
    assert not result.infrastructure_error, result.stderr
    return result


def ok(result) -> str:
    assert result.exit_code == 0 and not result.timed_out, result.render()
    return result.stdout


def test_every_capability_is_present(sandbox):
    readiness = asyncio.run(check_image(sandbox.settings))
    assert readiness.ok, readiness.describe()


def test_confinement_is_the_production_one(sandbox, tmp_path):
    out = ok(asyncio.run(sandbox.run(
        "id -u; "
        "touch /usr/claw-probe 2>/dev/null && echo root-writable || echo root-readonly; "
        "echo x > /tmp/t && echo tmp-ok; echo x > /workspace/w && echo ws-ok; "
        "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status",
        tmp_path,
    )))
    lines = out.split()
    assert lines[0] != "0"
    assert "root-readonly" in out and "tmp-ok" in out and "ws-ok" in out
    assert "CapEff:\t0000000000000000" in out and "NoNewPrivs:\t1" in out
    assert (tmp_path / "w").read_text() == "x\n"


def test_outside_network_is_blocked_and_loopback_works(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, """
        import socket, threading, http.server, urllib.request
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                socket.create_connection((host, port), timeout=3)
                print("reached", host)
            except OSError:
                print("blocked", host)
        try:
            socket.getaddrinfo("pypi.org", 443)
            print("dns-resolved")
        except OSError:
            print("dns-blocked")
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/", timeout=5) as r:
            print("loopback", r.status)
    """))
    assert "blocked 1.1.1.1" in out and "blocked 8.8.8.8" in out and "dns-blocked" in out
    assert "loopback 200" in out


def test_pip_install_cannot_reach_an_index(sandbox, tmp_path):
    result = asyncio.run(sandbox.run(
        "cd /tmp && pip install --no-cache-dir --retries 0 --timeout 3 six-does-not-exist-offline", tmp_path
    ))
    assert result.exit_code != 0


def test_pdf_reads_and_renders_with_pymupdf_and_poppler(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, """
        import subprocess, fitz
        from reportlab.pdfgen import canvas
        c = canvas.Canvas("/workspace/in.pdf")
        c.setFont("Helvetica", 28)
        c.drawString(72, 720, "Synthetic invoice 4821")
        c.showPage(); c.save()
        doc = fitz.open("/workspace/in.pdf")
        print("fitz-text", "Synthetic invoice 4821" in doc[0].get_text())
        doc[0].get_pixmap(dpi=72).save("/workspace/fitz.png")
        subprocess.run(["pdftoppm", "-png", "-r", "50", "-singlefile", "/workspace/in.pdf", "/workspace/pop"], check=True)
        txt = subprocess.run(["pdftotext", "/workspace/in.pdf", "-"], check=True, capture_output=True, text=True).stdout
        print("poppler-text", "Synthetic invoice 4821" in txt)
    """))
    assert "fitz-text True" in out and "poppler-text True" in out
    for name in ("fitz.png", "pop.png"):
        assert (tmp_path / name).read_bytes()[:4] == b"\x89PNG"


def _text_image(text: str, font: str, size: int) -> str:
    return f"""
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype({font!r}, {size})
        img = Image.new("L", (1400, 220), 255)
        ImageDraw.Draw(img).text((40, 50), {text!r}, font=font, fill=0)
        img.save("/tmp/text.png")
    """


def test_pytesseract_reads_english_and_thai(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, textwrap.dedent(_text_image("Invoice 48213", "DejaVuSans.ttf", 96)) + textwrap.dedent("""
        import pytesseract
        print("langs", sorted(set(pytesseract.get_languages()) & {"tha", "eng"}))
        print("eng", pytesseract.image_to_string(Image.open("/tmp/text.png"), lang="eng").strip())
    """) + textwrap.dedent(_text_image("สวัสดีครับ ภาษาไทย", "/usr/share/fonts/truetype/tlwg/Garuda.ttf", 96)) + textwrap.dedent("""
        print("tha", pytesseract.image_to_string(Image.open("/tmp/text.png"), lang="tha").replace(" ", "").strip())
    """)))
    assert "langs ['eng', 'tha']" in out
    assert "Invoice 48213" in out
    assert "สวัสดีครับ" in out


def test_rapidocr_uses_its_bundled_models_offline(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, textwrap.dedent(_text_image("HELLO 2026 OFFLINE", "DejaVuSans.ttf", 96)) + textwrap.dedent("""
        import glob, os
        from rapidocr_onnxruntime import RapidOCR
        result, _ = RapidOCR()("/tmp/text.png")
        print("text", " ".join(r[1] for r in (result or [])))
        fetched = [p for p in glob.glob("/tmp/**/*.onnx", recursive=True)] + glob.glob(os.path.expanduser("~/.rapidocr*"))
        print("downloaded", fetched)
    """)))
    assert "HELLO 2026 OFFLINE" in out.replace("  ", " ")
    assert "downloaded []" in out


def test_svg_to_png_with_cairosvg_and_rsvg(sandbox, tmp_path):
    (tmp_path / "in.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        '<rect width="120" height="80" fill="#ff0000"/><text x="10" y="50" font-family="Garuda">ไทย</text></svg>'
    )
    out = ok(run_py(sandbox, tmp_path, """
        import subprocess, cairosvg
        from PIL import Image
        cairosvg.svg2png(url="/workspace/in.svg", write_to="/workspace/cairo.png")
        subprocess.run(["rsvg-convert", "-o", "/workspace/rsvg.png", "/workspace/in.svg"], check=True)
        for name in ("cairo", "rsvg"):
            img = Image.open(f"/workspace/{name}.png").convert("RGB")
            print(name, img.size, img.getpixel((2, 2)))
    """))
    assert "cairo (120, 80) (255, 0, 0)" in out and "rsvg (120, 80) (255, 0, 0)" in out


def test_thai_text_shapes_and_draws_with_image_fonts(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, """
        import warnings
        import uharfbuzz as hb
        from PIL import Image, ImageDraw, ImageFont, features
        path = "/usr/share/fonts/truetype/tlwg/Garuda.ttf"
        blob = hb.Blob.from_file_path(path)
        font = hb.Font(hb.Face(blob))
        buf = hb.Buffer(); buf.add_str("สวัสดีครับ"); buf.guess_segment_properties()
        hb.shape(font, buf)
        print("notdef", sum(1 for g in buf.glyph_infos if g.codepoint == 0), "glyphs", len(buf.glyph_infos))
        print("raqm", features.check("raqm"))
        img = Image.new("L", (600, 120), 255)
        ImageDraw.Draw(img).text((10, 20), "สวัสดีครับ", font=ImageFont.truetype(path, 64), fill=0)
        print("inked", img.getextrema()[0] < 50)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Garuda"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fig, ax = plt.subplots(); ax.set_title("ยอดขายรายเดือน"); ax.bar(["ม.ค.", "ก.พ."], [3, 5])
            fig.savefig("/workspace/thai-chart.png")
        print("missing-glyph-warnings", sum("missing" in str(w.message).lower() for w in caught))
    """))
    assert "notdef 0" in out and "raqm True" in out and "inked True" in out
    assert "missing-glyph-warnings 0" in out
    assert (tmp_path / "thai-chart.png").stat().st_size > 1000


def test_pythainlp_works_with_bundled_data(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, """
        from pythainlp import word_tokenize
        from pythainlp.util import bahttext, normalize, thai_digit_to_arabic_digit
        from pythainlp.transliterate import romanize
        print("words", word_tokenize("ฉันรักภาษาไทย", engine="newmm"))
        print("baht", bahttext(121.5))
        print("digits", thai_digit_to_arabic_digit("๒๕๖๙"))
        print("roman", romanize("แมว", engine="royin"))
        print("norm", normalize("เเปลก") == "แปลก")
    """))
    assert "words ['ฉัน', 'รัก', 'ภาษาไทย']" in out
    assert "baht หนึ่งร้อยยี่สิบเอ็ดบาทห้าสิบสตางค์" in out
    assert "digits 2569" in out and "roman maeo" in out and "norm True" in out


def test_office_and_chart_regression(sandbox, tmp_path):
    out = ok(run_py(sandbox, tmp_path, """
        import openpyxl, docx, pptx, matplotlib
        from fpdf import FPDF
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        wb = openpyxl.Workbook(); wb.active["A1"] = "ยอดขาย"; wb.active["B1"] = 42; wb.save("/workspace/r.xlsx")
        print("xlsx", openpyxl.load_workbook("/workspace/r.xlsx").active["B1"].value)
        d = docx.Document(); d.add_paragraph("รายงานสังเคราะห์"); d.save("/workspace/r.docx")
        print("docx", docx.Document("/workspace/r.docx").paragraphs[0].text)
        p = pptx.Presentation(); s = p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text = "Q3"; p.save("/workspace/r.pptx")
        print("pptx", pptx.Presentation("/workspace/r.pptx").slides[0].shapes.title.text)
        pdf = FPDF(); pdf.add_page(); pdf.add_font("Garuda", fname="/usr/share/fonts/truetype/tlwg/Garuda.ttf")
        pdf.set_font("Garuda", size=16); pdf.cell(text="สวัสดี"); pdf.output("/workspace/r.pdf")
        fig, ax = plt.subplots(); ax.plot([1, 2, 3]); fig.savefig("/workspace/r.png")
    """))
    assert "xlsx 42" in out and "docx รายงานสังเคราะห์" in out and "pptx Q3" in out
    assert (tmp_path / "r.pdf").read_bytes()[:5] == b"%PDF-"
    assert (tmp_path / "r.png").stat().st_size > 1000


def test_html_to_png_without_a_browser(sandbox, tmp_path):
    (tmp_path / "page.html").write_text(
        '<html><head><meta charset="utf-8"></head><body style="font-family: Garuda"><h1>รายงานสังเคราะห์</h1><p>Total 1,234</p></body></html>'
    )
    out = ok(asyncio.run(sandbox.run(
        "python -c \"import weasyprint; weasyprint.HTML('/workspace/page.html').write_pdf('/tmp/page.pdf')\" "
        "&& pdftoppm -png -r 60 -singlefile /tmp/page.pdf /workspace/page && pdftotext /tmp/page.pdf -",
        tmp_path,
    )))
    assert "รายงานสังเคราะห์" in out and "Total 1,234" in out
    assert (tmp_path / "page.png").read_bytes()[:4] == b"\x89PNG"
