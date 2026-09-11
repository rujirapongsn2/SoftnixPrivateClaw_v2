"""Separate job outputs and explicit input snapshots (not a security boundary).

The configured sandbox and policy still govern shell/connector access. These
folders prevent normal file tools in unrelated jobs from overwriting outputs.
"""

import copy
import shutil
from pathlib import Path

from sbot.core.verification import MAX_FILE_BYTES


def scope_runner(runner, mission_id, node_id, input_files):
    root = Path(runner.workspace).resolve()
    if not isinstance(input_files, list) or len(input_files) > 50:
        raise ValueError('at most 50 input files are allowed')
    base = root / '.team-jobs'
    if base.is_symlink():
        raise ValueError('job directory must not be a symlink')
    folder = base / mission_id / node_id
    if not folder.resolve().is_relative_to(root) or any(p.is_symlink() for p in (base, base / mission_id, folder)):
        raise ValueError('invalid job workspace')
    sources, size = [], 0
    for raw in dict.fromkeys(input_files):
        if not isinstance(raw, str):
            raise ValueError('input files must be paths')
        source = (root / raw).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError('input file is missing or outside the owner workspace')
        size += source.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError('input files exceed the snapshot size limit')
        sources.append((raw, source))
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for index, (raw, source) in enumerate(sources):
        destination = folder / 'inputs' / str(index) / source.name
        if not destination.resolve().is_relative_to(folder.resolve()):
            raise ValueError('input destination escapes job workspace')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        manifest[raw] = destination.relative_to(folder).as_posix()
    scoped = copy.copy(runner)
    scoped.workspace = folder
    parent_guard = runner.arg_guard

    def guard(name, args):
        if name == 'project':
            return args, 'Shared projects are unavailable in background jobs; use the job workspace.'
        if name in ('write_file', 'edit_file'):
            path = Path(str(args.get('path', '')))
            if path.is_absolute() and path.is_relative_to('/workspace'):
                path = path.relative_to('/workspace')
            if (folder / path).resolve().is_relative_to((folder / 'inputs').resolve()):
                return args, 'Keep input snapshots unchanged; write a separate output.'
        return parent_guard(name, args) if parent_guard else (args, None)

    scoped.arg_guard = guard
    return scoped, manifest
