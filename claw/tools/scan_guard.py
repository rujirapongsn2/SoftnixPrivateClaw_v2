"""Refuse shell commands that walk the whole filesystem.

`find /` or `grep -r ... /` inside the sandbox walks the entire image: system
files only (the user's files are under /workspace, connector data is not on
disk at all), and it runs into the command timeout, so one call burns two
minutes of the turn's budget and finds nothing. Models reach for it when they
are lost, often several times in a row. Refusing up front, with a pointer to
where things actually are, turns those minutes into an immediate redirect.

This is guidance, not a security boundary: the sandbox is what confines a
command. A determined command can spell the path indirectly (`find $(echo /)`)
and that is fine — the point is to catch the habitual form.
"""

import shlex
from pathlib import PurePosixPath

_ROOTS = {"/", "/*", "/.", "//", "/./"}
# Wrappers that run the command that follows them.
_WRAPPERS = {"sudo", "env", "time", "nice", "nohup", "command", "exec", "builtin", "stdbuf", "ionice"}
_SEARCHERS = {"du", "tree", "fd", "fdfind"}
_GREPS = {"grep", "egrep", "fgrep", "zgrep"}
_SEPARATORS = ("&&", "||", ";", "|", "\n", "$(", "`", "(", ")", "{", "}")

BLOCKED_MESSAGE = (
    "Error: whole-filesystem scan blocked. `{command}` walks the entire sandbox (system files only) "
    "and runs into the command timeout. Your files are under /workspace: search there instead "
    "(for example `find /workspace -name ...` or `grep -r ... /workspace`). Data held by a "
    "connector/MCP server is not on this filesystem; use that connector's own tools to fetch it."
)


def _segments(command: str) -> list[str]:
    parts = [command]
    for separator in _SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    while tokens:
        head = PurePosixPath(tokens[0]).name
        if "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]  # VAR=value prefix
        elif head in _WRAPPERS:
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        elif head == "timeout":
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
            tokens = tokens[1:]  # the duration
        else:
            break
    return tokens


def _recursive_grep(args: list[str]) -> bool:
    for arg in args:
        if arg in {"--recursive", "--dereference-recursive", "--directories=recurse"}:
            return True
        if arg.startswith("-") and not arg.startswith("--") and ("r" in arg or "R" in arg):
            return True
    return False


def _positional(args: list[str]) -> list[str]:
    """Non-option arguments, in order (an option's own value is not tracked)."""
    positional, after_flags = [], False
    for arg in args:
        if after_flags or not arg.startswith("-") or arg == "-":
            positional.append(arg)
        elif arg == "--":
            after_flags = True
    return positional


def _shallow_find(args: list[str]) -> bool:
    """`find / -maxdepth 1` lists one directory; only unbounded walks are refused."""
    for index, arg in enumerate(args[:-1]):
        if arg == "-maxdepth" and args[index + 1].isdigit():
            return int(args[index + 1]) <= 2
    return False


def _scans_root(tokens: list[str]) -> bool:
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return False
    name, args = PurePosixPath(tokens[0]).name, tokens[1:]
    if name == "find":
        if _shallow_find(args):
            return False
        # Start points come before the first expression token.
        for arg in args:
            if arg in {"-H", "-L", "-P"}:
                continue
            if arg.startswith(("-", "(", "!")):
                break
            if arg in _ROOTS:
                return True
        return False
    if not any(arg in _ROOTS for arg in args):
        return False
    if name in _GREPS or name in {"rg", "ag", "ack"}:
        # The first positional is the search pattern (unless -e/--regexp gave it),
        # so `grep -rn "/" src` is a search for a slash, not a scan of `/`.
        paths = _positional(args)
        if not any(a in {"-e", "--regexp"} or a.startswith("--regexp=") for a in args):
            paths = paths[1:]
        if name in _GREPS and not _recursive_grep(args):
            return False
        return any(path in _ROOTS for path in paths)
    if name == "ls":
        return any(arg.startswith("-") and not arg.startswith("--") and "R" in arg for arg in args) or "--recursive" in args
    return name in _SEARCHERS


def whole_filesystem_scan(command: str) -> str | None:
    """The offending part of `command` when it walks `/`, else None."""
    for segment in _segments(command or ""):
        if _scans_root(_tokens(segment)):
            return segment[:120]
    return None


def blocked_message(command: str) -> str | None:
    """The tool result to return instead of running `command`, if it is refused."""
    offending = whole_filesystem_scan(command)
    return BLOCKED_MESSAGE.format(command=offending) if offending else None
