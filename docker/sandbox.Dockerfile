# Claw tool-ephemeral sandbox image.
#
# Each risky shell command (`exec` tool) runs in a short-lived container built
# from this image. It ships the document-generation stack pre-installed so the
# agent can produce PDF / Excel / Word / PowerPoint / archives offline, and the
# usual archive CLIs. With CLAW_SANDBOX__NETWORK=bridge the agent may also
# `pip install` extra libraries on demand.
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
RUN apt-get update && apt-get install -y --no-install-recommends \
        zip unzip \
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

WORKDIR /workspace
