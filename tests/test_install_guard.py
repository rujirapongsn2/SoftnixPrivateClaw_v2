"""Installs and clones belong in the sandbox's /tmp, never in the persistent workspace."""

from pathlib import Path

import pytest

from claw.tools.install_guard import workspace_install
from claw.tools.shell import ExecTool
from sbot.tools.shell import ExecTool as SbotExecTool


@pytest.mark.parametrize(
    "command",
    [
        "git clone https://github.com/a/b.git",
        "git clone https://github.com/a/b.git repo",
        "git clone --depth 1 -b main https://x/y.git /workspace/y",
        "git clone git@github.com:a/b.git",
        "npm install",
        "npm i left-pad",
        "npm ci",
        "yarn add x",
        "pnpm install",
        "bun install",
        "cd /workspace/app && npm install",
        "sudo npm install",
        "pip install --target /workspace/libs pandas",
        "pip install -t libs x",
        "python3 -m pip install -t ./vendor requests",
        "python3 -m venv .venv",
        "virtualenv env",
        "uv venv",
    ],
)
def test_installs_into_the_workspace_are_refused(command):
    assert workspace_install(command)


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp && git clone https://github.com/a/b.git",
        "cd /tmp && git clone https://github.com/a/b.git && cd b && npm install && npm run build",
        "git clone https://github.com/a/b.git /tmp/b",
        "cd /tmp; mkdir x; cd x && npm install",
        "npm install -g typescript",
        "npm install --prefix /tmp/x",
        "cd /tmp/app && npm install",
        "npm run build",
        "npm test",
        "pip install pandas",
        "pip install --target /tmp/libs x",
        "python3 -m venv /tmp/v",
        "yarn --version",
        "pnpm -v",
        "bun --version",
        "yarn run build",
        "git status",
        "git pull",
        "ls",
        "echo 'git clone x'",
    ],
)
def test_tmp_installs_and_ordinary_commands_pass(command):
    assert workspace_install(command) is None


class _NeverSandbox:
    async def run(self, command, workspace):
        raise AssertionError("must not run")


async def test_exec_explains_where_to_install_instead():
    result = await ExecTool(_NeverSandbox(), Path("/tmp")).execute(command="git clone https://github.com/a/b.git")
    assert result.startswith("Error:") and "/tmp" in result and "same command" in result


async def test_sbot_projects_keep_installing_in_their_own_workspace():
    """Sbot's workspace is a project folder where an install is the point, so only the scan guard applies."""

    class Sandbox:
        async def run(self, command, workspace):
            from claw.sandbox.ephemeral import SandboxResult

            return SandboxResult(0, "ok", "")

    result = await SbotExecTool(Sandbox(), Path("/tmp")).execute(command="npm install")
    assert "[exit code: 0]" in result
