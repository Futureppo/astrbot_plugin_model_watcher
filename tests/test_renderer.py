"""Complete single-image cards, wrapping, and font fallback tests."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined
from PIL import Image

from data.plugins.astrbot_plugin_model_watcher import renderer
from data.plugins.astrbot_plugin_model_watcher.card_templates import build_card_template
from data.plugins.astrbot_plugin_model_watcher.catalog import (
    CARD_TEMPLATE_LABELS,
    DEEPSEEK_CARD_TEMPLATE_LABELS,
    WatchSpec,
)


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


@pytest.mark.parametrize(
    "template_key",
    ["light", "dark", "diff", "soft", "bold", "deepseek_meme"],
)
def test_html_templates_preserve_data_and_escape_markup(notice, template_key):
    notice["name"] = '<img src="https://example.test">{{7*7}}'
    notice["deepseek"] = True
    notice["changes"]["changed"] = {
        "deepseek-flash": [
            {
                "path": "owned_by",
                "old": None,
                "new": "deepseek",
                "old_exists": False,
                "new_exists": True,
            },
            {
                "path": "max_output_tokens",
                "old": 100,
                "new": None,
                "old_exists": True,
                "new_exists": False,
            },
            {
                "path": "input_modalities",
                "old": ["text"],
                "new": ["text", "image"],
                "old_exists": True,
                "new_exists": True,
            },
        ],
    }
    template, data = build_card_template(notice, template_key)
    rendered = Environment(undefined=StrictUndefined).from_string(template).render(data)
    assert '<img src="https://example.test">' not in rendered
    assert "&lt;img src=" in rendered and "{{7*7}}" in rendered
    assert "new-model" in rendered and "old-model" in rendered
    fields = data["models"][0]["fields"]
    assert fields[0]["old"] == "（不存在）" and fields[0]["note"] == "信息补全"
    assert fields[1]["new"] == "（不存在）"
    assert fields[2]["summary"] == '新增 ["image"]'
    assert "api.deepseek.com" not in rendered
    assert "模拟数据" not in rendered


def test_every_provider_offers_shared_templates():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    for key, provider in schema["providers"]["templates"].items():
        field = provider["items"]["card_template"]
        assert field["type"] == "string" and field["default"] == "classic"
        choices = dict(CARD_TEMPLATE_LABELS)
        if key == "deepseek":
            choices.update(DEEPSEEK_CARD_TEMPLATE_LABELS)
        assert field["options"] == list(choices)
        assert field["labels"] == list(choices.values())
        entry = {name: item["default"] for name, item in provider["items"].items()}
        entry.update(entry_id=key, __template_key=key)
        if key == "custom":
            entry["full_url"] = "https://example.test/models"
        assert WatchSpec.from_entry(entry).card_template == "classic"
        for template_key in choices:
            assert (
                WatchSpec.from_entry(
                    dict(entry, card_template=template_key)
                ).card_template
                == template_key
            )
        if key != "deepseek":
            for template_key in DEEPSEEK_CARD_TEMPLATE_LABELS:
                with pytest.raises(ValueError, match="card template"):
                    WatchSpec.from_entry(dict(entry, card_template=template_key))


def test_meme_reactions_respect_direction_and_mixed_changes(notice):
    template_key = "deepseek_meme"
    notice["deepseek"] = True
    changes = notice["changes"]
    changes.update(added=[], removed=[])
    field = {
        "path": "context_window",
        "old": 1024,
        "new": 2048,
        "old_exists": True,
        "new_exists": True,
    }
    changes["changed"] = {"deepseek-flash": [field]}
    _, data = build_card_template(notice, template_key)
    assert data["event"] == "context_up"
    assert all(art["src"].startswith("data:image/") for art in data["artworks"])
    assert [art["name"] for art in data["artworks"]] == [
        "context-question",
        "context-answer",
    ]
    assert data["punchline"].replace("\n", "") == "也就翻了一倍"
    field["new"] = 512
    _, data = build_card_template(notice, template_key)
    assert data["event"] == "context_down"
    assert [art["name"] for art in data["artworks"]] == [
        "context-question",
        "context-answer",
    ]
    field.update(old=None, old_exists=False, new=2048)
    _, data = build_card_template(notice, template_key)
    assert data["event"] == "context_change"
    assert data["models"][0]["fields"][0]["note"] == "信息补全"
    changes["removed"] = ["old-model"]
    _, data = build_card_template(notice, template_key)
    assert data["event"] == "mixed"
    assert data["artworks"][0]["name"] == "mixed"
    assert data["removed"] == ["old-model"] and len(data["models"]) == 1
    changes["changed"] = {}
    _, data = build_card_template(notice, template_key)
    assert data["event"] == "removed"
    assert data["artworks"][0]["name"] == "removed"
    changes["added"] = ["new-model"]
    _, data = build_card_template(notice, template_key)
    assert data["layout"] == "lineup"
    assert [art["name"] for art in data["artworks"]] == ["added", "removed"]
