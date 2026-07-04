# Softnix PrivateClaw

**A streaming-first, multi-tenant personal AI agent platform.** Chat, browser
automation, files/documents, scheduled tasks, memory, and connectors to your
own apps — all self-hosted, with your own LLM provider.

This is a ground-up rebuild of `softnix-agenticclaw` (a fork of nanobot),
applying the lessons learned from it. The legacy repo is kept locally under
`softnix-agenticclaw/` for reference only — it is **not** part of this codebase
and is excluded from git.

## Features

- **Streaming chat** — token-by-token replies over WebSocket, tool activity shown live.
- **Multi-LLM** — any [LiteLLM](https://docs.litellm.ai/)-supported provider (Anthropic, OpenAI,
  Gemini, OpenRouter, local models, …) via one config value.
- **Tools** — filesystem (read/write/edit/list), shell (sandboxed), web search/fetch, browser
  automation, Excel/CSV/PDF/Word reading, subagents, dynamic multi-step workflows, and
  self-managed schedules — all callable by the agent itself.
- **Skills** — teach the agent reusable procedures; loaded on demand, not bloating every prompt.
- **Memory** — a living per-user memory doc + searchable history, auto-consolidated as chats grow.
- **Multimodal** — attach images (vision) and documents in the chat composer.
- **Connectors** — attach MCP servers (Gmail, GitHub, Notion, Outlook, Tavily, …) with one-click
  presets; secrets are encrypted at rest.
- **Schedules & heartbeat** — recurring/one-shot prompts, plus opt-in proactive check-ins where
  the agent decides for itself whether to reach out.
- **Channels** — web chat and Telegram (with per-user account linking so history/memory carry over).
- **Auth** — password + JWT, OIDC social login (Google/Microsoft), or a dev token for local scripts.
- **Multi-tenant admin** — user management, a global control policy (PII/secret masking),
  token/cost usage stats, and capability status, all from an in-app admin console.
- **Feedback loop** — 👍/👎 on replies, captured as the seed for future self-learning.
- **Production-ready** — Alembic migrations, secrets encrypted at rest, per-user rate limits and
  bounded memory, Docker/compose, health/readiness probes, graceful shutdown.

## Design principles (vs. the legacy platform)

| Legacy problem | This design |
|---|---|
| No token streaming; file-relay + polling between processes | LLM stream → agent loop → in-memory event bus → WebSocket, end to end |
| 1 user = 1 OS process/container | Many `ClawAgent` actors in one process; locks are per-session, not global |
| Whole agent lived in a sandbox | **Tool-ephemeral**: only shell commands pay the container cost (`docker run --rm`) |
| JSON + JSONL + SQLite scattered on disk, whole-file rewrites | Postgres, append-only `messages` table, one relational store, Alembic-migrated |
| Thai keyword heuristics hardcoded in the core loop | Core is language-neutral; user-facing text goes through `claw/i18n.py` |
| Char-count context budgeting | Token-aware `ContextAssembler`, trims at user-turn boundaries |
| 18K-line admin god file + 16K-line hand-written JS | Small focused modules; React + Vite frontend |
| LLM errors persisted as assistant content (poisoned history) | Provider errors raise; only the user message is persisted |
| Secrets stored in plaintext | Connector secrets encrypted at rest (Fernet) |

---

## Getting started

### Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Node.js 20+** and npm (for the web frontend)
- **Docker** (for Postgres, and for the sandbox/tool-ephemeral shell execution)
- An **API key** for at least one LLM provider (e.g. Anthropic, OpenAI, or an OpenRouter key)

### 1. Clone and configure

```bash
git clone https://github.com/rujirapongsn2/SoftnixPrivateClaw_v2.git
cd SoftnixPrivateClaw_v2
cp .env.example .env
```

Open `.env` and set at minimum:

```bash
CLAW_SECRET_KEY=$(openssl rand -hex 32)     # generate a real secret
CLAW_LLM__MODEL=anthropic/claude-sonnet-4-5 # any LiteLLM model id
CLAW_LLM__API_KEY=sk-...                    # your provider's API key
```

See [Configuration reference](#configuration-reference) below for every other option
(OIDC login, Telegram, browser automation, sandbox limits, rate limits, …).

### 2. Start Postgres

```bash
docker compose up -d postgres
```

This starts Postgres on host port **5442** (chosen to avoid clashing with a default
`5432` you might already have running) with credentials matching `.env.example`.

### 3. Install and run the backend

```bash
uv sync                                          # installs Python dependencies
uv run alembic upgrade head                      # create the database schema
uv run uvicorn claw.main:create_app --factory --port 8700
```

The API is now serving on `http://localhost:8700` (try `curl http://localhost:8700/api/health`).
Migrations also run automatically on every startup (`CLAW_AUTO_MIGRATE=true` by default), so step 3's
manual `alembic upgrade` is a safety net, not strictly required after the first run.

### 4. Install and run the web frontend

In a second terminal:

```bash
cd web
npm install
npm run dev
```

Open **http://localhost:5173**. Register the first account — it automatically becomes the
administrator. From there:

- Start chatting — the agent streams replies and shows tool activity live.
- Open **Settings** (bottom of the sidebar) to manage Skills, Memory, Connectors, Schedules,
  Heartbeat, and Telegram linking.
- Admins get an **Admin console** for user management, the control policy, and usage stats.

### 5. Run the tests

```bash
uv run pytest          # backend (uses SQLite in-memory, no external services needed)
cd web && npm run build # frontend type-check + production build
```

---

## Running in production (single container)

A multi-stage `Dockerfile` builds the web frontend and serves it together with the API from one
Python image — no separate frontend server needed.

```bash
export CLAW_SECRET_KEY=$(openssl rand -hex 32)
export CLAW_LLM__API_KEY=sk-...
docker compose -f docker-compose.prod.yml up --build
```

This starts Postgres + the app together. The app runs Alembic migrations on startup, then serves
both the API and the built web UI on `http://localhost:8700`. Set `CLAW_AUTH_MODE=password` (already
the default in `docker-compose.prod.yml`) so the dev token is disabled.

Database schema changes are managed with Alembic (`migrations/`). After editing a model in
`claw/db/models.py`, generate a new revision:

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

---

## Configuration reference

All settings are environment variables prefixed `CLAW_`; nested settings use `__` (double
underscore), e.g. `CLAW_LLM__MODEL`. See `.env.example` for the full list with comments. Key groups:

| Group | Variables | Notes |
|---|---|---|
| Database | `CLAW_DATABASE_URL`, `CLAW_AUTO_MIGRATE` | Postgres via `asyncpg` |
| Auth | `CLAW_SECRET_KEY`, `CLAW_AUTH_MODE`, `CLAW_DEV_TOKEN`, `CLAW_OPEN_REGISTRATION`, `CLAW_TOKEN_TTL_SECONDS` | `auth_mode=dev` also accepts a static token for scripts/tests |
| OIDC social login | `CLAW_PUBLIC_BASE_URL`, `CLAW_WEB_BASE_URL`, `CLAW_OIDC_GOOGLE_*`, `CLAW_OIDC_MICROSOFT_*` | A "Continue with …" button appears automatically once a provider's id+secret are set |
| LLM | `CLAW_LLM__MODEL`, `CLAW_LLM__API_KEY`, `CLAW_LLM__API_BASE`, `CLAW_LLM__MAX_TOKENS`, `CLAW_LLM__TEMPERATURE`, `CLAW_LLM__MAX_ITERATIONS`, `CLAW_LLM__MAX_CONTEXT_TOKENS` | Any LiteLLM-supported model id |
| Sandbox | `CLAW_SANDBOX__ENABLED`, `CLAW_SANDBOX__IMAGE`, `CLAW_SANDBOX__CPU_LIMIT`, `CLAW_SANDBOX__MEMORY_LIMIT`, `CLAW_SANDBOX__NETWORK`, `CLAW_SANDBOX__TIMEOUT_SECONDS` | Tool-ephemeral shell execution |
| Browser automation | `CLAW_BROWSER__ENABLED`, `CLAW_BROWSER__HEADLESS`, `CLAW_BROWSER__TIMEOUT_SECONDS` | Needs the `browser` extra (below) |
| Telegram | `CLAW_TELEGRAM_BOT_TOKEN` | Channel starts only when set; users link their own account in-app |
| Safety & scale | `CLAW_POLICY_ENFORCE`, `CLAW_MAX_RESIDENT_AGENTS`, `CLAW_MAX_SESSION_LOCKS`, `CLAW_TURNS_PER_MINUTE` | Control policy + resource caps + per-user rate limit |

### Optional dependency groups

Some tools need extra packages that aren't installed by default:

```bash
# Browser automation (the `browser` tool)
uv pip install playwright
uv run playwright install chromium

# Document tools (read_excel / read_pdf / read_docx)
uv sync --extra documents
```

---

## Project layout

```
claw/
  config.py           env-driven settings (CLAW_*)
  main.py              app factory: wires stores → runtime → API, serves web/dist in prod
  providers/           streaming LLM interface, LiteLLM implementation, provider quirk registry
  tools/                filesystem, shell, web, browser, documents, schedule, spawn, workflow, skills
  sandbox/              tool-ephemeral Docker runner (cpu/mem/pids/network limits)
  core/                 event bus, token-aware context, streaming agent loop, runtime,
                        memory consolidation, scheduler, heartbeat, connectors, connector presets
  auth/                 password hashing, JWT, OIDC (Google/Microsoft)
  security/              control policy engine, secret encryption at rest
  browser/               Playwright-backed browser automation manager
  channels/              Telegram channel + account linking
  db/                    SQLAlchemy models + stores, async engine
  api/                   FastAPI routers: auth, admin, chat (REST + WebSocket), management
migrations/              Alembic revisions
web/                      React 19 + Vite + Astryx design system — claude.ai-style chat UI
tests/                    ~29 test files covering the backend (fake LLM provider, SQLite)
```

## Status

Feature-complete relative to the legacy platform's must-keep capabilities, plus production
hardening not present in the original: Alembic migrations, secret encryption at rest, per-user
rate limiting and bounded memory, Docker/compose, health/readiness probes, and graceful shutdown.

Not yet built (tracked as future work, not blocking normal use): a proactive cross-channel notify
tool (push a message to Telegram outside of a user-initiated turn), deeper self-learning (reflecting
feedback into memory and auto-drafting skills with approval), load testing, and observability
dashboards.
