# Agent Skill bundles

Settings → Skills → Import skill accepts ZIPs and public GitHub repository URLs pinned to a full commit SHA. The archive must contain one `SKILL.md` with YAML `name` and `description`. Nested repository paths are detected automatically. Relative Markdown references must resolve inside the bundle.

Imports create a private enabled skill, an immutable resource snapshot, and source/version/SHA-256 metadata. Existing skill names are rejected instead of overwritten. Each user may keep up to 20 imported bundles with up to 100 MB of expanded resources in total. Imported instructions are read-only; owners can change enablement and Private/Group/Public sharing. Recipients must opt in before runtime resource reads. Existing text skills continue to work.

`read_skill(name, path=...)` loads resources on demand, with existing section/paging limits. `materialize=true` copies templates, images or fonts into the caller's workspace. Scripts are stored as reference text, cannot be materialized by this tool, and no package installation hooks are executed. Package files do not grant additional tool permissions.

Limits: 12 MB ZIP, 24 MB expanded, 1,500 archive entries, 2 MB per file. Absolute/parent paths, symlinks, duplicate paths and unsupported types are rejected. Binary resources stay out of model context.

HTML and SVG previews use an opaque sandboxed iframe and a restrictive CSP. Scripts and external fonts/assets remain disabled. Prefer inline SVG/CSS and local system fonts. The preview offers expansion and PNG export when SVG is present. `render_diagram` validates visible SVG dimensions and reports viewport overflow, then exports a bounded PNG (maximum 2400×2400); it does not prove semantic or geometric correctness of every connector. Publish both source and PNG when requested.

## Runtime setup

Run `uv sync`, `uv run python -m playwright install chromium` (on Linux use `--with-deps`), build `web`, then restart the app. Startup migrations add the bundle columns and version table. The Dockerfile installs Chromium and Thai fonts automatically.

The renderer runs Chromium in a disposable child process with CPU, file-size, descriptor, address-space (Linux), and wall-clock limits. It disables page JavaScript, service workers and outbound requests, uses two concurrent browser slots, permits six new renders per user per minute, and caches identical renders. Each workspace keeps at most 20 cached PNGs for 24 hours. If Chromium is unavailable the source preview remains usable and PNG export returns an explicit error.

Brand profiles, diagram source import commands and interactive animation are outside this release. There is no automatic package update/rollback UI; every imported snapshot is fixed.
