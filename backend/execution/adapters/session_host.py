"""Process-local ``RoomAgentSession`` host (one active session per Room).

The host resolves the active Room epoch from the bound epoch store and builds a
``RoomAgentSession`` with a frozen profile/catalog/scope snapshot before Run
creation. Lifecycle ``SessionEvent`` values are forwarded to an injected
listener.

The host is deliberately unreachable from routes: it exposes only the builder
surface used by the composition root and tests.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
from execution.orchestrator.a2a_runtime.observations import (
    RunAddressedToolObservationSink,
    await_recoverable,
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
        conversation_id: str,
        profile: OrchestratorProfile,
        candidate_scope: CandidateScopeSnapshot,
        requesting_subject_id: str,
        frozen_catalog: FrozenToolCatalogSnapshot,
        resource_manifest: RunResourceManifestSnapshot | None = None,
        conversation_history: tuple[ModelMessage, ...] = (),
        run_factory: RunFactory | None = None,
    ) -> RoomAgentSession:
        if conversation_id in self._sessions:
            raise SessionConflict("a session is already active for this Room")
        epoch = await self._epoch_store.read_active(conversation_id)
        if epoch is None:
            raise SessionConflict("Room epoch is not active")
        config = RoomAgentSessionConfig(
            session_id=f"room:{conversation_id}:epoch:{epoch.epoch}",
            conversation_id=conversation_id,
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
        self._sessions[conversation_id] = session
        return session

    def get_session(self, conversation_id: str) -> RoomAgentSession | None:
        return self._sessions.get(conversation_id)

    def drop_session(self, conversation_id: str) -> None:
        self._sessions.pop(conversation_id, None)

    async def prompt(
        self,
        conversation_id: str,
        message: UserMessage,
        *,
        client_request_id: str | None = None,
    ) -> KernelRunResult:
        session = self._require_session(conversation_id)
        return await self._await_recoverable(
            session, session.prompt(message, client_request_id=client_request_id)
        )

    async def continue_run(self, conversation_id: str) -> KernelRunResult:
        session = self._require_session(conversation_id)
        return await self._await_recoverable(session, session.continue_run())

    async def observe_tool(
        self, conversation_id: str, observation: ToolObservation
    ) -> KernelRunResult:
        session = self._require_session(conversation_id)
        return await self._await_recoverable(session, session.observe_tool(observation))

    async def _await_recoverable(
        self, session: RoomAgentSession, work: Awaitable[KernelRunResult]
    ) -> KernelRunResult:
        return await await_recoverable(
            work,
            schedule_recovery=lambda due: session.schedule_recovery(
                next_attempt_at=due
            ),
            now=self._clock.now,
        )

    async def abort(self, conversation_id: str) -> None:
        await self._require_session(conversation_id).abort()

    async def signal_run_cancellation(self, run, cancellation_command_id: str) -> None:
        """Signal a matching hosted Run without waiting for it to stop."""

        session = self._sessions.get(run.conversation_id)
        if session is None or not session.owns_run(run.run_id):
            return
        await session.signal_interrupt(run.run_id, cancellation_command_id)

    async def interrupt_run(self, run, cancellation_command_id: str) -> bool:
        """Signal a matching hosted Run and wait briefly for it to stop."""

        session = self._sessions.get(run.conversation_id)
        if session is None or not session.owns_run(run.run_id):
            return True
        return await session.interrupt(run.run_id, cancellation_command_id)

    async def reconcile_cancellation(self, run) -> KernelRunResult:
        """Close a durably canceling Run through the existing terminalizer."""

        if run.status != "canceling" or run.cancellation_command_id is None:
            raise SessionConflict("Run is not pending durable cancellation")
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
                    conversation_id=current.conversation_id,
                    user_message_id=current.request.user_message_id,
                    client_request_id=current.client_request_id,
                )
            )

        return await self._kernel_factory(run.tool_catalog).terminalize(
            run.run_id,
            status="canceled",
            reason="cancellation requested",
            cancellation_cause=run.cancellation_cause,
            lifecycle=emit,
        )

    async def abort_run(self, run) -> None:
        """Signal a durably canceling Run, then reconcile terminal settlement."""

        session = self._sessions.get(run.conversation_id)
        if session is not None and session.owns_run(run.run_id):
            await session.abort()
            await self.reconcile_cancellation(run)
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
                    conversation_id=current.conversation_id,
                    user_message_id=current.request.user_message_id,
                    client_request_id=current.client_request_id,
                )
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
                # Re-entry forwards every lifecycle boundary and awaits the
                # durable listener before the Kernel checkpoints a public Tool
                # terminal as emitted.
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
                        conversation_id=run.conversation_id,
                        user_message_id=run.request.user_message_id,
                        client_request_id=run.client_request_id,
                    )
                )

            return emit

        return RunAddressedToolObservationSink(
            run_store=self._run_store,
            kernel_factory=kernel_for_run,
            signal_factory=EventCancellationSignal,
            lifecycle_factory=lifecycle_for_run,
            clock=self._clock,
        )

    def _new_lifecycle_emitter(self) -> LifecycleEmitter:
        # Agent-card terminal projection performs durable Run/binding reads,
        # Mongo replacement, and room-event append. Allow it to finish under
        # transient local/replica-set latency.
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
        """Shut down every in-process session and forget it.

        Each session settles its in-flight Run as ``failed`` with reason
        ``interrupted``; see ``RoomAgentSession.shutdown``.
        ``RoomAgentSession.abort`` is the user-facing cancellation path and
        persists ``canceled``; do not call it here.
        """
        for session in list(self._sessions.values()):
            await session.shutdown()
        self._sessions.clear()

    def _require_session(self, conversation_id: str) -> RoomAgentSession:
        session = self._sessions.get(conversation_id)
        if session is None:
            raise SessionConflict("no active session for this Room")
        return session


__all__ = [
    "KernelForCatalog",
    "RoomSessionHost",
]
