"""Keep package installs and repository clones out of the persistent workspace.

`/workspace` is the user's storage: it is kept, counted against their quota and
scanned after every command. A `git clone` or `npm install` there leaves
thousands of files behind that nobody asked for. The sandbox's own `/tmp` is the
right place: it vanishes when the command ends, so the install and the work that
needs it have to happen in the same command, and only the result is copied into
`/workspace`.

Like `scan_guard`, this is guidance, not a security boundary — the sandbox is
what confines a command. It catches the habitual forms (clone, npm/yarn/pnpm/bun
install, pip --target, new virtualenvs) and says what to do instead.
"""

import posixpath
import shlex

from claw.tools.scan_guard import _segments, _strip_wrappers, _tokens

WORKSPACE = "/workspace"

BLOCKED_MESSAGE = (
    "Error: `{command}` would put installed packages or a cloned repository into /workspace, which is the "
    "user's persistent, size-limited storage. Do it in the sandbox's /tmp instead, in the same command, "
    "for example `cd /tmp && git clone <url> repo && cd repo && <build or run>`, and copy only the files "
    "the user needs into /workspace. Everything in /tmp is discarded when the command ends."
)

# git clone options that take a separate value.
_CLONE_VALUE_OPTIONS = {
    "-b", "--branch", "--depth", "-c", "--config", "-o", "--origin", "--reference", "--template",
    "-j", "--jobs", "--filter", "--separate-git-dir", "-u", "--upload-pack", "--shallow-since",
    "--shallow-exclude", "--server-option", "--bundle-uri",
}
_NPM_LIKE = {"npm", "yarn", "pnpm", "bun"}
_NPM_INSTALL = {"install", "i", "ci", "add", "update", "upgrade", "up"}
_PIP_PATH_OPTIONS = {"-t", "--target", "--prefix", "--root", "-d", "--dest", "--src"}
_VENV_COMMANDS = {"virtualenv", "venv"}


def _resolve(path: str, cwd: str) -> str:
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd, path))


def _in_workspace(path: str) -> bool:
    return path == WORKSPACE or path.startswith(WORKSPACE + "/")


def _positional(args: list[str], value_options: set[str]) -> list[str]:
    positional: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in value_options:
            skip = True
        elif arg.startswith("-") and arg != "-":
            continue
        else:
            positional.append(arg)
    return positional


def _option_values(args: list[str], options: set[str]) -> list[str]:
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg in options and index + 1 < len(args):
            values.append(args[index + 1])
        elif "=" in arg and arg.split("=", 1)[0] in options:
            values.append(arg.split("=", 1)[1])
    return values


def _violates(tokens: list[str], cwd: str) -> bool:
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return False
    name = posixpath.basename(tokens[0])
    args = tokens[1:]

    if name == "git" and args:
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub != "clone":
            return False
        rest = args[args.index("clone") + 1 :]
        positional = _positional(rest, _CLONE_VALUE_OPTIONS)
        if not positional:
            return False
        if len(positional) >= 2:
            destination = _resolve(positional[1], cwd)
        else:
            repo = positional[0].rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            destination = _resolve(repo.removesuffix(".git") or "repo", cwd)
        return _in_workspace(destination)

    if name in _NPM_LIKE:
        sub = next((a for a in args if not a.startswith("-")), "")
        # A bare `yarn` / `pnpm` / `bun` (no arguments at all) installs; `yarn --version` does not.
        if sub not in _NPM_INSTALL and not (name in {"yarn", "pnpm", "bun"} and not args):
            return False
        if any(a in {"-g", "--global"} for a in args):
            return False
        for prefix in _option_values(args, {"--prefix", "--cwd", "-C", "--dir"}):
            return _in_workspace(_resolve(prefix, cwd))
        return _in_workspace(cwd)

    if name in {"pip", "pip3"} or (name.startswith("python") and "-m" in args and "pip" in args):
        if "download" not in args and "install" not in args and "wheel" not in args:
            return False
        return any(_in_workspace(_resolve(v, cwd)) for v in _option_values(args, _PIP_PATH_OPTIONS))

    if name in _VENV_COMMANDS or (name.startswith("python") and "venv" in args and "-m" in args) or (
        name == "uv" and args[:1] == ["venv"]
    ):
        positional = _positional(args, {"-p", "--python", "--prompt", "-m"})
        positional = [p for p in positional if p not in {"venv"}]
        target = positional[-1] if positional else ".venv"
        return _in_workspace(_resolve(target, cwd))

    return False


def workspace_install(command: str) -> str | None:
    """The offending part of `command` when it installs or clones into /workspace, else None."""
    cwd = WORKSPACE
    for segment in _segments(command or ""):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = _tokens(segment)
        core = _strip_wrappers(list(tokens))
        if core and posixpath.basename(core[0]) in {"cd", "pushd"}:
            target = next((a for a in core[1:] if not a.startswith("-")), "")
            cwd = _resolve(target, cwd) if target and target != "-" else "/root"
            continue
        if _violates(tokens, cwd):
            return segment[:120]
    return None


def blocked_message(command: str) -> str | None:
    offending = workspace_install(command)
    return BLOCKED_MESSAGE.format(command=offending) if offending else None
