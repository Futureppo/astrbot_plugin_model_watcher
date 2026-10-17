"""Lifecycle, persistence, delivery, and failure isolation tests."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

from data.plugins.astrbot_plugin_model_watcher import main
from data.plugins.astrbot_plugin_model_watcher.catalog import WatchSpec


class Config(dict):
    def save_config(self):
        self.saved = True


@pytest_asyncio.fixture
async def watcher_factory(monkeypatch):
    plugins = []
    monkeypatch.setattr(main, "render_card", lambda *args: b"image")

    async def build(entries=None, stored=None):
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(get_insts=lambda: []),
            get_config=lambda: {"timezone": "Asia/Shanghai"},
            send_message=AsyncMock(return_value=True),
        )
        config = Config(
            providers=entries
            if entries is not None
            else [
                {
                    "entry_id": "one",
                    "base_url": "https://example.test",
                    "umo_whitelist": ["bot:GroupMessage:1", "bot:FriendMessage:2"],
                }
            ]
        )
        plugin = main.ModelWatcher(context, config)
        plugin.get_kv_data = AsyncMock(return_value=copy.deepcopy(stored))
        plugin.put_kv_data = AsyncMock()
        await plugin.initialize()
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="bot", support_proactive_message=True)
        )
        context.platform_manager.get_insts = lambda: [platform]
        plugins.append(plugin)
        return plugin

    yield build
    for plugin in plugins:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"detection_items": ["system_fingerprint"]},
        {"fingerprint_probe": {"enabled": True}},
        {
            "detection_items": ["system_fingerprint"],
            "fingerprint_probe": {"enabled": True},
            "ignored_paths": ["system_fingerprint"],
        },
    ],
)
async def test_fingerprint_requests_require_selection_and_switch(
    watcher_factory, settings
):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "one",
                "__template_key": "deepseek",
                "base_url": "https://example.test",
                **settings,
            }
        ]
    )
    spec = plugin._specs[0]
    assert not spec.fingerprint_enabled
    assert spec.fingerprint_interval == 240
    assert spec.fingerprint_max_tokens == 16
    plugin._clients["one"] = AsyncMock()
    plugin._state["entries"]["one"]["snapshot"] = {"a": {"id": "a"}}
    await plugin._run_cycle(spec, fingerprints=True)
    plugin._clients["one"].post.assert_not_called()


@pytest.mark.asyncio
async def test_fingerprint_baselines_failures_and_restart(watcher_factory, monkeypatch):
    entry = {
        "entry_id": "one",
        "__template_key": "deepseek",
        "base_url": "https://example.test/v1/",
        "api_key": "test-key",
        "detection_items": ["system_fingerprint"],
        "fingerprint_probe": {"enabled": True, "max_tokens": 8},
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    spec = plugin._specs[0]
    models = {"a": {"id": "a"}, "b": {"id": "b"}}
    monkeypatch.setattr(main, "fetch_catalog", AsyncMock(return_value=models))
    render = Mock(return_value=b"image")
    monkeypatch.setattr(main, "render_card", render)
    values = {"a": "fp-a", "b": "fp-b"}
    status = {"a": 200, "b": 200}
    requests = []

    def respond(request):
        body = json.loads(request.content)
        assert request.method == "POST"
        assert str(request.url) == "https://example.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert body == {
            "model": body["model"],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        requests.append(body["model"])
        return httpx.Response(
            status[body["model"]],
            json={"system_fingerprint": values[body["model"]]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        plugin._clients["one"] = client
        await plugin._run_cycle(spec)
        assert requests == []
        await plugin._run_cycle(spec, fingerprints=True)
        assert requests == ["a", "b"]
        plugin.context.send_message.assert_not_called()
        values["a"] = "fp-a-new"
        status["b"] = 429
        plugin.context.send_message.return_value = False
        await plugin._run_cycle(spec, fingerprints=True)
        state = plugin._state["entries"]["one"]
        assert state["snapshot"] == models
        assert state["system_fingerprints"] == {
            "a": {"system_fingerprint": "fp-a-new"},
            "b": {"system_fingerprint": "fp-b"},
        }
        assert '"fp-a" → "fp-a-new"' in render.call_args.args[0]
        assert "system_fingerprint" in render.call_args.args[0]
        assert len(state["pending"]) == 1

        reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
        reloaded._clients["one"] = client
        values.update(a=None, b="")
        status["b"] = 200
        await reloaded._run_cycle(reloaded._specs[0], fingerprints=True)
        reloaded.context.send_message.assert_awaited_once()
        assert (
            reloaded._state["entries"]["one"]["system_fingerprints"]
            == state["system_fingerprints"]
        )
        assert reloaded._state["entries"]["one"]["pending"] == []

        disabled = await watcher_factory(
            entries=[dict(entry, fingerprint_probe={"enabled": False})],
            stored=plugin._state,
        )
        disabled_state = disabled._state["entries"]["one"]
        assert "system_fingerprints" not in disabled_state
        assert disabled_state["pending"] == []


@pytest.mark.asyncio
async def test_fingerprint_schedule_is_independent(watcher_factory, monkeypatch):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "one",
                "__template_key": "deepseek",
                "base_url": "https://example.test",
                "interval_seconds": 1000,
                "detection_items": ["system_fingerprint"],
                "fingerprint_probe": {"enabled": True},
            }
        ]
    )
    clock = 0
    runs = []

    async def cycle(spec, *, fingerprints=False):
        runs.append((clock, fingerprints))
        if clock >= 480:
            plugin._stopping = True

    async def sleep(seconds):
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(plugin, "_run_cycle", cycle)
    monkeypatch.setattr(
        main.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock)
    )
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    await plugin._monitor(plugin._specs[0])
    assert runs == [(0, False), (0, True), (240, True), (480, True)]


@pytest.mark.asyncio
async def test_baseline_changes_and_restart(watcher_factory, monkeypatch):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "one",
                "__template_key": "openrouter",
                "name": "我的模型目录",
                "base_url": "https://example.test/api/",
                "full_url": "https://example.test/custom/catalog",
                "umo_whitelist": ["bot:GroupMessage:1", "bot:FriendMessage:2"],
            }
        ]
    )
    render = Mock(return_value=b"image")
    monkeypatch.setattr(main, "render_card", render)
    spec = plugin._specs[0]
    plugin._clients[spec.entry_id] = AsyncMock()
    fetch = AsyncMock(
        side_effect=[{"a": {"id": "a"}}, {"a": {"id": "a"}, "b": {"id": "b"}}]
    )
    monkeypatch.setattr(main, "fetch_catalog", fetch)
    await plugin._run_cycle(spec)
    plugin.context.send_message.assert_not_called()
    await plugin._run_cycle(spec)
    assert plugin.context.send_message.await_count == 2
    text = render.call_args.args[0]
    assert "条目名称：我的模型目录\n网址：https://example.test/api/\n" in text
    assert "custom/catalog" not in text
    stored = copy.deepcopy(plugin._state)
    reloaded = await watcher_factory(entries=plugin.config["providers"], stored=stored)
    reloaded._clients[spec.entry_id] = AsyncMock()
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(return_value=stored["entries"]["one"]["snapshot"]),
    )
    await reloaded._run_cycle(reloaded._specs[0])
    reloaded.context.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_failed_target_retries_during_api_failure(watcher_factory, monkeypatch):
    plugin = await watcher_factory()
    spec = plugin._specs[0]
    plugin._clients[spec.entry_id] = AsyncMock()
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(side_effect=[{}, {"a": "a"}, ValueError("bad JSON")]),
    )
    await plugin._run_cycle(spec)
    plugin.context.send_message.side_effect = [True, RuntimeError("offline")]
    await plugin._run_cycle(spec)
    assert plugin._state["entries"]["one"]["pending"][0]["targets"] == [
        "bot:FriendMessage:2"
    ]
    plugin.context.send_message.reset_mock(side_effect=True)
    await plugin._run_cycle(spec)
    plugin.context.send_message.assert_awaited_once()
    assert plugin.context.send_message.call_args.args[0] == "bot:FriendMessage:2"
    assert plugin._state["entries"]["one"]["snapshot"] == {"a": "a"}
    assert plugin._state["entries"]["one"]["pending"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403])
async def test_http_failure_logs_entry_and_reason_without_changing_baseline(
    watcher_factory, monkeypatch, status
):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "gemini-entry",
                "name": "Gemini官",
                "api_type": "gemini",
                "api_key": "private-key",
                "base_url": "https://generativelanguage.googleapis.com",
                "umo_whitelist": ["bot:GroupMessage:1"],
            }
        ]
    )
    spec = plugin._specs[0]
    state = plugin._state["entries"][spec.entry_id]
    state["snapshot"] = {"old": {"name": "old"}}
    state["pending"] = [{"text": "Previous change", "targets": list(spec.targets)}]
    warning = Mock()
    monkeypatch.setattr(main.logger, "warning", warning)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={
                    "error": {
                        "message": "Key private-key rejected",
                        "status": "PERMISSION_DENIED",
                    }
                },
            )
        )
    ) as client:
        plugin._clients[spec.entry_id] = client
        await plugin._run_cycle(spec)
    warning.assert_called_once()
    template, *args = warning.call_args.args
    message = template % tuple(args)
    assert "Gemini官" in message and "gemini-entry" in message
    assert f"HTTP {status}" in message
    assert "PERMISSION_DENIED" in message
    assert "private-key" not in message
    assert "keeping its baseline" in message
    assert state["snapshot"] == {"old": {"name": "old"}}
    assert state["pending"] == []
    plugin.context.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconfigure_identity_and_empty_whitelist(watcher_factory, monkeypatch):
    plugin = await watcher_factory(
        entries=[{"entry_id": "one", "base_url": "https://example.test"}]
    )
    plugin._state["entries"]["one"]["snapshot"] = {"a": "a"}
    changed = dict(
        plugin.config["providers"][0],
        name="Renamed",
        proxy="http://localhost:7890",
        ignored_paths=["pricing"],
    )
    reloaded = await watcher_factory(entries=[changed], stored=plugin._state)
    assert reloaded._state["entries"]["one"]["snapshot"] == {"a": "a"}
    reloaded._clients["one"] = AsyncMock()
    monkeypatch.setattr(main, "fetch_catalog", AsyncMock(return_value={"b": "b"}))
    await reloaded._run_cycle(reloaded._specs[0])
    assert reloaded._state["entries"]["one"]["snapshot"] == {"b": "b"}
    reloaded.context.send_message.assert_not_called()
    changed["api_key"] = "new-account"
    reset = await watcher_factory(entries=[changed], stored=reloaded._state)
    assert reset._state["entries"]["one"]["snapshot"] is None


@pytest.mark.asyncio
async def test_stable_ids_duplicate_entries_and_disable(watcher_factory):
    plugin = await watcher_factory(
        entries=[
            {"base_url": "https://example.test"},
            {"entry_id": "duplicate", "base_url": "https://example.test"},
            {
                "entry_id": "duplicate",
                "base_url": "https://example.test",
                "enabled": False,
            },
        ]
    )
    ids = [entry["entry_id"] for entry in plugin.config["providers"]]
    assert len(set(ids)) == 3 and plugin.config.saved
    assert len(plugin._specs) == 2
    reordered = await watcher_factory(
        entries=list(reversed(plugin.config["providers"])), stored=plugin._state
    )
    assert [entry["entry_id"] for entry in reordered.config["providers"]] == list(
        reversed(ids)
    )


@pytest.mark.asyncio
async def test_failed_storage_keeps_old_baseline(watcher_factory, monkeypatch):
    plugin = await watcher_factory()
    plugin._clients["one"] = AsyncMock()
    monkeypatch.setattr(main, "fetch_catalog", AsyncMock(return_value={"a": "a"}))
    plugin.put_kv_data.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError):
        await plugin._run_cycle(plugin._specs[0])
    assert plugin._state["entries"]["one"]["snapshot"] is None
    plugin.context.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_text_fallback_and_unavailable_platform(watcher_factory, monkeypatch):
    plugin = await watcher_factory()
    state = plugin._state["entries"]["one"]
    state["pending"] = [
        {
            "text": "完整变更内容",
            "targets": ["bot:GroupMessage:1", "absent:GroupMessage:9"],
        }
    ]
    monkeypatch.setattr(
        main, "render_card", lambda *args: (_ for _ in ()).throw(OSError("no font"))
    )
    await plugin._deliver(plugin._specs[0])
    plugin.context.send_message.assert_awaited_once()
    chain = plugin.context.send_message.call_args.args[1]
    assert "完整变更内容" in chain.chain[0].text
    assert state["pending"][0]["targets"] == ["absent:GroupMessage:9"]


@pytest.mark.asyncio
async def test_clients_use_independent_proxies_and_close(watcher_factory, monkeypatch):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "a",
                "base_url": "https://example.test",
                "proxy": "socks5://localhost:1080",
            },
            {
                "entry_id": "b",
                "base_url": "https://example.test",
                "proxy": "http://localhost:7890",
            },
        ]
    )
    clients = []

    def make_client(**kwargs):
        client = AsyncMock()
        clients.append((kwargs, client))
        return client

    monkeypatch.setattr(main.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(plugin, "_monitor", AsyncMock(side_effect=lambda spec: None))
    await plugin.start_watchers()
    await plugin.start_watchers()
    assert len(clients) == 2
    assert [options["proxy"] for options, _ in clients] == [
        "socks5://localhost:1080",
        "http://localhost:7890",
    ]
    assert all(options["trust_env"] is False for options, _ in clients)
    tasks = list(plugin._tasks)
    await plugin.terminate()
    assert all(task.done() for task in tasks)
    assert all(client.aclose.await_count == 1 for _, client in clients)


@pytest.mark.asyncio
async def test_slow_provider_does_not_block_another(watcher_factory, monkeypatch):
    plugin = await watcher_factory()
    second = WatchSpec.from_entry(
        {"entry_id": "two", "base_url": "https://example.test"}
    )
    plugin._specs.append(second)
    plugin._state["entries"]["two"] = {
        "fingerprint": second.fingerprint,
        "snapshot": None,
        "pending": [],
    }
    plugin._clients.update(one=AsyncMock(), two=AsyncMock())
    slow_started, release = asyncio.Event(), asyncio.Event()

    async def fetch(client, spec):
        if spec.entry_id == "one":
            slow_started.set()
            await release.wait()
        return {spec.entry_id: spec.entry_id}

    monkeypatch.setattr(main, "fetch_catalog", fetch)
    task = asyncio.create_task(plugin._run_cycle(plugin._specs[0]))
    await slow_started.wait()
    await asyncio.wait_for(plugin._run_cycle(second), 1)
    assert plugin._state["entries"]["two"]["snapshot"] == {"two": "two"}
    release.set()
    await task


@pytest.mark.asyncio
async def test_openrouter_new_failed_delivery_survives_restart(
    watcher_factory, monkeypatch
):
    entry = {
        "entry_id": "or",
        "__template_key": "openrouter",
        "base_url": "https://openrouter.ai/api",
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    plugin._state["entries"]["or"]["snapshot"] = {"a": "a"}
    plugin._clients["or"] = AsyncMock()
    plugin.context.send_message.return_value = False
    monkeypatch.setattr(
        main, "fetch_catalog", AsyncMock(return_value={"a": "a", "b": "b"})
    )
    await plugin._run_cycle(plugin._specs[0])
    assert len(plugin._state["entries"]["or"]["pending"]) == 1
    reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
    reloaded._clients["or"] = AsyncMock()
    await reloaded._run_cycle(reloaded._specs[0])
    reloaded.context.send_message.assert_awaited_once()
    assert reloaded._state["entries"]["or"]["pending"] == []
    again = await watcher_factory(entries=[entry], stored=reloaded._state)
    again._clients["or"] = AsyncMock()
    await again._run_cycle(again._specs[0])
    again.context.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [(["membership"], 0), (["pricing"], 1)])
async def test_explicit_comparison_mode_controls_attribute_notifications(
    watcher_factory, monkeypatch, mode, expected
):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "or",
                "__template_key": "openrouter",
                "base_url": "https://openrouter.ai/api",
                "comparison_mode": mode,
                "umo_whitelist": ["bot:GroupMessage:1"],
            }
        ]
    )
    plugin._state["entries"]["or"]["snapshot"] = {"a": {"id": "a", "pricing": "1"}}
    plugin._clients["or"] = AsyncMock()
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(return_value={"a": {"id": "a", "pricing": "2"}}),
    )
    await plugin._run_cycle(plugin._specs[0])
    assert plugin.context.send_message.await_count == expected


@pytest.mark.asyncio
async def test_mode_switch_discards_old_notices_without_resetting_snapshot(
    watcher_factory,
):
    plugin = await watcher_factory()
    state = plugin._state["entries"]["one"]
    state["snapshot"] = {"a": "a"}
    state["pending"] = [{"text": "old attributes", "targets": ["bot:GroupMessage:1"]}]
    entries = [{**plugin.config["providers"][0], "comparison_mode": "仅模型 ID"}]
    reloaded = await watcher_factory(entries=entries, stored=plugin._state)
    assert reloaded._state["entries"]["one"]["snapshot"] == {"a": "a"}
    assert reloaded._state["entries"]["one"]["pending"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template,setting,category",
    [
        ("openrouter", "comparison_mode", "pricing"),
        ("deepseek", "detection_items", "provider"),
    ],
)
async def test_empty_selection_clears_backlog_and_continues_refreshing(
    watcher_factory, monkeypatch, template, setting, category
):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "one",
                "__template_key": template,
                "base_url": "https://example.test",
                "umo_whitelist": ["bot:GroupMessage:1"],
            }
        ]
    )
    state = plugin._state["entries"]["one"]
    state["snapshot"] = {"m": {"pricing": {"prompt": "1"}}}
    state["pending"] = [{"text": "Old notice", "targets": ["bot:GroupMessage:1"]}]
    entry = {**plugin.config["providers"][0], setting: []}
    reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
    assert reloaded._state["entries"]["one"]["pending"] == []
    reloaded._clients["one"] = AsyncMock()
    current = {"new": {"pricing": {"prompt": "2"}}}
    monkeypatch.setattr(main, "fetch_catalog", AsyncMock(return_value=current))
    await reloaded._run_cycle(reloaded._specs[0])
    assert reloaded._state["entries"]["one"]["snapshot"] == current
    reloaded.context.send_message.assert_not_called()
    entry[setting] = ["membership", category]
    resumed = await watcher_factory(entries=[entry], stored=reloaded._state)
    resumed._clients["one"] = AsyncMock()
    await resumed._run_cycle(resumed._specs[0])
    resumed.context.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_selection_order_and_duplicates_preserve_pending_deliveries(
    watcher_factory,
):
    entry = {
        "entry_id": "one",
        "__template_key": "openrouter",
        "base_url": "https://openrouter.ai/api",
        "comparison_mode": ["membership", "pricing"],
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    plugin._state["entries"]["one"]["pending"] = [
        {"text": "Unsent", "targets": ["bot:GroupMessage:1"]}
    ]
    entry["comparison_mode"] = ["pricing", "membership", "pricing"]
    reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
    assert (
        reloaded._state["entries"]["one"]["pending"]
        == plugin._state["entries"]["one"]["pending"]
    )


@pytest.mark.asyncio
async def test_multiple_selected_changes_share_one_notice(watcher_factory, monkeypatch):
    entry = {
        "entry_id": "one",
        "__template_key": "openrouter",
        "base_url": "https://openrouter.ai/api",
        "comparison_mode": ["membership", "pricing", "lifecycle"],
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    plugin._clients["one"] = AsyncMock()
    plugin._state["entries"]["one"]["snapshot"] = {
        "m": {
            "pricing": {"prompt": "1"},
            "expiration_date": None,
            "description": "old description",
        }
    }
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(
            return_value={
                "new-model": {},
                "m": {
                    "pricing": {"prompt": "2"},
                    "expiration_date": "2027-01-01",
                    "description": "new description",
                },
            }
        ),
    )
    render = Mock(return_value=b"image")
    monkeypatch.setattr(main, "render_card", render)
    await plugin._run_cycle(plugin._specs[0])
    plugin.context.send_message.assert_awaited_once()
    text = render.call_args.args[0]
    assert (
        "new-model" in text and "pricing.prompt" in text and "expiration_date" in text
    )
    assert "description" not in text


@pytest.mark.asyncio
async def test_backlog_sends_one_notice_per_target_per_cycle(watcher_factory):
    plugin = await watcher_factory()
    targets = list(plugin._specs[0].targets)
    plugin._state["entries"]["one"]["pending"] = [
        {"text": f"notice {index}", "targets": list(targets)} for index in range(4)
    ]
    await plugin._deliver(plugin._specs[0])
    assert plugin.context.send_message.await_count == len(targets)
    assert len(plugin._state["entries"]["one"]["pending"]) == 3
    reloaded = await watcher_factory(stored=plugin._state)
    await reloaded._deliver(reloaded._specs[0])
    assert reloaded.context.send_message.await_count == len(targets)
    assert len(reloaded._state["entries"]["one"]["pending"]) == 2


@pytest.mark.asyncio
async def test_many_changes_generate_one_message_per_target(
    watcher_factory, monkeypatch
):
    plugin = await watcher_factory()
    plugin._state["entries"]["one"]["snapshot"] = {}
    plugin._clients["one"] = AsyncMock()
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(return_value={f"m-{i:03d}": f"m-{i:03d}" for i in range(150)}),
    )
    render = Mock(return_value=b"image")
    monkeypatch.setattr(main, "render_card", render)
    await plugin._run_cycle(plugin._specs[0])
    render.assert_called_once()
    assert '"m-000"' in render.call_args.args[0]
    assert '"m-149"' in render.call_args.args[0]
    assert plugin.context.send_message.await_count == 2
    assert all(
        len(call.args[1].chain) == 1
        for call in plugin.context.send_message.call_args_list
    )


@pytest.mark.asyncio
async def test_template_selection_survives_restart_without_resetting_baseline(
    watcher_factory, monkeypatch, tmp_path
):
    entry = {
        "entry_id": "one",
        "base_url": "https://example.test",
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    plugin._clients["one"] = AsyncMock()
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(return_value={"new-model": {"id": "new-model"}}),
    )
    plugin._state["entries"]["one"]["snapshot"] = {}
    plugin.context.send_message.return_value = False
    await plugin._run_cycle(plugin._specs[0])
    before = copy.deepcopy(plugin._state["entries"]["one"])
    assert before["pending"][0]["notification"]["changes"]["added"] == ["new-model"]
    reloaded = await watcher_factory(
        entries=[dict(entry, card_template="dark")], stored=plugin._state
    )
    assert reloaded._state["entries"]["one"]["snapshot"] == before["snapshot"]
    image = tmp_path / "rendered.png"
    image.write_bytes(b"image")
    reloaded.html_render = AsyncMock(return_value=str(image))
    original_render = Mock(side_effect=AssertionError("Unexpected classic fallback"))
    monkeypatch.setattr(main, "render_card", original_render)
    await reloaded._deliver(reloaded._specs[0])
    reloaded.html_render.assert_awaited_once()
    template, data = reloaded.html_render.call_args.args
    assert "#101319" in template
    assert data["added"] == ["new-model"]
    assert data["name"] == "Provider"
    original_render.assert_not_called()
    assert reloaded._state["entries"]["one"]["pending"] == []


@pytest.mark.asyncio
async def test_provider_template_choices_are_independent_and_render_failure_falls_back(
    watcher_factory, monkeypatch
):
    entries = [
        {
            "entry_id": key,
            "base_url": "https://example.test",
            "card_template": style,
            "umo_whitelist": ["bot:GroupMessage:1"],
        }
        for key, style in [("one", "light"), ("two", "bold")]
    ]
    plugin = await watcher_factory(entries=entries)
    plugin.html_render = AsyncMock(side_effect=RuntimeError("Renderer unavailable"))
    original_render = Mock(return_value=b"image")
    monkeypatch.setattr(main, "render_card", original_render)
    monkeypatch.setattr(
        main,
        "fetch_catalog",
        AsyncMock(return_value={"new-model": {"id": "new-model"}}),
    )
    for spec in plugin._specs:
        plugin._clients[spec.entry_id] = AsyncMock()
        plugin._state["entries"][spec.entry_id]["snapshot"] = {}
        await plugin._run_cycle(spec)
    assert plugin.html_render.await_count == 2
    assert "#f5f6fa" in plugin.html_render.call_args_list[0].args[0]
    assert "#f4f08e" in plugin.html_render.call_args_list[1].args[0]
    assert original_render.call_count == 2
    assert plugin.context.send_message.await_count == 2
