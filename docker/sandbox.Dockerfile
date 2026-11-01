# Claw tool-ephemeral sandbox image.
#
# Each risky shell command (`exec` tool) runs in a short-lived container built
# from this image. It ships the document-generation stack pre-installed so the
# agent can produce PDF / Excel / Word / PowerPoint / archives offline, and the
# usual archive CLIs. Production runs with CLAW_SANDBOX__NETWORK=none (no
# internet, no access to host services), so anything the agent commonly needs
# must be baked in here rather than `pip install`ed at run time.
#
# Build:  docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .
FROM python:3.12-slim

# System packages:
#  - zip/unzip: archive CLIs missing from slim (tar/gzip already present)
#  - fonts + cairo/pango/gdk-pixbuf: WeasyPrint (HTML→PDF) runtime deps
#  - fonts-thai-tlwg: Thai glyphs, without which any Thai text in a generated
#    PDF renders as tofu. It installs the TLWG families — Garuda, Loma, Norasi,
#    Kinnari, Laksaman, Purisa, Sawasdee — NOT "Noto Sans Thai", so a CSS stack
#    or a reportlab registerFont() call has to name one of those to hit them.
#    Files live in /usr/share/fonts/truetype/tlwg/ (e.g. Garuda.ttf).
#  - libjpeg/zlib: Pillow image backends used by reportlab/openpyxl images
#  - tesseract-ocr + tesseract-ocr-tha: OCR incl. Thai (agents apt-installed it
#    at run time before the sandbox lost network)
#  - ffmpeg: audio/video conversion (yt-dlp post-processing, clips)
#  - curl/git: common CLIs the agent reaches for; harmless with network=none
RUN apt-get update && apt-get install -y --no-install-recommends \
        zip unzip \
        tesseract-ocr tesseract-ocr-tha ffmpeg curl git ca-certificates \
        fonts-dejavu-core fonts-liberation fonts-thai-tlwg \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Document / data stack. Pinned to majors so rebuilds stay reproducible while
# still picking up patch releases.
#  - pypdf: PDF merge/split/extract/form-fill (previously only present as an
#    undeclared transitive dep of xhtml2pdf — pinned directly so it can't
#    silently disappear on an xhtml2pdf upgrade).
#  - PyMuPDF (fitz): the other half of the PDF skill — text/table extraction
#    with layout, thumbnail rendering.
RUN pip install --no-cache-dir \
        "reportlab>=4.1" \
        "weasyprint>=62" \
        "xhtml2pdf>=0.2.16" \
        "pypdf>=6.0" \
        "PyMuPDF>=1.24" \
        "openpyxl>=3.1" \
        "python-docx>=1.1" \
        "python-pptx>=1.0" \
        "pandas>=2.2" \
        "Pillow>=10.3" \
        "markdown>=3.6" \
        "tabulate>=0.9"

# Libraries agents used to `pip install` on demand (from claw2 audit history,
# Jul–Oct 2026). Baked in because the sandbox runs with network=none.
#  - matplotlib: charts as PNG; requests/httpx: HTTP clients (work only if the
#    sandbox network is ever re-enabled); formulas: evaluate Excel formulas;
#  - yfinance/yt-dlp/youtube-transcript-api: need network at run time, kept so
#    the code paths import cleanly;
#  - fastapi/uvicorn/pydantic/sqlalchemy/pytest: small app prototypes and tests
#    run against 127.0.0.1 inside the same container.
RUN pip install --no-cache-dir \
        "matplotlib>=3.8" \
        "requests>=2.31" \
        "httpx>=0.27" \
        "formulas>=1.2" \
        "yfinance>=0.2" \
        "yt-dlp" \
        "youtube-transcript-api>=0.6" \
        "fastapi>=0.110" \
        "uvicorn>=0.29" \
        "pydantic>=2.6" \
        "sqlalchemy>=2.0" \
        "pytest>=8.0"

WORKDIR /workspace
