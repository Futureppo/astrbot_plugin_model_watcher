"""Google catalog pagination and service account authentication regressions."""

import copy
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from data.plugins.astrbot_plugin_model_watcher.catalog import WatchSpec, fetch_catalog
from data.plugins.astrbot_plugin_model_watcher.google_auth import TOKEN_URL


@pytest.fixture(scope="module")
def service_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "test-key-id",
        "private_key": private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "client_email": "watcher@test-project.iam.gserviceaccount.com",
        "token_uri": TOKEN_URL,
    }, private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


@pytest.fixture
def vertex_spec(service_key):
    return WatchSpec.from_entry(
        {
            "entry_id": "vertex",
            "api_type": "vertex",
            "base_url": "https://us-central1-aiplatform.googleapis.com",
            "full_url": "https://us-central1-aiplatform.googleapis.com/v1beta1/publishers/*/models?pageSize=100&listAllVersions=true",
            "api_key": json.dumps(service_key[0]),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "gemini-test-secret"])
async def test_gemini_key_pagination_and_custom_paths(key, caplog):
    calls = []
    spec = WatchSpec.from_entry(
        {
            "entry_id": "gemini",
            "api_type": "gemini",
            "api_key": key,
            "base_url": "https://generativelanguage.googleapis.com",
            "full_url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=5&filter=example",
            "models_path": "result.models",
            "id_path": "details.name",
        }
    )

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers.get("x-goog-api-key") == (key or None)
        assert "Authorization" not in request.headers
        assert request.url.params["pageSize"] == "5"
        assert request.url.params["filter"] == "example"
        second = request.url.params.get("pageToken") == "private+/token="
        return httpx.Response(
            200,
            json={
                "result": {"models": [{"details": {"name": "b" if second else "a"}}]},
                "nextPageToken": "" if second else "private+/token=",
            },
        )

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert set(await fetch_catalog(client, spec)) == {"a", "b"}
    assert len(calls) == 2
    assert "private" not in caplog.text and "gemini-test-secret" not in caplog.text


@pytest.mark.parametrize(
    "api_type,base,expected",
    [
        ("gemini", "https://google.test", "https://google.test/v1beta/models"),
        ("gemini", "https://google.test/v1beta/", "https://google.test/v1beta/models"),
        ("gemini", "https://google.test/v1", "https://google.test/v1/models"),
        (
            "vertex",
            "https://google.test/",
            "https://google.test/v1beta1/publishers/*/models",
        ),
        (
            "vertex",
            "https://google.test/v1beta1/",
            "https://google.test/v1beta1/publishers/*/models",
        ),
    ],
)
def test_google_base_endpoints_and_defaults(api_type, base, expected):
    spec = WatchSpec.from_entry(
        {"entry_id": "one", "api_type": api_type, "base_url": base}
    )
    assert spec.url == expected
    assert spec.id_path == "name"
    assert spec.models_path == ("models" if api_type == "gemini" else "publisherModels")


@pytest.mark.asyncio
async def test_vertex_signed_exchange_cache_expiry_and_versions(
    vertex_spec, service_key
):
    exchanges = []

    def respond(request):
        if str(request.url) == TOKEN_URL:
            assert request.method == "POST"
            assert "Authorization" not in request.headers
            assert "x-goog-api-key" not in request.headers
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            claims = jwt.decode(body["assertion"][0], certs=service_key[1])
            assert claims["iss"] == service_key[0]["client_email"]
            assert claims["aud"] == TOKEN_URL
            assert claims["scope"] == "https://www.googleapis.com/auth/cloud-platform"
            assert claims["exp"] - claims["iat"] == 3600
            assert "PRIVATE KEY" not in request.content.decode()
            exchanges.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.headers["X-Goog-User-Project"] == "test-project"
        assert "x-goog-api-key" not in request.headers
        assert request.url.params["listAllVersions"] == "true"
        second = request.url.params.get("pageToken") == "next"
        return httpx.Response(
            200,
            json={
                "publisherModels": [
                    {
                        "name": "publishers/example/models/chat",
                        "versionId": "002" if second else "001",
                    }
                ],
                "nextPageToken": "" if second else "next",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        expected = {
            "publishers/example/models/chat@001",
            "publishers/example/models/chat@002",
        }
        assert set(await fetch_catalog(client, vertex_spec)) == expected
        assert set(await fetch_catalog(client, vertex_spec)) == expected
        assert len(exchanges) == 1
        vertex_spec.auth._expires_at = 0
        assert set(await fetch_catalog(client, vertex_spec)) == expected
        assert len(exchanges) == 2
    assert "PRIVATE KEY" not in repr(vertex_spec)
    assert "access-token" not in repr(vertex_spec)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401])
async def test_vertex_retries_rejected_token_once(vertex_spec, status):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"access_token": f"token-{len(calls)}", "expires_in": 3600}
            )
        if len(calls) == 2:
            return httpx.Response(401)
        return httpx.Response(status, json={"publisherModels": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if status == 200:
            assert await fetch_catalog(client, vertex_spec) == {}
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_catalog(client, vertex_spec)
    assert [request.method for request in calls] == ["POST", "GET", "POST", "GET"]


@pytest.mark.parametrize(
    "change",
    [
        {"type": "authorized_user"},
        {"private_key": "invalid"},
        {"project_id": ""},
        {"client_email": None},
        {"token_uri": "https://untrusted.test/token"},
    ],
)
def test_reject_invalid_credentials(service_key, change):
    info = {**service_key[0], **change}
    with pytest.raises(ValueError, match="Invalid service account JSON"):
        WatchSpec.from_entry(
            {
                "entry_id": "v",
                "api_type": "vertex",
                "base_url": "https://vertex.test",
                "api_key": json.dumps(info),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload",
    [
        (400, {"error": "invalid_grant"}),
        (200, []),
        (200, {}),
        (200, {"access_token": "token", "expires_in": -1}),
        (200, {"access_token": "token", "expires_in": True}),
    ],
)
async def test_bad_token_response_never_fetches_catalog(vertex_spec, status, payload):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await fetch_catalog(client, vertex_spec)
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [False, 5, "repeated"])
async def test_bad_google_pagination_retains_failure(token):
    spec = WatchSpec.from_entry(
        {"entry_id": "g", "api_type": "gemini", "base_url": "https://google.test"}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"models": [], "nextPageToken": token}
            )
        )
    ) as client:
        with pytest.raises(ValueError):
            await fetch_catalog(client, spec)


def test_google_identity_changes_with_credentials_and_api_type(service_key):
    entry = {
        "entry_id": "v",
        "api_type": "vertex",
        "base_url": "https://vertex.test",
        "api_key": json.dumps(service_key[0]),
    }
    before = WatchSpec.from_entry(entry)
    changed = copy.deepcopy(service_key[0])
    changed["project_id"] = "another-project"
    after = WatchSpec.from_entry({**entry, "api_key": json.dumps(changed)})
    assert before.fingerprint != after.fingerprint
    assert "PRIVATE KEY" not in before.fingerprint
