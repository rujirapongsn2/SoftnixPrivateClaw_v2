# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Softnix PrivateClaw — a self-hosted, multi-tenant personal AI agent platform. Streaming
chat, browser automation, a sandboxed shell/document toolset, a knowledge base (RAG),
scheduled tasks, per-user memory, and MCP connectors, running on the operator's own
infrastructure and LLM provider/keys. Admins get a Control Plane (providers, guardrails,
OAuth apps, audit logs, users/groups); every user gets their own chats, memory, skills,
connectors, and optionally a private ("bring your own key") model.

## Commands

### Backend (Python, from repo root — requires `uv`)

```bash
uv sync                                                                                # install deps
docker compose up -d postgres                                                         # Postgres on :5442 (dev)
uv run alembic upgrade head                                                            # run migrations
uv run uvicorn claw.main:create_app --factory --port 8700 --reload --reload-dir claw   # dev server
uv run pytest                                                                          # full suite (SQLite in-memory, offline fake LLM)
uv run pytest tests/test_auth.py                                                       # single file
uv run pytest tests/test_auth.py::test_some_function -v                                # single test
uv run ruff check .                                                                    # lint
uv run ruff format .                                                                   # format
```

`tests/conftest.py` provides a `FakeProvider(LLMProvider)` that replays scripted turns (no
real LLM calls) plus SQLite-backed `db_factory`/`stores` fixtures. `asyncio_mode = "auto"`
(pyproject.toml) — async tests need no `@pytest.mark.asyncio` marker.

### Frontend (from `web/`)

```bash
npm install
npm run dev       # vite dev server, :5173 (proxies /api and /ws to :8700)
npm run build     # "tsc -b && vite build" — this IS the frontend check (typecheck + build)
npx tsc --noEmit  # narrower one-off typecheck, skip the vite build step
```

There is no CI pipeline, Makefile, or lint/test script on the frontend side — `npm run
build`'s `tsc -b` step is the closest thing to a single frontend check.

### Required env vars (`.env`, `CLAW_` prefix, `__` for nested keys, e.g. `CLAW_LLM__MODEL`)

`CLAW_SECRET_KEY`, `CLAW_LLM__MODEL`, `CLAW_LLM__API_KEY`, `CLAW_DATABASE_URL` (must match
wherever Postgres actually runs). Everything else in `.env.example` is optional/feature-gated
(OIDC, Telegram, STT via `QROQ_*`, browser automation, sandbox limits, workspace/knowledge roots).

Sandbox tool execution needs the sandbox image built once: `docker build -f
docker/sandbox.Dockerfile -t claw-sandbox:latest .`

## Backend architecture (`claw/`)

**Entrypoint & state** — `claw/main.py::create_app()` wires everything into one dataclass,
`AppState` (`claw/api/deps.py`), stashed at `app.state.claw`: Settings, DB engine, one
`LiteLLMProvider`, an in-process `EventBus`, one `*Store` per domain, the `PolicyEngine`
(guardrails), `KnowledgeService`, `AgentRuntime`, and background services (scheduler,
heartbeat, Telegram). Routers pull it via `Depends(get_state)` — the app's only DI seam.
`lifespan()` runs Alembic (falls back to `create_all`), seeds the live `PolicyEngine`,
starts background workers, and drains in-flight turns (`AgentRuntime.drain()`) on shutdown.

**Agent loop** — `claw/core/runtime.py` (`AgentRuntime`, multi-tenant: an LRU cache of
per-user `ClawAgent`s) delegates each turn's LLM↔tool cycle to `claw/core/loop.py`: stream
from the provider → forward deltas as events → run any tool calls via `ToolRegistry` →
append tool results → repeat until done. Streaming to the browser is a **WebSocket**, not
SSE (`@router.websocket("/ws/chat/{session_id}")` in `claw/api/routes.py`), subscribed to
the `EventBus` per session, with a per-turn replay buffer for reconnects. Permission modes
(Ask/Auto) are gated in `loop.py`: `UNSAFE_TOOLS` block on a `confirm` callback that
round-trips through `AgentRuntime.request_confirmation` (published as a `ToolConfirmRequest`
event, 600s auto-decline timeout).

**Sandbox / tools** — shell execution is NOT in-process: `claw/sandbox/ephemeral.py`
shells out to `docker run --rm claw-sandbox:latest` per command (falls back to a plain
subprocess when disabled), bind-mounting `workspaces/<user_id>` as `/workspace`. Filesystem
and document tools (`claw/tools/filesystem.py`, `documents.py`) instead run in-process
against that same host workspace path — only shell commands are containerized.

**Multi-tenancy & shared config** — LLM providers (admin Control Plane vs per-user BYOK)
share one implementation, `claw/api/llm_shared.py`, keyed by `owner_id: str | None` (`None`
= admin-global) over the same `LLMConfigStore`; `claw/api/admin.py` and `claw/api/manage.py`
are thin wrappers calling it with different `owner_id`s. Guardrails are one shared engine,
`claw/security/policy.py::PolicyEngine`, invoked at three points: turn input, turn output
(`AgentRuntime._process_turn`), and tool args (`ClawAgent._guard_tool_args`, with a
per-tool exempt list so PII masking doesn't corrupt trusted connector calls).

**Knowledge base (RAG)** — tool-driven, not force-injected: the model calls
`claw/tools/knowledge.py::SearchKnowledgeTool` explicitly; `_process_turn` only injects a
short summary of which knowledge bases exist. Ingestion is a background pipeline
(`claw/knowledge/service.py`) that parses/chunks/OCRs off the event loop, writing both a
DB search index (`KnowledgeChunk`) and a portable **OKF (Open Knowledge Format)** bundle
(`claw/knowledge/okf.py::OkfBundle`) — a directory of markdown files with YAML frontmatter,
one per "concept", plus `index.md`/`log.md`.

**DB layer** — `claw/db/models.py` has plain SQLAlchemy 2.0 ORM classes, no business logic.
`claw/db/stores.py` has one `*Store` class per domain (17 total) holding the async
sessionmaker and exposing domain methods; it is the *only* place raw ORM queries appear —
routers and the runtime always go through a Store, never a bare `Session`.

**Config** — `claw/config.py`: one root `Settings(BaseSettings)` composed of nested
subsystem settings classes (`LLMSettings`, `BrowserSettings`, `SandboxSettings`,
`MemorySettings`, `KnowledgeSettings`, `SchedulerSettings`, …) as fields, each field's
default doubling as inline documentation. STT settings are a deliberate exception, using
un-prefixed `QROQ_*` env vars instead of `CLAW_`.

## Frontend architecture (`web/src/`)

No router library — `App.tsx` holds all navigation in `useState` (active session,
`settingsSection`/`adminSection` string-union state); the only URL interaction is a
one-shot OAuth-callback query-param read, immediately scrubbed via
`history.replaceState`. `Settings.tsx` and `Admin.tsx` are large single-file panels, each
driven by a `SettingsSection`/`AdminSection` type and rendered conditionally inside the
shell — there is no deep-linking or back-button support.

`@astryxdesign/core` is the internal component library (scoped npm package, e.g.
`@astryxdesign/core/Button`); `@astryxdesign/theme-neutral` supplies the CSS custom
properties. `web/src/styles.css` is the single global stylesheet (one flat file, organized
by commented section headers per feature area) — components consume theme vars with
hardcoded fallbacks (`var(--color-primary, #4b6bfb)`) so the app degrades gracefully if the
theme package's vars are absent.

Chat streaming mirrors the backend: `api.ts::openChatSocket` opens a WebSocket to
`/ws/chat/<sessionId>`; `Chat.tsx`'s `onMessage` switches on typed `AgentEvent`s
(`text_delta`, `tool_started`/`tool_finished`/`tool_progress`, `plan_updated`,
`tool_confirm_request`, `turn_completed`) to build live transcript text, a
`ToolCallRow[]` list, and a `WorkingPlan`, all fed into `ExecutionPanel.tsx` — the live
per-tool-call timeline the README calls the "Execution panel".

## Project-specific rules

These apply on top of normal engineering judgment, specifically for this repository:

1. **Design for scale, not just correctness.** Any UI or report design (dashboards,
   tables, charts, admin views) must account for long-term use and a large number of
   concurrent users — response-time and rendering efficiency are part of "done", not an
   afterthought. Don't ship a layout/query that merely displays correctly at small scale;
   check pagination, avoid unbounded fetches, avoid layouts that degrade as data grows
   (see `web/src/Admin.tsx`'s Overview tabs for the pattern: bounded queries, grouped by
   data type, no per-tab refetch).
2. **After completing a significant task, always tell the user to run `/code-review`**
   before considering the work finished — don't just report success and stop.
3. **Always warn the user before touching anything that changes agent-loop behavior
   itself** — `claw/core/loop.py`, `claw/core/runtime.py`, the `EventBus`, permission-mode
   gating, or any change to how the agent orchestrates tool calls/streaming. Flag it and
   get explicit go-ahead before editing, even if the requested change seems small.
4. **Proactively call out long-term risk in whatever task is at hand**, and propose a
   concrete mitigation — don't silently ship something that will predictably need
   revisiting (e.g. an unbounded list, a migration without a rollback path, a cache with
   no invalidation).
5. **Treat every change as security-relevant until ruled out.** Guardrails
   (`claw/security/policy.py`), auth (`claw/auth/`), the sandbox boundary
   (`claw/sandbox/ephemeral.py`), secret handling, and multi-tenant data isolation are
   easy to weaken by accident — check for that explicitly and suggest hardening where
   relevant, even when not asked.
6. **"It runs" is not the bar.** Every change should be considered against performance,
   security, and maintainability impact — not just whether the immediate task appears to
   work.
7. **New features must account for install/update, not just runtime code.**
   `install.sh` and `scripts/claw` are the source of truth for what a fresh install or
   `claw update` provisions: `uv sync` (Python deps), the web build, the
   `docker/sandbox.Dockerfile` sandbox image, the Postgres container, `.env` generation
   (never clobbered once it exists), Alembic migrations, and the systemd/launchd service
   unit. If a feature adds a new dependency, a new required env var, a new migration, a
   new background service, or changes what the sandbox image needs, check whether
   `install.sh`/`scripts/claw`/`.env.example`/`docker/sandbox.Dockerfile` need a matching
   update — don't leave the installer silently out of sync with the code.
8. **Verify with the Claude Browser tool when reachable.** For any change with a runtime
   surface (UI, API behavior, a running service), use the Claude Browser preview tooling
   to actually exercise it end-to-end before calling the work done — don't rely on
   typecheck/build/tests alone to claim a feature works, per the existing verification
   workflow. If the browser can't reach the environment, say so explicitly instead of
   assuming success.
