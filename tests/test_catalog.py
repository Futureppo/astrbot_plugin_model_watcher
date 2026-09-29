"""Behavior tests for configuration, catalogs, and attribute comparison."""

import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

from data.plugins.astrbot_plugin_model_watcher.catalog import (
    WatchSpec,
    compare_catalogs,
    fetch_catalog,
)


@pytest.fixture
def spec_factory():
    def build(**kwargs):
        return WatchSpec.from_entry(
            {"entry_id": "one", "base_url": "https://example.test", **kwargs}
        )

    return build


@pytest.mark.parametrize(
    "base,full,expected",
    [
        ("https://example.test/", "", "https://example.test/v1/models"),
        ("https://example.test/v1/", "", "https://example.test/v1/models"),
        ("https://openrouter.ai/api", "", "https://openrouter.ai/api/v1/models"),
        (
            "ignored",
            "https://example.test/catalog?type=all",
            "https://example.test/catalog?type=all",
        ),
    ],
)
def test_endpoint_precedence(spec_factory, base, full, expected):
    assert spec_factory(base_url=base, full_url=full).url == expected


@pytest.mark.parametrize("interval", [0, -1, "invalid", None, True, 1.5])
def test_invalid_intervals_default_to_thirty(spec_factory, interval):
    assert spec_factory(interval_seconds=interval).interval == 30


def test_targets_and_identity(spec_factory):
    original = spec_factory(
        api_key="secret", umo_whitelist=[" bot:GroupMessage:42 ", "bot:GroupMessage:42"]
    )
    renamed = spec_factory(
        api_key="secret",
        name="Another",
        proxy="socks5://localhost:1080",
        interval_seconds=7,
        ignored_paths=["pricing"],
    )
    assert original.targets == ("bot:GroupMessage:42",)
    assert original.fingerprint == renamed.fingerprint
    assert "secret" not in original.fingerprint
    assert spec_factory(api_key="different").fingerprint != original.fingerprint
    assert spec_factory(id_path="name").fingerprint != original.fingerprint
    with pytest.raises(ValueError):
        spec_factory(umo_whitelist=["42"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": ""},
        {"full_url": "file:///tmp/models"},
        {"proxy": "ftp://localhost"},
        {"models_path": "data..models"},
        {"full_url": "https://user:password@example.test/models"},
    ],
)
def test_invalid_configuration(spec_factory, kwargs):
    with pytest.raises(ValueError):
        spec_factory(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "test-key"])
async def test_fetch_authentication_and_pagination(spec_factory, key):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200, json={"data": [{"id": "b"}], "links": {"next": None}}
            )
        return httpx.Response(
            200, json={"data": [{"id": "a"}], "links": {"next": "?page=2"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await fetch_catalog(client, spec_factory(api_key=key))
    assert list(models) == ["a", "b"]
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(
        request.headers.get("Authorization") == (f"Bearer {key}" if key else None)
        for request in requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,kwargs,expected",
    [
        (
            {"result": {"models": [{"details": {"name": "m"}}]}},
            {"models_path": "result.models", "id_path": "details.name"},
            ["m"],
        ),
        (["a", "b"], {"models_path": "$"}, ["a", "b"]),
        (
            {"batches": [{"data": [{"id": "first"}]}]},
            {"models_path": "batches.0.data"},
            ["first"],
        ),
        ({"data": []}, {}, []),
    ],
)
async def test_custom_json_shapes(spec_factory, payload, kwargs, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        assert list(await fetch_catalog(client, spec_factory(**kwargs))) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": "unauthorized"},
        {"data": {}},
        {"data": [None]},
        {"data": [{}]},
        {"data": [{"id": " "}]},
        {"data": [{"id": 42}]},
        {"data": [{"id": "a"}, {"id": "a"}]},
        {"data": [], "links": {"next": "https://evil.test/models"}},
        {"data": [], "links": {"next": "/v1/models"}},
    ],
)
async def test_bad_catalog_is_rejected(spec_factory, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ValueError):
            await fetch_catalog(client, spec_factory(api_key="never-forward"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "links", [[], "next", {"next": False}, {"next": 0}, {"next": []}, {"next": {}}]
)
async def test_malformed_pagination_does_not_accept_a_partial_catalog(
    spec_factory, links
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"id": "first"}], "links": links}
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="pagination"):
            await fetch_catalog(client, spec_factory())


@pytest.mark.asyncio
async def test_failed_later_page_rejects_entire_catalog(spec_factory):
    def respond(request):
        if request.url.params.get("page"):
            return httpx.Response(503)
        return httpx.Response(
            200, json={"data": [{"id": "a"}], "links": {"next": "?page=2"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_catalog(client, spec_factory())


@pytest.mark.asyncio
async def test_request_url_secrets_are_not_logged(spec_factory, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": []})
        )
    ) as client:
        await fetch_catalog(
            client,
            spec_factory(
                full_url="https://example.test/models?token=secret-query-token"
            ),
        )
        await client.get("https://example.test/public")
    assert "secret-query-token" not in caplog.text
    assert "https://example.test/public" in caplog.text


def test_attribute_types_are_distinct_inside_arrays():
    before = {"m": {"values": [True]}}
    after = {"m": {"values": [1]}}
    assert compare_catalogs(before, after, ())["changed"]["m"][0]["path"] == "values"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body", [(401, "{}"), (429, "{}"), (500, "{}"), (200, "<html>Error</html>")]
)
async def test_http_errors_and_invalid_json(spec_factory, status, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    ) as client:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await fetch_catalog(client, spec_factory())


def test_compare_nested_attributes_and_ignored_paths():
    before = {
        "old": {"id": "old"},
        "same": {
            "id": "same",
            "pricing": {"prompt": "1", "completion": "2"},
            "temporary": 1,
            "gone": None,
        },
    }
    after = {
        "new": {"id": "new"},
        "same": {
            "id": "same",
            "pricing": {"prompt": "9", "completion": "3"},
            "temporary": 99,
            "fresh": None,
        },
    }
    original = copy.deepcopy(before)
    diff = compare_catalogs(
        before, after, ("pricing.prompt", "temporary", "missing.path")
    )
    assert diff["added"] == ["new"] and diff["removed"] == ["old"]
    fields = {field["path"]: field for field in diff["changed"]["same"]}
    assert set(fields) == {"pricing.completion", "gone", "fresh"}
    assert fields["fresh"]["old_exists"] is False
    assert fields["fresh"]["new"] is None
    assert fields["gone"]["new_exists"] is False
    assert before == original


def test_order_and_array_semantics():
    a = {"m": {"a": 1, "b": 2, "list": ["x", "y"]}}
    b = {"m": {"b": 2, "a": 1, "list": ["x", "y"]}}
    assert not compare_catalogs(a, b, ())["changed"]
    b["m"]["list"] = ["y", "x"]
    assert compare_catalogs(a, b, ())["changed"]
    assert not compare_catalogs(a, b, ("list",))["changed"]
    assert compare_catalogs(a, {}, ("$",))["removed"] == ["m"]


def test_all_provider_templates_and_defaults():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert schema["providers"]["default"] == []
    templates = schema["providers"]["templates"]
    expected_endpoints = {
        "openrouter": "https://openrouter.ai/api/v1/models",
        "openai": "https://api.openai.com/v1/models",
        "xai": "https://api.x.ai/v1/models",
        "kimi": "https://api.moonshot.cn/v1/models",
        "deepseek": "https://api.deepseek.com/v1/models",
        "kimi_intl": "https://api.moonshot.ai/v1/models",
        "groq": "https://api.groq.com/openai/v1/models",
        "mistral": "https://api.mistral.ai/v1/models",
        "together": "https://api.together.ai/v1/models",
        "cerebras": "https://api.cerebras.ai/v1/models",
        "sambanova": "https://api.sambanova.ai/v1/models",
        "nvidia": "https://integrate.api.nvidia.com/v1/models",
        "siliconflow": "https://api.siliconflow.cn/v1/models",
        "siliconflow_intl": "https://api.siliconflow.com/v1/models",
        "stepfun": "https://api.stepfun.com/v1/models",
        "novita": "https://api.novita.ai/v3/openai/models",
        "deepinfra": "https://api.deepinfra.com/v1/openai/models",
        "huggingface": "https://router.huggingface.co/v1/models",
        "chutes": "https://llm.chutes.ai/v1/models",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        "vertex": "https://us-central1-aiplatform.googleapis.com/v1beta1/publishers/*/models?pageSize=100&listAllVersions=true&filter=is_hf_wildcard(false)",
        "custom": "https://example.test/catalog",
    }
    assert set(templates) == set(expected_endpoints)
    for key, template in templates.items():
        entry = {
            field: copy.deepcopy(meta["default"])
            for field, meta in template["items"].items()
        }
        entry["entry_id"] = key
        if key == "custom":
            entry["full_url"] = "https://example.test/catalog"
        spec = WatchSpec.from_entry(entry)
        assert spec.url == expected_endpoints[key]
        assert spec.models_path == {
            "together": "$",
            "gemini": "models",
            "vertex": "publisherModels",
        }.get(key, "data")
        assert spec.interval == 30 and spec.targets == ()
        assert spec.comparison_mode == (
            "仅模型 ID" if key == "openrouter" else "模型 ID 和属性"
        )
        assert template["items"]["umo_whitelist"]["_special"] == "select_umos"


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"base_url": "https://openrouter.ai/api"}, "仅模型 ID"),
        (
            {
                "base_url": "https://relay.test",
                "__template_key": "openrouter",
                "name": "Renamed",
            },
            "仅模型 ID",
        ),
        ({"base_url": "https://other.test"}, "模型 ID 和属性"),
        (
            {
                "base_url": "https://openrouter.ai/api",
                "comparison_mode": "模型 ID 和属性",
            },
            "模型 ID 和属性",
        ),
        (
            {"base_url": "https://other.test", "comparison_mode": "仅模型 ID"},
            "仅模型 ID",
        ),
    ],
)
def test_comparison_mode_defaults_and_overrides(entry, expected):
    assert (
        WatchSpec.from_entry({"entry_id": "one", **entry}).comparison_mode == expected
    )


def test_ids_only_preserves_membership_changes_without_copying_attributes():
    class Uncopyable:
        def __deepcopy__(self, memo):
            pytest.fail("ID-only comparison copied model attributes")

    assert compare_catalogs(
        {"old": {}, "same": Uncopyable()},
        {"new": {}, "same": Uncopyable()},
        (),
        compare_attributes=False,
    ) == {"added": ["new"], "removed": ["old"], "changed": {}}
