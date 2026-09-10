# Build: docker build -f docker/verifier.Dockerfile -t sbot-verifier:latest .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress poppler-utils \
    fonts-dejavu-core fonts-liberation fonts-thai-tlwg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'PyMuPDF>=1.24,<2' 'pytest>=8,<9'
COPY scripts/verify-deliverable.py /opt/verify-deliverable.py
ENV HOME=/tmp
USER 65534:65534
ENTRYPOINT ["python", "/opt/verify-deliverable.py"]
