from __future__ import annotations

import asyncio
from typing import Protocol

from a2a_adapter.agent_card_health import probe_agent_card_for_health
from common.config.loader import settings
from common.observability import get_logger, traced_create_task
from common.protocols import LeaderElector
from common.types import AgentCard as CommonAgentCard
from models.agent import (
    AGENT_CARD_HEALTH_NO_SYNC,
    Agent,
    AgentStatus,
)

logger = get_logger(__name__)


def _reactivatable(agent: Agent) -> bool:
    """An inactive agent that is reachable again and not gated by setup."""
    return agent.agent_status == AgentStatus.inactive and not agent.inactive_reason


# Reactivation applies only while the stored record is still inactive and
# ungated: bundled-agent gating may set a reason while a probe is in flight.
_REACTIVATE_CONDITIONS = {
    "agent_status": AgentStatus.inactive.value,
    "inactive_reason": None,
}


class AgentHealthRepositoryPort(Protocol):
    async def get_by_id(self, agent_id: str) -> dict | None: ...

    async def list_visible(
        self, *, query: dict | None = None, **kwargs
    ) -> list[dict]: ...

    async def update(self, agent_id: str, updates: dict) -> dict | None: ...

    async def update_where(
        self, agent_id: str, conditions: dict, updates: dict
    ) -> bool: ...


class AgentHealthService:
    """
    Periodic health check service for registered agents.

    Runs background HTTP ping checks to verify agent connectivity and updates
    their status (active/inactive) based on reachability.

    Uses exponential backoff for faster detection of offline agents:
    - Normal interval: default 1 hour between full cycles
    - On failure: retry at 30s, 60s (total ~90s to mark inactive after 3 failures)
    """

    def __init__(
        self,
        *,
        repository: AgentHealthRepositoryPort | None = None,
        check_interval_seconds: int | None = None,
        timeout_seconds: float = 10.0,
        max_consecutive_failures: int = 3,
        initial_retry_delay: float = 30.0,  # First retry after 30s
        max_retry_delay: float = 120.0,  # Cap retry delay at 2 min
        backoff_multiplier: float = 2.0,  # Double delay each retry
    ):
        """
        Initialize the health check service.

        Args:
            check_interval_seconds: Interval between health check cycles
            timeout_seconds: HTTP request timeout per agent
            max_consecutive_failures: Number of failures before marking inactive
            initial_retry_delay: Initial retry delay in seconds
            max_retry_delay: Maximum retry delay in seconds
            backoff_multiplier: Backoff multiplier for the retry delay
        """
        if check_interval_seconds is None:
            check_interval_seconds = settings.agent_health_check_interval
        self.check_interval = check_interval_seconds
        self.timeout = timeout_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        self.backoff_multiplier = backoff_multiplier
        self._running = False
        self._task: asyncio.Task | None = None
        # Track consecutive failures per agent
        self._failure_counts: dict[str, int] = {}
        # Track active retry tasks per agent to avoid duplicates
        self._retry_tasks: dict[str, asyncio.Task] = {}
        self._leader: LeaderElector | None = None
        self._leader_lock_name: str | None = None
        self._repository = repository

    def set_leader_election(
        self, leader: LeaderElector | None, *, lock_name: str
    ) -> None:
        """Attach a leader election instance and the job lock it gates on."""
        self._leader = leader
        self._leader_lock_name = lock_name

    def bind_repository(self, repository) -> None:
        self._repository = repository

    def _require_repository(self):
        if self._repository is None:
            raise RuntimeError(
                "AgentHealthService.bind_repository() not called - startup incomplete"
            )
        return self._repository

    @staticmethod
    def _coerce_agent(agent: dict | Agent) -> Agent:
        return agent if isinstance(agent, Agent) else Agent.model_validate(agent)

    async def check_agent_health(
        self,
        agent_id: str,
        agent_url: str,
        agent_name: str | None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, CommonAgentCard | None]:
        """
        Check if an agent is reachable by asking the A2A adapter to probe its
        agent-card endpoint.

        A2A protocol agents typically only accept POST on root URL for message
        sending, so the adapter checks the agent card endpoint which accepts GET
        requests. When the endpoint returns a valid agent card, it is parsed and
        returned so callers can update the DB.

        Args:
            agent_id: The agent to check
            agent_url: The registered agent URL to probe
            agent_name: Display name used in log messages
            timeout: Optional override for HTTP timeout (defaults to self.timeout)

        Returns:
            Tuple of (is_healthy, fetched_agent_card_or_None)
        """
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            result = await probe_agent_card_for_health(
                agent_url,
                timeout=effective_timeout,
            )
            if result.is_healthy:
                logger.debug(f"Agent {agent_id} ({agent_name}) is healthy")
                if result.card is None:
                    logger.debug(f"Could not parse agent card for {agent_id}")
            elif result.status_code is not None:
                logger.warning(
                    f"Agent {agent_id} ({agent_name}) "
                    f"returned status {result.status_code}"
                )
            else:
                logger.warning(
                    "agent_health_probe_unreachable",
                    extra={
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "error_type": "adapter_error",
                    },
                )

            return result.is_healthy, result.card

        except Exception as exc:
            logger.error(
                "agent_health_check_failed",
                extra={
                    "agent_id": agent_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False, None

    async def sync_agent_card(
        self,
        agent_id: str,
        stored_card: dict,
        fetched_card: CommonAgentCard,
    ) -> None:
        """
        Persist the freshly fetched agent card to MongoDB using a partial update.

        All fields from the live agent card are synced except those in
        ``_AGENT_CARD_NO_SYNC``. Using a blocklist (rather than a whitelist)
        ensures that new fields added by the a2a SDK are picked up
        automatically without any code change here.

        The update is skipped entirely when none of the synced fields have
        changed, avoiding unnecessary DB writes on every health-check cycle.
        """
        # Blocklist imported from models.agent — see AGENT_CARD_HEALTH_NO_SYNC
        # for the rationale on why only the registered URL is protected here.

        try:
            card_dict = fetched_card.model_dump(mode="json", by_alias=True)
            partial_set = {
                f"agent_card.{field}": card_dict[field]
                for field in card_dict
                if field not in AGENT_CARD_HEALTH_NO_SYNC
                and card_dict[field] != stored_card.get(field)
            }

            if not partial_set:
                logger.debug(
                    f"Agent card unchanged for {agent_id} ({fetched_card.name}), skipping update"
                )
                return

            await self._require_repository().update(agent_id, partial_set)
            logger.debug(f"Agent card updated for {agent_id} ({fetched_card.name})")
        except Exception as exc:
            logger.warning(
                "agent_card_update_failed",
                extra={
                    "agent_id": agent_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def update_agent_status(self, agent_id: str, new_status: AgentStatus) -> bool:
        """
        Update agent status in the database.

        Args:
            agent_id: The agent ID to update
            new_status: The new status to set

        Returns:
            bool: True if update was successful
        """
        repo = self._require_repository()
        try:
            await repo.update(agent_id, {"agent_status": new_status.value})
            logger.info(f"Agent {agent_id} status updated to {new_status.value}")
            return True
        except Exception as exc:
            logger.error(
                "agent_status_update_failed",
                extra={"agent_id": agent_id, "error_type": type(exc).__name__},
            )
            return False

    async def reactivate_agent(self, agent_id: str) -> bool:
        """Mark a recovered agent active unless it is no longer inactive and ungated.

        Returns True only when the record changed.
        """
        try:
            changed = await self._require_repository().update_where(
                agent_id,
                _REACTIVATE_CONDITIONS,
                {"agent_status": AgentStatus.active.value},
            )
        except Exception as exc:
            logger.error(
                "agent_status_update_failed",
                extra={"agent_id": agent_id, "error_type": type(exc).__name__},
            )
            return False
        if changed:
            logger.info(f"Agent {agent_id} status updated to active")
        return bool(changed)

    async def _check_stored_agent(
        self, agent: Agent
    ) -> tuple[bool, CommonAgentCard | None]:
        return await self.check_agent_health(
            agent.agent_id, agent.agent_card.url, agent.agent_card.name
        )

    async def _sync_stored_card(
        self, agent: Agent, fetched_card: CommonAgentCard
    ) -> None:
        await self.sync_agent_card(
            agent.agent_id,
            agent.agent_card.model_dump(mode="json", by_alias=True),
            fetched_card,
        )

    def _get_retry_delay(self, failure_count: int) -> float:
        """Calculate retry delay with exponential backoff."""
        delay = self.initial_retry_delay * (
            self.backoff_multiplier ** (failure_count - 1)
        )
        return min(delay, self.max_retry_delay)

    def _should_stop_retry(self, failure_count: int) -> bool:
        return failure_count == 0 or failure_count >= self.max_consecutive_failures

    async def _load_retry_agent(self, agent_id: str) -> Agent | None:
        repo_agent = await self._require_repository().get_by_id(agent_id)
        if not repo_agent:
            return None
        agent = self._coerce_agent(repo_agent)
        if agent.agent_status == AgentStatus.deleted:
            return None
        return agent

    async def _handle_retry_success(
        self,
        agent_id: str,
        agent: Agent,
        fetched_card: CommonAgentCard | None,
    ) -> None:
        self._failure_counts[agent_id] = 0
        if fetched_card:
            await self._sync_stored_card(agent, fetched_card)
        if _reactivatable(agent) and await self.reactivate_agent(agent_id):
            logger.info(
                f"Agent {agent_id} ({agent.agent_card.name}) recovered - marked active"
            )

    async def _handle_retry_failure(
        self,
        agent_id: str,
        agent: Agent,
        failure_count: int,
    ) -> bool:
        self._failure_counts[agent_id] = failure_count + 1
        failures = self._failure_counts[agent_id]

        if failures < self.max_consecutive_failures:
            return False

        if agent.agent_status == AgentStatus.active:
            await self.update_agent_status(agent_id, AgentStatus.inactive)
            logger.warning(
                f"Agent {agent_id} ({agent.agent_card.name}) "
                f"marked inactive after {failures} consecutive failures"
            )
        return True

    async def _retry_agent_check(self, agent_id: str):
        """
        Retry checking a specific agent with exponential backoff.
        Runs until agent recovers or is marked inactive.
        """
        try:
            while self._running:
                failure_count = self._failure_counts.get(agent_id, 0)

                # Agent recovered or already marked inactive elsewhere
                if self._should_stop_retry(failure_count):
                    break

                delay = self._get_retry_delay(failure_count)
                logger.debug(
                    f"Scheduling retry for agent {agent_id} in {delay}s "
                    f"(failure {failure_count}/{self.max_consecutive_failures})"
                )
                await asyncio.sleep(delay)

                # Re-fetch agent to get current state
                agent = await self._load_retry_agent(agent_id)
                if agent is None:
                    break

                is_healthy, fetched_card = await self._check_stored_agent(agent)

                if is_healthy:
                    await self._handle_retry_success(agent_id, agent, fetched_card)
                    break
                should_stop = await self._handle_retry_failure(
                    agent_id, agent, failure_count
                )
                if should_stop:
                    break

        except asyncio.CancelledError:
            logger.debug(f"Retry task for agent {agent_id} cancelled")
        except Exception as exc:
            logger.error(
                "agent_health_retry_failed",
                extra={"agent_id": agent_id, "error_type": type(exc).__name__},
            )
        finally:
            # Clean up retry task reference
            self._retry_tasks.pop(agent_id, None)

    def _schedule_retry(self, agent_id: str):
        """Schedule a retry task for an agent if not already scheduled."""
        if agent_id not in self._retry_tasks or self._retry_tasks[agent_id].done():
            self._retry_tasks[agent_id] = traced_create_task(
                self._retry_agent_check(agent_id),
                name=f"agent-health-retry-{agent_id}",
            )

    async def run_health_check_cycle(self):
        """Run a single health check cycle for all agents."""
        try:
            # Host-discovered agents use the local discovery miss threshold as
            # their authoritative lifecycle; avoid racing that reconciliation.
            raw_agents = await self._require_repository().list_visible(
                query={
                    "agent_status": {"$ne": AgentStatus.deleted.value},
                    "source": {"$ne": "local"},
                }
            )
            agents = [self._coerce_agent(agent) for agent in raw_agents]

            logger.info(f"Running health check for {len(agents)} agents")

            for agent in agents:
                agent_id = agent.agent_id

                # Skip agents with active retry tasks to avoid race conditions
                if (
                    agent_id in self._retry_tasks
                    and not self._retry_tasks[agent_id].done()
                ):
                    logger.debug(f"Skipping agent {agent_id} - retry task in progress")
                    continue

                is_healthy, fetched_card = await self._check_stored_agent(agent)

                if is_healthy:
                    # Reset failure count and mark as active if was inactive
                    self._failure_counts[agent_id] = 0
                    if fetched_card:
                        await self._sync_stored_card(agent, fetched_card)
                    if _reactivatable(agent) and await self.reactivate_agent(agent_id):
                        logger.info(
                            f"Agent {agent_id} ({agent.agent_card.name}) "
                            f"recovered - marked active"
                        )
                else:
                    # Increment failure count
                    self._failure_counts[agent_id] = (
                        self._failure_counts.get(agent_id, 0) + 1
                    )
                    failures = self._failure_counts[agent_id]

                    # Only mark inactive after consecutive failures
                    if failures >= self.max_consecutive_failures:
                        if agent.agent_status == AgentStatus.active:
                            await self.update_agent_status(
                                agent_id, AgentStatus.inactive
                            )
                            logger.warning(
                                f"Agent {agent_id} ({agent.agent_card.name}) "
                                f"marked inactive after {failures} consecutive failures"
                            )
                    else:
                        # Schedule exponential backoff retry
                        self._schedule_retry(agent_id)

        except Exception as exc:
            logger.error(
                "agent_health_cycle_failed",
                extra={"error_type": type(exc).__name__},
            )

    async def _health_check_loop(self):
        """Background loop that periodically runs health checks."""
        logger.info(
            f"Agent health check service started "
            f"(interval: {self.check_interval}s, timeout: {self.timeout}s, "
            f"max_failures: {self.max_consecutive_failures}, "
            f"initial_retry: {self.initial_retry_delay}s)"
        )

        while self._running:
            try:
                await self._run_one_iteration()
                await asyncio.sleep(self.check_interval)
            except Exception as exc:
                logger.error(
                    "agent_health_loop_failed",
                    extra={"error_type": type(exc).__name__},
                )
                await asyncio.sleep(60)

    async def _run_one_iteration(self) -> None:
        """Run a single iteration, gated by leader election if available."""
        if self._leader:
            ttl = self.check_interval * 2
            acquired = await self._leader.try_acquire(self._leader_lock_name, ttl)
            if not acquired:
                return  # another instance is the leader
            try:
                await self.run_health_check_cycle()
            finally:
                await self._leader.release(self._leader_lock_name)
        else:
            await self.run_health_check_cycle()

    async def start(self):
        """Start the health check background task."""
        if not settings.agent_health_check_enabled:
            logger.info("Agent health check service is disabled")
            return

        if self._running:
            logger.warning("Health check service is already running")
            return

        self._running = True
        self._task = traced_create_task(
            self._health_check_loop(),
            name="agent-health-check",
        )
        logger.info("Agent health check service started")

    async def stop(self):
        """Stop the health check background task."""
        if not self._running:
            return

        self._running = False

        # Cancel all retry tasks
        for task in self._retry_tasks.values():
            task.cancel()
        self._retry_tasks.clear()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Agent health check service stopped")
