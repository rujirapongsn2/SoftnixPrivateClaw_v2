"""Opt-in Docker integration. The judge is fake; rendering/execution are real."""

import json
import os
from types import SimpleNamespace

import pytest

from sbot.config import ReliabilitySettings
from sbot.core.deep_verification import TaskVerifier
from sbot.core.verification import check_file
from sbot.providers.base import ChatResult

pytestmark = pytest.mark.skipif(
    os.environ.get("SBOT_TEST_DOCKER_VERIFIER") != "1", reason="opt-in Docker worker integration"
)


@pytest.mark.asyncio
async def test_real_docx_render_and_offline_tests(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Document verification", 0)
    document.add_paragraph("สวัสดีครับ / Hello")
    source = tmp_path / "report.docx"
    document.save(source)
    image_counts = []

    class Provider:
        async def chat(self, messages, **kwargs):
            assert len(messages) == 2
            if isinstance(messages[1]["content"], list):
                image_counts.append(sum(item.get("type") == "image_url" for item in messages[1]["content"]))
            else:
                assert "test_output.py" in messages[1]["content"]
            return ChatResult(content=json.dumps({"status": "passed", "issues": []}))

    async def resolve(model):
        return {"model": "fake-judge", "api_key": None, "api_base": None}

    runner = SimpleNamespace(provider=Provider(), _resolve_model=resolve)
    policy = ReliabilitySettings(verification_mode="shadow")
    verifier = TaskVerifier(runner, policy, SimpleNamespace(model=None), "Create a document")
    report = await verifier.review(source, check_file(source, source.name))
    assert report["status"] == "passed", report
    assert image_counts == [1]
    test = tmp_path / "test_output.py"
    test.write_text("def test_answer():\n    assert 2 + 2 == 4\n")
    verifier = TaskVerifier(
        runner,
        policy,
        SimpleNamespace(model=None),
        "Test code",
        specification={"kind": "code", "test_command": ["python", "-m", "pytest", "-q", test.name]},
    )
    reports = await verifier.review_task(tmp_path, [check_file(test, test.name)], "Code")
    assert reports[0]["status"] == "passed", reports
    test.write_text("def test_answer():\n    assert False\n")
    reports = await verifier.review_task(tmp_path, [check_file(test, test.name)], "Code")
    assert reports[0]["status"] == "failed", reports
