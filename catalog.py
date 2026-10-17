"""Model catalog configuration, retrieval, and structural comparison."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession

_PRIVATE_REQUEST = ContextVar("model_watcher_private_request", default=False)

CARD_TEMPLATE_LABELS = {
    "classic": "原版",
    "light": "A · 浅色卡片",
    "dark": "B · 深色监控台",
    "diff": "C · 差异对照",
    "soft": "D · 柔和彩色卡片",
    "bold": "E · 粗边框拼色",
}

CHECK_LABELS = {
    "membership": "模型新增、下架",
    "pricing": "价格与计费规则",
    "context": "上下文与输出长度",
    "modalities": "输入、输出类型",
    "parameters": "工具调用与支持参数",
    "reasoning": "推理能力与默认配置",
    "voices": "语音音色",
    "lifecycle": "停用日期",
    "aliases": "模型别名指向",
    "metadata": "其他元数据（高级）",
    "benchmarks": "评分与排行榜",
}
ATTRIBUTE_GROUPS = {
    "pricing": "pricing",
    "context_length": "context",
    "top_provider.context_length": "context",
    "top_provider.max_completion_tokens": "context",
    "per_request_limits": "context",
    "architecture.modality": "modalities",
    "architecture.input_modalities": "modalities",
    "architecture.output_modalities": "modalities",
    "supported_parameters": "parameters",
    "reasoning": "reasoning",
    "default_parameters": "reasoning",
    "supported_voices": "voices",
    "expiration_date": "lifecycle",
    "alias_target.slug": "aliases",
    "canonical_slug": "aliases",
    "benchmarks": "benchmarks",
}
SET_PATHS = {
    "architecture.input_modalities",
    "architecture.output_modalities",
    "supported_parameters",
    "supported_voices",
    "reasoning.supported_efforts",
}
DEEPSEEK_CHECK_LABELS = {
    "membership": "模型新增、下架",
    "name": "模型名称",
    "provider": "提供商",
    "context": "上下文与输出长度",
    "modalities": "输入、输出类型",
    "reasoning": "推理档位与默认配置",
    "api_capabilities": "API 能力与限制",
    "metadata": "其他元数据（高级）",
    "system_fingerprint": "后端指纹（system_fingerprint）",
}
DEEPSEEK_ATTRIBUTE_GROUPS = {
    "id": "membership",
    "name": "name",
    "owned_by": "provider",
    "context_window": "context",
    "max_output_tokens": "context",
    "input_modalities": "modalities",
    "output_modalities": "modalities",
    "effort.supported_levels": "reasoning",
    "effort.default_level": "reasoning",
    "api_capabilities": "api_capabilities",
    "system_fingerprint": "system_fingerprint",
}
DEEPSEEK_SET_PATHS = {
    "input_modalities",
    "output_modalities",
    "effort.supported_levels",
}
PROVIDER_CHECK_LABELS = {
    "openrouter": CHECK_LABELS,
    "deepseek": DEEPSEEK_CHECK_LABELS,
    "openai": {
        "membership": "模型新增、下架",
        "lifecycle": "停用日期",
        "provider": "提供商",
        "created": "创建时间",
        "metadata": "其他元数据（高级）",
    },
    "nvidia": {
        "membership": "模型新增、下架",
        "provider": "提供商",
        "created": "创建时间（高级）",
        "metadata": "其他元数据（高级）",
    },
    "opencode": {
        "membership": "模型新增、下架",
        "provider": "提供商",
        "created": "接口时间戳（高级）",
        "metadata": "其他元数据（高级）",
    },
}
PROVIDER_ATTRIBUTE_GROUPS = {
    "openrouter": ATTRIBUTE_GROUPS,
    "deepseek": DEEPSEEK_ATTRIBUTE_GROUPS,
    "openai": {
        "id": "membership",
        "shutdown_date": "lifecycle",
        "owned_by": "provider",
        "created": "created",
    },
    "nvidia": {
        "id": "membership",
        "owned_by": "provider",
        "created": "created",
    },
    "opencode": {
        "id": "membership",
        "owned_by": "provider",
        "created": "created",
    },
}
PRICE_KEYS = {
    "prompt",
    "completion",
    "request",
    "image",
    "image_token",
    "image_output",
    "audio",
    "audio_output",
    "input_audio_cache",
    "web_search",
    "internal_reasoning",
    "input_cache_read",
    "input_cache_write",
    "input_cache_write_1h",
    "discount",
}


def attribute_equal(path: str, before: Any, after: Any, deepseek: bool = False) -> bool:
    """Compare catalog values with field-specific numeric and set semantics.

    Args:
        path: Model-relative attribute path, including any array indexes.
        before: Previous JSON value.
        after: Current JSON value.
        deepseek: Use DeepSeek list semantics without OpenRouter pricing rules.

    Returns:
        Whether the values are equivalent, retaining unknown array ordering,
        pricing override precedence, and distinctions between null and zero.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(
            attribute_equal(f"{path}.{key}", before[key], after[key], deepseek)
            for key in before
        )
    if isinstance(before, list) and isinstance(after, list):
        set_paths = DEEPSEEK_SET_PATHS if deepseek else SET_PATHS
        if path in set_paths or (
            not deepseek
            and path.startswith("pricing.overrides.")
            and path.endswith(".utc_days")
        ):
            return {json.dumps(item, sort_keys=True) for item in before} == {
                json.dumps(item, sort_keys=True) for item in after
            }
        return len(before) == len(after) and all(
            attribute_equal(f"{path}.{index}", old, new, deepseek)
            for index, (old, new) in enumerate(zip(before, after))
        )
    if (
        not deepseek
        and path.startswith("pricing.")
        and path.rsplit(".", 1)[-1] in PRICE_KEYS
    ):
        if all(type(value) in (str, int, float) for value in (before, after)):
            try:
                old_price, new_price = Decimal(str(before)), Decimal(str(after))
                if old_price.is_finite() and new_price.is_finite():
                    return old_price == new_price
            except InvalidOperation:
                pass
    return json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)


class RequestLogFilter(logging.Filter):
    """Suppress HTTP dependency logs only inside this watcher's requests."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep unrelated HTTP logs while preventing request URL disclosure.

        Args:
            record: HTTP dependency log record.

        Returns:
            Whether the record belongs to an unrelated request context.
        """
        return not _PRIVATE_REQUEST.get()


def read_path(value: Any, path: str) -> Any:
    """Read a dotted object path with numeric array indexes.

    Args:
        value: JSON value to traverse.
        path: Dotted path, or ``$`` for the root value.

    Returns:
        The selected JSON value.

    Raises:
        ValueError: The path does not resolve.
    """
    if path == "$":
        return value
    try:
        for part in path.removeprefix("$.").split("."):
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list) and part.isdecimal():
                value = value[int(part)]
            else:
                raise ValueError("Invalid JSON path")
    except (KeyError, IndexError) as exc:
        raise ValueError("JSON path was not found") from exc
    return value


@dataclass(frozen=True)
class WatchSpec:
    """Validated configuration for one independent catalog watcher."""

    entry_id: str
    name: str
    base_url: str
    url: str
    api_key: str = field(repr=False)
    api_type: str
    auth: httpx.Auth | None = field(repr=False)
    models_path: str
    id_path: str
    interval: int
    proxy: str | None
    targets: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    comparison_mode: tuple[str, ...]
    provider: str
    is_openrouter: bool
    is_deepseek: bool
    fingerprint: str
    fingerprint_enabled: bool
    fingerprint_interval: int
    fingerprint_max_tokens: int
    fingerprint_url: str
    card_template: str

    @classmethod
    def from_entry(cls, entry: dict) -> WatchSpec:
        """Validate a template entry without including secrets in errors.

        Args:
            entry: A provider entry with a persisted entry ID.

        Returns:
            Configuration ready for polling.

        Raises:
            ValueError: A URL, path, or target has an invalid format.
        """
        full_url = str(entry.get("full_url") or "").strip()
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        api_type = str(entry.get("api_type") or "openai")
        if api_type not in {"openai", "gemini", "vertex"}:
            raise ValueError("Unsupported API type")
        if full_url:
            url = full_url
        elif api_type == "gemini":
            suffix = (
                "/models" if base_url.endswith(("/v1", "/v1beta")) else "/v1beta/models"
            )
            url = base_url + suffix
        elif api_type == "vertex":
            suffix = (
                "/publishers/*/models"
                if base_url.endswith("/v1beta1")
                else "/v1beta1/publishers/*/models"
            )
            url = base_url + suffix
        else:
            suffix = "/models" if base_url.endswith("/v1") else "/v1/models"
            url = base_url + suffix
        try:
            parsed = httpx.URL(url)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise ValueError("An absolute HTTP(S) API URL is required")
            if parsed.userinfo or parsed.fragment:
                raise ValueError("API URLs cannot contain credentials or fragments")
            proxy = str(entry.get("proxy") or "").strip() or None
            if proxy:
                proxy_url = httpx.URL(proxy)
                if proxy_url.scheme not in {"http", "https", "socks5", "socks5h"}:
                    raise ValueError("Unsupported proxy scheme")
                if not proxy_url.host:
                    raise ValueError("An absolute proxy URL is required")
        except httpx.InvalidURL as exc:
            raise ValueError("Invalid API or proxy URL") from exc
        provider = str(entry.get("__template_key") or "")
        is_openrouter = provider == "openrouter"
        is_deepseek = provider == "deepseek"
        if (
            (is_openrouter or parsed.host == "openrouter.ai")
            and api_type == "openai"
            and parsed.path.rstrip("/").endswith("/v1/models")
            and "output_modalities" not in parsed.params
        ):
            # OpenRouter otherwise defaults to models that produce text output.
            parsed = parsed.copy_set_param("output_modalities", "all")
        if provider in PROVIDER_CHECK_LABELS:
            labels = PROVIDER_CHECK_LABELS[provider]
            selection = entry.get(
                "comparison_mode" if is_openrouter else "detection_items",
                ["membership"]
                if is_openrouter
                else [
                    key
                    for key in labels
                    if key not in {"metadata", "system_fingerprint", "created"}
                ],
            )
            if not isinstance(selection, list) or any(
                not isinstance(item, str) or item not in labels for item in selection
            ):
                raise ValueError("Invalid provider detection items")
            comparison_mode = tuple(key for key in labels if key in selection)
        else:
            mode = entry.get("comparison_mode", "模型 ID 和属性")
            if mode not in ("仅模型 ID", "模型 ID 和属性"):
                raise ValueError("Invalid comparison mode")
            comparison_mode = (
                ("membership",) if mode == "仅模型 ID" else tuple(CHECK_LABELS)
            )
        raw_interval = entry.get("interval_seconds", 30)
        try:
            interval = int(raw_interval)
            if isinstance(raw_interval, bool) or str(interval) != str(raw_interval):
                interval = 30
            if interval <= 0:
                interval = 30
        except (TypeError, ValueError, OverflowError):
            interval = 30
        probe = entry.get("fingerprint_probe", {}) if is_deepseek else {}
        if not isinstance(probe, dict):
            raise ValueError("Fingerprint probe settings must be an object")
        fingerprint_enabled = (
            is_deepseek
            and probe.get("enabled", False) is True
            and "system_fingerprint" in comparison_mode
        )
        probe_limits = {}
        for key, default in (("interval_seconds", 240), ("max_tokens", 16)):
            raw = probe.get(key, default)
            try:
                value = int(raw)
                if isinstance(raw, bool) or str(value) != str(raw) or value <= 0:
                    value = default
            except (TypeError, ValueError, OverflowError):
                value = default
            probe_limits[key] = value
        targets = []
        for raw in entry.get("umo_whitelist", []):
            if not isinstance(raw, str):
                raise ValueError("UMO targets must be strings")
            target = raw.strip()
            if not target:
                continue
            try:
                session = MessageSession.from_str(target)
                if not session.platform_id or not session.session_id:
                    raise ValueError("Empty UMO component")
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid UMO target") from exc
            if target not in targets:
                targets.append(target)
        default_models_path = {"gemini": "models", "vertex": "publisherModels"}.get(
            api_type, "data"
        )
        default_id_path = "id" if api_type == "openai" else "name"
        models_path = str(entry.get("models_path") or "").strip() or default_models_path
        id_path = str(entry.get("id_path") or "").strip() or default_id_path
        ignored = tuple(
            dict.fromkeys(
                str(path).strip()
                for path in entry.get("ignored_paths", [])
                if str(path).strip()
            )
        )
        for path in (models_path, id_path, *ignored):
            if path != "$" and any(
                not part for part in path.removeprefix("$.").split(".")
            ):
                raise ValueError("JSON paths cannot contain empty segments")
        fingerprint_enabled = fingerprint_enabled and not any(
            path.removeprefix("$.") in {"$", "system_fingerprint"} for path in ignored
        )
        fingerprint_url = ""
        if fingerprint_enabled:
            try:
                probe_url = httpx.URL(base_url + "/chat/completions")
            except httpx.InvalidURL as exc:
                raise ValueError("Invalid fingerprint probe URL") from exc
            if (
                probe_url.scheme not in {"http", "https"}
                or not probe_url.host
                or probe_url.userinfo
                or probe_url.fragment
                or probe_url.query
            ):
                raise ValueError("Fingerprint probing requires a valid base API URL")
            fingerprint_url = str(probe_url)
        api_key = str(entry.get("api_key") or "").strip()
        auth = None
        if api_type == "vertex" and api_key:
            from .google_auth import ServiceAccountAuth

            auth = ServiceAccountAuth(api_key)
        # Persist a digest, never credentials or credential-bearing query strings.
        identity = [str(parsed), api_key, models_path, id_path]
        if api_type != "openai":
            identity.append(api_type)
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        card_template = entry.get("card_template", "classic")
        if (
            not isinstance(card_template, str)
            or card_template not in CARD_TEMPLATE_LABELS
        ):
            raise ValueError("Invalid notification card template")
        return cls(
            entry_id=str(entry["entry_id"]),
            name=str(entry.get("name") or entry.get("__template_key") or "Provider"),
            base_url=str(entry.get("base_url") or "").strip(),
            url=str(parsed),
            api_key=api_key,
            api_type=api_type,
            auth=auth,
            models_path=models_path,
            id_path=id_path,
            interval=interval,
            proxy=proxy,
            targets=tuple(targets),
            ignored_paths=ignored,
            comparison_mode=comparison_mode,
            provider=provider,
            is_openrouter=is_openrouter,
            is_deepseek=is_deepseek,
            fingerprint=fingerprint,
            fingerprint_enabled=fingerprint_enabled,
            fingerprint_interval=probe_limits["interval_seconds"],
            fingerprint_max_tokens=probe_limits["max_tokens"],
            fingerprint_url=fingerprint_url,
            card_template=card_template,
        )


async def fetch_catalog(client: httpx.AsyncClient, spec: WatchSpec) -> dict[str, Any]:
    """Fetch and validate a complete catalog before exposing any models.

    Args:
        client: An HTTP client configured with this entry's proxy and timeout.
        spec: Validated provider configuration.

    Returns:
        Raw model objects indexed by unique model ID.

    Raises:
        ValueError: JSON, model IDs, or pagination are invalid.
        httpx.HTTPError: A request fails.
    """
    url = initial_url = httpx.URL(spec.url)
    origin = (url.scheme, url.host, url.port)
    headers = {"Accept": "application/json"}
    if spec.api_type == "vertex":
        if spec.auth is None:
            raise ValueError("Vertex requires service account JSON in API Key")
    elif spec.api_type == "gemini" and spec.api_key:
        headers["x-goog-api-key"] = spec.api_key
    elif spec.api_key:
        headers["Authorization"] = f"Bearer {spec.api_key}"
    models: dict[str, Any] = {}
    visited: set[str] = set()
    for _ in range(100):
        if str(url) in visited:
            raise ValueError("Pagination loop detected")
        visited.add(str(url))
        # HTTPX logs complete URLs at INFO; custom URLs can contain API tokens.
        # Filters are scoped by async context so other plugins keep their logs.
        request_filter = RequestLogFilter()
        dependency_loggers = [
            logging.getLogger(name)
            for name in list(logging.Logger.manager.loggerDict)
            if name == "httpx" or name.startswith("httpcore.")
        ]
        for dependency_logger in dependency_loggers:
            dependency_logger.addFilter(request_filter)
        token = _PRIVATE_REQUEST.set(True)
        try:
            response = await asyncio.wait_for(
                client.get(url, headers=headers, auth=spec.auth), timeout=15
            )
        finally:
            _PRIVATE_REQUEST.reset(token)
            for dependency_logger in dependency_loggers:
                dependency_logger.removeFilter(request_filter)
        response.raise_for_status()
        payload = response.json()
        # Reject non-standard NaN/Infinity values rather than persisting them.
        json.dumps(payload, allow_nan=False)
        rows = read_path(payload, spec.models_path)
        if not isinstance(rows, list):
            raise ValueError("The models path must select an array")
        for row in rows:
            if isinstance(row, str):
                model_id = row
            elif isinstance(row, dict):
                model_id = read_path(row, spec.id_path)
            else:
                raise ValueError("Models must be objects or strings")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("Each model must have a nonempty string ID")
            # Model Garden may return multiple versions with the same resource name.
            if (
                spec.api_type == "vertex"
                and spec.id_path == "name"
                and isinstance(row, dict)
            ):
                version = row.get("versionId")
                if version is not None:
                    if not isinstance(version, str) or not version.strip():
                        raise ValueError("Invalid publisher model version")
                    model_id = f"{model_id}@{version}"
            if model_id in models:
                raise ValueError("Duplicate model ID")
            models[model_id] = row
        if spec.api_type in {"gemini", "vertex"}:
            if not isinstance(payload, dict):
                raise ValueError("Google catalogs must return an object")
            next_token = payload.get("nextPageToken")
            if next_token is None or next_token == "":
                return models
            if not isinstance(next_token, str):
                raise ValueError("Invalid Google pagination token")
            url = initial_url.copy_set_param("pageToken", next_token)
            continue
        links = payload.get("links") if isinstance(payload, dict) else None
        if links is not None and not isinstance(links, dict):
            raise ValueError("Invalid pagination links object")
        next_url = links.get("next") if isinstance(links, dict) else None
        if next_url is None or next_url == "":
            return models
        if not isinstance(next_url, str):
            raise ValueError("Invalid pagination link")
        url = url.join(next_url)
        if (url.scheme, url.host, url.port) != origin or url.userinfo or url.fragment:
            raise ValueError("Pagination must remain on the API origin")
    raise ValueError("Catalog exceeded the pagination limit")


async def fetch_fingerprints(
    client: httpx.AsyncClient, spec: WatchSpec, models: dict[str, Any]
) -> dict[str, Any]:
    """Probe each catalog model and return only valid backend fingerprints.

    Args:
        client: HTTP client using the provider's proxy and timeout.
        spec: DeepSeek configuration with explicitly enabled probes.
        models: Latest catalog whose model IDs should receive a short greeting.

    Returns:
        Successful model fingerprints; failed or missing values are omitted.
    """
    fingerprints = {}
    if not spec.fingerprint_enabled:
        return fingerprints
    headers = {"Accept": "application/json"}
    if spec.api_key:
        headers["Authorization"] = f"Bearer {spec.api_key}"
    request_filter = RequestLogFilter()
    dependency_loggers = [
        logging.getLogger(name)
        for name in list(logging.Logger.manager.loggerDict)
        if name == "httpx" or name.startswith("httpcore.")
    ]
    for dependency_logger in dependency_loggers:
        dependency_logger.addFilter(request_filter)
    token = _PRIVATE_REQUEST.set(True)
    try:
        for model_id in models:
            try:
                response = await asyncio.wait_for(
                    client.post(
                        spec.fingerprint_url,
                        headers=headers,
                        json={
                            "model": model_id,
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": spec.fingerprint_max_tokens,
                            "thinking": {"type": "disabled"},
                            "stream": False,
                        },
                    ),
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                value = (
                    payload.get("system_fingerprint")
                    if isinstance(payload, dict)
                    else None
                )
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or payload.get("error")
                ):
                    raise ValueError("Response has no valid system_fingerprint")
                fingerprints[model_id] = {"system_fingerprint": value}
            except Exception as exc:
                status = (
                    f"HTTP {exc.response.status_code}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else type(exc).__name__
                )
                logger.warning(
                    "Model watcher fingerprint probe failed for entry %s, model %s (%s); keeping its baseline.",
                    spec.entry_id,
                    model_id,
                    status,
                )
    finally:
        _PRIVATE_REQUEST.reset(token)
        for dependency_logger in dependency_loggers:
            dependency_logger.removeFilter(request_filter)
    return fingerprints


def compare_catalogs(
    previous: dict[str, Any],
    current: dict[str, Any],
    ignored_paths: tuple[str, ...],
    checks: tuple[str, ...] | None = None,
    openrouter: bool = False,
    deepseek: bool = False,
    provider: str = "",
) -> dict[str, Any]:
    """Compare catalog membership and nested attributes without mutating snapshots.

    Args:
        previous: Last successfully fetched raw catalog.
        current: Newly fetched raw catalog.
        ignored_paths: Model-relative paths to omit from both sides.
        checks: Selected categories; None compares all available categories.
        openrouter: Whether to apply OR categories, field semantics, and schedules.
        deepseek: Whether to apply DeepSeek categories and list semantics.
        provider: Template key selecting provider-specific attribute categories.

    Returns:
        Added and removed IDs plus changed fields with explicit value presence.
    """
    if checks is None:
        checks = tuple(
            PROVIDER_CHECK_LABELS.get(
                provider, DEEPSEEK_CHECK_LABELS if deepseek else CHECK_LABELS
            )
        )
    result: dict[str, Any] = {
        "added": sorted(current.keys() - previous.keys())
        if "membership" in checks
        else [],
        "removed": sorted(previous.keys() - current.keys())
        if "membership" in checks
        else [],
        "changed": {},
    }
    if not (set(checks) - {"membership"}) or "$" in ignored_paths:
        return result
    categorized = openrouter or deepseek or provider in PROVIDER_CHECK_LABELS
    groups = PROVIDER_ATTRIBUTE_GROUPS.get(
        provider, DEEPSEEK_ATTRIBUTE_GROUPS if deepseek else ATTRIBUTE_GROUPS
    )
    for model_id in sorted(previous.keys() & current.keys()):
        before, after = (
            copy.deepcopy(value)
            if isinstance(value, dict) or "metadata" in checks
            else {}
            for value in (previous[model_id], current[model_id])
        )
        for value in (before, after):
            # Keep categories disjoint, including shared parents such as architecture.
            nodes = [("", value)] if categorized else []
            while nodes:
                parent_path, node = nodes.pop()
                if not isinstance(node, dict):
                    continue
                for key in list(node):
                    path = f"{parent_path}.{key}" if parent_path else key
                    group = groups.get(path)
                    if group:
                        if group not in checks:
                            node.pop(key)
                        continue
                    descendants = [
                        group
                        for prefix, group in groups.items()
                        if prefix.startswith(path + ".")
                    ]
                    if descendants and isinstance(node[key], dict):
                        nodes.append((path, node[key]))
                    elif "metadata" not in checks and not any(
                        group in checks for group in descendants
                    ):
                        node.pop(key)
            # Empty shared containers carry no selected attributes.
            if (openrouter or deepseek) and isinstance(value, dict):
                shared_parents = (
                    ("effort",)
                    if deepseek
                    else ("architecture", "top_provider", "alias_target")
                )
                for parent in shared_parents:
                    if value.get(parent) == {} and (
                        "metadata" not in checks
                        or any(
                            group not in checks
                            for prefix, group in groups.items()
                            if prefix.startswith(parent + ".")
                        )
                    ):
                        value.pop(parent)
            for path in ignored_paths:
                if path == "$":
                    continue
                parts = path.removeprefix("$.").split(".")
                try:
                    parent = read_path(
                        value, ".".join(parts[:-1]) if len(parts) > 1 else "$"
                    )
                    if isinstance(parent, dict):
                        parent.pop(parts[-1], None)
                    elif isinstance(parent, list) and parts[-1].isdecimal():
                        # Keep positions stable when ignoring one array element.
                        parent[int(parts[-1])] = None
                except (ValueError, IndexError):
                    continue
        if openrouter and isinstance(before, dict) and isinstance(after, dict):
            old_prices, new_prices = before.get("pricing"), after.get("pricing")
            if isinstance(old_prices, dict) and isinstance(new_prices, dict):
                rules = old_prices.get("overrides")
                if (
                    isinstance(rules, list)
                    and rules
                    and attribute_equal(
                        "pricing.overrides", rules, new_prices.get("overrides")
                    )
                    and all(
                        isinstance(rule, dict)
                        and set(rule)
                        <= PRICE_KEYS | {"utc_start", "utc_end", "utc_days"}
                        and (
                            "utc_days" in rule
                            or {"utc_start", "utc_end"} <= rule.keys()
                        )
                        for rule in rules
                    )
                ):
                    # OR publishes the complete schedule. Only suppress a rate when
                    # both observed values occur in that unchanged schedule and every
                    # window explicitly defines it; unknown conditions remain visible.
                    for key in PRICE_KEYS & old_prices.keys() & new_prices.keys():
                        if all(key in rule for rule in rules) and all(
                            any(
                                attribute_equal(
                                    f"pricing.{key}", prices[key], rule[key]
                                )
                                for rule in rules
                            )
                            for prices in (old_prices, new_prices)
                        ):
                            new_prices[key] = copy.deepcopy(old_prices[key])
        changes = []
        stack = [("", before, after)]
        while stack:
            path, old, new = stack.pop()
            if isinstance(old, dict) and isinstance(new, dict):
                for key in sorted(old.keys() | new.keys(), reverse=True):
                    field = f"{path}.{key}" if path else key
                    if key not in old or key not in new:
                        changes.append(
                            {
                                "path": field,
                                "old_exists": key in old,
                                "new_exists": key in new,
                                "old": old.get(key),
                                "new": new.get(key),
                            }
                        )
                    else:
                        stack.append((field, old[key], new[key]))
            elif not (
                attribute_equal(path, old, new, deepseek=deepseek)
                if openrouter or deepseek
                else json.dumps(old, sort_keys=True, ensure_ascii=False)
                == json.dumps(new, sort_keys=True, ensure_ascii=False)
            ):
                changes.append(
                    {
                        "path": path or "$",
                        "old_exists": True,
                        "new_exists": True,
                        "old": old,
                        "new": new,
                    }
                )
        if changes:
            result["changed"][model_id] = sorted(
                changes, key=lambda change: change["path"]
            )
    return result
