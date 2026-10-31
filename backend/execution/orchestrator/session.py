"""Room-owned facade over the generic orchestrator kernel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol

from .kernel import KernelRunResult, OrchestratorKernel, SystemClock, UUIDFactory
from .lifecycle import LifecycleEmitter, SessionEvent, SessionEventListener
from .models import (
    BudgetState,
    CandidateScopeSnapshot,
    FrozenToolCatalogSnapshot,
    ModelMessage,
    OrchestratorProfile,
    OrchestratorRunState,
    RecoveryClaim,
    RunRequestSnapshot,
    RunResourceManifestSnapshot,
    ToolObservation,
    UserMessage,
)
from .persistence import SUSPENDED_RUN_STATUSES, TERMINAL_RUN_STATUSES
from .ports import OrchestratorRunStore, ToolCatalog

# Live execution lease. The in-process driver owns the Run's recovery claim while
# it executes and renews it here, so a driver that dies (deploy, crash, SIGKILL)
# leaves an expired owner behind: recovery terminalizes that Run as interrupted
# instead of resuming work the dead process was responsible for.
SESSION_EXECUTION_OWNER_PREFIX = "session:"
_EXECUTION_LEASE_SECONDS = 60.0
_EXECUTION_LEASE_RENEW_SECONDS = 20.0

# A suspended Run has no watchdog while it waits. Its recovery schedule is a
# finite far-future value rather than None, because None means "due now".
DORMANT_RECOVERY_DELAY = timedelta(days=3650)


def dormant_recovery_at(now: datetime) -> datetime:
    """Return the recovery schedule of a Run that is waiting, not working."""
    return now + DORMANT_RECOVERY_DELAY


def execution_owner_id(session_id: str) -> str:
    """Return the lease owner used by an in-process Run driver."""
    return f"{SESSION_EXECUTION_OWNER_PREFIX}{session_id}"


def interrupted_execution_owner(owner_id: str | None) -> bool:
    """True when an expired lease was held by an in-process Run driver."""
    return bool(owner_id) and owner_id.startswith(SESSION_EXECUTION_OWNER_PREFIX)


_ACTIVE_EXECUTION_STATUSES = frozenset({"queued", "running", "finalizing"})


def interrupted_run_needs_settlement(status: str, owner_id: str | None) -> bool:
    """True when recovery must fail a Run whose driver died.

    Suspended and canceling Runs keep their existing recovery paths; only an
    actively executing Run with a lapsed driver lease is interrupted.
    """
    return status in _ACTIVE_EXECUTION_STATUSES and interrupted_execution_owner(
        owner_id
    )


logger = logging.getLogger(__name__)


class SessionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RoomAgentSessionConfig:
    session_id: str
    conversation_id: str
    profile: OrchestratorProfile
    candidate_scope: CandidateScopeSnapshot
    room_epoch: int
    requesting_subject_id: str
    tool_catalog: ToolCatalog
    frozen_tool_catalog: FrozenToolCatalogSnapshot | None = None
    resource_manifest: RunResourceManifestSnapshot | None = None
    conversation_history: tuple[ModelMessage, ...] = ()


class RunFactory(Protocol):
    def create_run(
        self,
        *,
        config: RoomAgentSessionConfig,
        message: UserMessage,
        client_request_id: str | None,
    ) -> OrchestratorRunState: ...


class DefaultRunFactory:
    def __init__(
        self,
        *,
        clock: SystemClock | None = None,
        id_factory: UUIDFactory | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.id_factory = id_factory or UUIDFactory()

    def create_run(
        self,
        *,
        config: RoomAgentSessionConfig,
        message: UserMessage,
        client_request_id: str | None,
    ) -> OrchestratorRunState:
        now = self.clock.now()
        run_id = self.id_factory.new_id("run")
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise ValueError("canonical Runs require a nonempty client_request_id")
        fingerprint = sha256(
            (
                f"{config.conversation_id}:{config.room_epoch}:"
                f"{config.requesting_subject_id}:{message.model_dump_json()}"
            ).encode()
        ).hexdigest()
        return OrchestratorRunState(
            run_id=run_id,
            session_id=config.session_id,
            conversation_id=config.conversation_id,
            client_request_id=client_request_id,
            request=RunRequestSnapshot(
                request_fingerprint=fingerprint,
                room_epoch=config.room_epoch,
                requesting_subject_id=config.requesting_subject_id,
                user_message_id=message.message_id,
            ),
            profile=config.profile,
            candidate_scope=config.candidate_scope,
            status="running",
            transcript=[message],
            background_context=list(config.conversation_history),
            tool_catalog=config.frozen_tool_catalog,
            resource_manifest=config.resource_manifest,
            tool_batches=[],
            artifact_refs=[],
            budget=BudgetState(
                deadline_at=now + timedelta(seconds=config.profile.deadline_seconds)
            ),
            compaction_summary=None,
            compaction_baseline_tokens=None,
            proposed_final_message_id=None,
            terminal_reason=None,
            projection_state="pending",
            # A live provider session is not generic-recovery work. Schedule
            # recovery only at the profile deadline/watchdog boundary; explicit
            # suspension and shutdown recovery may move this boundary earlier.
            recovery_claim=RecoveryClaim(
                next_attempt_at=now + timedelta(seconds=config.profile.deadline_seconds)
            ),
            projection_outbox=[],
            processed_command_ids=[],
            state_version=0,
            created_at=now,
            updated_at=now,
        )


class EventCancellationSignal:
    def __init__(self) -> None:
        self.event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    async def wait(self) -> None:
        await self.event.wait()

    def cancel(self) -> None:
        self.event.set()


class RoomAgentSession:
    def __init__(
        self,
        *,
        config: RoomAgentSessionConfig,
        kernel: OrchestratorKernel,
        run_store: OrchestratorRunStore,
        run_factory: RunFactory | None = None,
        lifecycle: LifecycleEmitter | None = None,
        clock: SystemClock | None = None,
    ) -> None:
        self.config = config
        self.kernel = kernel
        self.run_store = run_store
        self.run_factory = run_factory or DefaultRunFactory()
        self.lifecycle = lifecycle or LifecycleEmitter()
        self.clock = clock or SystemClock()
        self._run_id: str | None = None
        self._task: asyncio.Task[KernelRunResult] | None = None
        self._settled_task: asyncio.Task[KernelRunResult] | None = None
        self._settlement_lock = asyncio.Lock()
        self._signal = EventCancellationSignal()
        self._sequence = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._lease_owner: str | None = None
        self._lease_task: asyncio.Task[None] | None = None
        # Set once by shutdown() and never reset: a shut-down session is
        # discarded by its host and must not start another Run.
        self._shutting_down = False

    async def _acquire_execution_lease(self) -> None:
        """Publish a live driver lease before executing a Run."""
        if self._run_id is None or self._lease_owner is not None:
            return
        run = await self.run_store.load(self._run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        now = self.clock.now()
        owner = execution_owner_id(self.config.session_id)
        claimed = await self.run_store.claim_recovery(
            run.run_id,
            expected_state_version=run.state_version,
            owner_id=owner,
            lease_expires_at=now + timedelta(seconds=_EXECUTION_LEASE_SECONDS),
            claimed_at=now,
            allow_scheduled=True,
        )
        if claimed.outcome not in {"accepted", "replayed"}:
            # Another driver still holds a live lease; leave its claim alone.
            return
        self._lease_owner = owner
        self._lease_task = asyncio.create_task(self._renew_execution_lease())

    async def _renew_execution_lease(self) -> None:
        owner = self._lease_owner
        while owner is not None and self._run_id is not None:
            await asyncio.sleep(_EXECUTION_LEASE_RENEW_SECONDS)
            run = await self.run_store.load(self._run_id)
            if (
                run is None
                or run.status in TERMINAL_RUN_STATUSES
                or run.recovery_claim.owner_id != owner
            ):
                return
            await self.run_store.renew_recovery(
                run.run_id,
                expected_state_version=run.state_version,
                owner_id=owner,
                lease_expires_at=self.clock.now()
                + timedelta(seconds=_EXECUTION_LEASE_SECONDS),
            )

    async def _stop_lease_renewal(self) -> None:
        task, self._lease_task = self._lease_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _release_execution_lease(self) -> None:
        """Hand the claim back with the next attempt this Run still needs."""
        await self._stop_lease_renewal()
        owner, self._lease_owner = self._lease_owner, None
        if owner is None or self._run_id is None:
            return
        run = await self.run_store.load(self._run_id)
        if run is None or run.recovery_claim.owner_id != owner:
            return
        await self.run_store.release_recovery(
            run.run_id,
            expected_state_version=run.state_version,
            owner_id=owner,
            next_attempt_at=self._next_recovery_attempt(run),
        )

    def _next_recovery_attempt(self, run: OrchestratorRunState) -> datetime | None:
        """Restore the watchdog this Run needs once the driver stops owning it.

        Terminal Runs need no recovery and suspended Runs stay dormant until a
        resume re-arms them. Everything else keeps the deadline watchdog; a
        typed recoverable failure moves that boundary earlier afterwards
        through ``schedule_run_recovery``.
        """
        if run.status in TERMINAL_RUN_STATUSES:
            return None
        if run.status in SUSPENDED_RUN_STATUSES:
            return dormant_recovery_at(self.clock.now())
        return run.budget.deadline_at

    def owns_run(self, run_id: str) -> bool:
        return self._run_id == run_id

    async def has_active_run(self) -> bool:
        """True while this session owns a non-terminal Run."""
        if self._task is not None and not self._task.done():
            return True
        if self._run_id is None:
            return False
        existing = await self.run_store.load(self._run_id)
        if existing is None:
            return False
        return existing.status not in TERMINAL_RUN_STATUSES

    async def prompt(
        self,
        message: UserMessage,
        *,
        client_request_id: str | None = None,
    ) -> KernelRunResult:
        if self._task is not None and not self._task.done():
            raise SessionConflict("a Run is already active")
        if await self.has_active_run():
            raise SessionConflict("a Run is already active")
        run = self.run_factory.create_run(
            config=self.config,
            message=message,
            client_request_id=client_request_id,
        )
        created = await self.run_store.create(
            run, command_id=f"create:{client_request_id or run.run_id}"
        )
        if created.outcome not in {"accepted", "replayed"} or created.run is None:
            raise SessionConflict(f"Run create failed: {created.outcome}")
        if created.outcome == "replayed":
            replayed = _replayed_result(created.run)
            if replayed is None:
                raise SessionConflict("replayed Run is already active")
            self._run_id = created.run.run_id
            self._signal = EventCancellationSignal()
            return replayed
        self._run_id = created.run.run_id
        self._signal = EventCancellationSignal()
        await self._emit("session_started", created.run)
        await self._emit(
            "run_started",
            created.run,
            payload={
                "status": created.run.status,
                "mode": created.run.profile.profile_id,
            },
        )
        return await self._start_kernel()

    async def continue_run(self) -> KernelRunResult:
        run = await self._current_run()
        if run.status in SUSPENDED_RUN_STATUSES:
            raise SessionConflict("suspended Run requires a new ToolObservation")
        return await self._start_kernel()

    async def observe_tool(self, observation: ToolObservation) -> KernelRunResult:
        if self._run_id is None:
            raise SessionConflict("no active Run")
        if self._task is not None and not self._task.done():
            raise SessionConflict("Run is currently executing")
        self._idle.clear()
        await self._acquire_execution_lease()
        self._task = asyncio.create_task(
            self.kernel.observe_tool(
                self._run_id,
                observation,
                signal=self._signal,
                lifecycle=self._emit_kernel_event,
            )
        )
        self._settled_task = None
        return await self._await_task()

    async def signal_interrupt(self, run_id: str, cancellation_command_id: str) -> None:
        """Signal matching live work without waiting for provider cooperation."""

        if not self.owns_run(run_id):
            return
        self._signal.cancel()
        run = await self.run_store.load(run_id)
        if run is None:
            raise SessionConflict("interrupted Run is missing")
        if (
            run.status not in {"canceling", "canceled"}
            or run.cancellation_command_id != cancellation_command_id
        ):
            raise SessionConflict("invalid durable cancellation postcondition")

    async def interrupt(self, run_id: str, cancellation_command_id: str) -> bool:
        """Signal matching live work and wait briefly for cooperative shutdown."""

        if not self.owns_run(run_id):
            return True
        await self.signal_interrupt(run_id, cancellation_command_id)
        stopped = True
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._await_task()),
                    timeout=5.0,
                )
            except TimeoutError:
                # Durable recovery remains due; descendant cleanup must not be
                # blocked by a provider task that ignores its signal.
                stopped = False
        return stopped

    async def abort(self) -> None:
        """Signal only after another owner has durably claimed cancellation."""

        if self._run_id is None:
            return
        run = await self._current_run()
        if run.status == "canceled":
            return
        if run.status != "canceling" or run.cancellation_command_id is None:
            raise SessionConflict("Run cancellation has not been durably claimed")
        await self.interrupt(run.run_id, run.cancellation_command_id)

    async def wait_for_idle(self) -> None:
        await self._idle.wait()

    async def shutdown(self) -> None:
        """Fail the in-flight Run; an interrupted execution is never resumed.

        Graceful-shutdown surface: the drivers this process owns are settled as
        ``failed`` with reason ``interrupted`` so the Room sees a terminal Turn
        instead of waiting for a recovery worker. If the process is killed
        before this completes, the execution lease expires and recovery applies
        the same outcome.
        """
        task = self._task
        if task is None or task.done():
            return
        # The cancelled waiter in _await_task must not release the lease: that
        # write would race the settlement CAS below. Shutdown releases it.
        self._shutting_down = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self._terminalize_interrupted_run()

    async def _terminalize_interrupted_run(self) -> None:
        # Renewal writes the Run too, so stop it before settling.
        await self._stop_lease_renewal()
        if self._run_id is not None:
            run = await self.run_store.load(self._run_id)
            if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                try:
                    await self.kernel.terminalize(
                        self._run_id,
                        status="failed",
                        reason="interrupted",
                        lifecycle=self._emit_kernel_event,
                    )
                except Exception:
                    # Keep this driver's claim: once its lease expires, recovery
                    # sees an interrupted driver and retries the same settlement.
                    # Releasing it would make recovery resume the Run instead.
                    logger.warning(
                        "interrupted Run settlement failed; recovery will retry",
                        exc_info=True,
                    )
                    return
        await self._release_execution_lease()

    async def schedule_recovery(self, *, next_attempt_at: datetime) -> None:
        """Request an early wake without replacing a recovery worker's claim."""
        if self._run_id is None:
            return
        await schedule_run_recovery(
            self.run_store, self._run_id, next_attempt_at=next_attempt_at
        )

    def subscribe(self, listener: SessionEventListener):
        return self.lifecycle.subscribe(listener)

    async def _start_kernel(self) -> KernelRunResult:
        if self._run_id is None:
            raise SessionConflict("no active Run")
        self._idle.clear()
        await self._acquire_execution_lease()
        self._task = asyncio.create_task(
            self.kernel.run(
                self._run_id,
                signal=self._signal,
                lifecycle=self._emit_kernel_event,
            )
        )
        self._settled_task = None
        return await self._await_task()

    async def _await_task(self) -> KernelRunResult:
        task = self._task
        if task is None:
            raise SessionConflict("no active task")
        try:
            result = await task
            async with self._settlement_lock:
                if self._settled_task is not task:
                    terminal_type = {
                        "final_answer": "run_final_answer_ready",
                        "waiting_external": "run_waiting_external",
                        "awaiting_user": "run_awaiting_user",
                        "budget_exhausted": "run_budget_exhausted",
                        "aborted": "run_canceled",
                        "failed": "run_failed",
                    }.get(result.outcome)
                    if terminal_type is not None:
                        await self._emit(terminal_type, result.run)
                    if self._run_id is not None:
                        run = await self.run_store.load(self._run_id)
                        if run is not None:
                            await self._emit("session_idle", run)
                    self._settled_task = task
                    self._idle.set()
            return result
        finally:
            if not self._shutting_down:
                await self._release_execution_lease()

    async def _current_run(self) -> OrchestratorRunState:
        if self._run_id is None:
            raise SessionConflict("no active Run")
        run = await self.run_store.load(self._run_id)
        if run is None:
            raise SessionConflict("active Run is missing")
        return run

    async def _emit_kernel_event(
        self,
        event_type: str,
        run: OrchestratorRunState,
        payload: dict[str, object],
    ) -> None:
        await self._emit(event_type, run, payload=payload)

    async def _emit(
        self,
        event_type: str,
        run: OrchestratorRunState,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._sequence += 1
        await self.lifecycle.emit(
            SessionEvent(
                event_type=event_type,  # type: ignore[arg-type]
                session_id=self.config.session_id,
                run_id=run.run_id,
                causation_id=run.request.user_message_id,
                sequence=self._sequence,
                timestamp=self.clock.now(),
                payload=payload or {"status": run.status},
                conversation_id=run.conversation_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
            )
        )


async def schedule_run_recovery(
    run_store: OrchestratorRunStore, run_id: str, *, next_attempt_at: datetime
) -> None:
    """Share version-fenced early admission across hosted and run-addressed work."""
    for _attempt in range(4):
        run = await run_store.load(run_id)
        if run is None:
            raise SessionConflict("active Run is missing")
        result = await run_store.schedule_recovery(
            run.run_id,
            expected_state_version=run.state_version,
            next_attempt_at=next_attempt_at,
        )
        if result.outcome in {"accepted", "replayed"}:
            return
        current = result.run
        if (
            current is None
            or current.recovery_claim.owner_id is not None
            or current.status in {"canceling", *TERMINAL_RUN_STATUSES}
            or current.recovery_claim.quarantined_at is not None
        ):
            return
        await asyncio.sleep(0)
    raise SessionConflict("recoverable Run could not be scheduled")


def _replayed_result(run: OrchestratorRunState) -> KernelRunResult | None:
    outcomes = {
        "completed": "final_answer",
        "failed": "failed",
        "canceled": "aborted",
        "budget_exhausted": "budget_exhausted",
        "waiting_external": "waiting_external",
        "awaiting_user": "awaiting_user",
    }
    outcome = outcomes.get(run.status)
    if outcome is None:
        return None
    return KernelRunResult(outcome, run)  # type: ignore[arg-type]


SessionRunResult = KernelRunResult

__all__ = [
    "DORMANT_RECOVERY_DELAY",
    "DefaultRunFactory",
    "EventCancellationSignal",
    "RoomAgentSession",
    "RoomAgentSessionConfig",
    "RunFactory",
    "SessionConflict",
    "SessionRunResult",
    "dormant_recovery_at",
]
