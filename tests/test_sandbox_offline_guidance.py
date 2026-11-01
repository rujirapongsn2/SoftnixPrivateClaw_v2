"""Offline sandbox readiness and agent guidance, without docker.

The probe script itself runs here against the host's python, so the
categories it reports are checked on real lookups, not on a stub.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claw.config import LLMSettings, SandboxSettings, Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.sandbox import capabilities, ephemeral
from claw.sandbox.capabilities import CAPABILITIES, OFFLINE_PROMPT, Readiness, offline_guidance, parse_probe
from claw.tools.install_guard import offline_blocked_message
from claw.tools.shell import ExecTool
from sbot.tools.shell import ExecTool as SbotExecTool
from tests.conftest import FakeProvider, text_turn

REPO = Path(__file__).resolve().parents[1]
OFFLINE = SandboxSettings(enabled=True, network="none")
ONLINE = SandboxSettings(enabled=True, network="bridge")


def _run_probe(spec: dict, pythonpath: Path | None = None) -> Readiness:
    env = {**os.environ, "PYTHONPATH": str(pythonpath)} if pythonpath else None
    proc = subprocess.run(
        [sys.executable, "-c", capabilities._PROBE, json.dumps(spec)], capture_output=True, check=False, env=env
    )
    result = ephemeral.SandboxResult(proc.returncode, proc.stdout.decode(), proc.stderr.decode())
    return parse_probe(result, "host-python")


# ---- the probe ---------------------------------------------------------------


def test_probe_names_each_kind_of_gap_separately(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _run_probe({
            "capabilities": [{
                "modules": ["json", "no_such_module_offline"],
                "executables": ["sh", "no-such-cli-offline"],
                "data": [str(tmp_path / "missing" / "*.traineddata")],
                "module_data": [["json", "no-models/*.onnx"]],
            }],
            "writable": [str(tmp_path / "ok"), str(locked / "sub") if os.geteuid() else "/proc/claw-probe"],
        })
    finally:
        locked.chmod(0o700)
    assert not result.ok
    assert result.missing["module"] == ["no_such_module_offline"]
    assert result.missing["executable"] == ["no-such-cli-offline"]
    assert result.missing["data"] == [str(tmp_path / "missing" / "*.traineddata"), "json/no-models/*.onnx"]
    assert len(result.missing["not_writable"]) == 1
    text = result.describe()
    for label in ("Python packages missing", "executables missing", "models/data files missing", "not writable"):
        assert label in text


def test_probe_imports_modules_so_a_broken_package_is_reported(tmp_path):
    (tmp_path / "broken_native_offline.py").write_text("raise ImportError('libpango-1.0.so.0: cannot open')\n")
    result = _run_probe(
        {"capabilities": [{"modules": ["broken_native_offline", "json"], "executables": [], "data": [],
                           "module_data": []}], "writable": []},
        pythonpath=tmp_path,
    )
    assert result.missing["module"] == []
    assert result.missing["import_failed"] == ["broken_native_offline (ImportError: libpango-1.0.so.0: cannot open)"]
    assert "Python packages that fail to import" in result.describe()


def test_probe_passes_when_everything_is_there(tmp_path):
    result = _run_probe({
        "capabilities": [{"modules": ["json"], "executables": ["sh"], "data": [sys.executable],
                          "module_data": [["json", "__init__.py"]]}],
        "writable": [str(tmp_path)],
    })
    assert result.ok and result.describe() == ""


def test_probe_reports_each_missing_item_once():
    result = _run_probe({
        "capabilities": [{"modules": ["gone_mod"], "executables": [], "data": [], "module_data": []}] * 2,
        "writable": [],
    })
    assert result.missing["module"] == ["gone_mod"]


@pytest.mark.parametrize(
    ("result", "expected", "rebuild"),
    [
        (ephemeral.SandboxResult(125, "", "docker: No such image: x\n", infrastructure_error=True),
         "is missing (docker: No such image: x)", True),
        (ephemeral.SandboxResult(127, "", "python: not found\n"), "has no usable python", True),
        (ephemeral.SandboxResult(125, "", "docker: invalid mount config\n", infrastructure_error=True),
         "docker could not start", False),
        (ephemeral.SandboxResult(-1, "", "No such file: docker", infrastructure_error=True),
         "docker is unavailable", False),
        (ephemeral.SandboxResult(-9, "", "", timed_out=True), "timed out", False),
        (ephemeral.SandboxResult(1, "Traceback...", "boom\n"), "the readiness probe failed", False),
    ],
)
def test_a_probe_that_cannot_run_says_whether_a_rebuild_helps(result, expected, rebuild):
    readiness = parse_probe(result, "img")
    assert not readiness.ok and expected in readiness.describe()
    assert readiness.needs_rebuild is rebuild


def test_only_missing_image_contents_call_for_a_rebuild():
    assert Readiness(missing={"module": ["cv2"]}).needs_rebuild
    assert Readiness(missing={"import_failed": ["weasyprint (OSError: libpango)"]}).needs_rebuild
    assert Readiness(missing={"data": ["tha.traineddata"]}).needs_rebuild
    unwritable = Readiness(missing={"not_writable": ["/workspace"]})
    assert not unwritable.ok and not unwritable.needs_rebuild


@pytest.mark.parametrize(
    ("readiness", "code"),
    [
        (Readiness(), capabilities.EXIT_READY),
        (Readiness(missing={"executable": ["pdftoppm"]}), capabilities.EXIT_REBUILD),
        (Readiness(missing={"not_writable": ["/workspace"]}), capabilities.EXIT_ENVIRONMENT),
        (Readiness(error="docker is unavailable"), capabilities.EXIT_ENVIRONMENT),
    ],
)
def test_cli_exit_code_tells_the_updater_whether_to_rebuild(monkeypatch, tmp_path, readiness, code):
    async def fake_check(settings, workspaces_root=None):
        return readiness

    monkeypatch.setenv("CLAW_WORKSPACES_ROOT", str(tmp_path))
    monkeypatch.setattr(capabilities, "check_image", fake_check)
    assert capabilities.main(["img"]) == code


def test_cli_with_a_broken_config_does_not_ask_for_a_rebuild(monkeypatch):
    def boom():
        raise ValueError("bad .env")

    monkeypatch.setattr("claw.config.load_settings", boom)
    assert capabilities.main(["img"]) == capabilities.EXIT_ENVIRONMENT


@pytest.mark.parametrize(
    ("readiness", "advice"),
    [
        (Readiness(missing={"module": ["cv2"]}), "rebuild the image: docker build"),
        (Readiness(missing={"not_writable": ["/workspace"]}), "Rebuilding the image will not fix this"),
        (Readiness(error="docker is unavailable (x)"), "Rebuilding the image will not fix this"),
    ],
)
def test_startup_warning_advises_a_rebuild_only_when_it_helps(monkeypatch, readiness, advice):
    async def fake_check(settings, workspaces_root=None):
        return readiness

    monkeypatch.setattr(capabilities, "check_image", fake_check)
    problem = asyncio.run(ephemeral.image_readiness_problem(OFFLINE))
    assert advice in problem


def test_update_rebuilds_only_on_the_rebuild_exit_code():
    update = (REPO / "scripts" / "claw").read_text()
    assert "uv run --no-sync --quiet python -m claw.sandbox.capabilities" in update
    assert "[[ $rc -eq 3 ]]" in update and "not rebuilding" in update
    assert "uv run --no-sync --quiet python -m claw.sandbox.capabilities" in (REPO / "install.sh").read_text()


def test_the_probe_covers_the_offline_stack_the_agent_is_promised():
    modules = {m for c in CAPABILITIES for m in c.modules}
    executables = {e for c in CAPABILITIES for e in c.executables}
    data = " ".join(d for c in CAPABILITIES for d in c.data) + " " + " ".join(
        f"{m}/{rel}" for c in CAPABILITIES for m, rel in c.module_data
    )
    assert {"pytesseract", "rapidocr_onnxruntime", "cv2", "uharfbuzz", "pythainlp", "cairosvg", "fitz",
            "openpyxl", "docx", "pptx", "matplotlib"} <= modules
    assert {"tesseract", "pdftoppm", "pdftotext", "rsvg-convert", "ffmpeg"} <= executables
    for needed in ("tha.traineddata", "eng.traineddata", "rapidocr_onnxruntime/models/*.onnx", "Garuda.ttf"):
        assert needed in data


def test_check_image_runs_under_exec_confinement(monkeypatch):
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self):
            empty = {kind: [] for kind in ("module", "executable", "data", "not_writable")}
            return json.dumps(empty).encode(), b""

    async def fake(*argv, **_kw):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(capabilities.asyncio, "create_subprocess_exec", fake)
    settings = SandboxSettings(network="none", image="claw-sandbox:test", memory_limit="1g", cpu_limit=1.0)
    assert asyncio.run(capabilities.check_image(settings)).ok
    argv = calls[0]
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--user", "--pids-limit"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "1g"
    assert any(a.startswith("type=bind,") and a.endswith("target=/workspace") for a in argv)


def test_dockerfile_installs_headless_opencv_only():
    dockerfile = (REPO / "docker" / "sandbox.Dockerfile").read_text()
    assert "opencv-python-headless" in dockerfile
    assert '"opencv-python>' not in dockerfile and '"opencv-python=' not in dockerfile
    assert '--no-deps "rapidocr-onnxruntime' in dockerfile
    assert "poppler-utils" in dockerfile and "librsvg2-bin" in dockerfile
    assert "playwright" not in dockerfile.lower()


def test_every_chained_dockerfile_step_stops_at_the_first_failure():
    """A `;` chain without `set -e` lets a failed pip install or model check
    exit 0, and the image builds without the capability."""
    dockerfile = (REPO / "docker" / "sandbox.Dockerfile").read_text()
    steps, current = [], None
    for line in dockerfile.splitlines():
        if current is None and line.startswith("RUN "):
            current = line[4:]
        elif current is not None:
            current += " " + line
        if current is not None and not line.endswith("\\"):
            steps.append(current)
            current = None
    assert len(steps) >= 4
    for step in steps:
        shell = step.split('python -c "', 1)[0]
        if ";" in shell:
            assert step.lstrip().startswith("set -e;"), step[:80]
    for pinned in ('"onnxruntime>=', '"tqdm>=', '"xlsxwriter>='):
        assert pinned in dockerfile


def test_installers_use_the_shared_probe():
    for script in ("scripts/claw", "install.sh"):
        text = (REPO / script).read_text()
        assert "python -m claw.sandbox.capabilities" in text
        assert "SANDBOX_PROBE=" not in text


# ---- the offline install guard -----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pip install yfinance",
        "cd /tmp && pip3 install --user rapidocr",
        "python -m pip install -U pythainlp",
        "python3 -m pip download torch",
        "sudo apt-get install -y poppler-utils",
        "apt update",
        "npm i puppeteer",
        "uv add httpx",
        "uv pip install requests",
        "playwright install chromium",
        "python -m playwright install --with-deps",
        "git clone https://github.com/example/repo /tmp/repo",
        "env PIP_INDEX_URL=x pip install foo",
        "conda install -y numpy",
        "pip3.12 install foo",
        "git -C /tmp clone https://github.com/example/repo",
        "git -c http.sslVerify=false clone git@github.com:example/repo.git",
        "uvx ruff check .",
        "uv tool install ruff",
        "cd /workspace && git clone https://github.com/example/repo",
        "npm install left-pad",
        'bash -c "pip install x"',
        "sh -c 'cd /tmp && npm i x'",
        "npx cowsay hi",
        "uv run --with requests x.py",
        "conda env create -f env.yml",
        "npm --prefix /tmp install x",
        "apt-get -o Debug::pkgProblemResolver=1 install x",
        "git clone example.com:team/repo.git",
        "echo requests | xargs pip install",
        "xargs -n 1 pip3 install < reqs.txt",
        "pip install --index-url https://pypi.org/simple x",
        "pip install --index-url file:///workspace/wheels --extra-index-url https://pypi.org/simple x",
    ],
)
def test_network_installs_are_refused_offline(command):
    message = offline_blocked_message(command)
    assert message is not None and "network=none" in message


@pytest.mark.parametrize(
    "command",
    [
        "pip install --no-index /workspace/wheels/foo.whl",
        "pip list",
        "pip show numpy",
        "git clone /workspace/project /tmp/copy",
        "apt list --installed",
        "npm run build",
        "echo 'pip install x'",
        "python -c 'import cv2; print(cv2.__version__)'",
        "tesseract in.png out -l tha+eng",
        "uv tool list",
        "uv sync --offline",
        "git -C /workspace/project log --oneline",
        "pip3.12 list",
        "pip show install",
        "pip3 install --index-url file:///workspace/wheels pkg",
        "pip install -i /workspace/wheels pkg",
        'bash -c "python report.py"',
        "uv run script.py",
        "conda env list",
        "npm --prefix /tmp run build",
        "apt-get -o Debug=1 check",
        "git clone ./repo /tmp/copy",
        "ls *.txt | xargs wc -l",
    ],
)
def test_offline_work_is_not_refused(command):
    assert offline_blocked_message(command) is None


class _RecordingSandbox:
    def __init__(self, settings):
        self.settings = settings
        self.commands = []

    async def run(self, command, workspace):
        self.commands.append(command)
        return ephemeral.SandboxResult(0, "ran", "")


@pytest.mark.parametrize("tool_cls", [ExecTool, SbotExecTool])
def test_exec_tool_refuses_installs_offline_before_reaching_the_sandbox(tool_cls, tmp_path):
    sandbox = _RecordingSandbox(OFFLINE)
    tool = tool_cls(sandbox, tmp_path)
    result = asyncio.run(tool.execute("pip install pytesseract"))
    assert "network=none" in str(result)
    assert sandbox.commands == []
    assert "no internet access" in tool.description
    for capability in CAPABILITIES:
        assert capability.offers in tool.description
    assert "git clone ... &&" not in tool.description


@pytest.mark.parametrize("tool_cls", [ExecTool, SbotExecTool])
@pytest.mark.parametrize("command", ["cd /workspace && git clone https://github.com/x/y", "npm install left-pad"])
def test_offline_refusal_wins_over_install_in_tmp_advice(tool_cls, command, tmp_path):
    result = str(asyncio.run(tool_cls(_RecordingSandbox(OFFLINE), tmp_path).execute(command)))
    assert "network=none" in result and "/tmp instead" not in result


def test_online_description_keeps_tmp_advice_and_offline_drops_it():
    assert "cd /tmp && git clone" in ExecTool.description
    assert "cd /tmp && git clone" not in ExecTool.offline_description
    shared = ExecTool.description.split(" /workspace is the user's")[0]
    assert ExecTool.offline_description.startswith(shared)


@pytest.mark.parametrize("tool_cls", [ExecTool, SbotExecTool])
def test_exec_tool_with_network_keeps_its_old_behavior(tool_cls, tmp_path):
    sandbox = _RecordingSandbox(ONLINE)
    tool = tool_cls(sandbox, tmp_path)
    asyncio.run(tool.execute("cd /tmp && pip install requests"))
    assert sandbox.commands == ["cd /tmp && pip install requests"]
    assert tool.description == tool_cls.description
    assert "no internet access" not in tool.description


def test_offline_guidance_points_away_from_installs_and_to_host_tools():
    text = offline_guidance()
    assert "no internet access" in text and "never try them as a fix" in text
    assert "web, search or connector tools" in text
    assert "not available offline" in OFFLINE_PROMPT


# ---- the system prompt ---------------------------------------------------------


def _claw_runtime(stores, provider, tmp_path, sandbox):
    from claw.core.runtime import AgentRuntime

    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=sandbox,
        llm=LLMSettings(),
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(), users=stores["users"], sessions=stores["sessions"],
        messages=stores["messages"], memory=memory, audit=stores["audit"],
    )


@pytest.mark.parametrize(("sandbox", "offline"), [(OFFLINE, True), (ONLINE, False)])
async def test_system_prompt_says_when_the_sandbox_is_offline(sandbox, offline, stores, tmp_path):
    provider = FakeProvider([text_turn("ok")])
    runtime = _claw_runtime(stores, provider, tmp_path, sandbox)
    user = await stores["users"].get_or_create_by_email("offline-guidance@example.test")
    session = await stores["sessions"].create(user.id)
    await runtime.handle_message(user.id, session.id, "hello")
    prompt = provider.calls[-1][0]["content"]
    assert (OFFLINE_PROMPT in prompt) is offline
    assert ("cd /tmp && git clone" in prompt) is not offline


def test_probe_workspace_is_made_where_docker_can_mount_it(monkeypatch, tmp_path):
    """Under docker-compose the app's own temp dir does not exist on the host;
    the workspaces root is mounted at the same path on both sides."""
    sources = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps({}).encode(), b""

    async def fake(*argv, **_kw):
        mount = next(a for a in argv if a.startswith("type=bind,"))
        source = Path(mount.split("source=", 1)[1].split(",", 1)[0])
        sources.append((source, source.is_dir(), source.stat().st_mode & 0o777))
        return Proc()

    monkeypatch.setattr(capabilities.asyncio, "create_subprocess_exec", fake)
    root = tmp_path / "workspaces"
    root.mkdir()
    asyncio.run(capabilities.check_image(SandboxSettings(network="none"), root))
    asyncio.run(capabilities.check_image(SandboxSettings(network="none"), tmp_path / "missing"))
    (inside, existed, mode), (fallback, _, _) = sources
    holder = inside.parent
    assert holder.parent == root.resolve() and holder.name.startswith(".claw-sandbox-probe-") and existed
    # Made like a user workspace (plain mkdir under the umask), not a 0700 temp dir.
    umask = os.umask(0)
    os.umask(umask)
    assert mode == 0o777 & ~umask
    assert not holder.exists()
    assert root.resolve() not in fallback.parents


def test_cli_probes_with_the_configured_sandbox(monkeypatch, tmp_path):
    seen = {}

    async def fake_check(settings, workspaces_root=None):
        seen.update(settings=settings, root=workspaces_root)
        return Readiness()

    monkeypatch.setenv("CLAW_SANDBOX__USER", "1001:1001")
    monkeypatch.setenv("CLAW_SANDBOX__MEMORY_LIMIT", "512m")
    monkeypatch.setenv("CLAW_SANDBOX__NETWORK", "bridge")
    monkeypatch.setenv("CLAW_WORKSPACES_ROOT", str(tmp_path))
    monkeypatch.setattr(capabilities, "check_image", fake_check)
    assert capabilities.main(["claw-sandbox:candidate"]) == 0
    settings = seen["settings"]
    assert (settings.user, settings.memory_limit, settings.network, settings.image) == (
        "1001:1001", "512m", "none", "claw-sandbox:candidate"
    )
    assert seen["root"] == tmp_path
