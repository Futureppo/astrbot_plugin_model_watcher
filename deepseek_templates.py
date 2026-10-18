"""Select event-specific DeepSeek memes and embed their packaged artwork."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .catalog import DEEPSEEK_ATTRIBUTE_GROUPS


def build_deepseek_meme(
    notification: dict[str, Any], data: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Choose a reaction without making claims beyond the detected changes.

    Args:
        notification: Complete DeepSeek catalog or fingerprint notification.
        data: Escaped-at-render-time fields prepared by the shared card builder.

    Returns:
        Self-contained Jinja2 template and data including the selected artwork.

    Raises:
        OSError: A packaged template or illustration cannot be read.
    """
    changes = notification["changes"]
    fields = [field for group in changes["changed"].values() for field in group]
    membership = bool(changes["added"] or changes["removed"])
    paths = {field["path"] for field in fields}
    groups = {
        next(
            (
                group
                for prefix, group in DEEPSEEK_ATTRIBUTE_GROUPS.items()
                if path == prefix or path.startswith(prefix + ".")
            ),
            "metadata",
        )
        for path in paths
    }
    if membership:
        groups.add("membership")
    event = "metadata"
    if len(groups) > 1:
        event = "mixed"
    elif groups == {"system_fingerprint"}:
        event = "fingerprint"
    elif changes["removed"] and not changes["added"] and not fields:
        event = "removed"
    elif changes["added"] and not changes["removed"] and not fields:
        event = "added"
    elif membership:
        event = "membership"
    elif groups == {"context"}:
        event = "context_change" if "context_window" in paths else "output_change"
    elif groups:
        event = next(iter(groups))

    # Use directional captions only when every change supports that claim.
    if (
        not membership
        and fields
        and all(
            field["old_exists"]
            and field["new_exists"]
            and all(
                type(field[side]) in (int, float) and field[side] > 0
                for side in ("old", "new")
            )
            for field in fields
        )
    ):
        if paths == {"context_window"}:
            if all(field["new"] > field["old"] for field in fields):
                event = "context_up"
            elif all(field["new"] < field["old"] for field in fields):
                event = "context_down"
        elif paths == {"max_output_tokens"}:
            if all(field["new"] > field["old"] for field in fields):
                event = "output_up"
            elif all(field["new"] < field["old"] for field in fields):
                event = "output_down"

    headline, punchline = {
        "fingerprint": ("你以为我没更新？", "指纹都换了！"),
        "added": ("给大家拎个新同事", "来，跟大家打个招呼"),
        "removed": ("我模型呢？", "刚才还在列表里啊！"),
        "name": ("换个名字就不认识了？", "名牌换了，重新认识一下"),
        "provider": ("等会儿，你哪家的？", "提供商这栏改了！"),
        "modalities": ("你的装备怎么又变了？", "输入输出，重新核对！"),
        "reasoning": ("这档位是你调的？", "先看看现在怎么想"),
        "api_capabilities": ("这个接口，能接上吗？", "能力和限制，重新核对"),
        "metadata": ("谁偷偷改我资料了？", "别藏了，改动都看见了"),
        "mixed": ("你管这叫小改动？", "来，一项一项对账"),
        "membership": ("这边刚来，那边就走", "这一轮，重新点名！"),
        "context_up": ("这次\n能记多少？", "也就多记了\n亿点点"),
        "context_down": ("说好的\n过目不忘？", "这回得\n少记一点了"),
        "context_change": ("上下文\n又改了？", "新旧数值\n摆上来"),
        "output_up": ("这次\n能写多少？", "也就多写了\n亿点点"),
        "output_down": ("怎么\n话变少了？", "这回得\n长话短说"),
        "output_change": ("这次\n能写多少？", "先看看\n新的上限"),
    }[event]
    if event == "context_up" and all(
        field["new"] == field["old"] * 2 for field in fields
    ):
        punchline = "也就\n翻了一倍"

    layout = "single"
    if event.startswith(("context_", "output_")):
        layout = "dialogue"
        assets = ["context-question.jpg", "context-answer.jpg"]
    elif event == "membership":
        layout = "lineup"
        assets = ["added.jpg", "removed.jpg"]
    else:
        assets = [
            {
                "fingerprint": "fingerprint.jpg",
                "added": "added.jpg",
                "removed": "removed.jpg",
                "name": "name.jpg",
                "provider": "provider.jpg",
                "modalities": "modalities.jpg",
                "reasoning": "reasoning.jpg",
                "api_capabilities": "api-capabilities.jpg",
                "metadata": "metadata.jpg",
                "mixed": "mixed.jpg",
            }[event]
        ]

    directory = Path(__file__).with_name("templates") / "deepseek"
    artworks = []
    for asset in assets:
        path = directory / asset
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        artworks.append({"name": path.stem, "src": f"data:image/jpeg;base64,{encoded}"})
    data.update(
        event=event,
        layout=layout,
        headline=headline,
        punchline=punchline,
        artworks=artworks,
    )
    template = (directory / "meme.html").read_text(encoding="utf-8")
    css = (directory / "meme.css").read_text(encoding="utf-8")
    return template.replace("/* MEME_STYLES */", css), data
