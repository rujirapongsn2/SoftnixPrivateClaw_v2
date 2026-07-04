# Softnix PrivateClaw — API server image.
# Multi-stage: build the web frontend, then serve API + static from one Python image.

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* ./
RUN uv pip install --system --no-cache \
      fastapi "uvicorn[standard]" litellm json-repair pydantic pydantic-settings \
      "sqlalchemy[asyncio]" asyncpg loguru httpx cryptography mcp croniter alembic

COPY claw/ ./claw/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY --from=web /web/dist ./web/dist

EXPOSE 8700
# Migrations run on startup (auto_migrate); serve the API.
CMD ["uvicorn", "claw.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8700"]
