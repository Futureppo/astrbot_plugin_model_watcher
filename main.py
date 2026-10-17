"""AstrBot entry point for persistent model catalog monitoring."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Image, Plain

from .card_templates import build_card_template
from .catalog import (
    PROVIDER_CHECK_LABELS,
    WatchSpec,
    compare_catalogs,
    fetch_catalog,
    fetch_fingerprints,
)
from .diagnostics import describe_fetch_failure
from .renderer import format_notification, render_card

STATE_KEY = "model_watcher_state_v1"


class ModelWatcher(star.Star):
    """Poll independent model catalogs and deliver changes to selected sessions."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._specs: list[WatchSpec] = []
        self._state: dict[str, Any] = {"version": 1, "entries": {}}
        self._tasks: list[asyncio.Task] = []
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._state_lock = asyncio.Lock()
        self._stopping = False

    async def initialize(self) -> None:
        """Restore state, assign stable entry IDs, and prepare active watchers."""
        stored = await self.get_kv_data(STATE_KEY, None)
        if (
            isinstance(stored, dict)
            and stored.get("version") == 1
            and isinstance(stored.get("entries"), dict)
        ):
            self._state = stored
        entries = self.config.get("providers", [])
        if not isinstance(entries, list):
            logger.warning("Model watcher providers must be a list.")
            return
        changed, seen = False, set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
                entry_id = uuid4().hex
                entry["entry_id"] = entry_id
                changed = True
            seen.add(entry_id)
            if not entry.get("enabled", True):
                # Disabled entries retain their baseline but discard old deliveries.
                state = self._state["entries"].get(entry_id)
                if isinstance(state, dict):
                    state["pending"] = []
                continue
            try:
                spec = WatchSpec.from_entry(entry)
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning(
                    "Model watcher entry %s has invalid configuration (%s).",
                    entry_id,
                    type(exc).__name__,
                )
                continue
            saved_mode = (
                list(spec.comparison_mode)
                if spec.provider in PROVIDER_CHECK_LABELS
                else entry.get("comparison_mode", "模型 ID 和属性")
            )
            if str(entry.get("interval_seconds", 30)) != str(spec.interval):
                logger.warning(
                    "Model watcher entry %s has an invalid interval; using 30 seconds.",
                    entry_id,
                )
            self._specs.append(spec)
            state = self._state["entries"].get(entry_id)
            if (
                not isinstance(state, dict)
                or state.get("fingerprint") != spec.fingerprint
                or not isinstance(state.get("snapshot"), (dict, type(None)))
                or not isinstance(state.get("pending"), list)
            ):
                state = {
                    "fingerprint": spec.fingerprint,
                    "snapshot": None,
                    "pending": [],
                }
                self._state["entries"][entry_id] = state
            # Rule changes discard queued notifications but retain the baseline.
            if (
                state.get("comparison_mode") != saved_mode
                or not spec.comparison_mode
                or (
                    spec.provider in PROVIDER_CHECK_LABELS
                    and set(state.get("ignored_paths", spec.ignored_paths))
                    != set(spec.ignored_paths)
                )
            ):
                state["pending"] = []
            state["comparison_mode"] = saved_mode
            if spec.provider in PROVIDER_CHECK_LABELS:
                state["ignored_paths"] = list(spec.ignored_paths)
            else:
                state.pop("ignored_paths", None)
            if spec.is_deepseek:
                if (
                    not spec.fingerprint_enabled
                    or state.get("system_fingerprint_url") != spec.fingerprint_url
                ):
                    state.pop("system_fingerprints", None)
                    if state.pop("system_fingerprint_url", None):
                        state["pending"] = []
                if spec.fingerprint_enabled:
                    state["system_fingerprint_url"] = spec.fingerprint_url
            pending = []
            for notice in state["pending"]:
                targets = [
                    target for target in notice["targets"] if target in spec.targets
                ]
                if targets:
                    pending.append({**notice, "targets": targets})
            state["pending"] = pending
        if changed:
            self.config.save_config()
        self._state["entries"] = {
            key: value for key, value in self._state["entries"].items() if key in seen
        }
        await self.put_kv_data(STATE_KEY, copy.deepcopy(self._state))
        # During cold startup adapters are created after plugin initialization.
        if self.context.platform_manager.get_insts():
            await self.start_watchers()

    @filter.on_astrbot_loaded()
    async def start_watchers(self) -> None:
        """Start polling once adapters are ready, including after a hot reload."""
        if self._tasks or self._stopping:
            return
        for spec in self._specs:
            try:
                self._clients[spec.entry_id] = httpx.AsyncClient(
                    proxy=spec.proxy,
                    trust_env=False,
                    timeout=httpx.Timeout(15),
                    follow_redirects=False,
                    headers={"User-Agent": "AstrBot-Model-Watcher/0.0.8"},
                )
            except Exception as exc:
                logger.warning(
                    "Model watcher entry %s could not create its HTTP client (%s).",
                    spec.entry_id,
                    type(exc).__name__,
                )
                continue
            self._tasks.append(
                asyncio.create_task(
                    self._monitor(spec), name=f"model-watcher-{spec.entry_id}"
                )
            )

    async def _monitor(self, spec: WatchSpec) -> None:
        """Run non-overlapping cycles for one configured provider.

        Args:
            spec: Provider to monitor until plugin termination.
        """
        loop = asyncio.get_running_loop()
        intervals = {False: spec.interval}
        if spec.fingerprint_enabled:
            intervals[True] = spec.fingerprint_interval
        next_runs = dict.fromkeys(intervals, 0.0)
        while not self._stopping:
            for fingerprints, interval in intervals.items():
                if loop.time() < next_runs[fingerprints]:
                    continue
                try:
                    await self._run_cycle(spec, fingerprints=fingerprints)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Model watcher %s cycle failed for entry %s (%s).",
                        "fingerprint" if fingerprints else "catalog",
                        spec.entry_id,
                        type(exc).__name__,
                    )
                next_runs[fingerprints] = loop.time() + interval
            await asyncio.sleep(max(0, min(next_runs.values()) - loop.time()))

    async def _run_cycle(self, spec: WatchSpec, *, fingerprints: bool = False) -> None:
        """Commit catalog or fingerprint changes before trying deliveries.

        Args:
            spec: Provider whose next polling cycle should run.
            fingerprints: Probe backend fingerprints instead of fetching the catalog.
        """
        if fingerprints and not spec.fingerprint_enabled:
            return
        snapshot_key = "system_fingerprints" if fingerprints else "snapshot"
        checks = (
            ("system_fingerprint",)
            if fingerprints
            else tuple(
                key for key in spec.comparison_mode if key != "system_fingerprint"
            )
        )
        current = None
        try:
            if fingerprints:
                entry_state = self._state["entries"][spec.entry_id]
                models = entry_state["snapshot"]
                if models is not None:
                    # Failed probes retain their last successful value. Removed
                    # models are dropped without generating fingerprint notices.
                    current = {
                        model_id: value
                        for model_id, value in entry_state.get(snapshot_key, {}).items()
                        if model_id in models
                    }
                    current.update(
                        await fetch_fingerprints(
                            self._clients[spec.entry_id], spec, models
                        )
                    )
            else:
                current = await fetch_catalog(self._clients[spec.entry_id], spec)
        except Exception as exc:
            logger.warning(
                "Model watcher fetch failed: %s; keeping its baseline.",
                describe_fetch_failure(spec, exc),
            )
        if current is not None:
            async with self._state_lock:
                existing = self._state["entries"][spec.entry_id]
                state = copy.deepcopy(existing)
                previous = state.get(snapshot_key)
                if previous is not None and spec.targets:
                    changes = compare_catalogs(
                        previous,
                        current,
                        spec.ignored_paths,
                        checks=checks,
                        openrouter=spec.is_openrouter,
                        deepseek=spec.is_deepseek,
                        provider=spec.provider,
                    )
                    if changes["added"] or changes["removed"] or changes["changed"]:
                        zone_name = str(
                            self.context.get_config().get("timezone") or ""
                        ).strip()
                        try:
                            detected_at = (
                                datetime.now(ZoneInfo(zone_name))
                                if zone_name
                                else datetime.now().astimezone()
                            )
                        except (ValueError, ZoneInfoNotFoundError):
                            detected_at = datetime.now().astimezone()
                        notice = {
                            "provider": spec.provider,
                            "openrouter": spec.is_openrouter,
                            "deepseek": spec.is_deepseek,
                            "fingerprints": fingerprints,
                            "name": spec.name,
                            "base_url": spec.base_url,
                            "time": detected_at.strftime("%Y-%m-%d %H:%M:%S %z"),
                            "count": len(state["snapshot"])
                            if fingerprints
                            else len(current),
                            "changes": changes,
                        }
                        text = await asyncio.to_thread(format_notification, notice)
                        state["pending"].append(
                            {
                                "text": text,
                                "notification": notice,
                                "targets": list(spec.targets),
                            }
                        )
                state[snapshot_key] = current
                # Persist snapshot and outbox together. Roll back on storage failure.
                self._state["entries"][spec.entry_id] = state
                try:
                    await self.put_kv_data(STATE_KEY, copy.deepcopy(self._state))
                except BaseException:
                    self._state["entries"][spec.entry_id] = existing
                    raise
        # Retry already queued deliveries even if the current fetch failed.
        await self._deliver(spec)

    async def _deliver(self, spec: WatchSpec) -> None:
        """Send at most one complete notification per target in each cycle.

        Args:
            spec: Provider owning the persisted notification queue.
        """
        state = self._state["entries"][spec.entry_id]
        attempted: set[str] = set()
        for notice in list(state["pending"]):
            targets = [
                target for target in notice["targets"] if target not in attempted
            ]
            if not targets:
                continue
            png = None
            if spec.card_template != "classic" and notice.get("notification"):
                try:
                    template, data = await asyncio.to_thread(
                        build_card_template, notice["notification"], spec.card_template
                    )
                    path = await asyncio.wait_for(
                        self.html_render(
                            template,
                            data,
                            return_url=False,
                            options={"full_page": True, "type": "jpeg", "quality": 95},
                        ),
                        timeout=30,
                    )
                    png = await asyncio.to_thread(Path(path).read_bytes)
                except Exception as exc:
                    logger.warning(
                        "Model watcher template %s failed for entry %s (%s); using the classic card.",
                        spec.card_template,
                        spec.entry_id,
                        type(exc).__name__,
                    )
            if not png:
                try:
                    png = await asyncio.to_thread(render_card, notice["text"])
                except Exception as exc:
                    logger.warning(
                        "Model watcher card rendering failed (%s); using text.",
                        type(exc).__name__,
                    )
            for target in targets:
                attempted.add(target)
                chain = (
                    MessageChain([Image.fromBytes(png)])
                    if png
                    else MessageChain([Plain(f"模型列表更新\n{notice['text']}")])
                )
                try:
                    # An unavailable adapter should retain its pending delivery.
                    platform_id = target.split(":", 1)[0]
                    adapter = next(
                        (
                            p
                            for p in self.context.platform_manager.get_insts()
                            if str(p.meta().id) == platform_id
                        ),
                        None,
                    )
                    if adapter is None or not getattr(
                        adapter.meta(), "support_proactive_message", True
                    ):
                        continue
                    sent = await asyncio.wait_for(
                        self.context.send_message(target, chain), timeout=30
                    )
                    if not sent:
                        continue
                except Exception as exc:
                    logger.warning(
                        "Model watcher delivery failed for entry %s (%s).",
                        spec.entry_id,
                        type(exc).__name__,
                    )
                    continue
                async with self._state_lock:
                    notice["targets"].remove(target)
                    await self.put_kv_data(STATE_KEY, copy.deepcopy(self._state))
        async with self._state_lock:
            state["pending"] = [
                notice for notice in state["pending"] if notice["targets"]
            ]
            await self.put_kv_data(STATE_KEY, copy.deepcopy(self._state))

    async def terminate(self) -> None:
        """Cancel polling, await active workers, and close every HTTP client."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
