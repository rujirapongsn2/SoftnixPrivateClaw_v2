import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "project_worktree", Path(__file__).parents[2] / "scripts/project-worktree.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def project(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.txt").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def change(worker, text):
    (worker / "app.txt").write_text(text)
    git(worker, "add", ".")
    git(worker, "commit", "-m", text)


def test_worktree_promotes_only_tested_candidate(project):
    created = module.execute(project, "create", "api", [])
    worker = Path(created["path"])
    assert module.execute(project, "create", "api", [])["path"] == created["path"]
    change(worker, "new")
    assert (project / "app.txt").read_text() == "base"
    result = module.execute(
        project,
        "integrate",
        "api",
        [sys.executable, "-c", "from pathlib import Path; assert Path('app.txt').read_text() == 'new'"],
    )
    assert result["status"] == "integrated"
    assert git(project, "rev-parse", "HEAD") == result["commit"]
    assert (project / "app.txt").read_text() == "new"
    assert module.execute(project, "integrate", "api", ["true"])["status"] == "already_integrated"


def test_failed_test_keeps_main_unchanged(project):
    worker = Path(module.execute(project, "create", "api", [])["path"])
    base = git(project, "rev-parse", "HEAD")
    change(worker, "new")
    with pytest.raises(ValueError, match="tests failed"):
        module.execute(project, "integrate", "api", [sys.executable, "-c", "raise SystemExit(1)"])
    assert git(project, "rev-parse", "HEAD") == base
    assert (project / "app.txt").read_text() == "base"
    assert list((project / ".agent-worktrees").glob("integration-*"))


def test_conflict_does_not_promote(project):
    worker = Path(module.execute(project, "create", "api", [])["path"])
    change(worker, "worker")
    change(project, "main")
    base = git(project, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="retained"):
        module.execute(project, "integrate", "api", ["true"])
    assert git(project, "rev-parse", "HEAD") == base
    assert (project / "app.txt").read_text() == "main"


def test_no_implicit_test_or_unsafe_assignment(project):
    with pytest.raises(ValueError, match="slug"):
        module.execute(project, "create", "../escape", [])
    module.execute(project, "create", "api", [])
    with pytest.raises(ValueError, match="explicit test"):
        module.execute(project, "integrate", "api", [])
