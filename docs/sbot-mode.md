# Sbot mode integration

PrivateClaw is the host application and identity/configuration authority. Sbot is a module in the same Python process; start only `claw.main:create_app`.

## Boundaries

| Shared | Isolated by mode |
| --- | --- |
| Login, users, organization groups, provider keys, models, connectors, guardrails, skills, knowledge, branding, usage quotas | Sessions, messages, core memory, history search, schedules, heartbeat cadence, event buses, runtime caches, file workspaces, shares |

Sbot adds bots, bot groups, missions, blackboards, project containers and immutable Blueprint versions. Shared Control Plane includes container policies. Settings is one implementation; Memory, Schedule and Heartbeat use the active mode. Telegram and its account settings continue to target PrivateClaw. Public Blueprints mean access by authenticated users according to the existing Blueprint visibility rules, not anonymous file URLs.

Routes: `/chat/privateclaw[/session]`, `/chat/sbot[/session]`. Sbot HTTP/WebSocket routes live under `/modes/sbot/api/*` and `/modes/sbot/ws/chat/*`. Shared settings/auth/admin retain `/api/*`. Switching mode navigates away from a transcript without cancelling its server-side work. Last session is retained per mode in the browser tab and cleared on logout.

Legacy tables retain their names and data. All Sbot domain tables use `sbot_` prefixes and the same shared `users` foreign key. One Alembic chain extends PrivateClaw revision `c6d7e8f9a0b1` with `f0a1b2c3d4e5`; never run Sbot's original migration chain against this database. The new revision contains frozen schema definitions. Production startup aborts on migration failure.

## Install/update

1. Back up the PrivateClaw database and data roots. Rehearse on a database copy first.
2. Run `uv sync`, `npm ci --prefix web`, and `npm run build --prefix web`.
3. Run `uv run alembic upgrade head` with the host's `CLAW_DATABASE_URL`.
4. Start one API/scheduler process using `claw.main:create_app`.

`CLAW_SBOT_ENABLED=true` enables the mode. `CLAW_SBOT_WORKSPACES_ROOT` defaults to a **sibling** of `CLAW_WORKSPACES_ROOT` (for example `workspaces-sbot`), never a nested directory. Overlapping roots are rejected. Existing PrivateClaw file URLs therefore keep their original paths without gaining access to Sbot files. `CLAW_BLUEPRINTS_ROOT` defaults to `blueprints`.

Docker Compose production configuration mounts both workspace roots at identical host/container paths and persists Blueprint storage. `CLAW_SANDBOX__PROJECTS_ENABLED=false` is the initial fallback. An admin can enable the feature from Control Plane → Project containers; that setting is persisted in the database and takes effect without an app restart. When the fixed developer image is missing, the app starts a bounded background build from its packaged `docker/developer.Dockerfile` and reports Docker, image and overall readiness in the same panel. The operator can still build `sbot-developer:latest` manually before enabling projects. Nested Compose is separately opt-in through `CLAW_SANDBOX__PROJECT_DOCKER_ENABLED`; use only trusted workloads on an isolated development host. Project policies must also allow the user/group.

Both modes consume one provider protocol and the same connector manager/policy instance. Per-user rate limiting and usage quotas remain shared; changing mode does not give a new rate-limit allowance. Control Plane session/message activity aggregates both stores. Original runtime code is retained for PrivateClaw.

## Rollout and rollback

First enable for a non-production instance. Check both chats, group delegation, a durable mission, restart recovery, Preview/download, shared credential changes, and container lifecycle. Compare counts/checksums of legacy data before/after migration. Disable the mode with `CLAW_SBOT_ENABLED=false` for an application-level rollback that preserves Sbot data. The migration downgrade drops Sbot tables: back up/export that data first; ordinary rollback should use the feature flag instead.

Existing standalone Sbot data is not imported automatically. A later import must map users explicitly, remap session/bot/mission references, copy files to the Sbot root and re-encrypt credentials only if intentionally importing shared settings. Existing PrivateClaw users/configurations remain authoritative.

## Validation and remaining gates

Automated checks cover the production app composition, shared service identity, both runtime turns, cross-mode API/file denial, memory isolation, independent heartbeat values, combined reports, account deletion and additive migration/downgrade preserving legacy records. The imported Sbot suites cover bot/group/mission/Blueprint/artifact behavior. Legacy standalone Sbot migration tests are replaced by the host-lineage test.

The initial integration was exercised using an isolated SQLite database and fake providers. The legacy PrivateClaw migration chain requires PostgreSQL (`pg_trgm`), so the SQLite roundtrip tests the new revision on the legacy table structure rather than claiming a full PostgreSQL upgrade. PostgreSQL rehearsal, Docker lifecycle/host failure and browser UI acceptance remain required before production. Browser automation was unavailable in this session, and the Docker daemon was not running.

The original PrivateClaw Connector Presets suite has three existing failures (`env_fields` and the obsolete server command expectation), reproduced on the unchanged base. Do not interpret them as new mode failures. Long autonomous work still requires task-specific acceptance criteria; integration does not make model output inherently correct. Multi-process mission accounting and hostile tenant isolation remain outside this release's verified guarantees.

Validation snapshot: combined backend suite passed **1,002 tests, 1 skipped** (excluding the separately reproduced legacy Connector Presets failures). Frontend TypeScript/Vite production build passed, with the existing large-bundle warning. Real HTTP checks against an isolated preview returned 200 for both chat pages, login, shared admin overview/users, both memory APIs, bots, Blueprints and project inventory. No real provider calls or production database changes were made.
