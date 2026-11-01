"""Fetch the sandbox image prebuilt from a registry instead of building it.

CI (.github/workflows/sandbox-image.yml) builds docker/sandbox.Dockerfile once,
tests that exact image offline, and publishes it under a tag derived from the
Dockerfile's content: `<registry>/claw-sandbox:df-<first 12 hex of sha256>`.
The Dockerfile copies nothing from the repo, so its bytes alone decide what
the image holds, and every checkout knows which tag matches its code without
a version file. An unchanged Dockerfile keeps its tag, so there is nothing new
to pull.

`claw update` and install.sh call `pull` before falling back to a local
`docker build`. The pulled image is probed under exec confinement before it
gets the local name the service runs (`--pull=never`), so a broken or wrong
image never replaces a working one. The runtime keeps using the local name and
never pulls by itself.

Usage: python -m claw.sandbox.image_source tag
       python -m claw.sandbox.image_source pull [LOCAL_IMAGE]
Exit status of pull: 0 pulled, verified and tagged; 5 no registry configured;
6 the pull failed; 7 the pulled image is not ready (the local image is left as
it was). Anything but 0 means: build locally instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "docker" / "sandbox.Dockerfile"
REPOSITORY = "claw-sandbox"
TAG_PREFIX = "df-"
# Must match the `sha256sum … | cut -c1-12` step in .github/workflows/sandbox-image.yml.
TAG_HEX_CHARS = 12

EXIT_PULLED = 0
EXIT_NO_REGISTRY = 5
EXIT_PULL_FAILED = 6
EXIT_NOT_READY = 7

PULL_TIMEOUT_S = 1800


def dockerfile_tag(dockerfile: Path = DOCKERFILE) -> str:
    return TAG_PREFIX + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:TAG_HEX_CHARS]


def remote_ref(registry: str, dockerfile: Path = DOCKERFILE) -> str:
    """`registry` is host[:port]/project, e.g. registry.softnix.co.th/privateclaw."""
    registry = registry.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"{registry}/{REPOSITORY}:{dockerfile_tag(dockerfile)}"


def _docker(*args: str, timeout: float) -> subprocess.CompletedProcess:
    # Pull progress goes straight to the operator's terminal.
    return subprocess.run(["docker", *args], timeout=timeout, check=False)


def pull(registry: str, local_image: str, settings, workspaces_root: Path | None = None) -> int:
    """Pull the prebuilt image for this checkout, probe it, then name it `local_image`."""
    from claw.sandbox.capabilities import check_image

    if not registry.strip():
        print("no sandbox registry configured (CLAW_SANDBOX__REGISTRY); building locally", file=sys.stderr)
        return EXIT_NO_REGISTRY
    ref = remote_ref(registry)
    print(f"==> pulling prebuilt sandbox image {ref}")
    try:
        pulled = _docker("pull", ref, timeout=PULL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"pull of {ref} failed: {exc}", file=sys.stderr)
        return EXIT_PULL_FAILED
    if pulled.returncode != 0:
        print(f"pull of {ref} failed (exit {pulled.returncode}); is this host logged in to the registry?",
              file=sys.stderr)
        return EXIT_PULL_FAILED
    readiness = asyncio.run(check_image(settings.model_copy(update={"image": ref}), workspaces_root))
    if not readiness.ok:
        print(f"pulled {ref} is not ready, keeping {local_image} as it was: {readiness.describe()}",
              file=sys.stderr)
        return EXIT_NOT_READY
    try:
        tagged = _docker("tag", ref, local_image, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"could not tag {ref} as {local_image}: {exc}", file=sys.stderr)
        return EXIT_PULL_FAILED
    if tagged.returncode != 0:
        print(f"could not tag {ref} as {local_image}", file=sys.stderr)
        return EXIT_PULL_FAILED
    # The local name keeps the layers. Leaving the registry name on the image
    # would keep every superseded df-<hash> image (~2.5 GB each) from becoming
    # dangling and prunable.
    _docker("rmi", ref, timeout=60)
    print(f"==> {local_image} is now {ref} (offline capabilities verified)")
    return EXIT_PULLED


def main(argv: list[str]) -> int:
    if argv[:1] == ["tag"]:
        print(dockerfile_tag())
        return 0
    if argv[:1] == ["pull"]:
        from claw.config import load_settings

        try:
            app = load_settings()
        except Exception as exc:  # noqa: BLE001 - a broken config falls back to the local build, which reports it
            print(f"sandbox image pull could not read the settings: {exc}", file=sys.stderr)
            return EXIT_PULL_FAILED
        local_image = argv[1] if len(argv) > 1 else app.sandbox.image
        return pull(app.sandbox.registry, local_image, app.sandbox, app.workspaces_root)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
