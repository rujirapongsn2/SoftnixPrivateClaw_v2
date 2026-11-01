"""Refuse shell commands that read, list or export system files.

The sandbox container has its own /etc, /proc, /root and so on. They hold
nothing of the user's, but a request like "export /etc/passwd as a PDF" still
worked and looked like a breach to anyone watching. This guard refuses any
`exec` command that names a system path outside the user's workspace, before
the container starts, and tells the model to decline in the user's language.

It works on the command text, so it catches the direct forms: `cat /etc/passwd`,
`cat /"etc"/passwd`,
`cd /etc && cat passwd`, `open('/etc/hostname')`, `ls ~`, `cd ../..`,
`os.path.join('/', 'etc')`. It cannot see a path that is built at run time
(base64, hex, `chr()` arithmetic, string slicing). The container hardening in
claw/sandbox/ephemeral.py (non-root, read-only root, no capabilities, no
network) limits what such a command could reach; this guard is the main
control for the honest-but-unwise request.

Allowed on purpose: /workspace, the container's own /tmp scratch, /usr (tools,
fonts, stdlib), /opt (pinned engines such as GridCraft), /bin, /lib, and the
harmless device/proc files shell scripts use (/dev/null, /dev/stdout, ...).
"""

from __future__ import annotations

import posixpath
import re

from claw.tools.scan_guard import _segments, _strip_wrappers, _tokens

_SYSTEM_DIRS = (
    "etc", "proc", "sys", "root", "home", "var", "dev", "boot", "run", "srv",
    "mnt", "media", "private", "Users", "lost+found", "snap",
)
# Paths under a system dir that scripts use routinely and that reveal nothing.
_ALLOWED = (
    "/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/fd",
    "/dev/urandom", "/dev/random", "/dev/tty", "/dev/shm",
    "/proc/cpuinfo", "/proc/meminfo", "/proc/loadavg", "/proc/self/fd",
    "/var/tmp",
)
_SAFE_CD_ROOTS = ("/workspace", "/tmp", "/usr", "/opt", "/var/tmp")

_DIRS_RE = "|".join(re.escape(d) for d in _SYSTEM_DIRS)
# An absolute path to a system dir. The look-behind keeps URLs (`https://x.com/etc`),
# relative paths (`./etc`, `data/etc`), `$VAR/etc` and exclusion patterns
# (`-not -path "*/proc/*"`, `grep -v "^/proc"`) out; the look-ahead keeps
# `/etcetera` or `/var_name` out.
_ABS_RE = re.compile(
    rf"(?<![\w.~\-$@%*^])(/+(?:{_DIRS_RE}))(?=$|[/\s'\"`;|&)<>,\]}}*:])([^\s'\"`;|&)<>,\]}}]*)"
)
# `../` steps that climb out of /workspace into a system dir, e.g. `../../etc/passwd`.
_TRAVERSAL_RE = re.compile(rf"(?:\.\./)+(?:{_DIRS_RE})(?=$|[/\s'\"`;|&)<>,\]}}])")
# The home directory (/root in the container) and the root dir spelled for code.
# The home directory (/root in the container), spelled `~`, `$HOME` or via the
# password database. `~` as awk's match operator (`$4 ~ /re/`, `!~`) is not a path.
_HOME_RE = re.compile(
    # A lone quoted `"~"` / `'~'` is a literal string (pandas `.str.replace("~", "")`),
    # unless it is expanduser's argument.
    r"(?<![\w/.\-!~=$])(?!(?<=\")(?<!expanduser\(\")~\")(?!(?<=')(?<!expanduser\(')~')~(?!/\.fonts\b|/\.local/share/fonts\b)(?=/|$|[;|&)'\"`]|\s+(?![/\"]))"
    r"|\$\{?HOME\b|Path\.home\(\)|getpwnam|getpwuid|\bpwd\.getpw|\bspwd\b"
)
_ROOT_JOIN_RE = re.compile(
    rf"""(?:join|Path|PurePath|PurePosixPath)\(\s*(['"])/\1\s*[,/)]\s*(?:['"](?:{_DIRS_RE})['"])?"""
)
_CHDIR_ROOT_RE = re.compile(r"""chdir\(\s*(['"])/+\1\s*\)""")

BLOCKED_MESSAGE = (
    "Error: blocked by the system-file policy: `{what}` is a system path outside /workspace. "
    "Reading, listing, copying or exporting system files (/etc, /proc, /sys, /root, /home, /var, /dev, "
    "the home directory, ...) is not allowed in PrivateClaw, even inside the sandbox, and this applies "
    "to every tool. Do not retry with another command, another path spelling or an encoding. Tell the "
    "user plainly, in the user's own language, that you cannot access system files and can only work "
    "with files in their workspace (in Thai, for example: \"ขออภัย ไม่สามารถอ่านหรือส่งออกไฟล์ระบบได้ "
    "ทำงานได้เฉพาะไฟล์ใน workspace ของคุณเท่านั้น\"), then offer to help with their own files."
)


def _allowed(path: str) -> bool:
    path = "/" + path.lstrip("/")
    return any(path == a or path.startswith(a + "/") for a in _ALLOWED)


def _cd_escape(command: str) -> str | None:
    """A `cd`/`pushd` that leaves the workspace for somewhere outside the safe roots."""
    cwd = "/workspace"
    for segment in _segments(command):
        tokens = _strip_wrappers(_tokens(segment))
        if not tokens or posixpath.basename(tokens[0]) not in {"cd", "pushd"}:
            continue
        args = [t for t in tokens[1:] if not t.startswith("-")]
        target = args[0] if args else "~"
        if target.startswith(("~", "$")):
            return segment[:120]
        cwd = posixpath.normpath(posixpath.join(cwd, target))
        if not any(cwd == r or cwd.startswith(r + "/") for r in _SAFE_CD_ROOTS):
            return segment[:120]
    return None


def system_path_reference(command: str) -> str | None:
    """The offending part of `command` when it touches system files, else None."""
    text = command or ""
    # Also look at the text with shell quotes removed, so `/"etc"/passwd` or
    # `/e""tc/passwd` read as the path the shell actually opens.
    for candidate in (text, text.replace('"', "").replace("'", "")):
        for match in _ABS_RE.finditer(candidate):
            full = match.group(1) + match.group(2)
            if not _allowed(full):
                return full[:120]
    for regex in (_TRAVERSAL_RE, _HOME_RE, _ROOT_JOIN_RE, _CHDIR_ROOT_RE):
        match = regex.search(text)
        if match:
            return match.group(0).strip()[:120]
    return _cd_escape(text)


def blocked_message(command: str) -> str | None:
    """The tool result to return instead of running `command`, if it is refused."""
    what = system_path_reference(command)
    return BLOCKED_MESSAGE.format(what=what) if what else None
