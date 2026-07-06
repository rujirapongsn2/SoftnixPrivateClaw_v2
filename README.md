# Softnix PrivateClaw

**A streaming-first, multi-tenant personal AI agent platform.** Chat, browser
automation, files/documents, knowledge bases, scheduled tasks, memory, and
connectors to your own apps — all self-hosted, with your own LLM provider.

This is a ground-up rebuild of `softnix-agenticclaw` (a fork of nanobot),
applying the lessons learned from it. The legacy repo is kept locally under
`softnix-agenticclaw/` for reference only — it is **not** part of this codebase
and is excluded from git.

## Features

- **Streaming chat** — token-by-token replies over WebSocket, tool activity shown live inline and in
  a collapsible right-hand **Execution panel** (a vertical, real-time diagram of what the agent is doing).
- **Multi-LLM** — any [LiteLLM](https://docs.litellm.ai/)-supported provider (Anthropic, OpenAI,
  Gemini, OpenRouter, local models, …). Admins configure one or more providers + models (with cost
  tier and description) in the admin console; each chat has its own sticky model picker.
- **Ask / Auto permission mode** — a composer toggle that gates risky actions (sandbox shell commands,
  spawning subagents, running a multi-step workflow) behind an inline approval card, or lets them run
  unattended — the user's choice, per turn.
- **Knowledge (RAG)** — upload PDF/Word/text/Markdown/HTML documents into named knowledge bases
  (private or shared/public); the agent searches them with Postgres trigram similarity (works for
  Thai and English with no embedding model) and cites its source. Storage follows the
  [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog) — a bundle of
  plain frontmattered markdown files — so your knowledge stays portable and human-readable outside
  the app. Selectable from the chat composer's `+` menu as a mention chip, alongside Skills and
  Connectors.
- **Tools** — filesystem (read/write/edit/list), sandboxed shell (with a document/archive stack —
  generate PDF, Excel, Word, PowerPoint, zip/tar.gz — plus internet access to `pip install` on
  demand), web search/fetch, browser automation, Excel/CSV/PDF/Word *reading*, subagents, dynamic
  multi-step workflows (with a live sub-step checklist, not just a spinner), knowledge search, and
  self-managed schedules — all callable by the agent itself.
- **Skills** — teach the agent reusable procedures; loaded on demand, not bloating every prompt. The
  agent can author and update its own skills through a dedicated tool.
- **Memory** — a living per-user memory doc + searchable history, auto-consolidated as chats grow.
- **Multimodal + speech-to-text** — attach images (vision) and documents in the chat composer, or
  dictate with the mic button (Groq Whisper — optional, enabled once configured).
- **Connectors** — attach MCP servers (Gmail, Outlook, OneDrive, Outlook Calendar, GitHub, Notion,
  Tavily, Composio, Softnix ONE, …) through a guided, non-technical setup: API-key/token fields where
  that's all a service needs, or one-click OAuth ("Connect with Google/Microsoft") once an admin
  registers the OAuth app once. Secrets are encrypted at rest. A downloadable browser extension
  (`browser-extension/`) lets the agent drive the *user's own* logged-in Chrome tabs instead of an
  isolated headless browser.
- **File artifacts** — files the agent creates or edits (a report, a chart, a generated document)
  show up as an openable/downloadable chip under its reply, not just a filename in text.
- **Schedules & heartbeat** — recurring/one-shot prompts, plus opt-in proactive check-ins where
  the agent decides for itself whether to reach out. Scheduled runs get their own session, tagged
  with an alarm-clock marker in the sidebar.
- **Channels** — web chat and Telegram (with per-user account linking so history/memory carry over).
- **Auth** — password + JWT, OIDC social login (Google/Microsoft), or a dev token for local scripts.
- **In-panel admin console** (not a modal) with sub-sections:
  - **Overview** — usage stats, activity charts, token usage per model, configured LLM providers,
    and sessions-by-user / sessions-by-day breakdowns.
  - **LLM Providers** — add/edit providers and models (with cost tier + description shown in the
    chat model picker); keys encrypted at rest.
  - **Guardrails** — a control policy engine (PII/secret masking or blocking) with built-in rules
    plus admin-editable custom keyword/regex rules, and an enforce/monitor-only toggle.
  - **OAuth apps** — register the Google/Microsoft OAuth client used for one-click connector setup;
    redirect URIs shown resolved against your real public URL (works behind a tunnel/reverse proxy).
  - **Audit Logs** — a searchable, paginated trail of tool calls, sandbox shell commands (with
    whether they had network access), and policy hits.
  - **Users** — manage accounts, roles, and reset passwords.
- **Feedback loop** — 👍/👎 on replies, captured as the seed for future self-learning.
- **Production-ready** — Alembic migrations, secrets encrypted at rest, per-user rate limits and
  bounded memory, Docker/compose, health/readiness probes, graceful shutdown.

## Design principles (vs. the legacy platform)

| Legacy problem | This design |
|---|---|
| No token streaming; file-relay + polling between processes | LLM stream → agent loop → in-memory event bus (with per-turn replay for reconnects) → WebSocket, end to end |
| 1 user = 1 OS process/container | Many `ClawAgent` actors in one process; locks are per-session, not global |
| Whole agent lived in a sandbox | **Tool-ephemeral**: only shell commands pay the container cost (`docker run --rm`), using a custom image with a document/data stack pre-installed |
| JSON + JSONL + SQLite scattered on disk, whole-file rewrites | Postgres, append-only `messages` table, one relational store, Alembic-migrated |
| Thai keyword heuristics hardcoded in the core loop | Core is language-neutral; user-facing text goes through `claw/i18n.py`; knowledge search uses trigram similarity, not keyword matching, so Thai works without a tokenizer |
| Char-count context budgeting | Token-aware `ContextAssembler`, trims at user-turn boundaries |
| 18K-line admin god file + 16K-line hand-written JS | Small focused modules; React + Vite frontend, admin console rendered in-panel with its own sections |
| LLM errors persisted as assistant content (poisoned history) | Provider errors raise; only the user message is persisted |
| Secrets stored in plaintext | Connector secrets, LLM provider keys, and OAuth client secrets all encrypted at rest (Fernet) |
| Risky actions ran unattended by default | Ask/Auto permission mode gates sandbox exec, subagent spawn, and workflows behind an inline approval card |
| Sidebar "unread" status was a fragile live-only heuristic | Persisted per-session read timestamps compared against the server's own `updated_at`, so status survives reloads and doesn't race a fast reply |

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
(OIDC login, Telegram, browser automation, sandbox limits, speech-to-text, rate limits, …).

### 2. Start Postgres

```bash
docker compose up -d postgres
```

This starts Postgres on host port **5442** (chosen to avoid clashing with a default
`5432` you might already have running) with credentials matching `.env.example`.

### 3. Build the sandbox image

Shell commands the agent runs (`exec`) execute in a short-lived container, not the host process.
Build the image once — it ships a document/archive stack (`reportlab`, `python-docx`,
`python-pptx`, `openpyxl`, `pandas`, `zip`/`unzip`) so the agent can generate PDF, Word, Excel,
PowerPoint, and archives without you installing anything extra later:

```bash
docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .
```

By default the sandbox also has internet access (`CLAW_SANDBOX__NETWORK=bridge`) so the agent can
`pip install` an occasional missing package on demand; every command it runs is written to the
admin audit log regardless. Set `CLAW_SANDBOX__NETWORK=none` if you'd rather it stayed fully
isolated (the agent then can only use what's already in the image).

### 4. Install and run the backend

```bash
uv sync                                          # installs Python dependencies
uv run alembic upgrade head                      # create the database schema
uv run uvicorn claw.main:create_app --factory --port 8700
```

The API is now serving on `http://localhost:8700` (try `curl http://localhost:8700/api/health`).
Migrations also run automatically on every startup (`CLAW_AUTO_MIGRATE=true` by default), so step 4's
manual `alembic upgrade` is a safety net, not strictly required after the first run.

> If you're iterating on `claw/` with `uvicorn --reload`, scope the reload watcher to that directory
> only (`--reload-dir claw`) — the agent's per-user workspaces and knowledge bundles live under
> `workspaces/` and `knowledge/` at the repo root, and a naive whole-project watch will restart the
> server (killing in-flight turns) every time the agent writes a file there.

### 5. Install and run the web frontend

In a second terminal:

```bash
cd web
npm install
npm run dev
```

Open **http://localhost:5173**. Register the first account — it automatically becomes the
administrator. From there:

- Start chatting — the agent streams replies and shows tool activity live, inline and in the
  right-hand Execution panel.
- Open **Settings** (bottom of the sidebar) to manage Skills, Knowledge, Memory, Connectors,
  Schedules, Heartbeat, Telegram linking, and the browser extension.
- Admins get an **Admin console** with Overview, LLM Providers, Guardrails, OAuth apps, Audit Logs,
  and Users.

### 6. Run the tests

```bash
uv run pytest          # backend (uses SQLite in-memory, no external services needed)
cd web && npm run build # frontend type-check + production build
```

---

## Production install (one line, host-native) — recommended

The app runs **natively on the host** as a service; Postgres and the agent's tool-sandbox run as
**Docker containers**. One installer handles macOS, Ubuntu/Debian, and Rocky/RHEL/Fedora
(x86_64 & arm64), checks the prerequisites, and sets everything up:

```bash
git clone <your-repo-url> privateclaw && cd privateclaw
./install.sh
```

That's it. The installer:

1. **Checks prerequisites** — verifies **Docker** is installed and its daemon is running (offers to
   install Docker Engine on Linux; guides you to Docker Desktop on macOS), and installs **uv** if
   missing. No system Python or Node is required — `uv` fetches Python 3.12, and the web UI builds
   with your host Node if present, otherwise in a throwaway Docker container.
2. Installs app dependencies (`uv sync`), builds the web frontend, and builds `claw-sandbox:latest`.
3. Starts a **Postgres** container (`claw-postgres`, data in the `claw-pgdata` volume).
4. Writes a `.env` (generates `CLAW_SECRET_KEY`, sets `CLAW_AUTH_MODE=password`, prompts for the LLM
   key), runs migrations, and installs a service — **systemd** on Linux, **launchd** on macOS —
   that auto-starts on boot/login.
5. Health-checks `http://127.0.0.1:<port>/api/ready` and prints where to open the app.

Then open `http://localhost:8700` and register the first account — it becomes the admin. Put your
reverse proxy / tunnel in front for TLS (the app listens on the port in `.env`).

**Flags:** `./install.sh --yes` (non-interactive; reads `CLAW_LLM__API_KEY`, `CLAW_LLM__MODEL`,
`CLAW_PORT`, `CLAW_PG_PORT`, `CLAW_DATA_DIR` from the environment), `--no-service` (skip the system
service), `--port` / `--pg-port` / `--data-dir`. Re-running is idempotent (it won't clobber an
existing `.env`).

**Manage it** with the generated control CLI (symlinked to `claw` when possible, else
`./scripts/claw`):

```bash
claw status      # supervisor state + /api/ready health
claw restart
claw logs        # tail service logs
claw update      # git pull + uv sync + rebuild web + migrate + restart
```

**Why the sandbox needs no special wiring here:** because the app runs on the host, the `exec` tool
calls the host's own `docker` directly and `workspace.resolve()` is already a real host path — none
of the socket-mounting / path-matching that the Docker-Compose deployment (below) requires.

**Operational notes** (same as below): single instance only; back up the `claw-pgdata` volume with
`pg_dump`; `CLAW_SANDBOX__NETWORK=bridge` lets the agent `pip install` on demand (set `none` to
isolate).

---

## Running in production (Docker Compose, single host)

> Alternative to the host-native installer above — use this if you'd rather run the **app itself in
> a container** too. It relies on Docker-outside-of-Docker for the sandbox, which is more moving
> parts; the host-native installer is simpler for most single-host deployments.

A multi-stage `Dockerfile` builds the web frontend and serves it together with the API from one
Python image — no separate frontend server needed. `docker-compose.prod.yml` runs Postgres + the
app, behind your own reverse proxy / tunnel (which terminates TLS). The app binds to
`127.0.0.1:8700` only.

### How the sandbox runs in production (important)

The `exec` tool runs each shell command / document-generation task in a short-lived
`claw-sandbox:latest` container. In production the app container drives the **host Docker daemon**
(Docker-outside-of-Docker) via the mounted `/var/run/docker.sock`. Two consequences:

- **The sandbox image must exist on the host daemon** — build it there before the first run.
- **Data must live at an absolute host path mounted at the same path inside the container.** The
  sandbox bind-mounts `workspace.resolve()` into a sibling container via the host daemon, so that
  path has to resolve identically on the host and in the app. `docker-compose.prod.yml` wires this
  through `CLAW_DATA_DIR` — set it to a real absolute host path.
- **Security note:** mounting the docker socket grants the app container host-root-equivalent
  access. This is inherent to DooD; only run this on a host you control.

### Deploy

```bash
# 1. One-time: build the sandbox image on the host daemon.
docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .

# 2. Create the persistent data dir and a host .env (git-ignored).
mkdir -p /srv/claw/data/workspaces /srv/claw/data/knowledge
cat > .env <<'EOF'
CLAW_DATA_DIR=/srv/claw/data
POSTGRES_PASSWORD=$(openssl rand -hex 16)
CLAW_SECRET_KEY=$(openssl rand -hex 32)
CLAW_LLM__MODEL=openrouter/anthropic/claude-sonnet-5
CLAW_LLM__API_KEY=sk-...
CLAW_PUBLIC_BASE_URL=https://claw.example.com   # the URL your proxy serves
CLAW_WEB_BASE_URL=https://claw.example.com
# optional: QROQ_KEY=..., CLAW_OIDC_*, CLAW_TELEGRAM_BOT_TOKEN
EOF

# 3. Build + start.
docker compose -f docker-compose.prod.yml up --build -d
```

The app runs Alembic migrations on startup, then serves the API + built web UI on
`127.0.0.1:8700`; point your reverse proxy at it. `CLAW_AUTH_MODE=password` (the prod default)
disables the dev token. `CLAW_PUBLIC_BASE_URL` / `CLAW_WEB_BASE_URL` must be the real public URL —
OIDC and connector-OAuth redirect URIs derive from them.

**Operational notes:**
- **Single instance only** — the event bus, per-user agent cache, session locks, and Ask/Auto
  confirmations are in-process memory; don't run more than one `app` replica.
- **Back up Postgres** — the `pgdata` volume is the only copy of users, chats, the knowledge index,
  and config. Add a `pg_dump` cron; the `CLAW_DATA_DIR` bind holds uploaded files + knowledge bundles.
- **Sandbox network** — `CLAW_SANDBOX__NETWORK=bridge` (default) lets the agent `pip install` on
  demand; set `none` to isolate it to what's baked into `claw-sandbox`.

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
| OIDC social login | `CLAW_PUBLIC_BASE_URL`, `CLAW_WEB_BASE_URL`, `CLAW_OIDC_GOOGLE_*`, `CLAW_OIDC_MICROSOFT_*` | A "Continue with …" button appears automatically once a provider's id+secret are set; also used to derive connector OAuth redirect URIs |
| LLM (env default) | `CLAW_LLM__MODEL`, `CLAW_LLM__API_KEY`, `CLAW_LLM__API_BASE`, `CLAW_LLM__MAX_TOKENS`, `CLAW_LLM__TEMPERATURE`, `CLAW_LLM__MAX_ITERATIONS`, `CLAW_LLM__MAX_CONTEXT_TOKENS` | Any LiteLLM-supported model id; admins can additionally configure providers/models in-app (Admin console → LLM Providers), which take precedence per chat |
| Sandbox | `CLAW_SANDBOX__ENABLED`, `CLAW_SANDBOX__IMAGE`, `CLAW_SANDBOX__CPU_LIMIT`, `CLAW_SANDBOX__MEMORY_LIMIT`, `CLAW_SANDBOX__NETWORK`, `CLAW_SANDBOX__TIMEOUT_SECONDS` | Tool-ephemeral shell execution; `IMAGE` defaults to `claw-sandbox:latest` (see step 3 above), `NETWORK` defaults to `bridge` |
| Knowledge | `CLAW_KNOWLEDGE_ROOT` | Directory holding each knowledge base's OKF bundle (default `./knowledge`) |
| Speech-to-text | `QROQ_KEY`, `QROQ_URL`, `QROQ_MODEL` | **Not** `CLAW_`-prefixed. Groq (or any OpenAI-compatible `/audio/transcriptions` endpoint) for the composer's mic button; the button only appears once `QROQ_KEY` is set |
| Browser automation | `CLAW_BROWSER__ENABLED`, `CLAW_BROWSER__HEADLESS`, `CLAW_BROWSER__TIMEOUT_SECONDS`, `CLAW_BROWSER__CLIENT_EXTENSION_ENABLED` | Server-side headless browser, or pair the user's own Chrome via the extension in `browser-extension/` |
| Telegram | `CLAW_TELEGRAM_BOT_TOKEN` | Channel starts only when set; users link their own account in-app |
| Safety & scale | `CLAW_POLICY_ENFORCE`, `CLAW_MAX_RESIDENT_AGENTS`, `CLAW_MAX_SESSION_LOCKS`, `CLAW_TURNS_PER_MINUTE` | Guardrails default enforce/monitor mode (admin-editable at runtime) + resource caps + per-user rate limit |

### Optional dependency groups

Document *reading* tools (`read_excel`, `read_pdf`, `read_docx`) and PDF/Word parsing are core
dependencies (installed by `uv sync`). Two extras remain opt-in:

```bash
# Browser automation (the server-side `browser` tool)
uv pip install playwright
uv run playwright install chromium

# Explicit "documents" extra (kept for compatibility; pypdf/python-docx are already core deps)
uv sync --extra documents
```

Document *writing* (PDF/Word/Excel/PowerPoint generation) doesn't need a Python extra at all — it
happens inside the sandbox container, which already has the stack baked in (see step 3).

---

## Project layout

```
claw/
  config.py             env-driven settings (CLAW_*)
  main.py                app factory: wires stores → runtime → API, serves web/dist in prod
  providers/             streaming LLM interface, LiteLLM implementation, provider quirk registry
  tools/                  filesystem, shell, web, browser, documents, knowledge, schedule, spawn,
                          workflow, skills — all callable by the agent
  sandbox/                tool-ephemeral Docker runner (cpu/mem/pids/network limits)
  knowledge/               OKF bundle writer + document parsing/chunking for the Knowledge feature
  integrations/            self-hosted MCP server implementations bundled with the app (GitHub,
                          Notion, Gmail, Outlook, OneDrive, Outlook Calendar, Tavily, …)
  core/                   event bus (with per-turn replay), token-aware context, streaming agent
                          loop, runtime, memory consolidation, scheduler, heartbeat, connectors,
                          connector presets
  auth/                   password hashing, JWT, OIDC (Google/Microsoft), connector OAuth
  security/                guardrails/control policy engine, secret encryption at rest
  browser/                 Playwright-backed browser automation manager + client-extension broker
  channels/                 Telegram channel + account linking
  db/                       SQLAlchemy models + stores, async engine
  api/                      FastAPI routers: auth, admin, chat (REST + WebSocket), management,
                          knowledge, connector OAuth, browser extension
migrations/                Alembic revisions
docker/sandbox.Dockerfile   the tool-ephemeral sandbox image (document/archive stack)
browser-extension/          downloadable Chrome extension for client-side browser pairing
web/                        React 19 + Vite + Astryx design system — claude.ai-style chat UI, with
                          an in-panel admin console and a live Execution diagram
tests/                      backend test suite (fake LLM provider, SQLite)
```

## Status

Feature-complete relative to the legacy platform's must-keep capabilities, plus substantial
production and UX work not present in the original: Alembic migrations, secret encryption at rest,
per-user rate limiting and bounded memory, Docker/compose, health/readiness probes, graceful
shutdown, an in-panel admin console with configurable LLM providers and guardrails, one-click
connector OAuth, a document-capable sandbox, Ask/Auto permission gating, a knowledge base (RAG) built
on the Open Knowledge Format, and a persisted read-tracking fix for the sidebar's unread indicator.

Not yet built (tracked as future work, not blocking normal use): a proactive cross-channel notify
tool (push a message to Telegram outside of a user-initiated turn), deeper self-learning (reflecting
feedback into memory and auto-drafting skills with approval), load testing, and observability
dashboards.
