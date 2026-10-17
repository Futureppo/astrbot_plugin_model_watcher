"""Prepare shared HTML card templates from structured model changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import (
    ATTRIBUTE_GROUPS,
    CARD_TEMPLATE_LABELS,
    CHECK_LABELS,
    DEEPSEEK_ATTRIBUTE_GROUPS,
    DEEPSEEK_CHECK_LABELS,
    DEEPSEEK_SET_PATHS,
    PROVIDER_ATTRIBUTE_GROUPS,
    PROVIDER_CHECK_LABELS,
    SET_PATHS,
)


def build_card_template(
    notification: dict[str, Any], template_key: str
) -> tuple[str, dict[str, Any]]:
    """Prepare a trusted template and separate escaped notification values.

    Args:
        notification: Provider, timestamp, model count, and complete change details.
        template_key: One of the shared HTML card template identifiers.

    Returns:
        Jinja2 template and data for AstrBot's HTML renderer.

    Raises:
        ValueError: The requested HTML template is not supported.
        OSError: A packaged template asset cannot be read.
    """
    if template_key not in CARD_TEMPLATE_LABELS or template_key == "classic":
        raise ValueError("Unknown HTML card template")
    directory = Path(__file__).with_name("templates")
    template = (directory / "card.html").read_text(encoding="utf-8")
    css = (directory / "shared.css").read_text(encoding="utf-8")
    css += (directory / f"{template_key}.css").read_text(encoding="utf-8")
    # Only packaged CSS enters the template source; API content stays in data.
    template = template.replace("/* CARD_STYLES */", css)
    changes = notification["changes"]
    deepseek = notification.get("deepseek", False)
    openrouter = notification.get("openrouter", False)
    provider = notification.get("provider", "")
    categorized = deepseek or openrouter or provider in PROVIDER_CHECK_LABELS
    groups = PROVIDER_ATTRIBUTE_GROUPS.get(
        provider, DEEPSEEK_ATTRIBUTE_GROUPS if deepseek else ATTRIBUTE_GROUPS
    )
    labels = PROVIDER_CHECK_LABELS.get(
        provider, DEEPSEEK_CHECK_LABELS if deepseek else CHECK_LABELS
    )
    set_paths = DEEPSEEK_SET_PATHS if deepseek else SET_PATHS if openrouter else ()
    models = []
    for model_id, fields in changes["changed"].items():
        rendered_fields = []
        for field in fields:
            path = field["path"]
            group = next(
                (
                    group
                    for prefix, group in groups.items()
                    if path == prefix or path.startswith(prefix + ".")
                ),
                "metadata",
            )
            title = labels[group] if categorized else "属性变化"
            if deepseek and path == "system_fingerprint":
                title = "后端指纹"
            rendered = {"path": path, "label": title, "summary": "", "note": ""}
            for side in ("old", "new"):
                rendered[side] = (
                    json.dumps(field[side], ensure_ascii=False, sort_keys=True)
                    if field[f"{side}_exists"]
                    else "（不存在）"
                )
            if (
                (not field["old_exists"] or field["old"] is None)
                and field["new_exists"]
                and field["new"] is not None
            ):
                rendered["note"] = "信息补全"
            if (
                categorized
                and path in set_paths
                and all(
                    isinstance(field[side], list)
                    and all(isinstance(item, str) for item in field[side])
                    for side in ("old", "new")
                )
            ):
                parts = []
                added = sorted(set(field["new"]) - set(field["old"]))
                removed = sorted(set(field["old"]) - set(field["new"]))
                if added:
                    parts.append("新增 " + json.dumps(added, ensure_ascii=False))
                if removed:
                    parts.append("移除 " + json.dumps(removed, ensure_ascii=False))
                rendered["summary"] = "；".join(parts)
            rendered_fields.append(rendered)
        models.append({"id": model_id, "fields": rendered_fields})
    fingerprints = notification.get("fingerprints", False)
    date, _, time = str(notification["time"]).partition(" ")
    return template, {
        "name": str(notification["name"]),
        "base_url": notification.get("base_url") or "（未填写基础 API）",
        "date": date,
        "time": time,
        "total": notification["count"],
        "title": "后端指纹更新" if fingerprints else "模型目录更新",
        "label": "BACKEND FINGERPRINT" if fingerprints else "MODEL CATALOG",
        "mode": "fingerprint" if fingerprints else "catalog",
        "kind": "指纹监控" if fingerprints else "目录监控",
        "added": changes["added"],
        "removed": changes["removed"],
        "models": models,
    }
