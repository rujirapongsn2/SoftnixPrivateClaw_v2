# Softnix PrivateClaw

**A self-hosted, multi-tenant personal AI agent platform.** Streaming chat, browser
automation, file/document tools, a knowledge base (RAG), scheduled tasks, memory, and
connectors to your own apps (Gmail, Notion, GitHub, …) — running on your own
infrastructure, with your own LLM provider and keys.

## Why use it

- **Your data stays yours** — self-hosted, Postgres-backed, secrets (API keys, OAuth
  tokens) encrypted at rest. No third party sees your chats or documents.
- **Any LLM** — Anthropic, OpenAI, Gemini, OpenRouter, local models, or any
  [LiteLLM](https://docs.litellm.ai/)-supported provider. Admins configure global
  models in a Control Plane; individual users can also **bring their own key** for a
  private model only they can use.
- **Real agent, not a chatbot** — sandboxed shell/document tools, browser automation,
  knowledge search, subagents, and self-managed schedules, with streaming output and a
  live execution panel so you see what it's doing.
- **Multi-tenant by design** — every user gets their own chats, memory, skills,
  connectors, and (optionally) private models; admins get a Control Plane for
  providers, guardrails, OAuth apps, and audit logs.
- **Production-ready** — Alembic migrations, health checks, graceful shutdown, rate
  limits, and three supported install paths (below).

## Features

- **Streaming chat** with live tool activity and a collapsible Execution panel.
- **Multi-LLM + BYOK** — admin-managed global providers, plus per-user private
  providers that only appear in that user's own model picker.
- **Ask / Auto permission mode** — approve risky actions per turn, or let them run.
- **Knowledge (RAG)** — upload documents into knowledge bases; searchable by the agent
  (Thai + English, no embedding model needed), stored as portable Open Knowledge
  Format bundles.
- **Tools** — filesystem, sandboxed shell (generates PDF/Word/Excel/PowerPoint/zip),
  web search/fetch, browser automation, subagents, multi-step workflows, schedules.
- **Skills & Memory** — the agent can write its own reusable skills and maintains a
  living per-user memory doc.
- **Connectors** — MCP servers (Gmail, Outlook, OneDrive, GitHub, Notion, Tavily, …)
  via guided setup or one-click OAuth.
- **Channels** — web chat and Telegram, with account linking.
- **Control Plane (admin console)** — providers/models, guardrails (PII/secret
  masking), OAuth apps, audit logs, users & groups.

---

## Installation

Three ways to run it, pick one:

### Option A — One-line installer (recommended for a real server)

Runs the app **natively on the host** as an always-on service (auto-restarts on
crash and on boot); Postgres and the tool-sandbox run as Docker containers.
Supports **macOS**, **Ubuntu/Debian**, and **Rocky/RHEL/Fedora** (x86_64 & arm64).

```bash
git clone https://github.com/rujirapongsn2/SoftnixPrivateClaw_v2.git privateclaw
cd privateclaw
./install.sh
```

The installer checks/installs Docker and `uv`, installs dependencies, builds the web
UI and sandbox image, starts Postgres, writes `.env` (prompts for your LLM API key),
runs migrations, and installs a system service:

- **Linux** → a `systemd` unit (starts on boot, restarts on crash).
- **macOS** → a `launchd` agent by default (starts on login); pass
  `./install.sh --macos-daemon` for a boot-time daemon on a headless Mac server.

```bash
claw status | restart | logs | update    # manage the service (or ./scripts/claw)
```

Useful flags: `--yes` (non-interactive), `--no-service`, `--port`, `--pg-port`,
`--data-dir`. Re-running is safe (idempotent, won't overwrite an existing `.env`).

### Option B — Docker Compose (app also containerized)

Use this if you'd rather the app itself ran in a container too (more moving parts —
it drives the host Docker daemon for the sandbox).

```bash
docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .   # once
mkdir -p /srv/claw/data/workspaces /srv/claw/data/knowledge
cat > .env <<'EOF'
CLAW_DATA_DIR=/srv/claw/data
POSTGRES_PASSWORD=change-me
CLAW_SECRET_KEY=change-me
CLAW_LLM__MODEL=openrouter/anthropic/claude-sonnet-5
CLAW_LLM__API_KEY=sk-...
CLAW_PUBLIC_BASE_URL=https://claw.example.com
CLAW_WEB_BASE_URL=https://claw.example.com
EOF
docker compose -f docker-compose.prod.yml up --build -d
```

Serves on `127.0.0.1:8700` — put your reverse proxy / tunnel in front for TLS.
Both containers restart automatically on crash or host reboot. Single instance only
(in-process session state); back up the `pgdata` volume with `pg_dump`.

### Option C — Manual dev setup (for local development)

```bash
git clone https://github.com/rujirapongsn2/SoftnixPrivateClaw_v2.git
cd SoftnixPrivateClaw_v2
cp .env.example .env   # set CLAW_SECRET_KEY, CLAW_LLM__MODEL, CLAW_LLM__API_KEY

docker compose up -d postgres                                    # Postgres on :5442
docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest . # tool sandbox

uv sync
uv run alembic upgrade head
uv run uvicorn claw.main:create_app --factory --port 8700 --reload --reload-dir claw
```

In a second terminal:

```bash
cd web && npm install && npm run dev
```

Open `http://localhost:5173` (frontend proxies API calls to `:8700`).

Run tests: `uv run pytest` (backend, SQLite in-memory) and `cd web && npm run build`
(frontend type-check + build).

---

## Getting started

1. Open the app (`http://localhost:8700` for A/B, `http://localhost:5173` for C) and
   **register the first account** — it automatically becomes the administrator.
2. Start chatting — replies stream live with tool activity shown inline.
3. **Settings** (bottom of the sidebar): Skills, Knowledge, Memory, **My Models**
   (bring your own key), Connectors, Schedules, Heartbeat, Telegram, Browser extension.
4. **Control Plane** (admins only): Overview, LLM Providers, Guardrails, OAuth apps,
   Audit Logs, Users & Groups.

If you skipped setting an LLM key during install, add one in **Control Plane → LLM
Providers** (or `.env` → `CLAW_LLM__API_KEY`).

## Configuration

All settings are environment variables prefixed `CLAW_` (nested keys use `__`, e.g.
`CLAW_LLM__MODEL`). See `.env.example` for the full annotated list — database, auth,
OIDC social login, LLM defaults, sandbox limits, knowledge storage, speech-to-text,
browser automation, Telegram, and rate limits. Anything an admin can also configure
live in-app (LLM providers/models, guardrails, OAuth apps) takes precedence per chat.

## Project layout

```
claw/        FastAPI backend — config, providers, tools, sandbox, knowledge, core
             agent loop/runtime, auth, security, browser automation, db, api routers
migrations/  Alembic schema revisions
docker/      Sandbox image (document/archive tool stack)
web/         React + Vite frontend — chat UI, Settings, Control Plane
tests/       Backend test suite (fake LLM provider, SQLite)
install.sh, scripts/claw   Host-native installer + service control CLI
```
