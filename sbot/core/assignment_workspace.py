"""Per-assignment file workspace with explicit, read-only input snapshots."""

import copy
import shutil
import uuid
from pathlib import Path

from sbot.core.verification import MAX_FILE_BYTES
from sbot.sandbox.ephemeral import EphemeralSandbox


def isolate_runner(runner, input_files: list[str]):
    if not isinstance(input_files, list) or len(input_files) > 50:
        raise ValueError("input_files must contain at most 50 paths")
    # Host subprocess mode cannot enforce a read-only mount: fail closed rather
    # than presenting directory separation as a security boundary.
    if not isinstance(runner.sandbox, EphemeralSandbox) or not runner.sandbox.settings.enabled:
        raise ValueError("isolated assignments require the container sandbox")
    root = Path(runner.workspace).resolve()
    sources = []
    total = 0
    for raw in input_files:
        if not isinstance(raw, str):
            raise ValueError("input_files must be paths")
        source = (root / raw).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("input file is outside the workspace or missing")
        total += source.stat().st_size
        if total > MAX_FILE_BYTES:
            raise ValueError("assignment inputs exceed 100 MB")
        sources.append(source)
    base = root / ".assignments"
    if base.is_symlink():
        raise ValueError("assignment directory must not be a symlink")
    folder = base / uuid.uuid4().hex
    inputs = folder / "inputs"
    inputs.mkdir(parents=True)
    manifest = {}
    for index, (raw, source) in enumerate(zip(input_files, sources)):
        destination = inputs / str(index) / source.name
        destination.parent.mkdir()
        shutil.copyfile(source, destination)
        manifest[raw] = destination.relative_to(folder).as_posix()
    isolated = copy.copy(runner)
    isolated.workspace = folder
    isolated.sandbox = EphemeralSandbox(runner.sandbox.settings, readonly_inputs=True)
    # External connector/project capabilities can bypass a file workspace.
    # Isolated workers receive only their sandbox and explicit input files.
    isolated.connectors = None
    isolated.project_access = None
    parent_guard = runner.arg_guard

    def guard(name, args):
        if name == "project":
            return args, "Persistent projects are unavailable inside isolated assignments."
        if name in {"write_file", "edit_file"}:
            path = Path(str(args.get("path", "")))
            if path.is_absolute() and path.is_relative_to("/workspace"):
                path = path.relative_to("/workspace")
            target = (folder / path).resolve()
            if target.is_relative_to(inputs.resolve()):
                return args, "Assignment inputs are read-only; write a new output file."
        return parent_guard(name, args) if parent_guard else (args, None)

    isolated.arg_guard = guard
    return isolated, manifest


def rebase_result(result: dict, workspace: Path, root: Path) -> dict:
    """Public paths are owner-workspace-relative, including after reload."""
    workspace, root = workspace.resolve(), root.resolve()
    prefix = workspace.relative_to(root).as_posix()

    def rebase(path):
        resolved = (workspace / path).resolve()
        if not resolved.is_relative_to(workspace.resolve()):
            raise ValueError("artifact escapes assignment")
        return f"{prefix}/{resolved.relative_to(workspace).as_posix()}"

    result = copy.deepcopy(result)
    result["artifacts"] = [rebase(p) for p in result.get("artifacts", [])]
    for check in result.get("checks", []):
        if check.get("artifact"):
            check["artifact"] = rebase(check["artifact"])
    return result
