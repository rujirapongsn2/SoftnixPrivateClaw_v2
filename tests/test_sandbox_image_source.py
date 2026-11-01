"""Pulling the prebuilt sandbox image: the tag rule, the pull-probe-tag order,
and the CI workflow and installers that depend on them."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from claw.config import SandboxSettings, Settings
from claw.sandbox import image_source
from claw.sandbox.capabilities import Readiness

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "sandbox-image.yml"


def test_tag_is_derived_from_the_dockerfile_bytes(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\n")
    first = image_source.dockerfile_tag(dockerfile)
    assert re.fullmatch(r"df-[0-9a-f]{12}", first)
    assert first == "df-" + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    assert image_source.dockerfile_tag(dockerfile) == first
    dockerfile.write_text("FROM python:3.12-slim\nRUN true\n")
    assert image_source.dockerfile_tag(dockerfile) != first


def test_the_shell_rule_in_ci_gives_the_same_tag():
    """CI computes the tag with sha256sum; it must name what hosts pull."""
    tool = ["sha256sum"] if shutil.which("sha256sum") else ["shasum", "-a", "256"]
    if shutil.which(tool[0]) is None:
        pytest.skip("no sha256sum/shasum on this host")
    digest = subprocess.run([*tool, str(image_source.DOCKERFILE)], capture_output=True, text=True, check=True)
    assert "df-" + digest.stdout[: image_source.TAG_HEX_CHARS] == image_source.dockerfile_tag()
    workflow = WORKFLOW.read_text()
    assert f"sha256sum docker/sandbox.Dockerfile | cut -c1-{image_source.TAG_HEX_CHARS}" in workflow
    assert 'TAG="df-$(' in workflow


def test_the_dockerfile_copies_nothing_from_the_repo():
    """The tag only names the image if the Dockerfile alone decides its contents."""
    dockerfile = image_source.DOCKERFILE.read_text()
    assert not re.search(r"^\s*(COPY|ADD)\s", dockerfile, re.MULTILINE)


@pytest.mark.parametrize(
    "registry",
    ["registry.softnix.co.th/privateclaw", "https://registry.softnix.co.th/privateclaw/", " registry.softnix.co.th/privateclaw "],
)
def test_remote_ref_normalizes_the_registry(registry):
    assert image_source.remote_ref(registry) == (
        f"registry.softnix.co.th/privateclaw/claw-sandbox:{image_source.dockerfile_tag()}"
    )


class _Docker:
    def __init__(self, pull_code=0, tag_code=0):
        self.calls = []
        self.codes = {"pull": pull_code, "tag": tag_code, "rmi": 0}

    def __call__(self, *args, timeout):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.codes[args[0]])


def _fake(monkeypatch, docker, readiness):
    probed = []

    async def fake_check(settings, workspaces_root=None):
        probed.append(settings.image)
        return readiness

    monkeypatch.setattr(image_source, "_docker", docker)
    monkeypatch.setattr("claw.sandbox.capabilities.check_image", fake_check)
    return probed


def test_without_a_registry_nothing_is_pulled(monkeypatch):
    docker = _Docker()
    _fake(monkeypatch, docker, Readiness())
    assert image_source.pull("", "claw-sandbox:latest", SandboxSettings()) == image_source.EXIT_NO_REGISTRY
    assert docker.calls == []


def test_a_failed_pull_leaves_the_local_image_alone(monkeypatch):
    docker = _Docker(pull_code=1)
    probed = _fake(monkeypatch, docker, Readiness())
    code = image_source.pull("registry.softnix.co.th/privateclaw", "claw-sandbox:latest", SandboxSettings())
    assert code == image_source.EXIT_PULL_FAILED
    assert [c[0] for c in docker.calls] == ["pull"] and probed == []


def test_a_pulled_image_that_is_not_ready_is_never_tagged(monkeypatch):
    docker = _Docker()
    probed = _fake(monkeypatch, docker, Readiness(missing={"module": ["cv2"]}))
    code = image_source.pull("registry.softnix.co.th/privateclaw", "claw-sandbox:latest", SandboxSettings())
    assert code == image_source.EXIT_NOT_READY
    assert [c[0] for c in docker.calls] == ["pull"]
    assert probed == [image_source.remote_ref("registry.softnix.co.th/privateclaw")]


def test_a_verified_image_takes_the_local_name(monkeypatch):
    docker = _Docker()
    probed = _fake(monkeypatch, docker, Readiness())
    ref = image_source.remote_ref("registry.softnix.co.th/privateclaw")
    code = image_source.pull("registry.softnix.co.th/privateclaw", "claw-sandbox:latest", SandboxSettings())
    assert code == image_source.EXIT_PULLED
    # The registry name is dropped so the superseded image can be pruned later.
    assert docker.calls == [("pull", ref), ("tag", ref, "claw-sandbox:latest"), ("rmi", ref)]
    assert probed == [ref]


def test_a_broken_env_falls_back_to_building_instead_of_crashing(monkeypatch):
    def broken():
        raise ValueError("CLAW_SANDBOX__CPU_LIMIT is not a number")

    monkeypatch.setattr("claw.config.load_settings", broken)
    assert image_source.main(["pull"]) == image_source.EXIT_PULL_FAILED


def test_registry_setting_defaults_to_building_locally(monkeypatch):
    monkeypatch.delenv("CLAW_SANDBOX__REGISTRY", raising=False)
    assert Settings(_env_file=None).sandbox.registry == ""
    monkeypatch.setenv("CLAW_SANDBOX__REGISTRY", "registry.softnix.co.th/privateclaw")
    assert Settings(_env_file=None).sandbox.registry == "registry.softnix.co.th/privateclaw"


# ---- the workflow ----------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_runs_when_the_dockerfile_changes(workflow):
    triggers = workflow[True]  # PyYAML reads the bare `on:` key as True
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == ["docker/sandbox.Dockerfile"]
    assert "workflow_dispatch" in triggers


def test_no_job_output_carries_the_registry_secret(workflow):
    # GitHub drops job outputs that contain a secret, so the next job would see
    # an empty registry and log in to Docker Hub (seen on the first real run).
    text = WORKFLOW.read_text()
    assert "outputs.registry" not in text
    assert "registry" not in workflow["jobs"]["build"]["outputs"]
    for name in ("test", "promote"):
        runs = " ".join(str(s.get("env", "")) + s.get("run", "") for s in workflow["jobs"][name]["steps"])
        assert "env.REGISTRY_HOST" in runs, name


def test_only_main_can_publish(workflow):
    jobs = workflow["jobs"]
    assert jobs["build"]["if"] == "github.ref == 'refs/heads/main'"
    # test and promote run only after build, so they inherit the restriction.
    assert jobs["test"]["needs"] == "build" and "build" in jobs["promote"]["needs"]


def test_the_build_cache_stays_small(workflow):
    push = next(s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "push")
    assert push["with"]["cache-to"] == "type=gha,mode=min"


def test_every_job_isolates_docker_credentials_first(workflow):
    for name, job in workflow["jobs"].items():
        first = job["steps"][0]
        assert "DOCKER_CONFIG=$RUNNER_TEMP/.docker" in first["run"], name


def test_registry_and_path_match_what_hosts_pull(workflow):
    env = workflow["env"]
    assert env["IMAGE_PATH"] == f"privateclaw/{image_source.REPOSITORY}"
    example = (REPO / ".env.example").read_text()
    assert f"CLAW_SANDBOX__REGISTRY={env['REGISTRY_HOST']}/privateclaw" in example


def test_the_tested_digest_is_what_gets_promoted(workflow):
    jobs = workflow["jobs"]
    build = {s.get("id"): s for s in jobs["build"]["steps"]}
    assert set(build["push"]["with"]["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}
    assert build["push"]["with"]["tags"].endswith("-beta")
    test_runs = " ".join(s.get("run", "") for s in jobs["test"]["steps"])
    assert "@${{ needs.build.outputs.digest }}" in test_runs
    assert "python -m claw.sandbox.capabilities" in test_runs
    assert "tests/test_sandbox_offline_image.py" in test_runs
    test_env = next(s["env"] for s in jobs["test"]["steps"] if "pytest" in s.get("run", ""))
    assert test_env["CLAW_TEST_DOCKER"] == "1"
    promote = " ".join(s.get("run", "") for s in jobs["promote"]["steps"])
    assert "imagetools create" in promote and "@${{ needs.build.outputs.digest }}" in promote
    assert not any("build-push-action" in s.get("uses", "") for s in jobs["promote"]["steps"])
    assert jobs["promote"]["needs"] == ["build", "test"]


def test_secrets_never_reach_the_log(workflow):
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "echo \"$HARBOR_PASSWORD" not in run and "echo $HARBOR_PASSWORD" not in run
            if "HARBOR_PASSWORD" in run:
                assert "--password-stdin" in run


# ---- the installers ----------------------------------------------------------------


def test_update_pulls_before_it_builds():
    update = (REPO / "scripts" / "claw").read_text()
    body = update[update.index("update_sandbox_image() {"):update.index("cmd_update() {")]
    assert body.index("python -m claw.sandbox.image_source pull") < body.index("if ! docker build -f docker/sandbox.Dockerfile")


def test_install_pulls_before_it_builds_and_records_the_registry():
    install = (REPO / "install.sh").read_text()
    step = install[install.index("# 3) Sandbox image"):install.index("# 4) Postgres container")]
    assert step.index("elif sandbox_image_pull") < step.index("docker build -f docker/sandbox.Dockerfile")
    assert "--sandbox-registry) SANDBOX_REGISTRY=" in install
    assert "CLAW_SANDBOX__REGISTRY=${SANDBOX_REGISTRY}" in install


def _record_registry(tmp_path, env_text, registry):
    install = (REPO / "install.sh").read_text()
    start = install.index('  if [[ -n "$SANDBOX_REGISTRY" ]]; then\n    if ! grep')
    block = install[start:install.index("\n  fi\n", start) + 6]
    (tmp_path / ".env").write_text(env_text)
    script = 'ok() { echo "ok $*"; }; warn() { echo "warn $*"; }\n' + block
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, env={"SANDBOX_REGISTRY": registry, "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True, check=True).stdout
    return (tmp_path / ".env").read_text(), out


def test_install_records_the_registry_in_an_existing_env(tmp_path):
    env, out = _record_registry(tmp_path, "CLAW_PORT=8700\n", "registry.softnix.co.th/privateclaw")
    assert env.startswith("CLAW_PORT=8700\n")
    assert env.endswith("CLAW_SANDBOX__REGISTRY=registry.softnix.co.th/privateclaw\n")
    assert out.startswith("ok ")


def test_install_never_rewrites_a_registry_already_in_env(tmp_path):
    before = "CLAW_SANDBOX__REGISTRY=other.example/x\n"
    env, out = _record_registry(tmp_path, before, "registry.softnix.co.th/privateclaw")
    assert env == before and out.startswith("warn ")
    env, out = _record_registry(tmp_path, "CLAW_SANDBOX__REGISTRY=registry.softnix.co.th/privateclaw\n",
                                "registry.softnix.co.th/privateclaw")
    assert env == "CLAW_SANDBOX__REGISTRY=registry.softnix.co.th/privateclaw\n" and out == ""


def test_install_leaves_env_alone_without_the_flag(tmp_path):
    env, out = _record_registry(tmp_path, "CLAW_PORT=8700\n", "")
    assert env == "CLAW_PORT=8700\n" and out == ""
