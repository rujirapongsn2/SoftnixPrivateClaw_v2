"""Disposable Playwright worker used by :mod:`claw.skills.render`."""

import asyncio
import json
import os
import re
import resource
import sys
from pathlib import Path


def _limits() -> None:
    limits = (
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_CPU, 25, 30),
        (resource.RLIMIT_FSIZE, 25 * 1024 * 1024, 25 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 256, 256),
        (resource.RLIMIT_RSS, 1024**3, 1024**3),
    )
    if sys.platform.startswith("linux"):
        limits += ((resource.RLIMIT_AS, 4 * 1024**3, 4 * 1024**3),)
    for kind, soft, hard in limits:
        try:
            resource.setrlimit(kind, (soft, hard))
        except (ValueError, OSError):
            pass


async def _render(root: Path, source: Path, target: Path, width: int, height: int) -> dict:
    from claw.api.file_preview import preview_html
    from playwright.async_api import async_playwright

    root = root.resolve()
    source = source.resolve()
    target = target.resolve()
    if not source.is_relative_to(root) or not target.is_relative_to(root):
        raise ValueError("Renderer paths must stay inside the workspace")
    markup = preview_html(source)["html"]
    if not re.search(r"<svg\b", markup, re.I):
        raise ValueError("Diagram must contain an SVG element")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=[
                "--disable-background-networking",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-sync",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--renderer-process-limit=1",
            ]
        )
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                java_script_enabled=False,
                service_workers="block",
            )
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(markup, wait_until="load", timeout=10_000)
            bounds = await page.evaluate("""() => {
              const svg = document.querySelector('svg');
              const r = svg.getBoundingClientRect();
              return {width: r.width, height: r.height,
                overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight};
            }""")
            if bounds["width"] < 1 or bounds["height"] < 1:
                raise ValueError("SVG has no visible dimensions")
            await page.screenshot(path=str(target), type="png", animations="disabled")
        finally:
            await browser.close()
    return {
        "width": width,
        "height": height,
        "warnings": ["Content exceeds the canvas; increase dimensions or simplify the diagram"]
        if bounds["overflow"]
        else [],
    }


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit("invalid renderer arguments")
    _limits()
    root, source, target = map(Path, sys.argv[1:4])
    result = asyncio.run(_render(root, source, target, int(sys.argv[4]), int(sys.argv[5])))
    os.write(1, json.dumps(result, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
