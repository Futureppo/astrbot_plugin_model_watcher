"""Useful error context and credential redaction regressions."""

import json
from urllib.parse import parse_qs, quote

import httpx
import pytest

from data.plugins.astrbot_plugin_model_watcher.catalog import WatchSpec, fetch_catalog
from data.plugins.astrbot_plugin_model_watcher.diagnostics import describe_fetch_failure
from data.plugins.astrbot_plugin_model_watcher.google_auth import TOKEN_URL

from .test_google import service_key as service_key


@pytest.fixture
def spec():
    return WatchSpec.from_entry(
        {
            "entry_id": "gemini-entry",
            "name": "Gemini官",
            "api_type": "gemini",
            "base_url": "https://generativelanguage.googleapis.com",
            "api_key": "private-api-key",
        }
    )


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (
            400,
            {
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "API key not valid.",
                }
            },
            ["code=400", "INVALID_ARGUMENT", "API key not valid."],
        ),
        (
            403,
            {
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": "API is disabled",
                    "details": [
                        {
                            "reason": "SERVICE_DISABLED",
                            "metadata": {"secret": "hidden-metadata"},
                        }
                    ],
                }
            },
            ["PERMISSION_DENIED", "API is disabled", "reason=SERVICE_DISABLED"],
        ),
        (
            401,
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "message": "Incorrect API key",
                }
            },
            ["invalid_request_error", "invalid_api_key", "Incorrect API key"],
        ),
        (
            403,
            {"message": "Access denied", "token": "hidden-token"},
            ["message=Access denied"],
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
            ["error=invalid_grant", "Invalid JWT Signature."],
        ),
    ],
)
def test_error_shapes_include_actionable_details(spec, status, payload, expected):
    response = httpx.Response(
        status, json=payload, request=httpx.Request("GET", spec.url)
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    summary = describe_fetch_failure(spec, failure.value)
    assert "name='Gemini官'" in summary
    assert "entry=gemini-entry" in summary
    assert "stage=catalog" in summary
    assert "host=generativelanguage.googleapis.com" in summary
    assert "proxy=direct" in summary
    assert f"HTTP {status}" in summary
    assert all(text in summary for text in expected)
    assert "hidden-" not in summary


def test_echoed_credentials_urls_and_control_characters_are_redacted():
    key = "private/key+with spaces"
    spec = WatchSpec.from_entry(
        {
            "entry_id": "one",
            "name": "OpenAI官\nforged log " + key,
            "full_url": "https://api.example.test/models?key=query-secret",
            "api_key": key,
            "proxy": "http://proxy-user:proxy-password@localhost:7890",
        }
    )
    request = httpx.Request(
        "GET",
        "https://api.example.test/models?key=query-secret&pageToken=page-secret",
        headers={"Authorization": "Bearer runtime-token"},
    )
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "message": f"Invalid key {key} {quote(key, safe='')} query-secret page-secret runtime-token proxy-user proxy-password. See {spec.url}\n\x1b[31mDenied"
            }
        },
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    summary = describe_fetch_failure(spec, failure.value)
    for secret in (
        key,
        quote(key, safe=""),
        "query-secret",
        "page-secret",
        "runtime-token",
        "proxy-user",
        "proxy-password",
        spec.url,
    ):
        assert secret not in summary
    assert "[redacted]" in summary
    assert "proxy=configured" in summary
    assert "Denied" in summary
    assert "\n" not in summary and "\x1b" not in summary


@pytest.mark.parametrize(
    "body,content_type,expected,omitted",
    [
        (
            "<html>secret-challenge</html>",
            "text/html",
            "HTML/XML error response",
            "secret-challenge",
        ),
        (
            "<html>secret-challenge</html>",
            "",
            "HTML/XML error response",
            "secret-challenge",
        ),
        ("Upstream denied access", "text/plain", "Upstream denied access", "unused"),
        ("", "", "Empty error response", "unused"),
        (
            '{"access_token":"hidden-token"}',
            "application/json",
            "No structured error details",
            "hidden-token",
        ),
        ("x" * 65537, "text/plain", "exceeds 64 KiB", "x" * 100),
        (
            '{"access_token":"malformed-secret"',
            "application/json",
            "Malformed JSON error response",
            "malformed-secret",
        ),
        (
            "[" * 1500 + '"nested-secret"' + "]" * 1500,
            "application/json",
            "error",
            "nested-secret",
        ),
    ],
    ids=[
        "html",
        "untyped-html",
        "plain-text",
        "empty",
        "unknown-fields",
        "oversized",
        "malformed-json",
        "deep-json",
    ],
)
def test_unstructured_errors_are_bounded(spec, body, content_type, expected, omitted):
    response = httpx.Response(
        403,
        text=body,
        headers={"Content-Type": content_type},
        request=httpx.Request("GET", spec.url),
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    summary = describe_fetch_failure(spec, failure.value)
    assert expected in summary
    assert omitted not in summary


def test_long_error_message_is_redacted_before_truncation(spec):
    response = httpx.Response(
        400,
        json={"error": {"message": "x" * 495 + spec.api_key + "x" * 1000}},
        request=httpx.Request("GET", spec.url),
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    summary = describe_fetch_failure(spec, failure.value)
    detail = summary.split("; detail=", 1)[1]
    assert len(detail) == 512 and detail.endswith("...")
    assert "private-api" not in detail


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadTimeout, ValueError, TimeoutError]
)
def test_non_http_failures_retain_safe_reason(spec, error_type):
    failure = error_type(f"Connection failed for {spec.url}?key=private-api-key")
    summary = describe_fetch_failure(spec, failure)
    assert error_type.__name__ in summary
    assert "Connection failed" in summary
    assert "HTTP n/a" in summary
    assert "stage=fetch" in summary
    assert spec.api_key not in summary
    assert "https://" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", ["oauth", "catalog"])
async def test_vertex_failures_hide_service_account_and_runtime_tokens(
    service_key, fail_stage
):
    info = service_key[0]
    spec = WatchSpec.from_entry(
        {
            "entry_id": "vertex",
            "api_type": "vertex",
            "base_url": "https://us-central1-aiplatform.googleapis.com",
            "api_key": json.dumps(info),
        }
    )
    observed_secrets = []

    def respond(request):
        if request.method == "POST" and fail_stage == "catalog":
            return httpx.Response(
                200, json={"access_token": "runtime-access-token", "expires_in": 3600}
            )
        if request.method == "POST":
            observed_secrets.append(parse_qs(request.content.decode())["assertion"][0])
        else:
            observed_secrets.append("runtime-access-token")
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "Denied: "
                    + " ".join(
                        [
                            *observed_secrets,
                            info["private_key"],
                            info["client_email"],
                            json.dumps(info),
                        ]
                    )
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await fetch_catalog(client, spec)
    summary = describe_fetch_failure(spec, failure.value)
    assert f"stage={fail_stage}" in summary
    assert (
        f"host={httpx.URL(TOKEN_URL).host if fail_stage == 'oauth' else httpx.URL(spec.url).host}"
        in summary
    )
    assert "Denied" in summary
    for secret in [
        *observed_secrets,
        info["client_email"],
        info["private_key"].splitlines()[1],
    ]:
        assert secret not in summary
    assert "PRIVATE KEY" not in summary
