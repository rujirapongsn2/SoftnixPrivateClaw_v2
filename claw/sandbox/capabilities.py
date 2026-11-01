"""What the sandbox image must hold for offline work, and a cheap check that it does.

A network=none sandbox cannot `pip install`, `apt-get install` or download a
model at run time, so every capability the agent is told about has to be baked
into docker/sandbox.Dockerfile. This table is the one list of them: the image
readiness probe checks it, and the agent's offline guidance is written from it,
so the two cannot disagree.

The probe imports each module (so a package whose native libraries are missing
or built for another NumPy counts as broken), looks executables up with `which`,
globs for models, fonts and language data, and writes one byte into each
directory a command needs to write. It never starts OCR or renders anything,
so it stays cheap enough to run at every start (a few seconds, mostly imports).

Usage: python -m claw.sandbox.capabilities [IMAGE]
Exit status: 0 ready, 3 the image lacks something (rebuild it), 4 something
a rebuild cannot fix (docker, permissions, a timeout, the configuration).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from claw.config import SandboxSettings
from claw.sandbox.ephemeral import SandboxResult


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    # Agent-facing: what to reach for. Written into the offline guidance.
    offers: str
    modules: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    # Absolute globs inside the image (fonts, language data).
    data: tuple[str, ...] = ()
    # (module, glob relative to the module's package directory), for data that
    # ships inside a wheel, such as OCR models.
    module_data: tuple[tuple[str, str], ...] = ()


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "documents",
        "PDF/Word/Excel/PowerPoint: reportlab, weasyprint (HTML→PDF), fpdf2, pypdf, PyMuPDF (`fitz`), "
        "python-docx, python-pptx, openpyxl, xlsxwriter, pandas, markdown",
        modules=("reportlab", "weasyprint", "fpdf", "pypdf", "fitz", "docx", "pptx", "openpyxl", "xlsxwriter",
                 "pandas", "markdown"),
    ),
    Capability(
        "pdf-tools",
        "Poppler CLIs: `pdftoppm` (PDF→PNG), `pdftotext`, `pdfinfo`",
        executables=("pdftoppm", "pdftotext", "pdfinfo"),
    ),
    Capability(
        "ocr-tesseract",
        "OCR for Thai and English: `tesseract` CLI and `pytesseract` with `lang='tha+eng'`",
        modules=("pytesseract",),
        executables=("tesseract",),
        data=("/usr/share/tesseract-ocr/*/tessdata/tha.traineddata", "/usr/share/tesseract-ocr/*/tessdata/eng.traineddata"),
    ),
    Capability(
        "ocr-rapidocr",
        "OCR for Latin/Chinese text: `rapidocr_onnxruntime.RapidOCR` with its bundled models (not Thai)",
        modules=("rapidocr_onnxruntime", "onnxruntime", "cv2"),
        module_data=(("rapidocr_onnxruntime", "models/*.onnx"),),
    ),
    Capability(
        "images-charts",
        "Images and charts: Pillow, OpenCV (`cv2`, headless), matplotlib (PNG/SVG/PDF)",
        modules=("PIL", "cv2", "matplotlib"),
    ),
    Capability(
        "svg",
        "SVG → PNG/PDF: CairoSVG (`cairosvg`) and the `rsvg-convert` CLI",
        modules=("cairosvg",),
        executables=("rsvg-convert",),
    ),
    Capability(
        "thai-text",
        "Thai text: TLWG fonts in /usr/share/fonts/truetype/tlwg/ (Garuda, Loma, Norasi, Sawasdee…), "
        "pythainlp word/sentence tokenizing with its bundled dictionary, uharfbuzz shaping",
        modules=("pythainlp", "uharfbuzz"),
        data=("/usr/share/fonts/truetype/tlwg/Garuda.ttf",),
        module_data=(("pythainlp", "corpus/words_th.txt"),),
    ),
    Capability(
        "media",
        "Audio/video: `ffmpeg`",
        executables=("ffmpeg",),
    ),
)

# Where a command writes besides /workspace. HOME and the caches point into
# /tmp under the hardened run (see ephemeral.hardening_flags).
WRITABLE_DIRS = ("/tmp", "$HOME", "$MPLCONFIGDIR", "$XDG_CACHE_HOME", "/workspace")

_PROBE = r"""
import glob, importlib, importlib.util, json, os, shutil, sys
spec = json.loads(sys.argv[1])
out = {"module": [], "import_failed": [], "executable": [], "data": [], "not_writable": []}
for cap in spec["capabilities"]:
    for m in cap["modules"]:
        try:
            found = importlib.util.find_spec(m) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out["module"].append(m)
            continue
        try:
            importlib.import_module(m)
        except BaseException as exc:
            out["import_failed"].append(f"{m} ({type(exc).__name__}: {str(exc)[:80]})")
    for e in cap["executables"]:
        if shutil.which(e) is None:
            out["executable"].append(e)
    for pattern in cap["data"]:
        if not glob.glob(pattern):
            out["data"].append(pattern)
    for m, rel in cap["module_data"]:
        try:
            s = importlib.util.find_spec(m)
        except (ImportError, ValueError):
            s = None
        roots = list(s.submodule_search_locations or []) if s else []
        if not any(glob.glob(os.path.join(r, rel)) for r in roots):
            out["data"].append(m + "/" + rel)
for d in spec["writable"]:
    path = os.path.expandvars(d)
    if path.startswith("$"):
        continue
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".claw-write-probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
    except OSError:
        out["not_writable"].append(path)
print(json.dumps({kind: list(dict.fromkeys(items)) for kind, items in out.items()}))
"""

_PROBLEM_LABELS = {
    "module": "Python packages missing",
    "import_failed": "Python packages that fail to import",
    "executable": "executables missing",
    "data": "models/data files missing",
    "not_writable": "not writable by the sandbox user",
}


# Missing image contents, which a rebuild fixes. Everything else the probe can
# report (unwritable directories, docker down, a timeout) a rebuild cannot fix.
_CONTENT_KINDS = ("module", "import_failed", "executable", "data")

# `python -m claw.sandbox.capabilities` exit codes, read by scripts/claw.
EXIT_READY = 0
EXIT_REBUILD = 3
EXIT_ENVIRONMENT = 4


@dataclass(slots=True)
class Readiness:
    missing: dict[str, list[str]] = field(default_factory=dict)
    # A probe that could not run at all (no docker, no image, no python).
    error: str = ""
    # True when that error is one a rebuild fixes (no such image, no python).
    error_needs_image: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and not any(self.missing.values())

    @property
    def needs_rebuild(self) -> bool:
        return self.error_needs_image or any(self.missing.get(kind) for kind in _CONTENT_KINDS)

    def describe(self) -> str:
        if self.error:
            return self.error
        return "; ".join(
            f"{_PROBLEM_LABELS[kind]}: {', '.join(items)}" for kind, items in self.missing.items() if items
        )


def probe_spec() -> str:
    return json.dumps({
        "capabilities": [
            {"modules": c.modules, "executables": c.executables, "data": c.data, "module_data": c.module_data}
            for c in CAPABILITIES
        ],
        "writable": WRITABLE_DIRS,
    })


def parse_probe(result: SandboxResult, image: str) -> Readiness:
    first_err = (result.stderr.strip().splitlines() or [""])[0]
    if result.timed_out:
        return Readiness(error=f"probing {image} timed out")
    if result.exit_code == 125:
        if "no such image" in first_err.lower() or "unable to find image" in first_err.lower():
            return Readiness(error=f"image {image} is missing ({first_err})", error_needs_image=True)
        return Readiness(error=f"docker could not start {image} ({first_err})")
    if result.infrastructure_error:
        return Readiness(error=f"docker is unavailable ({first_err}); sandbox commands will fail")
    if result.exit_code == 127:
        return Readiness(error=f"image {image} has no usable python to run the probe ({first_err})",
                         error_needs_image=True)
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return Readiness(error=f"the readiness probe failed in {image} (exit {result.exit_code}: {first_err})")
    return Readiness(missing={kind: list(found.get(kind) or []) for kind in _PROBLEM_LABELS})


async def check_image(settings: SandboxSettings, workspaces_root: Path | None = None) -> Readiness:
    """Probe `settings.image` through EphemeralSandbox, so it runs with exactly
    the docker flags, limits and timeout a real exec command gets, with a
    throwaway directory as /workspace and the network forced off. Never raises.

    The directory is made inside `workspaces_root` when it exists: that path
    is the one Docker can already bind-mount, including when the app drives
    the host daemon from inside a container (docker-compose.prod.yml), where
    a temp dir of the app container does not exist on the host. The dot
    prefix keeps it out of the per-user directory scans. The mounted
    directory is made like a user workspace (plain mkdir, the process umask),
    so a sandbox user that could not write a real workspace cannot write it
    either."""
    from claw.sandbox.ephemeral import EphemeralSandbox

    parent = workspaces_root if workspaces_root is not None and workspaces_root.is_dir() else None
    holder = Path(tempfile.mkdtemp(prefix=".claw-sandbox-probe-", dir=parent))
    try:
        workspace = holder / "workspace"
        workspace.mkdir()
        sandbox = EphemeralSandbox(settings.model_copy(update={"enabled": True, "network": "none"}))
        result = await sandbox.run(shlex.join(["python", "-c", _PROBE, probe_spec()]), workspace)
    finally:
        # The sandbox user may own what the probe wrote; leftovers are harmless.
        shutil.rmtree(holder, ignore_errors=True)
    return parse_probe(result, settings.image)


# The system-prompt line; the exec tool description carries the full list.
# Scoped to `exec`: sbot's `project` containers are a separate tool with their
# own network and package lifecycle.
OFFLINE_PROMPT = (
    "Commands you run with `exec` have no internet (network=none): never `pip install`, `apt-get install`, "
    "`git clone` or download models or data with `exec`, and do not treat that as a fix for an error. Use the "
    "preinstalled packages the exec tool lists; if one you need is missing, tell the user it is not available "
    "offline and offer a listed alternative. Get outside data with your web, search or connector tools, never "
    "through `exec`."
)


def is_offline(settings) -> bool:
    """True when shell commands run in a sandbox with no network at all.
    None (a tool built without a real sandbox) is not offline."""
    return settings is not None and bool(settings.enabled) and settings.network == "none"


def offline_guidance() -> str:
    """What the agent is told when its sandbox has no network."""
    offers = "\n".join(f"- {c.offers}" for c in CAPABILITIES)
    return (
        "The sandbox has no internet access (network=none): `pip install`, `apt-get install`, `git clone`, "
        "model downloads and any HTTP request from a shell command fail, so never try them as a fix. "
        "Use what is preinstalled:\n"
        f"{offers}\n"
        "If a task needs a package or model that is not listed and not importable, say plainly that it is "
        "not available offline and offer a listed alternative. For data from outside, use your web, "
        "search or connector tools, not the shell."
    )


def main(argv: list[str]) -> int:
    from claw.config import load_settings

    # The operator's .env (user, limits, tmp size, hardening), so the probe runs
    # under the confinement real exec commands get.
    try:
        app = load_settings()
        settings = app.sandbox.model_copy(update={"network": "none", **({"image": argv[0]} if argv else {})})
        result = asyncio.run(check_image(settings, app.workspaces_root))
    except Exception as exc:  # noqa: BLE001 - a broken config is not a reason to rebuild the image
        print(f"sandbox readiness check could not run: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    if result.ok:
        print(f"{settings.image}: offline capabilities ready ({', '.join(c.name for c in CAPABILITIES)})")
        return EXIT_READY
    print(f"{settings.image}: {result.describe()}", file=sys.stderr)
    return EXIT_REBUILD if result.needs_rebuild else EXIT_ENVIRONMENT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
