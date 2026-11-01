"""Installers keep the pinned GridCraft binary in place (docs/spreadsheet-gridcraft-spec.md).

The shell blocks are cut out of the real scripts and run against a fake
install-gridcraft.sh, so a change to either script that breaks the behaviour fails here.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()
CLAW = (REPO / "scripts" / "claw").read_text()

FAKE_INSTALLER = """#!/usr/bin/env bash
echo "installer vendor=[${CLAW_SPREADSHEET_VENDOR_DIR-unset}]" >> "$CALLS"
exit ${FAKE_EXIT:-0}
"""


def _project(tmp_path, env_text=None, fake_exit=0):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    fake = tmp_path / "scripts" / "install-gridcraft.sh"
    fake.write_text(FAKE_INSTALLER)
    fake.chmod(0o755)
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text)
    return {"CALLS": str(tmp_path / "calls"), "FAKE_EXIT": str(fake_exit), "PATH": "/usr/bin:/bin"}


def _calls(tmp_path):
    f = tmp_path / "calls"
    return f.read_text().splitlines() if f.exists() else []


def _update(tmp_path, env_text, fake_exit=0):
    env = _project(tmp_path, env_text, fake_exit)
    start = CLAW.index("env_value() {")
    helpers = CLAW[start:CLAW.index("\n}\n", start) + 3]
    block = CLAW[CLAW.index("update_gridcraft() {"):CLAW.index("cmd_update() {")]
    script = f'PROJECT_DIR="{tmp_path}"\n{helpers}\n{block}\nupdate_gridcraft; echo "rc=$?"'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True).stdout


def test_update_installs_when_the_tool_is_on(tmp_path):
    out = _update(tmp_path, "CLAW_SPREADSHEET_ENABLED=true\n")
    assert _calls(tmp_path) == ["installer vendor=[]"] and out.endswith("rc=0\n")


def test_update_passes_the_vendor_dir_from_env(tmp_path):
    _update(tmp_path, "CLAW_SPREADSHEET_ENABLED=True\nCLAW_SPREADSHEET_VENDOR_DIR=/opt/gc\n")
    assert _calls(tmp_path) == ["installer vendor=[/opt/gc]"]


def test_update_leaves_a_disabled_tool_alone(tmp_path):
    for env_text in ("CLAW_SPREADSHEET_ENABLED=false\n", "# CLAW_SPREADSHEET_ENABLED=true\n", "CLAW_PORT=8700\n"):
        out = _update(tmp_path, env_text)
        assert _calls(tmp_path) == [] and out == "rc=0\n", env_text


def test_update_survives_a_failed_install_and_says_so(tmp_path):
    out = _update(tmp_path, "CLAW_SPREADSHEET_ENABLED=true\n", fake_exit=1)
    assert out.endswith("rc=0\n") and "WARNING: GridCraft install failed" in out


def test_update_runs_the_install_before_the_restart():
    body = CLAW[CLAW.index("cmd_update() {"):]
    assert body.index("update_sandbox_image") < body.index("update_gridcraft") < body.index("cmd_restart")


def _step_3b(tmp_path, env_text, spreadsheet, fake_exit=0):
    env = _project(tmp_path, env_text, fake_exit)
    start = INSTALL.index("# 3b) GridCraft")
    block = INSTALL[start:INSTALL.index("# 4) Postgres container")]
    script = (
        'step() { :; }; ok() { echo "ok $*"; }; warn() { echo "warn $*"; }\n'
        f"SPREADSHEET={spreadsheet}\n{block}\necho \"value=$SPREADSHEET_ENABLED_VALUE\""
    )
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True).stdout


def test_install_is_opt_in(tmp_path):
    assert _step_3b(tmp_path, None, 0).strip() == "value=false" and _calls(tmp_path) == []


def test_install_flag_installs_and_turns_the_tool_on(tmp_path):
    out = _step_3b(tmp_path, None, 1)
    assert _calls(tmp_path) == ["installer vendor=[]"] and out.strip().endswith("value=true")


def test_install_rerun_follows_an_env_that_already_enables_it(tmp_path):
    out = _step_3b(tmp_path, "CLAW_SPREADSHEET_ENABLED=true\nCLAW_SPREADSHEET_VENDOR_DIR=/opt/gc\n", 0)
    assert _calls(tmp_path) == ["installer vendor=[/opt/gc]"] and out.strip().endswith("value=true")


def test_a_failed_install_keeps_the_tool_off(tmp_path):
    out = _step_3b(tmp_path, None, 1, fake_exit=1)
    assert "warn GridCraft install failed" in out and out.strip().endswith("value=false")


def _record(tmp_path, env_text, value):
    start = INSTALL.index("  # GridCraft installed above: turn the tool on.")
    block = INSTALL[start:INSTALL.index("else\n  LLM_KEY=", start)]
    (tmp_path / ".env").write_text(env_text)
    script = f'ok() {{ echo "ok $*"; }}\nSPREADSHEET_ENABLED_VALUE={value}\n{block}'
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, check=True)
    return (tmp_path / ".env").read_text()


def test_existing_env_gets_the_key_only_when_absent(tmp_path):
    assert _record(tmp_path, "CLAW_PORT=8700\n", "true").endswith("CLAW_SPREADSHEET_ENABLED=true\n")
    explicit = "CLAW_SPREADSHEET_ENABLED=false\n"
    assert _record(tmp_path, explicit, "true") == explicit
    assert _record(tmp_path, "CLAW_PORT=8700\n", "false") == "CLAW_PORT=8700\n"


def test_fresh_env_writes_the_install_result():
    assert "${SPREADSHEET_ENV_LINE}" in INSTALL
    assert 'SPREADSHEET_ENV_LINE="# CLAW_SPREADSHEET_ENABLED=false"' in INSTALL  # commented unless installed
    assert 'SPREADSHEET_ENV_LINE="CLAW_SPREADSHEET_ENABLED=true"' in INSTALL
    assert INSTALL.index("# 3b) GridCraft") < INSTALL.index("cat > .env <<EOF")
    assert "--spreadsheet) SPREADSHEET=1" in INSTALL


def test_install_script_pin_matches_the_tool():
    from claw.tools.spreadsheet import GRIDCRAFT_VERSION

    assert f'VERSION="{GRIDCRAFT_VERSION}"' in (REPO / "scripts" / "install-gridcraft.sh").read_text()
    assert os.access(REPO / "scripts" / "install-gridcraft.sh", os.X_OK)


def test_a_fresh_env_without_the_flag_still_lets_a_later_run_enable_it(tmp_path):
    # Fresh install, no flag: the line must not be an active key.
    out = _step_3b(tmp_path, None, 0)
    assert out.strip() == "value=false"
    line = subprocess.run(
        ["bash", "-c", INSTALL[INSTALL.index("# 3b) GridCraft"):INSTALL.index("if [[ -f .env ]]")] + 'echo "$SPREADSHEET_ENV_LINE"'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    assert line.startswith("#")
    # ...and the later --spreadsheet run on that .env adds the active key.
    assert _record(tmp_path, line + "\nCLAW_PORT=8700\n", "true").endswith("CLAW_SPREADSHEET_ENABLED=true\n")
