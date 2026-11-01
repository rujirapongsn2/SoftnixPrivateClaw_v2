"""The tool sandbox is isolated (network=none) by default and after an update."""

import asyncio
import json
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from claw.config import SandboxSettings
from claw.sandbox import ephemeral
from claw.sandbox.network_migration import apply, migrate_sandbox_network
from sbot.config import SandboxSettings as SbotSandboxSettings

REPO = Path(__file__).resolve().parents[1]


def test_code_default_is_none(monkeypatch):
    monkeypatch.delenv("CLAW_SANDBOX__NETWORK", raising=False)
    assert SandboxSettings().network == "none"
    assert SbotSandboxSettings().network == "none"


def test_env_example_and_compose_default_to_none():
    example = (REPO / ".env.example").read_text()
    active = [ln for ln in example.splitlines() if ln.startswith("CLAW_SANDBOX__NETWORK=")]
    assert active == ["CLAW_SANDBOX__NETWORK=none"]
    assert "${CLAW_SANDBOX__NETWORK:-none}" in (REPO / "docker-compose.prod.yml").read_text()
    assert "CLAW_SANDBOX__NETWORK=none" in (REPO / "install.sh").read_text()


def test_explicit_bridge_becomes_none_and_rest_is_untouched():
    text = "A=1\nCLAW_SANDBOX__NETWORK=bridge\nCLAW_SANDBOX__TIMEOUT_SECONDS=120\n"
    result = migrate_sandbox_network(text)
    assert result.changed and result.previous == "bridge"
    assert result.text == "A=1\nCLAW_SANDBOX__NETWORK=none\nCLAW_SANDBOX__TIMEOUT_SECONDS=120\n"


@pytest.mark.parametrize(
    "line",
    [
        'CLAW_SANDBOX__NETWORK="bridge"',
        "CLAW_SANDBOX__NETWORK = 'host'",
        "export CLAW_SANDBOX__NETWORK=bridge  # internet",
        "  CLAW_SANDBOX__NETWORK=bridge",
    ],
)
def test_quoted_exported_and_commented_values_are_migrated(line):
    result = migrate_sandbox_network(f"{line}\n")
    assert result.changed
    assert result.text.strip().endswith("CLAW_SANDBOX__NETWORK=none")
    assert ("export " in result.text) == line.lstrip().startswith("export")


def test_every_active_occurrence_changes_because_the_last_one_wins():
    text = "CLAW_SANDBOX__NETWORK=none\nCLAW_SANDBOX__NETWORK=bridge\n"
    result = migrate_sandbox_network(text)
    assert result.previous == "bridge"
    assert result.text == "CLAW_SANDBOX__NETWORK=none\nCLAW_SANDBOX__NETWORK=none\n"


def test_already_none_is_a_no_op_and_idempotent():
    text = "CLAW_SANDBOX__NETWORK=none\r\nX=1\r\n"
    result = migrate_sandbox_network(text)
    assert not result.changed and result.text == text
    once = migrate_sandbox_network("CLAW_SANDBOX__NETWORK=bridge\n").text
    assert not migrate_sandbox_network(once).changed


def test_commented_line_is_ignored_and_missing_key_is_appended():
    text = "# CLAW_SANDBOX__NETWORK=bridge\nX=1"
    result = migrate_sandbox_network(text)
    assert result.changed and result.previous is None
    assert result.text.startswith("# CLAW_SANDBOX__NETWORK=bridge\nX=1\n")
    assert result.text.endswith("CLAW_SANDBOX__NETWORK=none\n")


def test_keep_marker_opts_out():
    text = "CLAW_SANDBOX__NETWORK=bridge  # claw:keep-network\n"
    result = migrate_sandbox_network(text)
    assert result.kept and not result.changed and result.text == text


def test_apply_backs_up_and_keeps_file_mode(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("SECRET=x\nCLAW_SANDBOX__NETWORK=bridge\n")
    os.chmod(env, 0o600)
    assert apply(env, now=datetime(2026, 11, 1, 12, 0, 0)) == 0
    assert env.read_text() == "SECRET=x\nCLAW_SANDBOX__NETWORK=none\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    backup = tmp_path / ".env.bak-20261101-120000-sandbox-network"
    assert backup.read_text() == "SECRET=x\nCLAW_SANDBOX__NETWORK=bridge\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "bridge -> none" in out and "SECRET" not in out and "=x" not in out
    # Second run: nothing to do, no new backup.
    apply(env)
    assert "already none" in capsys.readouterr().out
    assert len(list(tmp_path.glob(".env.bak-*"))) == 1


def test_apply_check_writes_nothing(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("CLAW_SANDBOX__NETWORK=bridge\n")
    apply(env, check=True)
    assert env.read_text() == "CLAW_SANDBOX__NETWORK=bridge\n"
    assert not list(tmp_path.glob(".env.bak-*"))
    assert "would change" in capsys.readouterr().out


def test_apply_without_env_file_is_fine(tmp_path):
    assert apply(tmp_path / ".env") == 0


class _Proc:
    def __init__(self, code, stderr=b"", stdout=b""):
        self.returncode = code
        self._stderr = stderr
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, self._stderr


def _probe_output(**missing):
    return json.dumps({k: missing.get(k, []) for k in ("module", "executable", "data", "not_writable")}).encode()


def _fake_exec(monkeypatch, code, stderr=b"", stdout=None):
    calls = []
    stdout = _probe_output() if stdout is None else stdout

    async def fake(*argv, **_kw):
        calls.append(argv)
        return _Proc(code, stderr, stdout)

    monkeypatch.setattr(ephemeral.asyncio, "create_subprocess_exec", fake)
    return calls


def test_image_self_check_passes_for_a_complete_image(monkeypatch):
    calls = _fake_exec(monkeypatch, 0)
    assert asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(network="none"))) is None
    assert "--network" in calls[0] and "none" in calls[0]


def test_image_self_check_names_missing_packages_and_rebuild(monkeypatch):
    _fake_exec(monkeypatch, 0, stdout=_probe_output(module=["matplotlib"], executable=["pdftoppm"]))
    problem = asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(network="none")))
    assert "Python packages missing: matplotlib" in problem and "executables missing: pdftoppm" in problem
    assert "docker build -f docker/sandbox.Dockerfile" in problem


def test_image_self_check_reports_a_missing_image(monkeypatch):
    _fake_exec(monkeypatch, 125, b"Unable to find image 'claw-sandbox:latest' locally\n")
    problem = asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(network="none")))
    assert "is missing" in problem and "Unable to find image" in problem and "docker build" in problem


def test_image_self_check_never_raises_without_docker(monkeypatch):
    async def boom(*_a, **_k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ephemeral.asyncio, "create_subprocess_exec", boom)
    problem = asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(network="none")))
    assert "docker is unavailable" in problem


def test_image_self_check_skipped_unless_network_none(monkeypatch):
    calls = _fake_exec(monkeypatch, 1)
    assert asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(network="bridge"))) is None
    assert asyncio.run(ephemeral.image_readiness_problem(SandboxSettings(enabled=False))) is None
    assert calls == []


def test_quiet_apply_prints_nothing_when_nothing_changes(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("CLAW_SANDBOX__NETWORK=none\n")
    apply(env, quiet=True)
    apply(tmp_path / "missing.env", quiet=True)
    assert capsys.readouterr().out == ""


def test_serve_wrapper_and_update_run_the_migration():
    serve = (REPO / "scripts" / "claw-serve.sh").read_text()
    update = (REPO / "scripts" / "claw").read_text()
    assert "claw.sandbox.network_migration --quiet" in serve
    assert serve.index("network_migration") < serve.index('. "$PROJECT_DIR/.env"')
    assert "claw.sandbox.network_migration" in update


def test_apply_rewrites_the_target_of_a_symlinked_env_and_keeps_the_link(tmp_path, capsys):
    real = tmp_path / "etc-claw.env"
    real.write_text("SECRET=x\nCLAW_SANDBOX__NETWORK=bridge\n")
    os.chmod(real, 0o600)
    link = tmp_path / ".env"
    link.symlink_to(real)

    assert apply(link, now=datetime(2026, 11, 1, 12, 0, 0)) == 0

    assert link.is_symlink()
    assert real.read_text() == "SECRET=x\nCLAW_SANDBOX__NETWORK=none\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    backup = tmp_path / ".env.bak-20261101-120000-sandbox-network"
    assert backup.read_text() == "SECRET=x\nCLAW_SANDBOX__NETWORK=bridge\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.stat().st_uid == real.stat().st_uid


def test_quiet_apply_is_silent_for_a_kept_value(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("CLAW_SANDBOX__NETWORK=bridge  # claw:keep-network\n")
    assert apply(env, quiet=True) == 0
    assert capsys.readouterr().out == ""
    assert "kept" in (apply(env) == 0 and capsys.readouterr().out)


def test_env_backups_are_ignored_by_git_and_docker():
    assert ".env.*" in (REPO / ".gitignore").read_text().splitlines()
    assert "!.env.example" in (REPO / ".gitignore").read_text().splitlines()
    assert ".env.*" in (REPO / ".dockerignore").read_text().splitlines()
