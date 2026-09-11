#!/usr/bin/env python3
"""Git worktree lifecycle inside a project container; no credentials or network."""

import argparse
import fcntl
import json
import pathlib
import re
import subprocess
import sys
import uuid


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args], text=True
    ).strip()


def clean(root, tracked_only=False):
    args = ["status", "--porcelain"]
    if tracked_only:
        args.append("--untracked-files=no")
    if git(root, *args):
        raise ValueError(f"working tree has changes: {root}")


def execute(root, action, assignment, test_command):
    root = pathlib.Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", assignment):
        raise ValueError("assignment must be a lowercase slug, up to 48 characters")
    common = pathlib.Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    if not common.is_relative_to(root):
        raise ValueError("run from the main project checkout, not another worktree")
    folder = root / ".agent-worktrees"
    if folder.is_symlink():
        raise ValueError("worktree directory must not be a symlink")
    folder.mkdir(exist_ok=True)
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    branch = "codex/assignment-" + assignment
    worker = folder / assignment
    if worker.is_symlink():
        raise ValueError("assignment worktree must not be a symlink")
    with (common / "sbot-worktree.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        contents = exclude.read_text() if exclude.exists() else ""
        if "/.agent-worktrees/" not in contents.splitlines():
            with exclude.open("a") as stream:
                stream.write("\n/.agent-worktrees/\n")
        if action == "create":
            clean(root)
            if worker.exists():
                if git(worker, "symbolic-ref", "--short", "HEAD") != branch:
                    raise ValueError("existing worktree belongs to another branch")
            else:
                git(root, "worktree", "add", "-b", branch, str(worker), "HEAD")
            return {
                "status": "ready",
                "path": str(worker),
                "branch": branch,
                "base_commit": git(worker, "rev-parse", "HEAD"),
            }
        if not worker.is_dir() or git(worker, "symbolic-ref", "--short", "HEAD") != branch:
            raise ValueError("assignment worktree does not exist")
        if action == "status":
            return {
                "path": str(worker),
                "branch": branch,
                "commit": git(worker, "rev-parse", "HEAD"),
                "changes": git(worker, "status", "--porcelain"),
            }
        if not test_command:
            raise ValueError("integration requires an explicit test command after --")
        clean(root)
        clean(worker)
        base = git(root, "rev-parse", "HEAD")
        if not git(worker, "log", "--oneline", f"{base}..HEAD"):
            return {"status": "already_integrated", "commit": base}
        candidate = folder / ("integration-" + uuid.uuid4().hex)
        git(root, "worktree", "add", "--detach", str(candidate), base)
        try:
            git(candidate, "merge", "--no-ff", "--no-edit", branch)
            result = subprocess.run(test_command, cwd=candidate, timeout=900)
            if result.returncode:
                raise ValueError(f"tests failed with exit code {result.returncode}")
            clean(candidate, tracked_only=True)
            commit = git(candidate, "rev-parse", "HEAD")
            if git(root, "rev-parse", "HEAD") != base:
                raise ValueError("main branch changed during verification; integrate again")
            clean(root)
            git(root, "merge", "--ff-only", commit)
            result = {
                "status": "integrated",
                "commit": commit,
                "base_commit": base,
                "test_command": test_command,
                "test_exit_code": 0,
            }
            try:
                git(root, "worktree", "remove", "--force", str(candidate))
            except subprocess.SubprocessError:
                result["cleanup_warning"] = (
                    f"Integrated successfully; remove temporary worktree {candidate} when convenient."
                )
            return result
        except Exception as exc:
            raise ValueError(f"{exc}; integration worktree retained at {candidate}") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "status", "integrate"])
    parser.add_argument("assignment")
    parser.add_argument("test_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.test_command
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        print(json.dumps(execute(pathlib.Path.cwd(), args.action, args.assignment, command)))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
