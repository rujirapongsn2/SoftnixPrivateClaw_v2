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
#  - libjpeg/zlib: Pillow image backends used by reportlab/openpyxl images
RUN apt-get update && apt-get install -y --no-install-recommends \
        zip unzip \
        fonts-dejavu-core fonts-liberation \
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
