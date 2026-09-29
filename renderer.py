"""Local, paginated model change cards with a complete text fallback."""

from __future__ import annotations

import json
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

WIDTH = 1200
HEIGHT = 1700
BODY_WIDTH = WIDTH - 128
LINES_PER_PAGE = 39


@lru_cache(maxsize=6)
def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Find a font with Chinese glyphs on supported operating systems.

    Args:
        size: Font size in pixels.

    Returns:
        A font suitable for the Chinese card labels.

    Raises:
        OSError: No usable Chinese font is installed.
    """
    candidates = [
        Path(get_astrbot_data_path()) / "font.ttf",
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ]
    for candidate in candidates:
        try:
            font = ImageFont.truetype(str(candidate), size)
            missing = bytes(font.getmask("\U0010ffff"))
            if all(bytes(font.getmask(char)) != missing for char in "模型更新"):
                return font
        except OSError:
            continue
    raise OSError("A Chinese font is required for model watcher cards")


def paginate_notification(notification: dict[str, Any]) -> list[str]:
    """Prepare stable text pages for image delivery and restart-safe cursors.

    Args:
        notification: Provider name, detection time, count, and catalog changes.

    Returns:
        Complete text pages, including every change and all old/new values.
    """
    changes = notification["changes"]
    lines = [
        f"提供商：{notification['name']}",
        f"检测时间：{notification['time']}",
        f"当前模型：{notification['count']} 个",
        f"新增 {len(changes['added'])} · 下架 {len(changes['removed'])} · 属性变化 {len(changes['changed'])}",
        "",
    ]
    for key, label in (("added", "新增模型"), ("removed", "下架模型")):
        if changes[key]:
            lines.append(f"【{label}】")
            lines.extend(json.dumps(item, ensure_ascii=False) for item in changes[key])
            lines.append("")
    if changes["changed"]:
        lines.append("【属性变化】")
        for model_id, fields in changes["changed"].items():
            lines.append(json.dumps(model_id, ensure_ascii=False))
            for field in fields:
                old = (
                    json.dumps(field["old"], ensure_ascii=False, sort_keys=True)
                    if field["old_exists"]
                    else "（不存在）"
                )
                new = (
                    json.dumps(field["new"], ensure_ascii=False, sort_keys=True)
                    if field["new_exists"]
                    else "（不存在）"
                )
                lines.append(f"  {field['path']}：{old} → {new}")
            lines.append("")
    try:
        font = load_font(24)
    except OSError:
        font = None
    wrapped, header = [], []
    for line_index, line in enumerate(lines):
        # Source strings can contain newlines; keep each resulting line visible.
        for paragraph in line.expandtabs(4).split("\n"):
            current = ""
            for char in paragraph:
                candidate = current + char
                width = font.getlength(candidate) if font else len(candidate) * 24
                if current and (width > BODY_WIDTH or len(candidate) > 100):
                    wrapped.append(current)
                    current = char
                else:
                    current = candidate
            wrapped.append(current)
        if line_index == 4:
            header = list(wrapped)
    # Keep subsequent cards identifiable when a notification spans many pages.
    # Unusually long provider names remain complete on the first pages.
    if len(header) > 12 or sum(len(line) + 1 for line in header) > 900:
        header = ["（续页，提供商与检测时间见首页）", ""]
    # Text fallbacks stay under 1,800 characters even on stricter adapters.
    pages, page = [], []
    length = 0
    for line in wrapped:
        if page and (len(page) >= LINES_PER_PAGE or length + len(line) + 1 > 1800):
            pages.append("\n".join(page))
            page = list(header)
            length = sum(len(item) + 1 for item in page)
        page.append(line)
        length += len(line) + 1
    if page:
        pages.append("\n".join(page))
    return pages


def render_page(text: str, page_number: int, page_count: int) -> bytes:
    """Render a pre-paginated notification with a fixed maximum page height.

    Args:
        text: Page text created by ``paginate_notification``.
        page_number: One-based page index.
        page_count: Total number of pages in the notification.

    Returns:
        PNG image bytes.

    Raises:
        OSError: No Chinese font is available.
        ValueError: Persisted text cannot fit the current font metrics.
    """
    title_font, body_font, footer_font = load_font(42), load_font(24), load_font(20)
    lines = text.split("\n")
    if len(lines) > LINES_PER_PAGE or any(
        body_font.getlength(line) > BODY_WIDTH + 1 for line in lines
    ):
        raise ValueError("Page does not fit the current font")
    height = min(HEIGHT, max(480, 206 + len(lines) * 34 + 150))
    image = Image.new("RGB", (WIDTH, height), "#F3F5F9")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, WIDTH - 24, height - 24), radius=28, fill="#FFFFFF")
    draw.rounded_rectangle((64, 66, 74, 120), radius=5, fill="#5168D8")
    draw.text((96, 64), "模型列表更新", font=title_font, fill="#172033")
    draw.text((66, 132), "MODEL WATCHER", font=footer_font, fill="#768299")
    draw.line((64, 180, WIDTH - 64, 180), fill="#E5E9F0", width=2)
    y = 206
    for line in lines:
        color = "#303C52"
        if line.startswith("【"):
            color = {"【新增模型】": "#177A58", "【下架模型】": "#BA4658"}.get(
                line, "#6652BB"
            )
        draw.text((64, y), line, font=body_font, fill=color)
        y += 34
    draw.line((64, height - 104, WIDTH - 64, height - 104), fill="#E5E9F0", width=2)
    draw.text(
        (64, height - 82), "Model Watcher · Futureppo", font=footer_font, fill="#768299"
    )
    draw.text(
        (WIDTH - 220, height - 82),
        f"{page_number} / {page_count}",
        font=footer_font,
        fill="#768299",
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
