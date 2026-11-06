from __future__ import annotations

import asyncio
import contextlib
import time

from common.observability import events as E
from common.observability import get_logger, traced_create_task
from local_agents.card_probe import LocalAgentCardProbe
from local_agents.config import LocalAgentDiscoveryConfig
from local_agents.models import DiscoveryTrigger, LocalAgentDiscoveryResult
from local_agents.port_scanner import HostPortScanner
from local_agents.protocols import LocalAgentWriter

logger = get_logger(__name__)
_MISSES_BEFORE_INACTIVE = 3


class LocalAgentService:
    def __init__(
        self,
        *,
        config: LocalAgentDiscoveryConfig,
        scanner: HostPortScanner,
        card_probe: LocalAgentCardProbe,
        writer: LocalAgentWriter,
    ) -> None:
        self.config = config
        self._scanner = scanner
        self._card_probe = card_probe
        self._writer = writer
        self._running = False
        self._periodic_task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[LocalAgentDiscoveryResult] | None = None
        self._active_trigger: DiscoveryTrigger | None = None
        self._task_lock = asyncio.Lock()
        self._miss_counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._periodic_task = traced_create_task(
            self._periodic_loop(),
            name="local-agent-discovery",
        )

    async def stop(self) -> None:
        self._running = False
        tasks = [task for task in (self._periodic_task, self._active_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._periodic_task = None
        self._active_task = None
        self._active_trigger = None

    async def request_discovery(
        self,
        trigger: DiscoveryTrigger = DiscoveryTrigger.MANUAL,
    ) -> LocalAgentDiscoveryResult:
        if not self.enabled:
            raise RuntimeError("Local agent discovery is disabled")

        async with self._task_lock:
            reused = self._active_task is not None and not self._active_task.done()
            if not reused:
                self._active_trigger = trigger
                self._active_task = traced_create_task(
                    self._run_discovery_cycle(trigger),
                    name="local-agent-discovery-cycle",
                )
            elif trigger == DiscoveryTrigger.MANUAL:
                # A user-requested refresh is authoritative even when it joins
                # an already running startup or scheduled discovery cycle.
                self._active_trigger = DiscoveryTrigger.MANUAL
            task = self._active_task

        if task is None:  # pragma: no cover - guarded by the lock above
            raise RuntimeError("Local agent discovery task was not created")
        try:
            result = await asyncio.shield(task)
            if reused:
                return result.model_copy(
                    update={
                        "trigger": trigger,
                        "reused_running_discovery": True,
                    }
                )
            return result
        finally:
            if task.done():
                async with self._task_lock:
                    if self._active_task is task:
                        self._active_task = None
                        self._active_trigger = None

    async def _periodic_loop(self) -> None:
        trigger = DiscoveryTrigger.STARTUP
        while self._running:
            try:
                await self.request_discovery(trigger)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.emit(
                    E.LOCAL_AGENT_DISCOVERY_CYCLE_FAILED, exc=exc, trigger=trigger
                )
            trigger = DiscoveryTrigger.SCHEDULED
            await asyncio.sleep(self.config.interval_seconds)

    async def _run_discovery_cycle(
        self,
        trigger: DiscoveryTrigger,
    ) -> LocalAgentDiscoveryResult:
        started = time.monotonic()
        open_ports = await self._scanner.scan_open_ports()
        discovered = await self._card_probe.probe_agent_cards(open_ports)

        found_local_ids: set[str] = set()
        added = 0
        reactivated = 0
        for discovery_url, card in discovered:
            upserted = await self._writer.upsert_local_agent(discovery_url, card)
            if not upserted.managed:
                continue
            found_local_ids.add(upserted.agent_id)
            self._miss_counts.pop(upserted.agent_id, None)
            added += int(upserted.added)
            reactivated += int(upserted.reactivated)

        effective_trigger = self._active_trigger or trigger
        inactive_ids: list[str] = []
        for agent_id in await self._writer.list_local_agent_ids():
            if agent_id in found_local_ids:
                continue
            if effective_trigger == DiscoveryTrigger.MANUAL:
                inactive_ids.append(agent_id)
                self._miss_counts.pop(agent_id, None)
                continue
            misses = self._miss_counts.get(agent_id, 0) + 1
            self._miss_counts[agent_id] = misses
            if misses >= _MISSES_BEFORE_INACTIVE:
                inactive_ids.append(agent_id)

        deactivated = await self._writer.mark_local_agents_inactive(inactive_ids)
        if deactivated > 0:
            logger.emit(E.LOCAL_AGENTS_DEACTIVATED, deactivated_count=deactivated)
        pruned = await self._writer.prune_inactive_local_agents()
        if pruned:
            logger.emit(E.LOCAL_AGENT_RECORDS_PRUNED, pruned_record_count=pruned)
        return LocalAgentDiscoveryResult(
            trigger=effective_trigger,
            open_ports=len(open_ports),
            agents_found=len(found_local_ids),
            agents_added=added,
            agents_reactivated=reactivated,
            agents_deactivated=deactivated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["LocalAgentService"]
