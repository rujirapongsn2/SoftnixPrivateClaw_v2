"""GridCraft is installed and turned on by install.sh / claw update without a manual step.

The real `ensure_gridcraft` is cut out of install.sh and run against a fake
scripts/install-gridcraft.sh, so a change that breaks the behaviour fails here.
"""

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()
CLAW = (REPO / "scripts" / "claw").read_text()

FAKE_INSTALLER = """#!/usr/bin/env bash
echo "installer vendor=[${CLAW_SPREADSHEET_VENDOR_DIR-unset}]" >> "$CALLS"
exit ${FAKE_EXIT:-0}
"""


def _function(name):
    start = INSTALL.index(f"{name}() {{")
    return INSTALL[start:INSTALL.index("\n}\n", start) + 3]


def _ensure(tmp_path, env_text=None, opt="auto", fake_exit=0):
    """Run ensure_gridcraft in tmp_path; returns (stdout, .env text or None, installer calls)."""
    (tmp_path / "scripts").mkdir(exist_ok=True)
    fake = tmp_path / "scripts" / "install-gridcraft.sh"
    fake.write_text(FAKE_INSTALLER)
    fake.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.unlink(missing_ok=True)
    (tmp_path / "calls").unlink(missing_ok=True)
    if env_text is not None:
        env_file.write_text(env_text)
    fn = "".join(_function(name) for name in ("env_get", "env_fill_empty", "ensure_gridcraft"))
    script = (
        "set -euo pipefail\n"  # install.sh's own options: a failing sed/grep must not abort it
        'step() { echo "step $*"; }; ok() { echo "ok $*"; }; warn() { echo "warn $*"; }\n'
        f'PROJECT_DIR="{tmp_path}"; SPREADSHEET_OPT={opt}\n{fn}\n'
        'ensure_gridcraft; echo "rc=$? result=$GRIDCRAFT_RESULT line=$SPREADSHEET_ENV_LINE"'
    )
    env = {"CALLS": str(tmp_path / "calls"), "FAKE_EXIT": str(fake_exit), "PATH": "/usr/bin:/bin"}
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True).stdout
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return out, (env_file.read_text() if env_file.exists() else None), calls


def test_an_env_without_the_key_gets_gridcraft_and_the_key(tmp_path):
    out, env, calls = _ensure(tmp_path, "CLAW_PORT=8700\n")
    assert calls == ["installer vendor=[]"]
    assert env.startswith("CLAW_PORT=8700\n") and env.endswith("CLAW_SPREADSHEET_ENABLED=true\n")
    assert "result=installed" in out and "rc=0" in out


def test_a_commented_default_counts_as_unset(tmp_path):
    _, env, calls = _ensure(tmp_path, "# CLAW_SPREADSHEET_ENABLED=false\nCLAW_PORT=8700\n")
    assert calls and env.endswith("CLAW_SPREADSHEET_ENABLED=true\n")


def test_an_empty_key_is_filled_in(tmp_path):
    _, env, _ = _ensure(tmp_path, "CLAW_PORT=8700\nCLAW_SPREADSHEET_ENABLED=\nCLAW_X=1\n")
    assert env == "CLAW_PORT=8700\nCLAW_SPREADSHEET_ENABLED=true\nCLAW_X=1\n"


def test_an_enabled_env_keeps_the_binary_current_and_is_not_rewritten(tmp_path):
    before = "CLAW_SPREADSHEET_ENABLED=True  # on\nCLAW_SPREADSHEET_VENDOR_DIR=/opt/gc\n"
    _, env, calls = _ensure(tmp_path, before)
    assert calls == ["installer vendor=[/opt/gc]"] and env == before


def test_an_explicit_false_is_respected(tmp_path):
    for value in ("false", "False", "0", "no", "off", '"false"'):
        before = f"CLAW_SPREADSHEET_ENABLED={value}\n"
        out, env, calls = _ensure(tmp_path, before)
        assert calls == [] and env == before and "result=off" in out, value


def test_the_flag_does_not_override_an_explicit_false(tmp_path):
    before = "CLAW_SPREADSHEET_ENABLED=false\n"
    out, env, calls = _ensure(tmp_path, before, opt="on")
    assert calls == [] and env == before and "warn .env sets CLAW_SPREADSHEET_ENABLED=false" in out


def test_no_spreadsheet_skips_everything_and_a_fresh_env_records_it(tmp_path):
    out, env, calls = _ensure(tmp_path, "CLAW_PORT=8700\n", opt="off")
    assert calls == [] and env == "CLAW_PORT=8700\n"
    assert "line=CLAW_SPREADSHEET_ENABLED=false" in out


def test_a_failed_install_warns_and_leaves_env_alone(tmp_path):
    out, env, calls = _ensure(tmp_path, "CLAW_PORT=8700\n", fake_exit=1)
    assert calls and env == "CLAW_PORT=8700\n"
    assert "warn GridCraft install failed" in out and "rc=0" in out and "result=failed" in out
    # a commented default, so a later run can still add the active key
    assert "line=# CLAW_SPREADSHEET_ENABLED=false" in out


def test_a_fresh_install_without_an_env_yet(tmp_path):
    out, env, calls = _ensure(tmp_path, None)
    assert calls == ["installer vendor=[]"] and env is None
    assert "result=installed line=CLAW_SPREADSHEET_ENABLED=true" in out


def test_the_vendor_dir_is_read_without_quotes_or_comments(tmp_path):
    for raw in ('"/opt/gc"', "'/opt/gc'", "/opt/gc  # note", '"/opt/gc" # note'):
        _, _, calls = _ensure(tmp_path, f"CLAW_SPREADSHEET_VENDOR_DIR={raw}\n")
        assert calls == ["installer vendor=[/opt/gc]"], raw


def test_filling_a_key_keeps_the_file_mode_and_survives_backslashes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=abc\nCLAW_SANDBOX__REGISTRY=\n")
    env_file.chmod(0o600)
    value = "reg.example/a\\b"  # one backslash; awk -v would turn it into an escape
    script = "set -euo pipefail\n" + _function("env_fill_empty") + 'env_fill_empty CLAW_SANDBOX__REGISTRY "$V"\n'
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "V": value}, check=True)
    assert env_file.read_text() == f"SECRET=abc\nCLAW_SANDBOX__REGISTRY={value}\n"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".env.tmp").exists()


def test_the_old_update_script_reaches_it_through_web_only():
    # An update that pulls this change still runs the previous scripts/claw, which only
    # calls install.sh --web-only and --browsers-only. That is the one path that makes the
    # first update install GridCraft with no manual step.
    web_only = re.search(r"if \[\[ \$WEB_ONLY -eq 1 \]\]; then (.*?); exit 0; fi", INSTALL).group(1)
    assert web_only == "build_web; ensure_gridcraft"
    body = CLAW[CLAW.index("cmd_update() {"):]
    assert body.index('install.sh" --web-only') < body.index("cmd_restart")


def test_a_full_install_runs_it_before_the_env_is_written():
    assert INSTALL.index("\nensure_gridcraft\n") < INSTALL.index("cat > .env <<EOF")
    assert "${SPREADSHEET_ENV_LINE}" in INSTALL


def test_the_opt_out_flag_and_env_exist():
    assert "--no-spreadsheet) SPREADSHEET_OPT=off" in INSTALL
    assert "--spreadsheet) SPREADSHEET_OPT=on" in INSTALL
    assert "false|0|no|off) SPREADSHEET_OPT=off" in INSTALL


def test_the_installer_cannot_hang_an_update():
    assert "--connect-timeout" in (REPO / "scripts" / "install-gridcraft.sh").read_text()


def test_install_script_pin_matches_the_tool():
    from claw.tools.spreadsheet import GRIDCRAFT_VERSION

    assert f'VERSION="{GRIDCRAFT_VERSION}"' in (REPO / "scripts" / "install-gridcraft.sh").read_text()
    assert os.access(REPO / "scripts" / "install-gridcraft.sh", os.X_OK)
