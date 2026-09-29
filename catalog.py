"""Model catalog configuration, retrieval, and structural comparison."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

from astrbot.core.platform.message_session import MessageSession

_PRIVATE_REQUEST = ContextVar("model_watcher_private_request", default=False)


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
    fingerprint: str

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
        raw_interval = entry.get("interval_seconds", 30)
        try:
            interval = int(raw_interval)
            if isinstance(raw_interval, bool) or str(interval) != str(raw_interval):
                interval = 30
            if interval <= 0:
                interval = 30
        except (TypeError, ValueError, OverflowError):
            interval = 30
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
            fingerprint=fingerprint,
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
        next_url = links.get("next") if isinstance(links, dict) else None
        if not next_url:
            return models
        if not isinstance(next_url, str):
            raise ValueError("Invalid pagination link")
        url = url.join(next_url)
        if (url.scheme, url.host, url.port) != origin or url.userinfo or url.fragment:
            raise ValueError("Pagination must remain on the API origin")
    raise ValueError("Catalog exceeded the pagination limit")


def compare_catalogs(
    previous: dict[str, Any], current: dict[str, Any], ignored_paths: tuple[str, ...]
) -> dict[str, Any]:
    """Compare catalog membership and nested attributes without mutating snapshots.

    Args:
        previous: Last successfully fetched raw catalog.
        current: Newly fetched raw catalog.
        ignored_paths: Model-relative paths to omit from both sides.

    Returns:
        Added and removed IDs plus changed fields with explicit value presence.
    """
    result: dict[str, Any] = {
        "added": sorted(current.keys() - previous.keys()),
        "removed": sorted(previous.keys() - current.keys()),
        "changed": {},
    }
    for model_id in sorted(previous.keys() & current.keys()):
        before, after = (
            copy.deepcopy(previous[model_id]),
            copy.deepcopy(current[model_id]),
        )
        for value in (before, after):
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
        if "$" in ignored_paths:
            continue
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
            elif json.dumps(old, sort_keys=True, ensure_ascii=False) != json.dumps(
                new, sort_keys=True, ensure_ascii=False
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
