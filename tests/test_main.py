"""Lifecycle, persistence, delivery, and failure isolation tests."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
async def test_legacy_pages_merge_only_unsent_content_and_keep_targets(
    watcher_factory, monkeypatch
):
    plugin = await watcher_factory()
    spec = plugin._specs[0]
    state = plugin._state["entries"]["one"]
    state["pending"] = [
        {
            "pages": ["first", "second", "third"],
            "targets": {spec.targets[0]: 1, spec.targets[1]: 0},
        }
    ]
    entry = dict(
        plugin.config["providers"][0],
        umo_whitelist=[spec.targets[0], "bot:GroupMessage:new"],
    )
    reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
    notice = reloaded._state["entries"]["one"]["pending"][0]
    assert notice["targets"] == [spec.targets[0]]
    assert notice["text"] == "second\n\nthird"
    await reloaded._deliver(reloaded._specs[0])
    assert reloaded.context.send_message.await_count == 1
    assert all(
        call.args[0] == spec.targets[0]
        for call in reloaded.context.send_message.call_args_list
    )


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
async def test_openrouter_upgrade_clears_backlog_and_keeps_baseline(
    watcher_factory, monkeypatch
):
    entry = {
        "entry_id": "or",
        "__template_key": "openrouter",
        "name": "Renamed",
        "base_url": "https://openrouter.ai/api",
        "umo_whitelist": ["bot:GroupMessage:1"],
    }
    plugin = await watcher_factory(entries=[entry])
    state = plugin._state["entries"]["or"]
    state.pop("comparison_mode")
    state["snapshot"] = {"a": {"id": "a", "pricing": {"prompt": "1"}}}
    state["pending"] = [
        {"pages": ["old attribute change"] * 10, "targets": {"bot:GroupMessage:1": 0}}
        for _ in range(20)
    ]
    reloaded = await watcher_factory(entries=[entry], stored=plugin._state)
    assert reloaded._state["entries"]["or"]["snapshot"] == state["snapshot"]
    assert reloaded._state["entries"]["or"]["pending"] == []
    assert reloaded.put_kv_data.call_args.args[1]["entries"]["or"]["pending"] == []
    reloaded._clients["or"] = AsyncMock()
    new = {"a": {"id": "a", "pricing": {"prompt": "2"}}}
    monkeypatch.setattr(main, "fetch_catalog", AsyncMock(return_value=new))
    await reloaded._run_cycle(reloaded._specs[0])
    reloaded.context.send_message.assert_not_called()
    assert reloaded._state["entries"]["or"]["snapshot"] == new
    monkeypatch.setattr(
        main, "fetch_catalog", AsyncMock(return_value={**new, "b": {"id": "b"}})
    )
    await reloaded._run_cycle(reloaded._specs[0])
    reloaded.context.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_openrouter_new_failed_delivery_survives_restart(
    watcher_factory, monkeypatch
):
    entry = {
        "entry_id": "or",
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
@pytest.mark.parametrize("mode,expected", [("仅模型 ID", 0), ("模型 ID 和属性", 1)])
async def test_explicit_comparison_mode_controls_attribute_notifications(
    watcher_factory, monkeypatch, mode, expected
):
    plugin = await watcher_factory(
        entries=[
            {
                "entry_id": "or",
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
