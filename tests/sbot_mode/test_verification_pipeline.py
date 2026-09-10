import hashlib
import pytest

from sbot.config import ReliabilitySettings, SandboxSettings
from sbot.core.assignment_workspace import isolate_runner, rebase_result
from sbot.core.delivery import LocalDelivery
from sbot.core.specialist import SpecialistRunner
from sbot.sandbox.ephemeral import EphemeralSandbox
from sbot.tools.finish_step import FinishStepTool


class Reviewer:
    cost = {"tokens": 7}

    def __init__(self, status):
        self.status = status

    async def review_task(self, directory, checks, summary):
        return [{"kind": "research_review", "status": self.status}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,status,expected",
    [
        ("enforce", "failed", "partial"),
        ("enforce", "not_verified", "partial"),
        ("enforce", "passed", "completed"),
        ("shadow", "failed", "completed"),
    ],
)
async def test_verification_modes(mode, status, expected, tmp_path):
    tool = FinishStepTool(tmp_path, verifier=Reviewer(status), verification_mode=mode)
    await tool.execute("completed", "Answer", "Claimed checks", [])
    assert tool.result["status"] == expected
    assert tool.result["verification_status"] == status
    assert tool.result["verification_cost"]["tokens"] == 7


@pytest.mark.asyncio
async def test_completion_replay_reuses_publication(tmp_path):
    (tmp_path / "report.txt").write_text("version one")
    tool = FinishStepTool(tmp_path, ["report.txt"])
    await tool.execute("completed", "Done", "Checked", ["report.txt"])
    first = tool.result["artifacts"]
    await tool.execute("completed", "Done", "Checked", ["report.txt"])
    assert tool.result["artifacts"] == first
    assert len(list((tmp_path / ".deliveries").glob("*/*"))) == 1


def test_isolated_workers_cannot_overwrite_each_other_or_sources(tmp_path):
    (tmp_path / "source.txt").write_text("original")
    sandbox = EphemeralSandbox(SandboxSettings(enabled=True))
    runner = SpecialistRunner(None, sandbox, tmp_path)
    a, manifest = isolate_runner(runner, ["source.txt"])
    b, _ = isolate_runner(runner, ["source.txt"])
    (a.workspace / "output.txt").write_text("A")
    (b.workspace / "output.txt").write_text("B")
    assert (a.workspace / "output.txt").read_text() == "A"
    assert (tmp_path / "source.txt").read_text() == "original"
    _, error = a.arg_guard("write_file", {"path": manifest["source.txt"]})
    assert error
    argv = a.sandbox._docker_argv("true", a.workspace)
    assert any("target=/workspace/inputs,readonly" in arg for arg in argv)
    result = rebase_result({"artifacts": ["output.txt"]}, a.workspace, tmp_path)
    assert (tmp_path / result["artifacts"][0]).read_text() == "A"


def test_isolation_fails_closed_without_sandbox(tmp_path):
    runner = SpecialistRunner(None, EphemeralSandbox(SandboxSettings(enabled=False)), tmp_path)
    with pytest.raises(ValueError, match="container"):
        isolate_runner(runner, [])


class Broker:
    def __init__(self, digest, fail=False):
        self.digest, self.fail = digest, fail
        self.calls = []

    def owned(self, owner, wid):
        assert owner == "owner"
        assert wid == "folder"

    async def call(self, owner, wid, payload):
        self.calls.append(payload["action"])
        if self.fail:
            raise ValueError("offline")
        return {"sha256": self.digest}


@pytest.mark.asyncio
async def test_local_receipt_prevents_repeat_write(tmp_path):
    digest = hashlib.sha256(b"data").hexdigest()
    broker = Broker(digest)
    stages, receipts = await LocalDelivery(broker, "owner", {"workspace_id": "folder"}).send(
        [{"path": "a.txt", "artifact": ".deliveries/a.txt", "sha256": digest}], tmp_path
    )
    assert stages["local"] == "completed"
    assert broker.calls == ["read"]
    assert receipts[0]["sha256"] == digest


@pytest.mark.asyncio
async def test_offline_local_delivery_stays_partial(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    tool = FinishStepTool(
        tmp_path,
        ["a.txt"],
        delivery=LocalDelivery(Broker("", fail=True), "owner", {"workspace_id": "folder"}),
    )
    await tool.execute("completed", "Done", "Checked", ["a.txt"])
    assert tool.result["status"] == "partial"
    assert tool.result["delivery"]["local"] == "not_confirmed"
    assert tool.result["artifacts"]


def test_pilot_scope():
    policy = ReliabilitySettings(verification_mode="shadow", pilot_owner_ids=["pilot"])
    assert policy.enabled_for("pilot")
    assert not policy.enabled_for("other")


@pytest.mark.asyncio
async def test_research_rejects_private_source_before_calling_model(tmp_path):
    from types import SimpleNamespace
    from sbot.core.deep_verification import TaskVerifier

    verifier = TaskVerifier(
        SimpleNamespace(),
        ReliabilitySettings(),
        SimpleNamespace(),
        "Research",
        specification={"kind": "research", "source_urls": ["https://127.0.0.1/private"]},
    )
    result = await verifier.review_task(tmp_path, [], "A claim")
    assert result[0]["status"] == "not_verified"
    assert result[0]["reason"] == "UnsafeUrlError"


@pytest.mark.asyncio
async def test_research_without_sources_cannot_pass(tmp_path):
    from types import SimpleNamespace
    from sbot.core.deep_verification import TaskVerifier

    verifier = TaskVerifier(
        SimpleNamespace(),
        ReliabilitySettings(),
        SimpleNamespace(),
        "Research",
        specification={"kind": "research"},
    )
    result = await verifier.review_task(tmp_path, [], "An unsupported answer")
    assert result[0]["status"] == "not_verified"
    assert result[0]["reason"] == "missing_source_urls"


@pytest.mark.asyncio
async def test_missing_renderer_is_not_a_pass(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sbot.core.deep_verification as module

    async def unavailable(*args, **kwargs):
        raise OSError("docker unavailable")

    monkeypatch.setattr(module, "run_process", unavailable)
    verifier = module.TaskVerifier(SimpleNamespace(), ReliabilitySettings(), SimpleNamespace(), "Render")
    result = await verifier.review(tmp_path / "document.docx", {"sha256": "digest"})
    assert result["status"] == "not_verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quote,status", [("Flights are suspended.", "passed"), ("Flights operate daily.", "not_verified")]
)
async def test_research_requires_evidence_from_fetched_source(tmp_path, monkeypatch, quote, status):
    import json
    from types import SimpleNamespace
    import httpx
    import sbot.tools.api as api
    from sbot.core.deep_verification import TaskVerifier
    from sbot.providers.base import ChatResult

    async def fetch(client, method, url, headers, body):
        return httpx.Response(200, text="<p>Flights are suspended.</p>", request=httpx.Request("GET", url))

    monkeypatch.setattr(api, "_send_pinned", fetch)

    class Provider:
        async def chat(self, messages, **kwargs):
            assert len(messages) == 2
            assert "secret-worker-memory" not in json.dumps(messages)
            return ChatResult(
                content=json.dumps(
                    {
                        "status": "passed",
                        "issues": [],
                        "claims": [
                            {
                                "claim": "No scheduled flights",
                                "source_url": "https://example.org/status",
                                "quote": quote,
                            }
                        ],
                    }
                )
            )

    async def resolve(model):
        return {"model": "judge", "api_key": None, "api_base": None}

    runner = SimpleNamespace(provider=Provider(), _resolve_model=resolve, memory="secret-worker-memory")
    verifier = TaskVerifier(
        runner,
        ReliabilitySettings(),
        SimpleNamespace(model=None),
        "Check flights",
        specification={"kind": "research", "source_urls": ["https://example.org/status"]},
    )
    result = await verifier.review_task(tmp_path, [], "No scheduled flights")
    assert result[0]["status"] == status
    if status == "passed":
        assert result[0]["claims"][0]["quote_offset"] == 0
        assert result[0]["sources"][0]["retrieved_at"]


@pytest.mark.asyncio
async def test_delivery_deadline_retains_published_partial_result(tmp_path):
    import asyncio
    import time

    class SlowDelivery:
        async def send(self, checks, workspace):
            await asyncio.sleep(1)

    (tmp_path / "a.txt").write_text("deliverable")
    tool = FinishStepTool(tmp_path, ["a.txt"], delivery=SlowDelivery())
    await tool.execute("completed", "Done", "Checked", ["a.txt"], deadline=time.monotonic() + 0.1)
    assert tool.result["status"] == "partial"
    assert tool.result["failure_reason"] == "completion_budget_exhausted"
    assert tool.result["artifacts"]
    assert tool.result["delivery"]["local"] == "not_confirmed"
