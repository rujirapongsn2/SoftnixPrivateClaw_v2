# Softnix PrivateClaw — API server image.
# Multi-stage: build the web frontend, then serve API + static from one Python image.

# ---- web build ----
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- app ----
FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# uv for fast, reproducible installs; docker CLI so the tool-ephemeral sandbox
# can `docker run` sibling containers against the host daemon (Docker-outside-of-
# Docker — the compose mounts /var/run/docker.sock).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# Install dependencies from the lockfile first (cached until pyproject/uv.lock
# change). --no-install-project installs only deps, not the app itself, so the
# code layers below stay cache-friendly. Matches dev exactly, incl. the deps a
# hand-written list historically missed (python-multipart, pypdf, python-docx).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python

COPY claw/ ./claw/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY --from=web /web/dist ./web/dist

EXPOSE 8700
# Migrations run on startup (auto_migrate); serve the API + built web UI.
CMD ["uvicorn", "claw.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8700"]
