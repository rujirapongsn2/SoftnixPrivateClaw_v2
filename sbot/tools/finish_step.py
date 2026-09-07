"""Explicit mission completion and validated, immutable file delivery."""

import zipfile
from pathlib import Path
from typing import ClassVar
from xml.etree import ElementTree

from sbot.tools.artifacts import PublishArtifactTool
from sbot.tools.base import Tool


class FinishStepTool(Tool):
    name = "finish_step"
    ends_turn_on_success = True
    description = (
        "Record this mission step result. Required before ending your reply. "
        "Use blocked or failed if unable to meet the instruction. For file tasks, "
        "list every requested deliverable. Only use completed after checking the acceptance criteria."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "summary": {"type": "string"},
            "evidence": {
                "type": "string",
                "description": "What was checked, results, and remaining limitations.",
            },
            "files": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "summary", "evidence", "files"],
    }

    def __init__(self, workspace: Path, required_files: list[str] | None = None):
        self.publisher = PublishArtifactTool(workspace)
        self.required_files = required_files or []
        self.result = None

    async def execute(self, status, summary, evidence, files, **kwargs):
        self.result = None
        if status not in {"completed", "blocked", "failed"}:
            return "Error: invalid completion status."
        if not summary.strip() or not evidence.strip():
            return "Error: provide a summary and concrete acceptance evidence."
        artifacts = []
        if status == "completed":
            submitted = {self.publisher._resolve(raw) for raw in files}
            if any(self.publisher._resolve(raw) not in submitted for raw in self.required_files):
                return "Error: files must include every required deliverable: " + ", ".join(
                    self.required_files
                )
            for raw in files:
                path = self.publisher._resolve(raw)
                if not path.is_file() or not path.stat().st_size:
                    return f"Error: missing or empty deliverable: {raw}"
                if path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
                    expected = {
                        ".docx": "word/document.xml",
                        ".xlsx": "xl/workbook.xml",
                        ".pptx": "ppt/presentation.xml",
                    }[path.suffix.lower()]
                    try:
                        with zipfile.ZipFile(path) as package:
                            if sum(entry.file_size for entry in package.infolist()) > 200 * 1024 * 1024:
                                return f"Error: Office file expands beyond the validation limit: {raw}"
                            if expected not in package.namelist() or package.testzip():
                                return f"Error: invalid Office file: {raw}"
                            if package.getinfo(expected).file_size > 8 * 1024 * 1024:
                                return f"Error: Office document XML exceeds the validation limit: {raw}"
                            root = ElementTree.fromstring(package.read(expected))
                            expected_root = {
                                ".docx": "document",
                                ".xlsx": "workbook",
                                ".pptx": "presentation",
                            }[path.suffix.lower()]
                            if root.tag.rsplit("}", 1)[-1] != expected_root:
                                return f"Error: invalid Office document structure: {raw}"
                    except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
                        return f"Error: invalid Office file: {raw}"
            # Validate the entire set before publishing any file.
            for raw in files:

                def collect(event):
                    artifacts.append(event["path"])

                answer = await self.publisher.execute(raw, progress=collect)
                if answer.startswith("Error"):
                    return answer
        self.result = {"status": status, "summary": summary, "evidence": evidence, "artifacts": artifacts}
        return "Step result recorded."
