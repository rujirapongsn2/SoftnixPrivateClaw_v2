"""Resource bounds of the diagram renderer.

Regression: the worker capped RLIMIT_AS at 4 GiB, and Chromium (which reserves
~1.4 TiB of address space per renderer while using a few hundred MiB) died at
launch with SIGTRAP, so no PNG was ever produced on Linux. Memory is now bounded
by the parent's process-tree RSS guard, which must also see and kill Chromium:
Playwright launches it detached, in a process group of its own.
"""

import asyncio
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

import claw.skills.render as render

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="the RSS guard reads /proc")


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def _alive(pid: int) -> bool:
    fields = render._proc_stat(pid)
    if fields is not None:
        return fields[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _leftovers(root: Path) -> list[str]:
    return [p.name for p in root.iterdir() if p.name.endswith(".tmp")]


def test_worker_limits_leave_address_space_to_the_parent_guard():
    probe = (
        "import json, resource; from claw.skills.render_worker import _limits;"
        "before = resource.getrlimit(resource.RLIMIT_AS); _limits();"
        "print(json.dumps({'as': [before, resource.getrlimit(resource.RLIMIT_AS)],"
        "'cpu': resource.getrlimit(resource.RLIMIT_CPU),"
        "'nofile': resource.getrlimit(resource.RLIMIT_NOFILE),"
        "'fsize': resource.getrlimit(resource.RLIMIT_FSIZE)}))"
    )
    limits = json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True, timeout=30))

    before, after = limits["as"]
    assert after == before
    assert limits["cpu"] == [25, 30]
    assert limits["nofile"] == [256, 256]
    assert limits["fsize"] == [25 * 1024 * 1024] * 2


@pytest.mark.parametrize(("width", "height"), [(400, 260), (1440, 1140)])
async def test_real_worker_renders_png_at_canvas_size(tmp_path, width, height):
    boxes = "".join(
        f'<rect x="{40 + i * 220}" y="120" width="180" height="90" rx="8" fill="#e2eef9" stroke="#2786c2"/>'
        f'<text x="{60 + i * 220}" y="170" font-size="18">Stage {i + 1}</text>'
        for i in range(6)
    )
    (tmp_path / "diagram.html").write_text(
        f'<html><body><svg xmlns="http://www.w3.org/2000/svg" width="{width - 40}" height="{height - 40}">'
        f'<rect width="100%" height="100%" fill="#fff"/>{boxes}</svg></body></html>'
    )

    result = await render.render_diagram(tmp_path, "diagram.html", width, height)

    assert _png_size(tmp_path / result["path"]) == (width, height)
    assert _leftovers(tmp_path) == []


# A stand-in worker shaped like Playwright: it starts a detached child (its own
# session and process group, as Chromium is) that does the actual work.
_FAKE_WORKER = r"""
import os, subprocess, sys, time
root = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", sys.argv[-1]], start_new_session=True)
with open(os.path.join(root, "child.pid"), "w") as f:
    f.write(str(child.pid))
time.sleep(60)
"""
_HOG = "import time; block = bytearray(256 * 1024 * 1024); block[::4096] = b'x' * len(block[::4096]); time.sleep(60)"
_SLEEPER = "import time; time.sleep(60)"


def _use_fake_worker(monkeypatch, tmp_path: Path, child_code: str) -> None:
    (tmp_path / "diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    # argv after "-c": root, source, temporary, width, height — the child's
    # code rides along as an extra last argument.
    monkeypatch.setattr(render, "_WORKER_ARGS", ("-c", _FAKE_WORKER))
    real_exec = asyncio.create_subprocess_exec

    async def with_child_code(*args, **kwargs):
        return await real_exec(*args, child_code, **kwargs)

    monkeypatch.setattr(render.asyncio, "create_subprocess_exec", with_child_code)


async def _child_pid(root: Path) -> int:
    for _ in range(100):
        if (root / "child.pid").is_file() and (root / "child.pid").read_text():
            return int((root / "child.pid").read_text())
        await asyncio.sleep(0.05)
    raise AssertionError("fake worker never started its child")


# A missed child also holds the worker's pipes, so asyncio's wait() blocks until
# it exits on its own; the call returning promptly is part of the contract.
_PROMPT_SECONDS = 10


def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


@linux_only
async def test_memory_guard_counts_and_kills_a_detached_child(monkeypatch, tmp_path):
    _use_fake_worker(monkeypatch, tmp_path, _HOG)
    monkeypatch.setattr(render, "_WORKER_MEMORY_BYTES", 96 * 1024 * 1024)

    started = time.monotonic()
    with pytest.raises(render.RenderRejected, match="simplify it"):
        await render.render_diagram(tmp_path, "diagram.svg", 640, 480)

    assert time.monotonic() - started < _PROMPT_SECONDS
    _assert_dead(int((tmp_path / "child.pid").read_text()))
    assert _leftovers(tmp_path) == []


@linux_only
async def test_timeout_kills_a_detached_child(monkeypatch, tmp_path):
    _use_fake_worker(monkeypatch, tmp_path, _SLEEPER)
    monkeypatch.setattr(render, "_WORKER_TIMEOUT_SECONDS", 1.5)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await render.render_diagram(tmp_path, "diagram.svg", 640, 480)

    assert time.monotonic() - started < _PROMPT_SECONDS
    _assert_dead(int((tmp_path / "child.pid").read_text()))
    assert _leftovers(tmp_path) == []


@linux_only
async def test_cancellation_kills_a_detached_child(monkeypatch, tmp_path):
    _use_fake_worker(monkeypatch, tmp_path, _SLEEPER)

    task = asyncio.create_task(render.render_diagram(tmp_path, "diagram.svg", 640, 480))
    child = await _child_pid(tmp_path)
    await asyncio.sleep(0.3)  # let the guard observe the child
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < _PROMPT_SECONDS
    _assert_dead(child)
    assert _leftovers(tmp_path) == []


def _chromium_pids() -> set[int]:
    pids = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        fields = render._proc_stat(int(entry.name))
        if b"headless_shell" in cmdline and fields and fields[0] != "Z":
            pids.add(int(entry.name))
    return pids


@linux_only
async def test_memory_guard_stops_real_chromium(monkeypatch, tmp_path):
    (tmp_path / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"><rect width="500" height="300"/></svg>'
    )
    before = _chromium_pids()
    # Well below what a launched Chromium uses, so the guard must fire on it.
    monkeypatch.setattr(render, "_WORKER_MEMORY_BYTES", 64 * 1024 * 1024)

    with pytest.raises(render.RenderRejected, match="simplify it"):
        await render.render_diagram(tmp_path, "diagram.svg", 640, 480)

    deadline = time.monotonic() + 3
    while _chromium_pids() - before and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _chromium_pids() - before == set()
    assert _leftovers(tmp_path) == []
