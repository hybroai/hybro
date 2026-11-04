"""Run-addressed generic observation delivery without process-local session state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from common.observability import events as E
from common.observability import get_logger

from ..budget import resumed_deadline_at
from ..kernel import KernelLifecycle, KernelRunResult, OrchestratorKernel, SystemClock
from ..lifecycle import LifecycleEmitter, SessionEvent, SessionEventListener
from ..models import OrchestratorRunState, ToolObservation
from ..persistence import SUSPENDED_RUN_STATUSES
from ..ports import CancellationSignal, OrchestratorRunStore
from ..session import schedule_run_recovery
from .errors import RecoverableAdapterError

logger = get_logger(__name__)


async def await_recoverable(
    work: Awaitable[KernelRunResult],
    *,
    schedule_recovery: Callable[[datetime], Awaitable[None]],
    now: Callable[[], datetime],
    conversation_id: str,
    run_id: Callable[[], str | None],
) -> KernelRunResult:
    """Request the same early wake at every accepted Kernel re-entry boundary."""
    try:
        return await work
    except RecoverableAdapterError:
        try:
            await schedule_recovery(now() + timedelta(seconds=5))
        except Exception as exc:
            # Read the Run id late: a prompt creates its Run inside ``work``.
            logger.emit(
                E.RUN_RECOVERY_SCHEDULE_FAILED,
                exc=exc,
                run_id=run_id(),
                conversation_id=conversation_id,
            )
        raise


KernelFactory = Callable[
    [OrchestratorRunState], OrchestratorKernel | Awaitable[OrchestratorKernel]
]
SignalFactory = Callable[[], CancellationSignal]
LifecycleFactory = Callable[[OrchestratorRunState], KernelLifecycle | None]


class RunAddressedToolObservationSink:
    def __init__(
        self,
        *,
        run_store: OrchestratorRunStore,
        kernel_factory: KernelFactory,
        signal_factory: SignalFactory,
        lifecycle_factory: LifecycleFactory | None = None,
        listener: SessionEventListener | None = None,
        clock: SystemClock | None = None,
    ) -> None:
        self.run_store = run_store
        self.kernel_factory = kernel_factory
        self.signal_factory = signal_factory
        self.lifecycle_factory = lifecycle_factory
        self.listener = listener
        self.clock = clock or SystemClock()

    async def deliver(self, run_id: str, observation: ToolObservation) -> None:
        run = await self.run_store.load(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in SUSPENDED_RUN_STATUSES:
            # No driver owns this re-entry. Re-arm the dormant watchdog at the
            # deadline the resume extends to, so a crash after re-entry is
            # still recovered.
            await schedule_run_recovery(
                self.run_store,
                run_id,
                next_attempt_at=resumed_deadline_at(run.budget, now=self.clock.now()),
            )
        built = self.kernel_factory(run)
        kernel = await built if hasattr(built, "__await__") else built
        lifecycle = self.lifecycle_factory(run) if self.lifecycle_factory else None

        if lifecycle is None and self.listener is not None:
            emitter = LifecycleEmitter()
            emitter.subscribe(self.listener)
            sequence = 0

            async def emit_kernel_event(
                event_type: str,
                current_run: OrchestratorRunState,
                payload: dict[str, object],
            ) -> None:
                nonlocal sequence
                sequence += 1
                await emitter.emit(
                    SessionEvent(
                        event_type=event_type,  # type: ignore[arg-type]
                        session_id=current_run.session_id,
                        run_id=current_run.run_id,
                        causation_id=current_run.request.user_message_id,
                        sequence=sequence,
                        timestamp=datetime.now(UTC),
                        payload=payload or {"status": current_run.status},
                        conversation_id=current_run.conversation_id,
                        user_message_id=current_run.request.user_message_id,
                        client_request_id=current_run.client_request_id,
                    )
                )

            lifecycle = emit_kernel_event

        await await_recoverable(
            kernel.observe_tool(
                run_id,
                observation,
                signal=self.signal_factory(),
                lifecycle=lifecycle,
            ),
            schedule_recovery=lambda due: schedule_run_recovery(
                self.run_store, run_id, next_attempt_at=due
            ),
            now=self.clock.now,
            conversation_id=run.conversation_id,
            run_id=lambda: run_id,
        )
