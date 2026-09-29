"""Exercise every shipped template through its configured request and parser."""

import json
from pathlib import Path

import httpx
import pytest

from data.plugins.astrbot_plugin_model_watcher.catalog import WatchSpec, fetch_catalog
from data.plugins.astrbot_plugin_model_watcher.google_auth import TOKEN_URL

from .test_google import service_key as service_key

TEMPLATES = json.loads(
    (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
)["providers"]["templates"]


@pytest.mark.asyncio
@pytest.mark.parametrize("template_key", list(TEMPLATES))
async def test_template_authentication_and_response_parsing(template_key, service_key):
    entry = {
        key: metadata["default"]
        for key, metadata in TEMPLATES[template_key]["items"].items()
    }
    entry.update(entry_id="test", __template_key=template_key, api_key="test-key")
    if template_key == "custom":
        entry["full_url"] = "https://custom.test/catalog"
    if template_key == "vertex":
        entry["api_key"] = json.dumps(service_key[0])
    spec = WatchSpec.from_entry(entry)
    calls = []

    def respond(request):
        calls.append(request)
        if template_key == "vertex" and request.method == "POST":
            assert str(request.url) == TOKEN_URL
            return httpx.Response(
                200, json={"access_token": "test-token", "expires_in": 3600}
            )
        assert request.method == "GET"
        assert request.url == httpx.URL(spec.url)
        if template_key == "gemini":
            assert request.headers["x-goog-api-key"] == "test-key"
            assert "Authorization" not in request.headers
            payload = {"models": [{"name": "models/test-model"}]}
        elif template_key == "vertex":
            assert request.headers["Authorization"] == "Bearer test-token"
            assert request.headers["X-Goog-User-Project"] == "test-project"
            payload = {
                "publisherModels": [
                    {"name": "publishers/test/models/test-model", "versionId": "2"}
                ]
            }
        else:
            assert request.headers["Authorization"] == "Bearer test-key"
            rows = [{"id": "test-model", "context_length": 4096}]
            payload = {"data": rows}
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await fetch_catalog(client, spec)
    expected_id = {
        "gemini": "models/test-model",
        "vertex": "publishers/test/models/test-model@2",
    }.get(template_key, "test-model")
    assert list(models) == [expected_id]
    assert len(calls) == (2 if template_key == "vertex" else 1)
