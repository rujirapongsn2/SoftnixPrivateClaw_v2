"""`claw` works when invoked through a symlink (install.sh links it into /usr/local/bin).

The location-resolving lines are cut out of the real scripts, placed in a throwaway project
tree and run through direct, relative and chained symlinks.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = {name: (REPO / "scripts" / name).read_text() for name in ("claw", "claw-serve.sh")}


def _resolver(text):
    start = text.index('SOURCE="${BASH_SOURCE[0]}"')
    return text[start:text.index("\n", text.index("SCRIPT_DIR=", start)) + 1]


def _project(tmp_path):
    scripts = tmp_path / "proj" / "scripts"
    scripts.mkdir(parents=True)
    probe = scripts / "claw"
    probe.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + _resolver(SCRIPTS["claw"])
                     + 'PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"\necho "$SCRIPT_DIR|$PROJECT_DIR"\n')
    probe.chmod(0o755)
    return probe


def _run(link):
    return subprocess.run([str(link)], capture_output=True, text=True, check=True).stdout.strip()


def test_a_direct_run_is_unchanged(tmp_path):
    probe = _project(tmp_path)
    real = probe.parent.resolve()
    assert _run(probe) == f"{real}|{real.parent}"


def test_an_absolute_symlink_resolves_to_the_project(tmp_path):
    probe = _project(tmp_path)
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "claw"
    link.symlink_to(probe.resolve())
    real = probe.parent.resolve()
    assert _run(link) == f"{real}|{real.parent}"  # not .../bin|<tmp_path>


def test_a_relative_symlink_resolves(tmp_path):
    probe = _project(tmp_path)
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "claw"
    link.symlink_to("../proj/scripts/claw")
    real = probe.parent.resolve()
    assert _run(link) == f"{real}|{real.parent}"


def test_a_chain_of_symlinks_resolves(tmp_path):
    probe = _project(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "claw").symlink_to("../b/claw")
    (tmp_path / "b" / "claw").symlink_to(probe.resolve())
    real = probe.parent.resolve()
    assert _run(tmp_path / "a" / "claw") == f"{real}|{real.parent}"


def test_the_real_script_starts_through_a_symlink(tmp_path):
    # No arguments prints the usage line; reaching it means the sourcing of its own
    # location did not abort the script.
    link = tmp_path / "claw"
    link.symlink_to(REPO / "scripts" / "claw")
    res = subprocess.run([str(link)], capture_output=True, text=True)
    assert res.returncode == 1 and "Usage: claw" in res.stdout


def test_both_scripts_follow_symlinks():
    for name, text in SCRIPTS.items():
        assert 'while [[ -L "$SOURCE" ]]' in text, name
        assert 'dirname "${BASH_SOURCE[0]}"' not in text, name
