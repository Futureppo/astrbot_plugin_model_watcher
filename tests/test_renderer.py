"""Card content, pagination, and font fallback tests."""

from io import BytesIO

import pytest
from PIL import Image

from data.plugins.astrbot_plugin_model_watcher import renderer


@pytest.fixture
def notice():
    return {
        "name": "模型供应商",
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
    pages = renderer.paginate_notification(notice)
    text = "\n".join(pages)
    for expected in [
        "模型供应商",
        "+0800",
        "new-model",
        "old-model",
        "updated-model",
        "pricing.prompt",
        "0.00001",
        "0.00002",
    ]:
        assert expected in text
    assert all(len(page) <= 1800 for page in pages)


def test_many_changes_and_long_names_are_not_truncated(notice):
    notice["changes"]["added"] = [f"model-{i:04d}" for i in range(220)]
    long_value = "独特属性内容" * 800
    notice["changes"]["changed"]["updated-model"][0]["new"] = long_value
    pages = renderer.paginate_notification(notice)
    joined = "".join("".join(page.split("\n")[5:]) for page in pages)
    assert len(pages) > 5
    assert all(f"model-{i:04d}" in joined for i in range(220))
    assert long_value in joined
    assert all("模型供应商" in page and "+0800" in page for page in pages)
    assert all(len(page.split("\n")) <= renderer.LINES_PER_PAGE for page in pages)


def test_png_dimensions_and_readability(notice):
    try:
        renderer.load_font(24)
    except OSError:
        pytest.skip("No CJK font installed on this host")
    pages = renderer.paginate_notification(notice)
    png = renderer.render_page(pages[0], 1, len(pages))
    image = Image.open(BytesIO(png))
    assert image.format == "PNG" and image.width == renderer.WIDTH
    assert 480 <= image.height <= renderer.HEIGHT
    assert len(image.getcolors(image.width * image.height)) > 50


def test_missing_font_preserves_text(notice, monkeypatch):
    def missing(size):
        raise OSError("No Chinese font")

    monkeypatch.setattr(renderer, "load_font", missing)
    pages = renderer.paginate_notification(notice)
    assert "模型供应商" in pages[0]
    with pytest.raises(OSError):
        renderer.render_page(pages[0], 1, len(pages))
