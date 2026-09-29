"""AstrBot entry point for persistent model catalog monitoring."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Image, Plain

from .catalog import WatchSpec, compare_catalogs, fetch_catalog
from .renderer import paginate_notification, render_page

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
            for notification in state["pending"]:
                notification["targets"] = {
                    target: cursor
                    for target, cursor in notification["targets"].items()
                    if target in spec.targets
                }
            state["pending"] = [
                notice for notice in state["pending"] if notice["targets"]
            ]
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
                    headers={"User-Agent": "AstrBot-Model-Watcher/0.0.2"},
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
        while not self._stopping:
            try:
                await self._run_cycle(spec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Model watcher cycle failed for entry %s (%s).",
                    spec.entry_id,
                    type(exc).__name__,
                )
            await asyncio.sleep(spec.interval)

    async def _run_cycle(self, spec: WatchSpec) -> None:
        """Commit a valid catalog and queue changes before trying deliveries.

        Args:
            spec: Provider whose next polling cycle should run.
        """
        current = None
        try:
            current = await fetch_catalog(self._clients[spec.entry_id], spec)
        except Exception as exc:
            # HTTP exceptions may embed keys, proxy passwords, or query tokens.
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else "n/a"
            )
            logger.warning(
                "Model watcher fetch failed for entry %s (%s, HTTP %s); keeping its baseline.",
                spec.entry_id,
                type(exc).__name__,
                status,
            )
        if current is not None:
            async with self._state_lock:
                existing = self._state["entries"][spec.entry_id]
                state = copy.deepcopy(existing)
                previous = state["snapshot"]
                if previous is not None and spec.targets:
                    changes = compare_catalogs(previous, current, spec.ignored_paths)
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
                            "name": spec.name,
                            "time": detected_at.strftime("%Y-%m-%d %H:%M:%S %z"),
                            "count": len(current),
                            "changes": changes,
                        }
                        pages = await asyncio.to_thread(paginate_notification, notice)
                        state["pending"].append(
                            {"pages": pages, "targets": dict.fromkeys(spec.targets, 0)}
                        )
                state["snapshot"] = current
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
        """Deliver pages independently and checkpoint each target's progress.

        Args:
            spec: Provider owning the persisted notification queue.
        """
        state = self._state["entries"][spec.entry_id]
        blocked: set[str] = set()
        for notice in list(state["pending"]):
            pages = notice["pages"]
            # At most one rendered page is held at a time, regardless of queue size.
            for index, text in enumerate(pages):
                targets = [
                    target
                    for target, cursor in notice["targets"].items()
                    if target not in blocked and cursor == index
                ]
                if not targets:
                    continue
                try:
                    png = await asyncio.to_thread(
                        render_page, text, index + 1, len(pages)
                    )
                except Exception as exc:
                    logger.warning(
                        "Model watcher card rendering failed (%s); using text.",
                        type(exc).__name__,
                    )
                    png = None
                for target in targets:
                    chain = (
                        MessageChain([Image.fromBytes(png)])
                        if png
                        else MessageChain(
                            [Plain(f"模型列表更新 [{index + 1}/{len(pages)}]\n{text}")]
                        )
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
                            blocked.add(target)
                            continue
                        sent = await asyncio.wait_for(
                            self.context.send_message(target, chain), timeout=30
                        )
                        if not sent:
                            blocked.add(target)
                            continue
                    except Exception as exc:
                        logger.warning(
                            "Model watcher delivery failed for entry %s (%s).",
                            spec.entry_id,
                            type(exc).__name__,
                        )
                        blocked.add(target)
                        continue
                    async with self._state_lock:
                        notice["targets"][target] = index + 1
                        await self.put_kv_data(STATE_KEY, copy.deepcopy(self._state))
            notice["targets"] = {
                target: cursor
                for target, cursor in notice["targets"].items()
                if cursor < len(pages)
            }
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
