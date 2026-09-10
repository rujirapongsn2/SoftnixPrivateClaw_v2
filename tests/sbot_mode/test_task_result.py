import hashlib
from types import SimpleNamespace

import pytest

from sbot.core.specialist import SpecialistOutcome
from sbot.core.task_result import TaskResult
from sbot.core.mission_engine import dependency_satisfied
from sbot.tools.finish_step import FinishStepTool


@pytest.mark.parametrize("text", ["", "  \n"])
def test_empty_result_is_failure_even_with_attachments(text):
    result = TaskResult.from_outcome(SpecialistOutcome(text=text, artifacts=["a.txt"]))
    assert result.status == "failed"
    assert result.failure_reason == "empty_output"
    assert result.verification_status == "not_verified"


@pytest.mark.parametrize(
    "flag,reason", [("timed_out", "timeout"), ("reached_max_iterations", "iteration_limit")]
)
def test_cutoff_keeps_partial_output(flag, reason):
    result = TaskResult.from_outcome(SpecialistOutcome(text="partial", **{flag: True}))
    assert result.status == "partial"
    assert result.failure_reason == reason
    assert result.scheduler_status == "error"


def test_only_recorded_completion_can_replace_closing_prose():
    result = TaskResult.from_outcome(
        SpecialistOutcome(text="", timed_out=True),
        {"status": "completed", "summary": "Delivered", "artifacts": ["a"], "checks": []},
    )
    assert result.status == "completed"
    assert result.verification_status == "not_verified"


def test_missing_completion_cannot_succeed_on_prose():
    result = TaskResult.from_outcome(SpecialistOutcome(text="All done!"), require_completion=True)
    assert result.status == "partial"
    assert result.failure_reason == "missing_completion_record"


def test_skipped_deliverable_does_not_satisfy_dependency():
    assert not dependency_satisfied(SimpleNamespace(status="skipped", budget={"required_files": ["a.docx"]}))
    assert dependency_satisfied(SimpleNamespace(status="skipped", budget={}))


@pytest.mark.asyncio
async def test_snapshot_hash_matches_publication_despite_source_edit(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("checked version")
    tool = FinishStepTool(tmp_path, ["report.txt"])
    publish = tool.publisher.execute

    async def change_source(path, **kwargs):
        source.write_text("changed after verification")
        return await publish(path, **kwargs)

    tool.publisher.execute = change_source
    assert await tool.execute("completed", "Report", "Reviewed", ["report.txt"]) == "Step result recorded."
    record = tool.result
    published = tmp_path / record["artifacts"][0]
    assert published.read_text() == "checked version"
    assert record["checks"][0]["sha256"] == hashlib.sha256(published.read_bytes()).hexdigest()
    assert record["verification_status"] == "not_verified"


@pytest.mark.asyncio
async def test_invalid_set_publishes_nothing(tmp_path):
    (tmp_path / "good.txt").write_text("good")
    (tmp_path / "bad.docx").write_text("not a document")
    tool = FinishStepTool(tmp_path, ["good.txt", "bad.docx"])
    assert (await tool.execute("completed", "Done", "Checked", ["good.txt", "bad.docx"])).startswith("Error")
    assert tool.result is None
    assert not (tmp_path / ".deliveries").exists()


@pytest.mark.asyncio
async def test_cross_workspace_deliverable_rejected(tmp_path):
    tool = FinishStepTool(tmp_path)
    assert (await tool.execute("completed", "Done", "Checked", ["../private.txt"])).startswith("Error")
    assert tool.result is None


@pytest.mark.asyncio
async def test_equivalent_paths_publish_once(tmp_path):
    (tmp_path / "a.txt").write_text("same file")
    tool = FinishStepTool(tmp_path)
    await tool.execute("completed", "Done", "Checked", ["a.txt", "./a.txt"])
    assert len(tool.result["artifacts"]) == 1
    assert len(tool.result["checks"]) == 1
