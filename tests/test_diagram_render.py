import pytest
from claw.skills.render import render_diagram


async def test_render_static_svg_and_boundaries(tmp_path):
    file = tmp_path / "diagram.svg"
    file.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"><rect width="500" height="300" fill="#fff"/><text x="20" y="50">Team · ทีม</text><script>document.querySelector("svg").remove()</script></svg>'
    )
    result = await render_diagram(tmp_path, "diagram.svg", 640, 480)
    assert (tmp_path / result["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result["warnings"] == []
    with pytest.raises(ValueError):
        await render_diagram(tmp_path, "../outside.svg")
    with pytest.raises(ValueError):
        await render_diagram(tmp_path, "diagram.svg", 9999, 9999)


async def test_render_reports_overflow(tmp_path):
    (tmp_path / "wide.html").write_text(
        '<html><body><svg width="1600" height="900"><rect width="1600" height="900" fill="blue"/></svg></body></html>'
    )
    result = await render_diagram(tmp_path, "wide.html", 640, 480)
    assert result["warnings"]


async def test_render_reuses_content_cache(tmp_path):
    file = tmp_path / "diagram.svg"
    file.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"><rect width="500" height="300"/></svg>'
    )
    first = await render_diagram(tmp_path, "diagram.svg", 640, 480, user_id="cache-user")
    first_mtime = (tmp_path / first["path"]).stat().st_mtime_ns
    second = await render_diagram(tmp_path, "diagram.svg", 640, 480, user_id="cache-user")
    assert second == first
    assert (tmp_path / second["path"]).stat().st_mtime_ns == first_mtime
    assert len(list(tmp_path.glob("diagram-render-*.png"))) == 1


async def test_render_rate_limits_new_browser_jobs(tmp_path, monkeypatch):
    import claw.skills.render as renderer
    from claw.core.limits import RateLimiter

    monkeypatch.setattr(renderer, "_EXPORT_LIMITER", RateLimiter(1))
    (tmp_path / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"/>'
    )
    await render_diagram(tmp_path, "diagram.svg", 640, 480, user_id="limited-user")
    with pytest.raises(PermissionError, match="rate limit"):
        await render_diagram(tmp_path, "diagram.svg", 641, 480, user_id="limited-user")


def test_render_cache_cleanup_removes_png_and_metadata(tmp_path):
    from claw.skills.render import _cleanup_cache

    keep = tmp_path / "diagram-render-keep.png"
    keep.write_bytes(b"png")
    for index in range(22):
        png = tmp_path / f"diagram-render-{index:02}.png"
        png.write_bytes(b"png")
        png.with_suffix(".json").write_text("{}")
    _cleanup_cache(tmp_path, keep)
    assert len(list(tmp_path.glob("diagram-render-*.png"))) <= 20
    for metadata in tmp_path.glob("diagram-render-*.json"):
        assert metadata.with_suffix(".png").exists()
