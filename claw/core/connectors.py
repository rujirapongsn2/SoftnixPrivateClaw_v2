"""Per-user MCP connector manager, plus a shared pool for admin-global ones.

Connects to the user's enabled MCP servers (stdio or streamable HTTP) and
registers their tools as `mcp_{connector}_{tool}` in the agent's registry.
Connections are cached per user and rebuilt only when the config changes.

Admin-global connectors (Control Plane "Pre-built Connectors", owner_id NULL —
see ConnectorStore.enabled_accessible) are different: unlike a per-user
connector, which is cheap to hold open only while that one user is active,
a global connector is shared by every user, so this manager keeps exactly ONE
live session per global connector for the whole process (see
_GlobalConnections/sync_global) instead of one per user. Each user's registry
still gets that tool registered (via sync_tools), it's just backed by the
shared session rather than a per-user connection.
"""

import asyncio
import json
import re
import shlex
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from loguru import logger
from mcp.shared.exceptions import McpError

from claw.db.stores import ConnectorStore
from claw.security.ssrf import resolve_public_ips
from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry

# Env keys with this prefix are turned into HTTP headers for remote (http)
# connectors instead of being passed as process env — this is how remote MCP
# endpoints (Composio, Softnix ONE, …) carry their bearer/api-key auth.
_HEADER_ENV_PREFIX = "HEADER_"

# Same idea for endpoints that expect auth as a URL query parameter instead of
# a header (e.g. Alpha Vantage's `?apikey=`) — kept out of the stored `url`
# itself so the secret stays in the encrypted `env` column, not the plaintext
# url column, and is appended to the request URL only at connect time.
_QUERY_ENV_PREFIX = "QUERY_"

# Defaults, overridable per-instance via ConnectorSettings (claw/config.py) —
# module constants only so direct construction (tests, scratch scripts)
# doesn't need a full Settings object.
#
# Per-connector connect+list_tools budget. sync_tools() holds a per-user lock
# for its whole duration, so without a timeout a single misbehaving MCP server
# (e.g. one that hangs mid-handshake instead of raising) can block that lock
# forever — every subsequent /connectors listing or chat turn for that user
# then hangs too, indefinitely, until the process is restarted. A caught
# exception (bad auth, connection refused, ...) already surfaces in seconds
# via the try/except below; this timeout only bounds the hang case.
_CONNECT_TIMEOUT_SECONDS = 20

# Per-tool-call budget, same rationale as _CONNECT_TIMEOUT_SECONDS but for an
# already-connected session: a remote MCP server that hangs instead of
# responding (e.g. one whose malformed error response the client library
# can't parse and then never returns from) would otherwise leave the whole
# chat turn spinning forever with no way to recover short of restarting the
# process. ToolRegistry.execute() already wraps every tool call in a generic
# try/except and turns any exception into a normal "Error executing ..."
# result the model sees, so a timeout here surfaces exactly like any other
# tool failure — no special-casing needed upstream. Uses the mcp SDK's own
# per-request timeout (ClientSession.call_tool's read_timeout_seconds) rather
# than an external asyncio.wait_for: it's scoped with anyio.fail_after inside
# the same request/task, so cleanup of the SDK's own response-stream
# bookkeeping is guaranteed via its `finally` regardless of the timeout,
# instead of abandoning a separately-spawned wait_for task. Note this still
# only stops the CLIENT from waiting — it does not send the MCP protocol's
# notifications/cancelled to the remote server, so a slow-but-eventually-
# completing call can still finish (and act on) its side effect server-side
# after the client has already reported the call as failed.
_TOOL_CALL_TIMEOUT_SECONDS = 60

# After a connector fails, hold off retrying the whole set for this long — see
# ConnectorSettings.error_retry_cooldown_seconds and sync_tools() for why. A
# module constant so direct construction (tests) has a sane default.
_ERROR_RETRY_COOLDOWN_SECONDS = 60

# Bounds for a connector's own `timeout_ms` override (Settings > Connectors'
# "Timeout (ms)" field) — mirrors the range enforced by ConnectorBody in
# claw/api/manage.py, re-checked here in case anything else ever writes
# timeout_ms directly (defense in depth, not the primary validation).
_MIN_TIMEOUT_MS = 1000
_MAX_TIMEOUT_MS = 120_000


# Which config values count as secrets for redaction. This decides what gets
# replaced by "***" in tool results and error messages, so it is wrong in both
# directions: too broad and real response data is corrupted before the model
# reads it; too narrow and a credential lands in the transcript, which is
# persisted and replayed to the LLM on every later turn.
#
# An env entry is classified by its PREFIX, because the prefix already says
# what the value is used for, and a name-guessing heuristic alone is not safe
# enough for the two auth-carrying prefixes (nothing tells "HEADER_X-Auth-Email"
# or "QUERY_pass" from a credential except that they *are* one):
#
#   HEADER_* -> a request header. Secret unless it's a known-boring one, so a
#               new/odd auth header name is redacted by default.
#   QUERY_*  -> a URL query parameter. Always masked inside a URL (?name=***),
#               which is where the leak actually happens; global replacement of
#               the bare value too, unless the param name is in
#               _QUERY_ALLOWLIST (a known-boring one like "page" or "status")
#               and it doesn't also look credential-shaped — same
#               redact-unless-known-boring polarity as HEADER_*, since an odd
#               auth param name ("code", "sid", a client's custom token param)
#               is exactly the case a fixed word list can't anticipate.
#   anything else -> process env for a stdio MCP server (PGPASSWORD,
#               DATABASE_URL, GITHUB_TOKEN). Never response data, so always
#               redacted.
_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "accept-charset",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
        "origin",
        "pragma",
        "referer",
        "user-agent",
        "x-requested-with",
    }
)

# Mirrors _HEADER_ALLOWLIST's polarity: a QUERY_* param is redacted-by-default
# unless it's a known-boring one, so an odd/new param name (e.g. "code", "sid"
# — neither a _SECRET_HEAD_WORDS hit nor token-shaped) still gets scrubbed
# instead of silently passing through. The cURL importer (web/src/Settings.tsx's
# CurlImportPanel) turns EVERY captured query parameter into a QUERY_* env var,
# auth or not, so this list exists to keep genuinely common, non-secret REST
# params (pagination, formatting, filtering) from being shredded out of
# ordinary response bodies by the global replace below.
_QUERY_ALLOWLIST = frozenset(
    {
        "page", "per_page", "page_size", "limit", "offset",
        "sort", "order", "order_by",
        "format", "fields", "include", "expand",
        "filter", "q", "query", "search",
        "lang", "language", "locale", "timezone", "tz",
        "view", "type", "status",
        "since", "until", "start_date", "end_date", "date_from", "date_to",
    }
)

# Key matching is head-noun based ("access_token", "X-Api-Key", "client_secret"
# all end in the credential word) rather than substring, because substring
# matching catches "author" (-> auth) and "token_count" and would blank an
# author's name or a token count out of every response.
_STRONG_SECRET_WORDS = frozenset(
    {
        "secret", "password", "passwd", "passphrase", "pwd", "pw", "pass",
        "credential", "credentials", "apikey", "bearer", "otp", "pin", "salt",
    }
)
_SECRET_HEAD_WORDS = _STRONG_SECRET_WORDS | {
    "key", "keys", "token", "tokens", "auth", "authorization", "cookie", "cookies",
    "session", "signature", "sig", "pat", "private", "access",
}
# Punctuation, plus camelCase boundaries so "apiKey" splits like "api_key".
# The character class must stay case-insensitive: with [^a-z0-9] an
# all-caps key like "HEADER_X-Api-Key" shreds into ("pi", "ey") and matches
# nothing.
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
# Long and opaque: "sk-live-abc123...", "ghp_xxxx", a base64/JWT fragment.
# Charset and length alone are not enough — "-" and "_" are in the class, so
# an ordinary slug ("in_progress_review", "acme-website-redesign") clears both
# and would be blanked out of every response that contains it. Requiring a
# digit AND a letter AND (an uppercase letter OR an 8-char unbroken
# alphanumeric run) keeps human-written slugs out while still matching real
# token formats, which are either mixed-case or long unbroken random strings.
_TOKEN_CHARSET_RE = re.compile(r"^[A-Za-z0-9_\-.=+]{16,}$")
_LONG_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]{8,}")

# Below this a value is too common to replace safely: blanking "10" or "ok"
# everywhere would shred surrounding text far worse than leaking it.
_MIN_REDACTABLE_LEN = 4
_MIN_REDACTABLE_NUMBER_LEN = 6


def _key_names_a_secret(key: str) -> bool:
    words = [w for w in _WORD_SPLIT_RE.split(key.strip()) if w]
    if not words:
        return False
    lowered = [w.lower() for w in words]
    if lowered[-1] in _SECRET_HEAD_WORDS:
        return True
    return any(word in _STRONG_SECRET_WORDS for word in lowered)


def _value_looks_like_a_token(value: str) -> bool:
    if not _TOKEN_CHARSET_RE.match(value):
        return False
    if not any(char.isdigit() for char in value):
        return False
    if not any(char.isalpha() for char in value):
        return False
    return any(char.isupper() for char in value) or bool(_LONG_ALNUM_RUN_RE.search(value))


def _is_secret_value(key: str, value: Any) -> bool:
    """Whether a *body template literal* is a credential — key or shape."""
    if not isinstance(value, str):
        # A numeric credential still has to be scrubbed; it just can't be
        # judged by shape, so the key alone decides.
        return _key_names_a_secret(key)
    if len(value) < _MIN_REDACTABLE_LEN:
        return False
    return _key_names_a_secret(key) or _value_looks_like_a_token(value)


# Placeholders are filled from the caller's arguments at call time, so a
# template holds no secret of its own there. Swapped for a sentinel so the
# template parses as JSON (see _body_secrets). The sentinel must be printable:
# json.loads is strict by default and rejects raw control characters inside a
# string, which would make every templated body unparseable and silently skip
# redaction entirely.
_BODY_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_PLACEHOLDER_SENTINEL = "claw~placeholder~2f8c1d"


def _collect_secret_literals(node: Any, key: str, out: list[str]) -> None:
    if isinstance(node, dict):
        for child_key, child in node.items():
            _collect_secret_literals(child, str(child_key), out)
    elif isinstance(node, list):
        for child in node:
            _collect_secret_literals(child, key, out)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, str):
        if node != _PLACEHOLDER_SENTINEL and _is_secret_value(key, node):
            out.append(node)
    elif isinstance(node, (int, float)) and _key_names_a_secret(key):
        # json.dumps, not str(): that is how the number is spelled in the
        # rendered body, so it is the form an echoing endpoint sends back.
        rendered = json.dumps(node)
        # Same floor the string branch has. Without it a body template of
        # {"private": 1} makes "1" a redaction literal, and every digit 1 in
        # every response — including the "[200] " status prefix — turns into
        # "***", corrupting the JSON the model has to read. A number that
        # short cannot be a credential anyway. The floor is higher than the
        # string one because a bare number carries no distinguishing shape.
        if len(rendered) >= _MIN_REDACTABLE_NUMBER_LEN:
            out.append(rendered)


def _body_secrets(connector) -> list[str]:
    """Literal credentials stored in a kind="api" operation's body template.

    A body pasted through the cURL importer (web/src/Settings.tsx's
    CurlImportPanel) carries whatever literal credentials the example request
    used — there is no BODY_* env equivalent to move them to. ConnectorStore
    encrypts the body at rest, but by the time a connector reaches here it is
    decrypted, so these values still need keeping out of transcripts and logs:
    an endpoint that echoes the request back would otherwise replay them
    verbatim.
    """
    values: list[str] = []
    for operation in getattr(connector, "operations", None) or []:
        # A stored row is JSON and can be hand-edited or imported, so an entry
        # that isn't a dict must not raise — _redact_secrets runs inside except
        # blocks whose whole job is to scrub the message being handled.
        if not isinstance(operation, dict):
            continue
        body = operation.get("body")
        if not isinstance(body, str) or not body:
            continue
        try:
            parsed = json.loads(_BODY_PLACEHOLDER_RE.sub(f'"{_PLACEHOLDER_SENTINEL}"', body))
        except ValueError:
            continue
        _collect_secret_literals(parsed, "", values)
    return values


def _redact_secrets(text: str, connector) -> str:
    """Strip a connector's secrets out of a freeform string before it's logged,
    returned via the API, or handed to the model. For an http connector using
    QUERY_* auth, the secret is embedded in the request URL itself, so an
    underlying httpx/mcp exception's message (e.g. on a bad/rate-limited key)
    includes the raw URL, secret and all — this scrubs any such value (header,
    query, or api-connector body literal) wherever it appears, not just the URL.

    See the _HEADER_ALLOWLIST comment for how each env prefix is classified."""
    for raw_key, value in (connector.env or {}).items():
        key = str(raw_key)
        if not value:
            continue
        if key.startswith(_QUERY_ENV_PREFIX):
            param = key[len(_QUERY_ENV_PREFIX) :]
            if param:
                # Mask by position, not by value: this catches the leak even
                # when the value is short or punctuated ("?pass=letmein"),
                # where a value-shape test would not fire. Anchoring on the
                # parameter name means there is no collateral damage, so this
                # runs before the length floor below (which exists only to keep
                # the global replace from shredding text around a short value).
                text = re.sub(rf"([?&]{re.escape(param)}=)[^&\s\"'<>]+", r"\1***", text)
            if param.lower() in _QUERY_ALLOWLIST and not (
                _key_names_a_secret(param) or _value_looks_like_a_token(value)
            ):
                # A known-boring, non-credential-shaped query value ("status=active")
                # also occurs as ordinary response data, so it is masked only
                # in the URL above, never replaced globally. Anything not on
                # the allowlist falls through to the global replace below,
                # since an unrecognized param name is exactly the case where a
                # heuristic can't rule out "credential".
                continue
        elif key.startswith(_HEADER_ENV_PREFIX):
            name = key[len(_HEADER_ENV_PREFIX) :]
            if name.lower() in _HEADER_ALLOWLIST and not _value_looks_like_a_token(value):
                # Same value-shape escape hatch the QUERY_* branch above uses:
                # the allowlist says "this header name is normally boring", but
                # APIs do smuggle credentials through boring names (a signed
                # session in a Cookie, a JWT in User-Agent for a fingerprinting
                # gateway). Name alone is not enough to conclude the value is
                # safe to echo, and a token-shaped value has no benign reading.
                continue
        if len(value) < _MIN_REDACTABLE_LEN:
            continue
        text = text.replace(value, "***")
    for value in _body_secrets(connector):
        text = text.replace(value, "***")
    return text


def _register_scoped(registry: ToolRegistry, state, tool: Tool, connector_name: str) -> bool:
    """Register `tool` unless its name is already taken in this registry.

    ToolRegistry.register is a bare dict assignment, and a user's connectors are
    registered after the admin-global ones, so without this a user could name a
    connector/operation so that `api_hr_get_salary` (or `mcp_...`) collides with
    an admin-global tool and silently take over every call the model makes to
    that name — including calls issued by a shared skill written against the
    global connector. First registration wins: globals, then the user's own.
    """
    if registry.has(tool.name):
        logger.warning(
            "Connector {} tool {} not registered: name already taken", connector_name, tool.name
        )
        return False
    registry.register(tool)
    state.tool_names.append(tool.name)
    return True


class McpToolProxy(Tool):
    def __init__(
        self,
        session: Any,
        connector: str,
        tool_name: str,
        description: str,
        schema: dict,
        *,
        tool_call_timeout_seconds: float = _TOOL_CALL_TIMEOUT_SECONDS,
        session_ref: Callable[[], Any] | None = None,
    ):
        # `session_ref`, when given, is looked up fresh on every call instead
        # of using the captured `session` — used only for global-connector
        # proxies (see sync_global). A global connector's session can be torn
        # down and replaced (admin edit, error-retry reconnect) while a proxy
        # built from the OLD session is still registered in some other user's
        # registry (it's only re-registered on that user's next sync_tools);
        # without this indirection, that stale proxy would call_tool() against
        # a permanently-closed session instead of transparently picking up the
        # new one. `session_ref` closes over the shared, in-place-mutated
        # _GlobalConnections state, so it always reflects the current session.
        self._session = session
        self._session_ref = session_ref
        self._remote_name = tool_name
        self._tool_call_timeout_seconds = tool_call_timeout_seconds
        self.name = f"mcp_{connector}_{tool_name}"
        self.description = f"[{connector}] {description or tool_name}"
        self.parameters = schema or {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        session = self._session_ref() if self._session_ref is not None else self._session
        if session is None:
            return f"Error: {self.name} is not currently connected (reconnecting) — try again shortly"
        try:
            result = await session.call_tool(
                self._remote_name, kwargs, read_timeout_seconds=timedelta(seconds=self._tool_call_timeout_seconds)
            )
        except McpError as exc:
            if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
                return (
                    f"Error: {self.name} timed out after {self._tool_call_timeout_seconds}s "
                    "waiting for a response"
                )
            raise
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        if getattr(result, "isError", False):
            return "Error: " + ("\n".join(parts) or "MCP tool call failed")
        return "\n".join(parts) or "(empty result)"


@dataclass
class _UserConnections:
    signature: tuple = ()
    stack: AsyncExitStack | None = None
    tool_names: list[str] = field(default_factory=list)
    statuses: dict[str, dict] = field(default_factory=dict)
    # time.monotonic() of the most recent sync that left at least one connector
    # in "error"; drives the retry cooldown in sync_tools. Monotonic (not
    # wall-clock) so an NTP/clock adjustment can't skew the cooldown window.
    # None once a sync ends fully healthy.
    errored_monotonic: float | None = None


@dataclass
class _GlobalConnections:
    """Process-wide (not per-user) pool for admin-global connectors.

    One live session per global connector, shared by every user — see the
    module docstring. `proxies` holds the already-constructed McpToolProxy
    instances (bound to the shared session); sync_tools registers the SAME
    proxy instance into each user's registry rather than building a new one,
    since a proxy is stateless besides its session reference.

    Reconnects are scoped to ONE connector at a time: `stacks`, `signatures`,
    and `errored_monotonic` are all keyed by connector name, each with its
    own AsyncExitStack, so editing, fixing, or removing connector A never
    tears down or reconnects connector B's already-live session — a single
    misconfigured or slow pre-built connector no longer causes a momentary
    disruption for every OTHER pre-built connector's users too (see
    sync_global). `signature`, by contrast, stays a single aggregate over
    every enabled global connector: sync_tools (per user) only needs a cheap
    "did anything about the global set change at all" signal to decide
    whether to re-derive its own tool registration — it doesn't need, and
    shouldn't have to know, which specific connector changed.
    """

    signature: tuple = ()
    stacks: dict[str, AsyncExitStack] = field(default_factory=dict)
    signatures: dict[str, tuple] = field(default_factory=dict)
    sessions: dict[str, Any] = field(default_factory=dict)
    # The connector row each name is currently serving. A kind="api" tool has
    # no session to go stale, so this is what its `connector_ref` reads to pick
    # up a rotated credential or a repointed base url — see GenericApiTool.
    rows: dict[str, Any] = field(default_factory=dict)
    proxies: dict[str, list["Tool"]] = field(default_factory=dict)
    statuses: dict[str, dict] = field(default_factory=dict)
    errored_monotonic: dict[str, float] = field(default_factory=dict)
    # True once sync_global has completed at least one full pass. Guards the
    # lock-busy bypass in sync_global: skipping the refresh when the lock is
    # contended is safe once the pool has been populated at least once (the
    # losing caller just proceeds on the still-valid, self-healing cached
    # snapshot), but NOT before that — otherwise two callers racing to
    # populate a still-empty pool for the very first time could both bail
    # out, serving zero global connectors for that call.
    synced_once: bool = False

    def tracked_names(self) -> set[str]:
        """Every connector name this pool holds ANY state for.

        Deliberately the union of all seven name-keyed dicts, not `stacks`
        alone: a kind="api" connector never opens a session, so it only ever
        appears in `signatures`/`proxies`/`statuses`/`errored_monotonic` (see
        sync_global). Driving sync_global's "no longer enabled" cleanup off
        `stacks` therefore skipped api connectors entirely — a disabled or
        deleted one kept its "connected" status forever, so sync_tools went on
        re-registering its credential-bearing tools into every user's registry
        until the process restarted. Any new name-keyed dict added to this
        dataclass must be unioned in here too, or it will resurrect that bug.
        """
        return (
            self.stacks.keys()
            | self.sessions.keys()
            | self.rows.keys()
            | self.proxies.keys()
            | self.statuses.keys()
            | self.signatures.keys()
            | self.errored_monotonic.keys()
        )


def _apply_global_shadowing(state: _GlobalConnections) -> None:
    """Split every connected global connector's built tools into the names it
    actually owns and the ones another global connector already claims.

    Two admin-global connectors can spell the same tool name — connector
    `github` with operation `issues_list` and connector `github_issues` with
    operation `list` both produce `api_github_issues_list`. Registration is
    first-wins per registry (_register_scoped), so exactly one of them serves
    the name; without this the loser still advertised it in `tool_names`, which
    is what resolve_tool_names hands to a linked skill and to the Connectors
    UI. A skill written against the loser was told to call a name that reaches
    the WINNER's base url with the winner's credentials — a silent
    cross-connector credential swap, not the unknown-tool error it looks like.

    Resolved pool-wide and in the same sorted order sync_tools registers in, so
    the reported status and every user's registry agree. Recomputed from
    `proxies` on each pass, never from the previous verdict, so it is
    idempotent and self-heals once the colliding connector is renamed or
    removed."""
    claimed: set[str] = set()
    for name in sorted(state.statuses):
        status = state.statuses[name]
        if status.get("status") != "connected":
            continue
        owned: list[str] = []
        shadowed: list[str] = []
        for tool in state.proxies.get(name, []):
            if tool.name in claimed:
                # Also catches two operations on the SAME connector colliding,
                # which _register_scoped rejects identically.
                shadowed.append(tool.name)
            else:
                claimed.add(tool.name)
                owned.append(tool.name)
        status["tools"] = len(owned)
        status["tool_names"] = owned
        if shadowed:
            status["shadowed_tools"] = shadowed
        else:
            status.pop("shadowed_tools", None)


class ConnectorManager:
    def __init__(
        self,
        store: ConnectorStore,
        connect_timeout_seconds: float = _CONNECT_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: float = _TOOL_CALL_TIMEOUT_SECONDS,
        error_retry_cooldown_seconds: float = _ERROR_RETRY_COOLDOWN_SECONDS,
    ):
        self.store = store
        self.connect_timeout_seconds = connect_timeout_seconds
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self.error_retry_cooldown_seconds = error_retry_cooldown_seconds
        self._users: dict[str, _UserConnections] = {}
        # Serialize sync_tools per user: it is now driven both by chat turns and
        # by the connectors listing endpoint (composer menu), which can overlap.
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = _GlobalConnections()
        self._global_lock = asyncio.Lock()

    def _lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _effective_timeout_seconds(self, connector, default_seconds: float) -> float:
        """A connector's own `timeout_ms` (if set) overrides the instance-wide
        connect/tool-call default, for both budgets — the UI exposes a single
        "Timeout (ms)" field per connector rather than separate connect vs.
        tool-call knobs."""
        raw = getattr(connector, "timeout_ms", None)
        if raw is None:
            return default_seconds
        return max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, raw)) / 1000

    async def status(self, user_id: str) -> dict[str, dict]:
        return dict(self._users.get(user_id, _UserConnections()).statuses)

    async def resolve_tool_names(
        self,
        user_id: str,
        connector_id: str,
        *,
        owned: list | None = None,
        global_connectors: list | None = None,
    ) -> list[str] | None:
        """The tool names a connector is CURRENTLY registered under
        (`mcp_{connector.name}_{tool}`), looked up by the connector's stable
        id rather than its (renameable) name — so a skill linked to this id
        stays correct across a rename or delete+recreate. Requires
        sync_tools() to have already run for this user in this process
        (i.e. call this after sync_tools, not before). None if the connector
        doesn't belong to this user (and isn't an admin-global one this user
        can reach), or isn't currently connected.

        `owned`/`global_connectors`, if given, are used instead of querying
        the store again — a caller resolving several skills in the same turn
        (see AgentRuntime) should fetch each list once and pass it to every
        call rather than repeating the same two queries per skill."""
        if owned is None:
            owned = await self.store.list_for_user(user_id)
        connector = next((c for c in owned if c.id == connector_id), None)
        if connector is not None:
            state = self._users.get(user_id)
            if state is None:
                return None
            status = state.statuses.get(connector.name)
            if status is None or status.get("status") != "connected":
                return None
            return status.get("tool_names")

        # Not one of this user's own — maybe an admin-global connector this
        # user can reach. A global connector never lands in _UserConnections
        # .statuses (only .tool_names — see sync_tools), so its live status
        # comes from the shared pool instead.
        if global_connectors is None:
            global_connectors = await self.store.list_for_global()
        global_connector = next((c for c in global_connectors if c.id == connector_id), None)
        if global_connector is None:
            return None
        # Shadowed out (mirrors sync_tools' own-vs-global tie-break) if this
        # user owns an ENABLED connector of the same name — must match
        # sync_tools' own_names (enabled_for_user) exactly. Comparing against
        # every owned connector regardless of `enabled` used to report a
        # global connector as shadowed (returning None) whenever the user
        # merely owned a *disabled* same-name connector, even though
        # sync_tools doesn't count a disabled connector when deciding
        # whether to register the global one — so the tool was actually live
        # in the registry while this method claimed it wasn't.
        enabled_own_names = {c.name for c in owned if c.enabled}
        if global_connector.name in enabled_own_names:
            return None
        status = self._global.statuses.get(global_connector.name)
        if status is None or status.get("status") != "connected":
            return None
        return status.get("tool_names")

    async def sync_tools(self, user_id: str, registry: ToolRegistry) -> None:
        """Ensure the registry reflects the user's enabled connectors. Cheap
        when unchanged. A connector that ended in "error" last time (including
        a connect timeout) is retried here even though its config didn't change
        — otherwise a transient failure would cache as permanently broken until
        an admin edits the connector. But that retry tears down and reconnects
        ALL of the user's connectors, waiting the full connect timeout for the
        broken one — up to a connector's own `timeout_ms` override if it has
        one (see `_effective_timeout_seconds`), which can be as long as
        `_MAX_TIMEOUT_MS` (120s), not just the shorter instance-wide default —
        and this method runs on every chat turn and every /connectors
        listing. So the retry is held off for
        `error_retry_cooldown_seconds` after the failure: within that window we
        short-circuit on the cached (error) state, exactly as for an unchanged
        all-healthy config, keeping turns and page loads fast. A config change
        (signature) or an explicit invalidate() still forces an immediate
        rebuild regardless of the cooldown.

        Also merges in the admin-global connectors this user can reach (see
        ConnectorStore.enabled_accessible): sync_global() first refreshes the
        shared pool, then any global connector not shadowed by one of this
        user's own (by name) gets its already-built tool proxies registered
        into this user's registry — bound to the shared session, never
        entered into this user's own stack, since a global connector's
        lifecycle belongs to the shared pool, not this user."""
        await self.sync_global()
        async with self._lock(user_id):
            connectors = await self.store.enabled_for_user(user_id)
            own_names = {c.name for c in connectors}
            global_state = self._global
            effective_global = tuple(
                sorted(
                    name
                    for name, status in global_state.statuses.items()
                    if status.get("status") == "connected" and name not in own_names
                )
            )
            signature = (
                tuple(sorted((c.id, c.updated_at.isoformat()) for c in connectors)),
                global_state.signature,
                effective_global,
            )
            state = self._users.get(user_id)
            had_error = state is not None and any(s.get("status") == "error" for s in state.statuses.values())
            within_error_cooldown = (
                had_error
                and state.errored_monotonic is not None
                and (time.monotonic() - state.errored_monotonic) < self.error_retry_cooldown_seconds
            )
            if state is not None and state.signature == signature and (not had_error or within_error_cooldown):
                return

            await self._close_user(user_id, registry)
            # _close_user can genuinely await real I/O (tearing down MCP
            # sessions), which yields to the event loop — a concurrent
            # sync_global() (another user's turn, or an admin editing one
            # global connector) could reconnect one or more connectors in
            # that window. self._global is mutated in place (never swapped to
            # a new object — see _GlobalConnections), so `global_state`
            # still refers to the live, current object either way; but its
            # .statuses/.proxies entries for whichever connector(s) just
            # changed need re-reading now, after the only await between here
            # and where they're used, so effective_global/signature reflect
            # the latest state rather than the snapshot taken before the await.
            global_state = self._global
            effective_global = tuple(
                sorted(
                    name
                    for name, status in global_state.statuses.items()
                    if status.get("status") == "connected" and name not in own_names
                )
            )
            signature = (signature[0], global_state.signature, effective_global)
            state = _UserConnections(signature=signature, stack=AsyncExitStack())
            self._users[user_id] = state

            for name in effective_global:
                # Only the names this global connector actually owns pool-wide
                # (see _apply_global_shadowing): registering a shadowed proxy
                # here would be a no-op in the common case, but if the owner is
                # excluded from effective_global by a same-name connector of
                # this user's, the runner-up would silently inherit the name
                # for this one user while every other user's registry — and
                # resolve_tool_names — still attribute it to the owner.
                owned = set(global_state.statuses.get(name, {}).get("tool_names") or ())
                for proxy in global_state.proxies.get(name, []):
                    if proxy.name in owned:
                        _register_scoped(registry, state, proxy, name)

            if not connectors:
                return

            # api-kind connectors need no session/handshake at all — build
            # their tools straight from the stored row, zero I/O, so a bad
            # base url or bad credential only surfaces when a tool is
            # actually called (see _build_api_tools).
            api_connectors = [c for c in connectors if c.kind == "api"]
            mcp_connectors = [c for c in connectors if c.kind != "api"]
            for connector in api_connectors:
                try:
                    tools = self._build_api_tools(connector)
                except Exception as exc:  # malformed stored `operations`
                    state.statuses[connector.name] = {"status": "error", "error": str(exc)}
                    logger.warning("API connector {} failed to build tools: {}", connector.name, exc)
                    continue
                registered = [t.name for t in tools if _register_scoped(registry, state, t, connector.name)]
                state.statuses[connector.name] = {
                    "status": "connected",
                    "tools": len(registered),
                    "tool_names": registered,
                }
                shadowed = [t.name for t in tools if t.name not in registered]
                if shadowed:
                    state.statuses[connector.name]["shadowed_tools"] = shadowed

            # No early-return for an empty `mcp_connectors` here: entering an
            # AsyncExitStack and asyncio.gather()-ing zero coroutines are both
            # no-ops, and falling through lets the errored_monotonic stamp
            # below still run for a user whose connectors are all kind="api"
            # (an early return here previously skipped it, so an all-api user
            # with one broken connector never got the error-retry cooldown —
            # see sync_global's equivalent stamp, which isn't gated this way).
            await state.stack.__aenter__()
            # Connect all of a user's connectors concurrently, not one at a
            # time — otherwise N broken/hanging connectors cost N times the
            # per-connector timeout instead of one timeout period total,
            # while this method holds the per-user lock throughout.
            results = await asyncio.gather(*(self._connect_one(state.stack, c) for c in mcp_connectors))
            for connector, session, listed, error in results:
                if error is not None:
                    state.statuses[connector.name] = error
                    logger.warning("MCP connector {} {}", connector.name, error["error"])
                    continue
                registered_names: list[str] = []
                shadowed_names: list[str] = []
                for tool in listed.tools:
                    proxy = McpToolProxy(
                        session,
                        connector.name,
                        tool.name,
                        tool.description or "",
                        tool.inputSchema or {},
                        tool_call_timeout_seconds=self._effective_timeout_seconds(
                            connector, self.tool_call_timeout_seconds
                        ),
                    )
                    if _register_scoped(registry, state, proxy, connector.name):
                        registered_names.append(proxy.name)
                    else:
                        shadowed_names.append(proxy.name)
                # `tool_names` here are the exact names a skill's instructions
                # must reference to call this connector's tools (the
                # `mcp_{connector}_{tool}` prefix, not the server's raw tool
                # name) — surfaced in the Connectors UI so skill authors don't
                # have to guess it.
                state.statuses[connector.name] = {
                    "status": "connected",
                    "tools": len(registered_names),
                    "tool_names": registered_names,
                }
                # A name collision drops the tool silently otherwise: the
                # connector reports "connected" and the model simply never
                # sees that tool, which looks like the MCP server being broken.
                # Same field the api branch above sets, for the same reason.
                if shadowed_names:
                    state.statuses[connector.name]["shadowed_tools"] = shadowed_names
                logger.info("MCP connector {} connected with {} tools", connector.name, len(registered_names))
            # Stamp the failure time so the next sync holds off retrying the
            # whole set until the cooldown elapses (see sync_tools docstring).
            if any(s.get("status") == "error" for s in state.statuses.values()):
                state.errored_monotonic = time.monotonic()

    def _build_api_tools(self, connector, *, connector_ref: Callable[[], Any] | None = None) -> list[Tool]:
        """Build a kind="api" connector's tools straight from its stored
        `operations` — pure in-memory construction, no I/O, no session. See
        claw/tools/api.py's GenericApiTool; imported lazily here to avoid a
        claw.core <-> claw.tools import cycle (connectors.py's _redact_secrets
        is in turn imported lazily by GenericApiTool.execute).

        `connector_ref` is passed only for admin-global connectors, whose tools
        outlive an edit in other users' registries — see GenericApiTool. A
        per-user connector needs no such indirection: the same sync_tools pass
        that could invalidate its row is the one that rebuilt these tools."""
        from claw.tools.api import GenericApiTool

        timeout = self._effective_timeout_seconds(connector, self.tool_call_timeout_seconds)
        return [
            GenericApiTool(connector, operation, timeout_seconds=timeout, connector_ref=connector_ref)
            for operation in connector.operations or []
        ]

    async def _connect_one(self, stack: AsyncExitStack, connector) -> tuple[Any, Any, Any, dict | None]:
        """Connect+list one connector under its own timeout. Never raises —
        returns (connector, session, listed, error_status), so callers can
        run several of these concurrently (via asyncio.gather) and still
        tell which ones failed."""
        timeout = self._effective_timeout_seconds(connector, self.connect_timeout_seconds)
        try:
            session, listed = await asyncio.wait_for(
                self._connect_and_list(stack, connector),
                timeout=timeout,
            )
            return connector, session, listed, None
        except TimeoutError:
            message = f"timed out after {timeout}s connecting/listing tools"
            return connector, None, None, {"status": "error", "error": message}
        except asyncio.CancelledError:
            # A broken connector's handshake fails deep inside the MCP SDK's
            # anyio internals (e.g. a DNS error becomes "Attempted to exit
            # cancel scope in a different task than it was entered in" because
            # the client's contexts are held across tasks in a shared stack),
            # which surfaces as a CancelledError on THIS gather-spawned child
            # task — with our own wait_for timeout as another source.
            # CancelledError is a BaseException, so the `except Exception`
            # below misses it; left unhandled it escapes gather() →
            # sync_tools() → warm_connectors()'s `except Exception` and
            # surfaces as a 500 on the /connectors listing (and would abort a
            # chat turn). uncancel() clears this child task's spurious
            # cancellation so it doesn't leak, and we report the connector as
            # errored. A genuine cancellation of the PARENT sync task is
            # unaffected: it is awaiting gather(), so its own CancelledError
            # still fires there regardless of what this child returns.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            message = f"failed connecting (within {timeout}s budget)"
            return connector, None, None, {"status": "error", "error": message}
        except Exception as exc:
            message = _redact_secrets(str(exc), connector)[:300]
            return connector, None, None, {"status": "error", "error": message}

    async def _connect_and_list(self, stack: AsyncExitStack, connector) -> tuple[Any, Any]:
        session = await self._connect(stack, connector)
        listed = await session.list_tools()
        return session, listed

    async def _connect(self, stack: AsyncExitStack, connector) -> Any:
        from mcp import ClientSession

        if connector.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            # Split env into HTTP headers (HEADER_*), URL query params
            # (QUERY_*), and everything else.
            headers = {
                key[len(_HEADER_ENV_PREFIX):]: value
                for key, value in (connector.env or {}).items()
                if key.startswith(_HEADER_ENV_PREFIX) and value
            }
            # An empty string is a deliberate "clear this param" override, not
            # "not set" — distinct from the key being absent entirely — so it
            # isn't filtered out here the way headers are; it's handled below.
            query_overrides = {
                key[len(_QUERY_ENV_PREFIX):]: value
                for key, value in (connector.env or {}).items()
                if key.startswith(_QUERY_ENV_PREFIX)
            }
            url = connector.url
            if query_overrides:
                parts = urlsplit(url)
                # Rebuild from the parsed pairs (not a dict) so a duplicate
                # query key already in the stored URL that no override
                # touches is preserved as-is instead of being collapsed to
                # its last value.
                existing = parse_qsl(parts.query, keep_blank_values=True)
                merged = [(k, v) for k, v in existing if k not in query_overrides]
                merged.extend((k, v) for k, v in query_overrides.items() if v)
                url = urlunsplit(parts._replace(query=urlencode(merged)))
            # Same SSRF threat model as a kind="api" connector (see
            # claw/security/ssrf.py): a per-user connector's url is chosen by
            # an ordinary user, so without this the agent can be pointed at
            # 169.254.169.254 or any other host inside the deployment's
            # network and this process will dial it. An admin-global connector
            # (owner_id NULL) is exempt: the operator configures those in the
            # Control Plane, pointing one at internal infrastructure is a
            # legitimate self-hosted setup, and an admin can already run
            # arbitrary stdio commands on this host anyway.
            #
            # Weaker than claw/tools/api.py's equivalent: the MCP client owns
            # the socket, so the validated address can't be pinned and a
            # rebinding attacker with a TTL-0 record can still win the race.
            # This closes the ordinary "just point it at an internal host"
            # case, which is the one a user reaches without any setup.
            if connector.owner_id is not None:
                await resolve_public_ips(url)
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers or None)
            )
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            argv = shlex.split(connector.command)
            if not argv:
                raise ValueError("empty command")
            # Built-in preset servers launch as `python -m claw.integrations.*`.
            # Use the running interpreter so the `claw` package is importable
            # regardless of what `python`/`python3` resolves to on PATH.
            if argv[0] in ("python", "python3"):
                argv[0] = sys.executable
            params = StdioServerParameters(
                command=argv[0], args=argv[1:], env={**(connector.env or {})} or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _close_user(self, user_id: str, registry: ToolRegistry) -> None:
        state = self._users.pop(user_id, None)
        if state is None:
            return
        for name in state.tool_names:
            registry.unregister(name)
        if state.stack is not None:
            try:
                await state.stack.aclose()
            except asyncio.CancelledError:
                # Same cross-task anyio hazard as _connect_one: tearing down a
                # (possibly half-entered) MCP client context that was entered
                # on a different task can surface as a CancelledError — a
                # BaseException the `except Exception` below would miss, so it
                # would escape _close_user → sync_tools → the /connectors
                # endpoint as a 500. Absorb this task's spurious cancellation
                # (uncancel) and move on; a genuine cancellation of the caller
                # still fires at its own next await. See _connect_one.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                logger.debug("MCP stack close for {} cancelled across tasks; ignored", user_id)
            except Exception:
                # MCP SDK cancel-scope cleanup can be noisy across tasks; harmless.
                logger.debug("MCP stack close for {} raised; ignored", user_id)

    async def invalidate(self, user_id: str) -> None:
        """Force reconnect on next sync (config changed)."""
        state = self._users.get(user_id)
        if state is not None:
            state.signature = ()

    async def status_global(self) -> dict[str, dict]:
        return dict(self._global.statuses)

    async def sync_global(self) -> None:
        """Ensure the shared pool reflects the admin-global connectors.

        Same signature-diff / error-retry-cooldown shape as sync_tools, but
        scoped to ConnectorStore.enabled_for_global() and keyed process-wide
        (self._global), not per user — see the module docstring for why a
        global connector gets exactly one live session for the whole process
        instead of one per user.

        Reconnects are scoped per connector (see _GlobalConnections):
        editing, fixing, or removing one global connector only tears down
        and rebuilds THAT connector's own session — every other global
        connector's already-live session, and every user currently reaching
        it, is left completely undisturbed. Only the aggregate `signature`
        is recomputed unconditionally on every call (cheap, no I/O); it
        exists solely so sync_tools can detect "something about the global
        set changed" without needing to know which connector.

        Called at the top of every user's sync_tools(), i.e. on every chat
        turn and /connectors listing — so if a rebuild (or a hung connect) is
        already in progress, every OTHER concurrent turn/listing would
        otherwise queue up waiting on the same lock instead of proceeding
        with the still-valid cached self._global it already has. Since this
        method only ever refreshes an already-cached, self-healing snapshot
        (sync_tools re-checks the signature next call regardless), it's safe
        to skip the refresh entirely rather than block when the lock is
        already held — the caller just proceeds with whatever self._global
        currently is, UNLESS the pool has never completed a first sync yet
        (self._global.synced_once), in which case it waits instead of
        risking every early caller bailing out on a still-completely-empty
        pool."""
        if self._global_lock.locked() and self._global.synced_once:
            return
        async with self._global_lock:
            connectors = await self.store.enabled_for_global()
            state = self._global
            current_names = {c.name for c in connectors}

            # A connector no longer enabled (disabled or deleted) loses its
            # session and all of its bookkeeping — closing only ITS stack,
            # never another's. Driven off every tracked name (see
            # _GlobalConnections.tracked_names), not just the ones holding a
            # stack, so a session-less kind="api" connector is torn down here
            # too. The set difference is materialized up front because
            # _close_one_global mutates the very dicts it is derived from.
            for stale_name in state.tracked_names() - current_names:
                await self._close_one_global(state, stale_name)

            to_reconnect = []
            for connector in connectors:
                connector_signature = (connector.id, connector.updated_at.isoformat())
                had_error = state.statuses.get(connector.name, {}).get("status") == "error"
                errored_at = state.errored_monotonic.get(connector.name)
                within_error_cooldown = (
                    had_error
                    and errored_at is not None
                    and (time.monotonic() - errored_at) < self.error_retry_cooldown_seconds
                )
                if state.signatures.get(connector.name) == connector_signature and (
                    not had_error or within_error_cooldown
                ):
                    continue  # unchanged and healthy (or still cooling down) — leave it running
                await self._close_one_global(state, connector.name)
                to_reconnect.append(connector)

            if to_reconnect:
                # api-kind connectors need no session/stack at all — build
                # their tools straight from the stored row (zero I/O) and
                # register them into `proxies` directly; `stacks`/`sessions`
                # simply never get an entry for these names (the .pop(name,
                # None) cleanup calls elsewhere already tolerate that).
                api_reconnect = [c for c in to_reconnect if c.kind == "api"]
                mcp_reconnect = [c for c in to_reconnect if c.kind != "api"]

                for connector in api_reconnect:
                    state.signatures[connector.name] = (connector.id, connector.updated_at.isoformat())
                    # Published before the tools are built so connector_ref
                    # resolves from the very first call.
                    state.rows[connector.name] = connector
                    try:
                        tools = self._build_api_tools(
                            connector,
                            connector_ref=(lambda name=connector.name: state.rows.get(name)),
                        )
                    except Exception as exc:  # malformed stored `operations`
                        # Isolated exactly like a failed MCP connect: this one
                        # connector goes to "error", every other connector in
                        # the shared global pool keeps working.
                        state.statuses[connector.name] = {"status": "error", "error": str(exc)}
                        state.errored_monotonic[connector.name] = time.monotonic()
                        logger.warning(
                            "API connector {} failed to build tools: {}", connector.name, exc
                        )
                        continue
                    state.proxies[connector.name] = tools
                    # tools/tool_names are provisional; _apply_global_shadowing
                    # below has the final say once the whole pool is rebuilt.
                    state.statuses[connector.name] = {
                        "status": "connected",
                        "tools": len(tools),
                        "tool_names": [t.name for t in tools],
                    }
                    state.errored_monotonic.pop(connector.name, None)
                    logger.info(
                        "API connector {} registered with {} operations", connector.name, len(tools)
                    )

                stacks = {c.name: AsyncExitStack() for c in mcp_reconnect}
                for c in mcp_reconnect:
                    state.stacks[c.name] = stacks[c.name]
                    state.signatures[c.name] = (c.id, c.updated_at.isoformat())
                    await stacks[c.name].__aenter__()
                # Connect the connectors that actually changed concurrently, not
                # one at a time — same rationale as sync_tools' own gather over a
                # user's connectors: N broken/hanging ones should cost one
                # timeout period total, not N of them, while still only ever
                # touching the sessions that actually need to change.
                results = await asyncio.gather(
                    *(self._connect_one(stacks[c.name], c) for c in mcp_reconnect)
                )
                for connector, session, listed, error in results:
                    if error is not None:
                        state.statuses[connector.name] = error
                        state.errored_monotonic[connector.name] = time.monotonic()
                        logger.warning("MCP global connector {} {}", connector.name, error["error"])
                        continue
                    state.errored_monotonic.pop(connector.name, None)
                    proxies = [
                        McpToolProxy(
                            session,
                            connector.name,
                            tool.name,
                            tool.description or "",
                            tool.inputSchema or {},
                            tool_call_timeout_seconds=self._effective_timeout_seconds(
                                connector, self.tool_call_timeout_seconds
                            ),
                            # See McpToolProxy.__init__: makes an already-
                            # registered (possibly stale) proxy in some other
                            # user's registry transparently follow this
                            # connector's session if it's later torn down and
                            # rebuilt, instead of erroring against a dead one.
                            session_ref=(lambda name=connector.name: state.sessions.get(name)),
                        )
                        for tool in listed.tools
                    ]
                    state.sessions[connector.name] = session
                    state.proxies[connector.name] = proxies
                    state.statuses[connector.name] = {
                        "status": "connected",
                        "tools": len(proxies),
                        "tool_names": [p.name for p in proxies],
                    }
                    logger.info(
                        "MCP global connector {} connected with {} tools", connector.name, len(proxies)
                    )

            # Unconditional, not gated on `to_reconnect`: removing a connector
            # (handled above, before the diff) frees tool names that a
            # still-running one may now own.
            _apply_global_shadowing(state)

            state.signature = tuple(sorted((c.id, c.updated_at.isoformat()) for c in connectors))
            state.synced_once = True

    async def _close_one_global(self, state: "_GlobalConnections", name: str) -> None:
        """Tear down exactly ONE global connector's own session/stack and
        clear its bookkeeping — never touches any other connector's stack,
        session, or status. Does NOT touch any user's registry/tool_names;
        those are unregistered when each user's own sync_tools next runs and
        notices the aggregate global signature changed (see sync_tools),
        same as how a per-user connector's removal is only reflected in the
        registry on that user's next sync."""
        stack = state.stacks.pop(name, None)
        state.sessions.pop(name, None)
        # Dropped BEFORE any await below, so an api tool still registered in
        # some user's registry starts refusing calls the moment the connector
        # is disabled or deleted, rather than at that user's next sync_tools.
        state.rows.pop(name, None)
        state.proxies.pop(name, None)
        state.statuses.pop(name, None)
        state.signatures.pop(name, None)
        state.errored_monotonic.pop(name, None)
        if stack is None:
            return
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            # See _connect_one/_close_user for why this cross-task anyio
            # hazard needs an explicit uncancel() instead of propagating.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            logger.debug("Global MCP stack close for {} cancelled across tasks; ignored", name)
        except Exception:
            logger.debug("Global MCP stack close for {} raised; ignored", name)

    async def close_global(self, name: str, connector_id: str | None = None) -> None:
        """Immediately tear down one global connector's live session, rather
        than waiting for sync_global's own "no longer enabled" cleanup to
        notice it's gone on some future call — that cleanup can be skipped
        entirely if the call loses the `_global_lock` race (see sync_global),
        which would leave a just-deleted connector's session/proxies (and any
        already-registered user proxies) live and callable for longer than
        expected. Used by delete_connector.

        Bounded lock wait: `_global_lock` is also held for the full duration
        of sync_global's own reconnect pass, which can take up to a
        connector's own (unrelated) connect timeout. Blocking the caller (an
        admin's DELETE request) for that long just to get an immediate close
        would be worse than the problem this method fixes, so on contention
        this gives up quickly and falls back to the same lazy, self-healing
        cleanup sync_global already does on its own next call.

        `connector_id`, when given, guards against a narrow race: if this
        call is delayed (lock contention) long enough that the connector
        `name` referred to has since been deleted AND a new, different
        connector recreated under the same name, closing unconditionally by
        name alone would tear down that unrelated newer connector's session
        instead of a no-op. Only close if the name's currently-tracked
        signature still belongs to the id this call was meant for."""
        try:
            await asyncio.wait_for(self._global_lock.acquire(), timeout=0.5)
        except TimeoutError:
            return
        try:
            if connector_id is not None:
                current = self._global.signatures.get(name)
                if current is not None and current[0] != connector_id:
                    return
            await self._close_one_global(self._global, name)
        finally:
            self._global_lock.release()

    async def invalidate_global(self, name: str | None = None) -> None:
        """Force a global connector to reconnect on its next sync_global,
        bypassing the error-retry cooldown too. Scoped to `name` alone by
        default use (an admin editing/deleting one connector) so it never
        disturbs any other connector's already-live session; pass no name
        only for an explicit "reconnect every global connector now"."""
        if name is None:
            self._global.signatures.clear()
            self._global.errored_monotonic.clear()
        else:
            self._global.signatures.pop(name, None)
            self._global.errored_monotonic.pop(name, None)
