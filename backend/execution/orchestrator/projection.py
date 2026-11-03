"""Projection outbox driver and worker for terminal Run side effects.

The kernel's terminal CAS mints ``ProjectionIntent`` entries into the Run's
``projection_outbox``. Projection is never performed in-process by the kernel:
the production ``SettlingProjectionDriver`` only attempts the idempotent
settlement transition, and a leader-elected ``ProjectionOutboxWorker`` claims,
projects, completes, and (when required) blocks each intent.

The worker is store-agnostic. Concrete projectors (event append, final-message
delivery, public run status) are injected by the composition root.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from common.observability import events as E
from common.utils.logger import get_logger

from .models import OrchestratorRunState, ProjectionIntent
from .ports import OrchestratorRunStore, StoreOutcome
from .settlement import transition_projection_settlement

logger = get_logger(__name__)

ProjectionProjector = Callable[
    [ProjectionIntent, OrchestratorRunState], Awaitable[StoreOutcome]
]
ProjectionListener = Callable[[OrchestratorRunState, ProjectionIntent], Awaitable[None]]

_TERMINAL_SSE_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "budget_exhausted": "failed",
}


def public_terminal_status(status: str) -> str | None:
    """Map an orchestrator terminal status onto the public SSE status set."""
    return _TERMINAL_SSE_STATUS.get(status)


class SettlingProjectionDriver:
    """Production kernel projection driver (non-blocking settlement only).

    Terminal CAS already persisted the required ``ProjectionIntent`` entries
    into the Run's outbox. This driver never claims or completes intents
    in-process; the ``ProjectionOutboxWorker`` owns those transitions. ``settle``
    is safe to call immediately after terminal CAS (it replays harmlessly while
    intents remain pending) and after a worker crash (it is idempotent).
    """

    def __init__(self, run_store: OrchestratorRunStore) -> None:
        self.run_store = run_store

    async def settle(self, run_id: str) -> OrchestratorRunState:
        run = await self.run_store.load(run_id)
        if run is None:
            raise KeyError(run_id)
        now = datetime.now(UTC)
        transition = transition_projection_settlement(
            run, expected_state_version=run.state_version, updated_at=now
        )
        if transition.outcome != "accepted":
            return run
        result = await self.run_store.cas_mutate(
            transition.run,
            expected_state_version=run.state_version,
            command_id=f"settle:{run.run_id}:{run.state_version}",
        )
        return result.run if result.run is not None else run


class ProjectionOutboxWorker:
    """Leader-elected worker that drains the durable projection outbox."""

    def __init__(
        self,
        *,
        run_store: OrchestratorRunStore,
        projectors: Mapping[str, ProjectionProjector],
        worker_id: str = "orchestrator-projection",
        lease_seconds: float = 60.0,
        max_attempts: int = 8,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 300.0,
        batch_limit: int = 100,
        after_project: ProjectionListener | None = None,
    ) -> None:
        self.run_store = run_store
        self.projectors = dict(projectors)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        # No lease renewal during projection: the fixed lease is sized
        # for fast idempotent projectors, and losing a lease yields a
        # harmless re-project (conflict on complete). Renewal is added
        # only if a projector becomes long-running.
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.batch_limit = batch_limit
        self.after_project = after_project

    async def run_once(self, due_at: datetime | None = None) -> int:
        """Claim and project one batch of due intents; return progress count."""
        now = due_at or datetime.now(UTC)
        due = await self.run_store.list_due_projection_intents(
            due_at=now, limit=self.batch_limit
        )
        progressed = 0
        for run_id, intent in due:
            if await self._process_intent(run_id, intent.intent_id, now=now):
                progressed += 1
        return progressed

    async def _process_intent(  # noqa: C901
        self, run_id: str, intent_id: str, *, now: datetime
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        intent = _find_intent(run, intent_id)
        if intent is None or intent.status in {"completed", "blocked"}:
            return False

        if intent.status == "claimed":
            lease_expired = (
                intent.claim_expires_at is not None and intent.claim_expires_at <= now
            )
            if not lease_expired:
                return False
            released = await self.run_store.release_projection_intent(
                run_id,
                intent_id,
                expected_state_version=run.state_version,
                owner_id=intent.claim_owner or self.worker_id,
                next_attempt_at=now,
                now=now,
            )
            if released.run is None:
                return False
            run = released.run
            intent = _find_intent(run, intent_id)
            if intent is None or intent.status != "pending":
                return False

        if intent.status != "pending":
            return False
        if intent.kind == "project_terminal_run_status" and any(
            item.required
            and item.intent_id != intent.intent_id
            and item.status != "completed"
            for item in run.projection_outbox
        ):
            # Settlement publication is the durable dependent intent. Keep it
            # pending until final-message/event projections are acknowledged.
            return False

        claimed = await self.run_store.claim_projection_intent(
            run_id,
            intent_id,
            expected_state_version=run.state_version,
            owner_id=self.worker_id,
            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
        )
        if claimed.run is None or claimed.outcome not in {"accepted", "replayed"}:
            return False
        run = claimed.run
        intent = _find_intent(run, intent_id)
        if intent is None or intent.status != "claimed":
            return False

        projector = self.projectors.get(intent.kind)
        try:
            outcome: StoreOutcome = (
                "error" if projector is None else await projector(intent, run)
            )
        except Exception as exc:
            logger.emit(
                E.ORCHESTRATOR_PROJECTION_FAILED,
                exc=exc,
                run_id=run_id,
                conversation_id=run.conversation_id,
                client_request_id=run.client_request_id,
                intent_id=intent_id,
                intent_kind=intent.kind,
            )
            outcome = "error"

        if outcome in {"accepted", "replayed"}:
            completed = await self.run_store.complete_projection_intent(
                run_id,
                intent_id,
                expected_state_version=run.state_version,
                owner_id=self.worker_id,
            )
            if (
                completed.outcome in {"accepted", "replayed"}
                and completed.run is not None
            ):
                run = completed.run
            else:
                run = await self.run_store.load(run_id) or run
            run = await self._settle(run, now)
            # The after_project listener handles non-settlement intents.
            # Canonical run_settled is projected inside its dependent
            # durable intent, before completion, by the composition root.
            if (
                intent.kind != "project_terminal_run_status"
                and run.projection_state == "settled"
                and self.after_project is not None
            ):
                await self.after_project(run, intent)
            return True

        if intent.attempt_count >= self.max_attempts:
            blocked = await self.run_store.block_projection_intent(
                run_id,
                intent_id,
                expected_state_version=run.state_version,
                owner_id=self.worker_id,
                reason="projection attempts exceeded",
            )
            if blocked.run is not None:
                run = blocked.run
            await self._settle(run, now)
            return False

        delay = min(
            self.backoff_base_seconds * (2 ** max(intent.attempt_count - 1, 0)),
            self.backoff_max_seconds,
        )
        await self.run_store.release_projection_intent(
            run_id,
            intent_id,
            expected_state_version=run.state_version,
            owner_id=self.worker_id,
            next_attempt_at=now + timedelta(seconds=delay),
            now=now,
        )
        return False

    async def _settle(
        self, run: OrchestratorRunState, now: datetime
    ) -> OrchestratorRunState:
        transition = transition_projection_settlement(
            run, expected_state_version=run.state_version, updated_at=now
        )
        if transition.outcome != "accepted":
            return run
        result = await self.run_store.cas_mutate(
            transition.run,
            expected_state_version=run.state_version,
            command_id=f"settle:{run.run_id}:{run.state_version}",
        )
        return result.run if result.run is not None else run


def _find_intent(
    run: OrchestratorRunState | None, intent_id: str
) -> ProjectionIntent | None:
    if run is None:
        return None
    return next(
        (item for item in run.projection_outbox if item.intent_id == intent_id), None
    )


__all__ = [
    "ProjectionListener",
    "ProjectionOutboxWorker",
    "ProjectionProjector",
    "SettlingProjectionDriver",
    "public_terminal_status",
]
