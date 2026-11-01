"""System files are refused before a command reaches the sandbox, and the
sandbox container itself runs hardened."""

import os
import subprocess
from pathlib import Path

import pytest

from claw.config import SandboxSettings
from claw.sandbox.ephemeral import EphemeralSandbox, hardening_flags, sandbox_user
from claw.tools.shell import ExecTool
from claw.tools.system_path_guard import system_path_reference
from sbot.tools.shell import ExecTool as SbotExecTool


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "cat /etc/hostname",
        "cd /etc; cat passwd",
        "cd /etc && cat passwd",
        "python3 -c \"print(open('/etc/hostname').read())\"",
        "python3 - <<'PY'\nsrc = open(\"/etc/passwd\").read()\nPY",
        'SRC = "/etc/passwd"',
        "cat //etc//passwd",
        "cat file:///etc/passwd",
        "cat /proc/self/environ",
        "tr '\\0' '\\n' < /proc/1/environ",
        "ls -la /root",
        "ls /home",
        "ls /var/lib",
        "ls /run",
        "ls /sys/class",
        "cat ../../etc/passwd",
        "cd ../.. && cat etc/passwd",
        "cd / && cat etc/passwd",
        "cd .. && ls",
        "cd",
        "cd ~",
        "ls ~",
        "ls -la ~ 2>/dev/null",
        "cat ~/.bashrc",
        "echo $HOME",
        "ls ${HOME}/.cache",
        "python3 -c 'import os;os.chdir(\"/\");print(open(\"etc/passwd\").read())'",
        "python3 -c \"import os;print(open(os.path.join('/', 'etc', 'passwd')).read())\"",
        "python3 -c 'import pwd;print(pwd.getpwall())'",
        "python3 -c 'from pathlib import Path;print(Path.home())'",
        "echo > /dev/tcp/host.docker.internal/80",
    ],
)
def test_system_paths_are_detected(command):
    assert system_path_reference(command)


@pytest.mark.parametrize(
    "command",
    [
        "python3 report.py > /dev/null 2>&1",
        "python3 x.py 2>/dev/null | head",
        "cat data/etc/notes.csv",
        "ls ./etc",
        "curl -s https://example.com/etc/passwd",
        "fc-list | grep -i tlwg",
        "ls /usr/share/fonts/truetype/tlwg/",
        "python3 -c \"import matplotlib, openpyxl\"",
        "cd /tmp && python3 build.py && cp out.pdf /workspace/",
        "cd /workspace/.tmp && ls",
        "mkdir -p sub && cd sub && cd .. && ls",
        "nproc; head -3 /proc/cpuinfo",
        "/opt/gridcraft/gridcraft-cli --version",
        "awk -F',' 'NR>1 { if ($4 !~ /^[0-9]+$/) print }' data/a.csv",
        "awk '$1 ~ /x/' data/a.csv",
        "python3 -c \"import glob,os;print(glob.glob(os.path.expanduser('~/.fonts/*')))\"",
        "find / -iname '*x*' -not -path '*/proc/*'",
        "grep -vE '^/proc|^/sys' list.txt",
        "head -c 16 /dev/urandom | base64",
        "ls /var/tmp",
        "du -sh /workspace",
    ],
)
def test_ordinary_document_work_is_allowed(command):
    assert system_path_reference(command) is None


class _NeverSandbox:
    async def run(self, command, workspace):  # pragma: no cover - must not be reached
        raise AssertionError("refused command reached the sandbox")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [ExecTool, SbotExecTool])
async def test_exec_refuses_before_the_sandbox_and_says_to_decline(tool):
    result = await tool(_NeverSandbox(), Path("/tmp")).execute(command="cat /etc/passwd")
    assert "system-file policy" in result
    assert "/etc/passwd" in result
    assert "user's own language" in result
    assert "ไฟล์ระบบ" in result


@pytest.mark.parametrize("tool", [ExecTool, SbotExecTool])
def test_exec_description_tells_the_model_system_files_are_off_limits(tool):
    assert "System files are off limits" in tool.description


def test_hardening_flags_confine_the_container():
    flags = hardening_flags(SandboxSettings())
    assert "--read-only" in flags
    assert flags[flags.index("--cap-drop") + 1] == "ALL"
    assert flags[flags.index("--security-opt") + 1] == "no-new-privileges"
    assert any(f.startswith("/tmp:rw,exec") for f in flags)
    assert "HOME=/tmp" in flags
    assert flags[flags.index("--user") + 1] == sandbox_user("auto")


def test_hardening_can_be_turned_off_and_user_overridden():
    assert hardening_flags(SandboxSettings(hardened=False)) == []
    assert "--user" not in hardening_flags(SandboxSettings(user=""))
    assert sandbox_user("1000:1000") == "1000:1000"
    expected = "65534:65534" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"
    assert sandbox_user("auto") == expected


def test_ephemeral_argv_carries_the_hardening(tmp_path):
    argv = EphemeralSandbox(SandboxSettings(network="none"))._docker_argv("true", tmp_path)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in argv and "--user" in argv
    assert argv.index("--read-only") < argv.index("claw-sandbox:latest")


def test_sbot_argv_carries_the_hardening(tmp_path):
    from sbot.config import SandboxSettings as SbotSettings
    from sbot.sandbox.ephemeral import EphemeralSandbox as SbotSandbox

    argv = SbotSandbox(SbotSettings(network="none"))._docker_argv("true", tmp_path)
    assert "--read-only" in argv and "ALL" in argv and "--user" in argv


DOCKER_IMAGE = os.environ.get("CLAW_SANDBOX_TEST_IMAGE", "claw-sandbox:latest")


def _docker_ready() -> bool:
    if os.environ.get("CLAW_TEST_DOCKER") != "1":
        return False
    try:
        return subprocess.run(
            ["docker", "image", "inspect", DOCKER_IMAGE], capture_output=True, timeout=20
        ).returncode == 0
    except Exception:
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(not _docker_ready(), reason="set CLAW_TEST_DOCKER=1 with Docker + sandbox image")
async def test_hardened_container_still_does_document_work(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.csv").write_text("region,amount\nเหนือ,1\nใต้,2\n", encoding="utf-8")
    script = (
        "set -e; id -u; echo hi > /workspace/new.txt; "
        "python3 -c \"import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as p;"
        "p.bar(['ก'],[1]);p.savefig('/workspace/c.png')\"; "
        "python3 -c \"import pandas as pd;pd.read_csv('/workspace/data/a.csv').to_excel('/workspace/o.xlsx')\"; "
        "cd /tmp && echo ok > t && cat t; "
        "touch /etc/probe 2>/dev/null && echo ROOTFS-WRITABLE || echo rootfs-readonly; "
        "cat /etc/shadow >/dev/null 2>&1 && echo SHADOW-READ || echo shadow-denied"
    )
    result = await EphemeralSandbox(SandboxSettings(image=DOCKER_IMAGE, network="none", timeout_seconds=60)).run(script, tmp_path)
    assert result.exit_code == 0, result.render()
    assert result.stdout.splitlines()[0] != "0"
    assert "rootfs-readonly" in result.stdout and "shadow-denied" in result.stdout
    for name in ("new.txt", "c.png", "o.xlsx"):
        assert (tmp_path / name).stat().st_size > 0


@pytest.mark.parametrize("command", ['cat /"etc"/passwd', 'cat /e""tc/passwd', "cat '/etc'/hosts",
                                     "python -c \"import os; print(os.listdir(os.path.expanduser('~')))\""])
def test_quoted_and_expanduser_spellings_are_refused(command):
    assert system_path_reference(command) is not None


def test_a_lone_quoted_tilde_is_just_a_string():
    assert system_path_reference('python -c "df.columns = df.columns.str.replace(\'~\', \'\')"') is None
    assert system_path_reference('python -c \'s = s.replace("~", "-")\'') is None


def test_a_root_server_runs_exec_as_the_workspace_owner(monkeypatch, tmp_path):
    from claw.sandbox import ephemeral

    monkeypatch.setattr(ephemeral.os, "getuid", lambda: 0)
    monkeypatch.setattr(ephemeral.os, "getgid", lambda: 0)
    st = tmp_path.stat()
    assert sandbox_user("auto", tmp_path) == f"{st.st_uid}:{st.st_gid}"
    assert sandbox_user("auto") == "65534:65534"
    assert sandbox_user("auto", tmp_path / "missing") == "65534:65534"
    flags = hardening_flags(SandboxSettings(), tmp_path)
    assert flags[flags.index("--user") + 1] == f"{st.st_uid}:{st.st_gid}"
