"""Room-owned facade over the generic orchestrator kernel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
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
from .ports import OrchestratorRunStore, ToolCatalog


class SessionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RoomAgentSessionConfig:
    session_id: str
    room_id: str
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
                f"{config.room_id}:{config.room_epoch}:"
                f"{config.requesting_subject_id}:{message.model_dump_json()}"
            ).encode()
        ).hexdigest()
        return OrchestratorRunState(
            schema_version=6,
            lifecycle_family="canonical",
            run_id=run_id,
            session_id=config.session_id,
            room_id=config.room_id,
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
        return existing.status not in {
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }

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
        if run.status in {"waiting_external", "awaiting_user"}:
            raise SessionConflict("suspended Run requires a new ToolObservation")
        return await self._start_kernel()

    async def observe_tool(self, observation: ToolObservation) -> KernelRunResult:
        if self._run_id is None:
            raise SessionConflict("no active Run")
        if self._task is not None and not self._task.done():
            raise SessionConflict("Run is currently executing")
        self._idle.clear()
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
        """Cancel the in-process task without persisting a terminal state.

        Graceful-shutdown surface: the Run stays non-terminal and is re-entered
        by recovery workers later. Unlike ``abort``, this never routes through
        the kernel's terminal settlement.
        """
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self._reschedule_interrupted_run()

    async def _reschedule_interrupted_run(self) -> None:
        if self._run_id is None:
            return
        for _attempt in range(4):
            run = await self.run_store.load(self._run_id)
            if run is None or run.status in {
                "completed",
                "failed",
                "canceled",
                "budget_exhausted",
            }:
                return
            candidate = run.model_copy(
                update={
                    "recovery_claim": RecoveryClaim(next_attempt_at=self.clock.now()),
                    "state_version": run.state_version + 1,
                    "updated_at": self.clock.now(),
                }
            )
            stored = await self.run_store.cas_mutate(
                candidate,
                expected_state_version=run.state_version,
                command_id=(
                    f"shutdown-reschedule:{self.config.session_id}:{run.state_version}"
                ),
            )
            if stored.outcome in {"accepted", "replayed"}:
                return
            await asyncio.sleep(0)
        raise SessionConflict("interrupted Run could not be rescheduled")

    def subscribe(self, listener: SessionEventListener):
        return self.lifecycle.subscribe(listener)

    async def _start_kernel(self) -> KernelRunResult:
        if self._run_id is None:
            raise SessionConflict("no active Run")
        self._idle.clear()
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
                    await self._emit(terminal_type, result.run, terminal=True)
                if self._run_id is not None:
                    run = await self.run_store.load(self._run_id)
                    if run is not None:
                        await self._emit("session_idle", run, terminal=True)
                self._settled_task = task
                self._idle.set()
        return result

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
        # A completed tool-result message is the durable boundary for the
        # corresponding agent card. Await listener settlement so its terminal
        # projection cannot be lost to the short best-effort listener timeout.
        settle_agent_projection = (
            event_type == "message_completed"
            and payload.get("message_kind") == "tool_result"
        )
        await self._emit(
            event_type,
            run,
            terminal=settle_agent_projection,
            payload=payload,
        )

    async def _emit(
        self,
        event_type: str,
        run: OrchestratorRunState,
        *,
        terminal: bool = False,
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
                room_id=run.room_id,
                user_message_id=run.request.user_message_id,
                client_request_id=run.client_request_id,
                lifecycle_family=run.lifecycle_family,
            ),
            terminal=terminal,
        )


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
    "DefaultRunFactory",
    "EventCancellationSignal",
    "RoomAgentSession",
    "RoomAgentSessionConfig",
    "RunFactory",
    "SessionConflict",
    "SessionRunResult",
]
