"""Whole-filesystem scans are refused before they reach the sandbox."""

from pathlib import Path

import pytest

from claw.tools.scan_guard import whole_filesystem_scan
from claw.tools.shell import ExecTool
from sbot.tools.shell import ExecTool as SbotExecTool


@pytest.mark.parametrize(
    "command",
    [
        'find / -xdev -type f -newermt "2026-10-01 15:00" 2>/dev/null | grep -vE "^/(proc|sys)" | head -60',
        'grep -rsi "mcp_fframs" / --include=*.json',
        "timeout 30 find / -xdev -type f",
        'grep -rl "6532e9ed" / 2>/dev/null | head',
        "sudo find / -name x",
        "find -L / -name a",
        "ls -R /",
        "du -sh /",
        "rg foo /",
        'grep -ri "x" /workspace | head; find / -iname "*x*" 2>/dev/null',
    ],
)
def test_root_scans_are_detected(command):
    assert whole_filesystem_scan(command)


@pytest.mark.parametrize(
    "command",
    [
        "find /workspace -name x",
        "grep -r foo /workspace",
        "ls /",
        "ls -la /",
        "grep foo /etc/hosts",
        'find . -name "*.py"',
        "cd / && ls",
        'echo "find / is slow"',
        "find /usr -name x",
        'python3 - <<EOF\nimport os\nprint(os.listdir("/"))\nEOF',
        'grep -rn "/" src',  # "/" is the pattern, not the path
        'rg "/" .',
        "find / -maxdepth 1",
        "find / -maxdepth 2 -name x",
    ],
)
def test_scoped_commands_are_allowed(command):
    assert whole_filesystem_scan(command) is None


@pytest.mark.parametrize("tool_type", [ExecTool, SbotExecTool])
async def test_exec_refuses_without_running(tool_type):
    class NeverSandbox:
        async def run(self, command, workspace):
            raise AssertionError("must not run")

    result = await tool_type(NeverSandbox(), Path("/tmp")).execute(command="find / -name video.mp4")
    assert result.startswith("Error: whole-filesystem scan blocked")
    assert "/workspace" in result
