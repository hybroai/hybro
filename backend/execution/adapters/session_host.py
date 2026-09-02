"""Process-local ``RoomAgentSession`` host (one active session per Room).

The host resolves the active Room epoch from the bound epoch store and builds a
``RoomAgentSession`` with a frozen profile/catalog/scope snapshot before Run
creation. Lifecycle ``SessionEvent`` values are forwarded to an injected
listener (the step-6 listener will write to the projection outbox; during the
step-5b dark launch a no-op/recording listener is used).

The host is deliberately unreachable from routes: it exposes only the builder
surface used by the composition root and tests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
from execution.orchestrator.a2a_runtime.observations import (
    RunAddressedToolObservationSink,
)
from execution.orchestrator.a2a_runtime.ports import RoomEpochStore
from execution.orchestrator.kernel import (
    KernelRunResult,
    OrchestratorKernel,
    SystemClock,
    UUIDFactory,
)
from execution.orchestrator.lifecycle import (
    LifecycleEmitter,
    SessionEvent,
    SessionEventListener,
)
from execution.orchestrator.models import (
    CandidateScopeSnapshot,
    FrozenToolCatalogSnapshot,
    ModelMessage,
    OrchestratorProfile,
    RunResourceManifestSnapshot,
    ToolObservation,
    UserMessage,
)
from execution.orchestrator.ports import OrchestratorRunStore
from execution.orchestrator.session import (
    DefaultRunFactory,
    EventCancellationSignal,
    RoomAgentSession,
    RoomAgentSessionConfig,
    RunFactory,
    SessionConflict,
)

KernelForCatalog = Callable[[FrozenToolCatalogSnapshot], OrchestratorKernel]


logger = logging.getLogger(__name__)


class RoomSessionHost:
    """Registry of one active ``RoomAgentSession`` per Room."""

    def __init__(
        self,
        *,
        kernel_factory: KernelForCatalog,
        run_store: OrchestratorRunStore,
        epoch_store: RoomEpochStore,
        listener: SessionEventListener | None = None,
        run_factory: RunFactory | None = None,
        clock: SystemClock | None = None,
        id_factory: UUIDFactory | None = None,
    ) -> None:
        self._kernel_factory = kernel_factory
        self._run_store = run_store
        self._epoch_store = epoch_store
        self._listener = listener
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or UUIDFactory()
        self._run_factory = run_factory or DefaultRunFactory(
            clock=self._clock, id_factory=self._id_factory
        )
        self._sessions: dict[str, RoomAgentSession] = {}

    async def create_session(
        self,
        *,
        room_id: str,
        profile: OrchestratorProfile,
        candidate_scope: CandidateScopeSnapshot,
        requesting_subject_id: str,
        frozen_catalog: FrozenToolCatalogSnapshot,
        resource_manifest: RunResourceManifestSnapshot | None = None,
        conversation_history: tuple[ModelMessage, ...] = (),
        run_factory: RunFactory | None = None,
    ) -> RoomAgentSession:
        if room_id in self._sessions:
            raise SessionConflict("a session is already active for this Room")
        epoch = await self._epoch_store.read_active(room_id)
        if epoch is None:
            raise SessionConflict("Room epoch is not active")
        config = RoomAgentSessionConfig(
            session_id=f"room:{room_id}:epoch:{epoch.epoch}",
            room_id=room_id,
            profile=profile,
            candidate_scope=candidate_scope,
            room_epoch=epoch.epoch,
            requesting_subject_id=requesting_subject_id,
            tool_catalog=FrozenToolCatalog(frozen_catalog),
            frozen_tool_catalog=frozen_catalog,
            resource_manifest=resource_manifest,
            conversation_history=conversation_history,
        )
        lifecycle = self._new_lifecycle_emitter()
        session = RoomAgentSession(
            config=config,
            kernel=self._kernel_factory(frozen_catalog),
            run_store=self._run_store,
            run_factory=run_factory or self._run_factory,
            lifecycle=lifecycle,
            clock=self._clock,
        )
        self._sessions[room_id] = session
        return session

    def get_session(self, room_id: str) -> RoomAgentSession | None:
        return self._sessions.get(room_id)

    def drop_session(self, room_id: str) -> None:
        self._sessions.pop(room_id, None)

    async def prompt(
        self,
        room_id: str,
        message: UserMessage,
        *,
        client_request_id: str | None = None,
    ) -> KernelRunResult:
        return await self._require_session(room_id).prompt(
            message, client_request_id=client_request_id
        )

    async def continue_run(self, room_id: str) -> KernelRunResult:
        return await self._require_session(room_id).continue_run()

    async def observe_tool(
        self, room_id: str, observation: ToolObservation
    ) -> KernelRunResult:
        return await self._require_session(room_id).observe_tool(observation)

    async def abort(self, room_id: str) -> None:
        await self._require_session(room_id).abort()

    async def abort_run(self, run) -> None:
        """Terminalize exactly one user-owned Run through its live signal/state machine.

        A hosted Assistant/Tool execution is signaled and awaited so provider IO
        stops before terminal children close. Suspended/restarted Runs have no
        live signal; they re-enter the same Kernel terminalizer with the normal
        lifecycle listener so HITL/Tool/Turn closure remains publicly durable.
        """

        session = self._sessions.get(run.room_id)
        if session is not None and session.owns_run(run.run_id):
            await session.abort()
            return
        if run.tool_catalog is None:
            raise SessionConflict("Run has no frozen tool catalog")
        emitter = self._new_lifecycle_emitter()

        async def emit(event_type, current, payload):
            await emitter.emit(
                SessionEvent(
                    event_type=event_type,
                    session_id=f"cancel:{current.run_id}",
                    run_id=current.run_id,
                    causation_id=current.request.user_message_id,
                    sequence=current.state_version,
                    timestamp=self._clock.now(),
                    payload=payload,
                    room_id=current.room_id,
                    user_message_id=current.request.user_message_id,
                    client_request_id=current.client_request_id,
                    lifecycle_family=current.lifecycle_family,
                ),
                terminal=True,
            )

        await self._kernel_factory(run.tool_catalog).terminalize(
            run.run_id,
            status="canceled",
            reason="cancellation requested",
            cancellation_cause="user_requested",
            lifecycle=emit,
        )

    def observation_sink(self) -> RunAddressedToolObservationSink:
        """Re-entry surface for observations with or without a live session."""

        def kernel_for_run(run) -> OrchestratorKernel:
            if run.tool_catalog is None:
                raise SessionConflict("Run has no frozen tool catalog")
            return self._kernel_factory(run.tool_catalog)

        def lifecycle_for_run(initial_run):
            emitter = self._new_lifecycle_emitter()

            async def emit(event_type, run, payload):
                if run.lifecycle_family == "legacy" and not (
                    (
                        event_type == "message_completed"
                        and payload.get("message_kind") == "tool_result"
                    )
                    or event_type == "tool_execution_completed"
                ):
                    return
                # Canonical re-entry forwards every lifecycle boundary and
                # awaits the durable listener before the Kernel checkpoints a
                # public Tool terminal as emitted. Legacy retains its narrow
                # compatibility projection.
                # The observation checkpoint version is durable and monotonic.
                # call_id is also included in downstream delivery identity, so
                # parallel results at the same version cannot collide.
                await emitter.emit(
                    SessionEvent(
                        event_type=event_type,
                        session_id=f"run-addressed:{initial_run.run_id}",
                        run_id=run.run_id,
                        causation_id=run.request.user_message_id,
                        sequence=run.state_version,
                        timestamp=self._clock.now(),
                        payload=payload,
                        room_id=run.room_id,
                        user_message_id=run.request.user_message_id,
                        client_request_id=run.client_request_id,
                        lifecycle_family=run.lifecycle_family,
                    ),
                    terminal=True,
                )

            return emit

        return RunAddressedToolObservationSink(
            run_store=self._run_store,
            kernel_factory=kernel_for_run,
            signal_factory=EventCancellationSignal,
            lifecycle_factory=lifecycle_for_run,
        )

    def _new_lifecycle_emitter(self) -> LifecycleEmitter:
        # Agent-card terminal projection performs durable Run/binding reads,
        # Mongo replacement, and room-event append. Keep the short timeout for
        # ordinary lifecycle noise, but allow this settled boundary to finish
        # under transient local/replica-set latency.
        emitter = LifecycleEmitter(
            settlement_timeout_seconds=30.0,
            error_hook=lambda exc: logger.warning(
                "orchestrator lifecycle listener failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            ),
        )
        if self._listener is not None:
            emitter.subscribe(self._listener)
        return emitter

    async def shutdown(self) -> None:
        """Cancel every in-process session task without persisting terminal state.

        This is the graceful-shutdown surface: the asyncio tasks are cancelled
        directly so Runs stay non-terminal and are re-entered by the recovery
        workers (plan 2.3). ``RoomAgentSession.abort`` is the user-facing
        cancellation path and persists ``canceled``; do not call it here.
        """
        for session in list(self._sessions.values()):
            await session.shutdown()
        self._sessions.clear()

    def _require_session(self, room_id: str) -> RoomAgentSession:
        session = self._sessions.get(room_id)
        if session is None:
            raise SessionConflict("no active session for this Room")
        return session


__all__ = [
    "KernelForCatalog",
    "RoomSessionHost",
]
