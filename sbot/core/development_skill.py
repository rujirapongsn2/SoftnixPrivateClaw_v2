"""Shared operating instructions for software development bots."""
DEVELOPMENT_SKILL = """# Software development
Use project for development. The ordinary exec tool is ephemeral.

Choose one project slug and include it in every delegation and mission node.
One slug is one application. For a new application, always choose a new slug;
create it first with project action=create, which rejects an existing slug. Never
reuse an existing project to evade a user's container limit. If the new
slug is rejected at capacity, report the limit and ask the user to remove an
unused project or have an administrator raise the limit.
Grant developers project, read_file, write_file, edit_file, list_dir and the exact
GitHub connector tools they need. Create a new project or start an existing one;
its shell /workspace maps to
file-tool projects/<slug>/. If disabled, report the configuration error.

Use a persisted mission for long work, with small implementation, test and
integration nodes. Record architecture, acceptance criteria and handoffs in
repository files and the shared blackboard. For parallel edits, create git
worktrees INSIDE /workspace (trees/api, trees/web), with distinct branches; tell
each worker its directory. Never concurrently edit one checkout. In the project container use
`sbot-worktree create <assignment-slug>` and give the returned directory to that
worker. Commit completed changes in that worktree. Put integration after all
implementation dependencies; run `sbot-worktree integrate <assignment-slug> --
<test-command> <arguments>` for each result. It tests the combined candidate before
fast-forwarding the main checkout; conflicts/test failures retain a candidate for
inspection and do not promote it. On an older image without sbot-worktree, report
that the developer image needs rebuilding; do not silently edit the shared checkout.

Write compose.yaml with healthchecks, restart: unless-stopped and named database
volumes. Use project compose_up, compose_ps and compose_logs. Bind service ports
to 0.0.0.0 inside the environment. The project start/status result includes
`public_ingress_port` and `public_bind`; every publicly reachable app must listen
on that exact address and port (do not assume framework defaults such as 3000).
The result also returns outer localhost port mappings for operator diagnostics.
Use project exec for builds, tests, migrations and curl. stop preserves
the container. To rebuild an environment with current host settings, use project
delete and then create with the same slug; delete preserves workspace files and
named volumes. compose_down preserves named volumes. Do not remove workspace data
to fix an error.

After the application passes its checks, call project status and use its
`app_url` as the primary delivery. End with a concise result and a clickable
Markdown link that opens the running application. Project source files under
`projects/<slug>/` are working files and must not be enumerated, attached or
published individually. Only call publish_artifact for a project file when the
user explicitly asks for a downloadable/exported artifact (for example a ZIP,
report or dataset).

Commit and verify the working tree. GitHub publish_files sends up to 100 text
files / 1 MB atomically to a feature branch with the connector credential.
Provide expected_sha for existing branches; resolve conflicts instead of force
pushing. Larger/binary repositories need operator-provisioned scoped Git
credentials in the environment. Never put tokens in chat, commands or source.
Open a draft PR with test evidence. Dispatch an existing Actions workflow only
when deployment is in scope; inspect its result and the deployed health endpoint.
A queued workflow is not a successful deployment.

Deliver repository/branch/SHA, test results and actual service status. A prose
implementation plan is not working software. State unfinished work explicitly.
"""
