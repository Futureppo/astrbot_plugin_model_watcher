"""Complete single-image cards, wrapping, and font fallback tests."""

from io import BytesIO

import pytest
from PIL import Image

from data.plugins.astrbot_plugin_model_watcher import renderer


@pytest.fixture
def notice():
    return {
        "name": "模型供应商",
        "base_url": "https://example.test/api/",
        "time": "2026-09-29 12:00:00 +0800",
        "count": 9,
        "changes": {
            "added": ["new-model"],
            "removed": ["old-model"],
            "changed": {
                "updated-model": [
                    {
                        "path": "pricing.prompt",
                        "old": "0.00001",
                        "new": "0.00002",
                        "old_exists": True,
                        "new_exists": True,
                    }
                ]
            },
        },
    }


def test_card_contains_complete_change_details(notice):
    text = renderer.format_notification(notice)
    for expected in [
        "条目名称：模型供应商",
        "网址：https://example.test/api/",
        "+0800",
        "new-model",
        "old-model",
        "updated-model",
        "pricing.prompt",
        "0.00001",
        "0.00002",
    ]:
        assert expected in text
    assert text.count("条目名称：") == 1


def test_many_changes_and_long_names_are_not_truncated(notice):
    notice["changes"]["added"] = [f"model-{i:04d}" for i in range(220)]
    long_value = "独特属性内容" * 800
    notice["changes"]["changed"]["updated-model"][0]["new"] = long_value
    text = renderer.format_notification(notice)
    joined = "".join(text.split("\n"))
    assert all(f"model-{i:04d}" in joined for i in range(220))
    assert long_value in joined
    assert text.count("条目名称：模型供应商") == 1
    assert text.count("网址：https://example.test/api/") == 1
    assert len(text.split("\n")) > 220


def test_png_dimensions_and_readability(notice):
    try:
        renderer.load_font(24)
    except OSError:
        pytest.skip("No CJK font installed on this host")
    notice["changes"]["added"] = [f"model-{i:04d}" for i in range(80)]
    text = renderer.format_notification(notice)
    png = renderer.render_card(text)
    image = Image.open(BytesIO(png))
    assert image.format == "PNG" and image.width == renderer.WIDTH
    assert image.height > 1700
    assert image.height == 206 + len(text.split("\n")) * 34 + 150
    assert len(image.getcolors(image.width * image.height)) > 50


def test_missing_font_preserves_text(notice, monkeypatch):
    def missing(size):
        raise OSError("No Chinese font")

    monkeypatch.setattr(renderer, "load_font", missing)
    text = renderer.format_notification(notice)
    assert "模型供应商" in text
    with pytest.raises(OSError):
        renderer.render_card(text)


def test_oversized_image_fails_before_allocation(monkeypatch):
    try:
        renderer.load_font(24)
    except OSError:
        pytest.skip("No CJK font installed on this host")
    monkeypatch.setattr(
        renderer.Image, "new", lambda *args: pytest.fail("Image allocated")
    )
    with pytest.raises(ValueError, match="pixel budget"):
        renderer.render_card("model\n" * 1500)
