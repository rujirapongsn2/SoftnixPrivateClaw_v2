"""Offline worker: only /input is mounted, read-only. Outputs go to /output."""

import json
import pathlib
import subprocess
import sys

import pymupdf as fitz

if sys.argv[1] == "--code":
    import shutil

    shutil.copytree("/input", "/tmp/work")
    try:
        result = subprocess.run(
            json.loads(sys.argv[2]),
            cwd="/tmp/work",
            timeout=40,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print(
            json.dumps(
                {"exit_code": result.returncode, "output": result.stdout.decode("utf-8", "replace")[-8000:]}
            )
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"unavailable": type(exc).__name__}))
    sys.exit(0)

source = pathlib.Path("/input") / sys.argv[1]
output = pathlib.Path("/output")
if not source.resolve().is_relative_to("/input"):
    raise ValueError("invalid source")
if source.suffix.lower() not in {".docx", ".pptx", ".pdf"}:
    raise ValueError("unsupported document")
if source.suffix.lower() == ".pdf":
    pdf = source
else:
    subprocess.run(
        [
            "libreoffice",
            "-env:UserInstallation=file:///tmp/lo-profile",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output),
            str(source),
        ],
        check=True,
        timeout=45,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pdf = output / (source.stem + ".pdf")
with fitz.open(pdf) as document:
    if not 0 < len(document) <= 20:
        raise ValueError("document exceeds 20-page visual review limit")
    pages = []
    full_text = []
    for index, page in enumerate(document):
        filename = f"page-{index + 1}.jpg"
        page.get_pixmap(matrix=fitz.Matrix(1, 1)).save(str(output / filename))
        text = page.get_text()
        full_text.append(text)
        pages.append({"image": filename, "text": text[:400]})
    text = "\n".join(full_text)
    (output / "extracted.json").write_text(
        json.dumps({"text": text[:100000], "complete": len(text) <= 100000}), encoding="utf-8"
    )
    print(json.dumps({"pages": pages}, ensure_ascii=False))
