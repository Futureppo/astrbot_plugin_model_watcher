"""Bounded failure summaries for model catalog requests."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, quote, quote_plus

import httpx

from .catalog import WatchSpec


def describe_fetch_failure(spec: WatchSpec, exc: Exception) -> str:
    """Describe a failed fetch without logging credentials or raw response bodies.

    Args:
        spec: Watcher configuration used for the failed fetch.
        exc: Request, authentication, or catalog validation failure.

    Returns:
        A single-line summary with entry context and redacted error details.
    """
    try:
        request = getattr(exc, "request", None)
    except RuntimeError:
        request = None
    url = request.url if request is not None else httpx.URL(spec.url)
    stage = (
        "fetch"
        if request is None
        else "oauth"
        if spec.api_type == "vertex"
        and request.method == "POST"
        and url.host == "oauth2.googleapis.com"
        else "catalog"
    )
    status = "n/a"
    detail = str(exc) or "No error message"
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = str(response.status_code)
        detail = "No structured error details"
        # Do not dump HTML challenges, arbitrary JSON fields, or large bodies.
        if len(response.content) > 65536:
            detail = "Error response exceeds 64 KiB; body omitted"
        else:
            try:
                payload = response.json()
            except (ValueError, RecursionError):
                media_type = response.headers.get("content-type", "").lower()
                if "html" in media_type or response.text.lstrip().startswith("<"):
                    detail = "HTML/XML error response; possible gateway or access block"
                elif "json" in media_type or response.text.lstrip().startswith(
                    ("{", "[")
                ):
                    detail = "Malformed JSON error response; body omitted"
                elif response.text.strip():
                    detail = response.text
                else:
                    detail = "Empty error response"
            else:
                if isinstance(payload, dict):
                    error = payload.get("error", payload)
                    fields = []
                    if isinstance(error, str):
                        fields.append(f"error={error}")
                        error = payload
                    if isinstance(error, dict):
                        for key in (
                            "code",
                            "status",
                            "type",
                            "message",
                            "error_description",
                        ):
                            value = error.get(key)
                            if isinstance(value, (str, int)) and not isinstance(
                                value, bool
                            ):
                                fields.append(f"{key}={value}")
                        details = error.get("details")
                        if isinstance(details, list):
                            for item in details[:5]:
                                if isinstance(item, dict) and isinstance(
                                    item.get("reason"), str
                                ):
                                    fields.append(f"reason={item['reason']}")
                    if fields:
                        detail = "; ".join(fields)

    # Providers can echo credentials inside otherwise useful error messages.
    secrets = {spec.api_key}
    if spec.api_type == "vertex" and spec.api_key:
        info = json.loads(spec.api_key)
        secrets.update(value for value in info.values() if isinstance(value, str))
        secrets.update(info.get("private_key", "").splitlines())
    for source_url in (httpx.URL(spec.url), url):
        secrets.update(source_url.params.values())
    if spec.proxy:
        proxy_url = httpx.URL(spec.proxy)
        secrets.update((proxy_url.username, proxy_url.password))
    if request is not None:
        for header in (
            "authorization",
            "proxy-authorization",
            "x-goog-api-key",
            "x-api-key",
            "cookie",
        ):
            value = request.headers.get(header, "")
            secrets.add(value)
            if header.endswith("authorization"):
                secrets.add(value.partition(" ")[2])
        if stage == "oauth":
            try:
                secrets.update(
                    value for _, value in parse_qsl(request.content.decode())
                )
            except (httpx.RequestNotRead, UnicodeDecodeError):
                pass
    variants = set()
    for secret in secrets - {""}:
        variants.update(
            (
                secret,
                json.dumps(secret)[1:-1],
                quote(secret, safe=""),
                quote_plus(secret),
            )
        )
    values = [spec.entry_id, spec.name, url.host, detail]
    for index, value in enumerate(values):
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[redacted]",
            value,
            flags=re.DOTALL,
        )
        value = re.sub(r"(?i)\b(?:https?|socks5h?)://\S+", "[URL omitted]", value)
        for secret in sorted(variants, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        value = re.sub(
            r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", value
        )
        value = re.sub(
            r"\b(?:sk-[A-Za-z0-9_*.-]+|AIza[A-Za-z0-9_-]+)", "[redacted]", value
        )
        value = " ".join(
            "".join(char if char.isprintable() else " " for char in value).split()
        )
        limit = 512 if index == 3 else 128
        values[index] = value[: limit - 3] + "..." if len(value) > limit else value
    entry_id, name, host, detail = values
    return (
        f"entry={entry_id} name={name!r} api={spec.api_type} stage={stage} "
        f"host={host} proxy={'configured' if spec.proxy else 'direct'} "
        f"({type(exc).__name__}, HTTP {status}); detail={detail}"
    )
