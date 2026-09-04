from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from common.a2a_constants import SSEProcessingStatus
from common.dto import (
    CancellationAck,
    ExecutionAck,
    ExecutionRequest,
    HITLRequest,
    HITLResponse,
    RunInfo,
)
from common.observability import bind_log_context, traced_create_task
from common.protocols import EventPublisher
from common.utils.logger import get_logger
from execution.cancellation.finalizer import (
    CancellationFinalizationResult,
    CancellationFinalizer,
)
from execution.cancellation.ports import (
    CancellationMarkerRepositoryPort,
    CancellationMessageReaderPort,
)
from execution.cancellation.service import CancellationService
from execution.events import emit_room_processing_status
from execution.hitl.translators import (
    hitl_response_dict_to_common,
    model_hitl_request_to_common,
)
from execution.idempotency import (
    IDEMPOTENCY_FINGERPRINT_VERSION,
    build_execution_request_fingerprint,
    normalize_client_request_id,
)
from execution.orchestration.run_store import OrchestrationRunStore
from execution.orchestrator_routing import (
    OrchestratorHITLNotOwnedError,
    OrchestratorRoutingError,
)
from execution.ports import (
    AgentTaskCleanupPort,
    CancellationStatePort,
    ClientRequestIdResolver,
    HITLMessageCancellationPort,
    RunEventEnabled,
    RunLifecyclePort,
    RunReadPort,
    TaskFactory,
)
from execution.shutdown import GRACEFUL_SHUTDOWN_CANCEL_REASON
from execution.translators import room_response_to_execution_ack
from models.orchestration import OrchestrationStatus
from models.request import OrchestrationRequest, RoomCenterUserMessageRequest
from models.run import RunState

logger = get_logger(__name__)


@dataclass(frozen=True)
class _RequestIdempotency:
    client_request_id: str | None = None
    fingerprint: str | None = None
    fingerprint_version: int | None = None


class RoomCenterPort(Protocol):
    async def get_idempotent_user_message(
        self,
        *,
        room_id: str,
        client_request_id: str,
        idempotency_fingerprint: str,
        idempotency_fingerprint_version: int,
    ) -> Any | None: ...

    async def send_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: Any = None,
        mentioned_agent_ids: Any = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> Any: ...

    async def persist_message_to_room(
        self,
        request: RoomCenterUserMessageRequest,
        target_group: Any = None,
        mentioned_agent_ids: Any = None,
        *,
        idempotency_fingerprint: str | None = None,
        idempotency_fingerprint_version: int | None = None,
    ) -> tuple[Any, Any | None]: ...

    async def run_message_preflight_to_room(self, context: Any) -> Any: ...

    def discard_message_preflight(self, context: Any) -> None: ...

    async def update_user_message_orchestration_status(
        self,
        message_id: str,
        status: str,
    ) -> bool: ...


class HITLServicePort(Protocol):
    async def request_interaction(self, **kwargs: Any) -> list[Any] | None: ...

    async def handle_response(
        self,
        room_id: str,
        request_id: str,
        user_input: str,
        user_id: str,
    ) -> dict[str, Any]: ...

    async def handle_batch_response(
        self,
        room_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        user_id: str,
        client_request_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_pending_requests(self, room_id: str) -> list[Any]: ...

    async def cancel_request(
        self,
        request_id: str,
        room_id: str | None = None,
    ) -> Any: ...


class ExecutionFacade:
    def __init__(
        self,
        *,
        room_center: RoomCenterPort,
        hitl_manager: HITLServicePort,
        run_lifecycle: RunLifecyclePort,
        run_reader: RunReadPort,
        cancellation_state: CancellationStatePort,
        cancellation_repository: CancellationMarkerRepositoryPort,
        cancellation_message_reader: CancellationMessageReaderPort,
        hitl_message_cancellation: HITLMessageCancellationPort,
        agent_task_cleanup: AgentTaskCleanupPort,
        event_publisher: EventPublisher,
        run_event_enabled: RunEventEnabled,
        client_request_id_resolver: ClientRequestIdResolver,
        orchestration_run_store: OrchestrationRunStore | None = None,
        task_factory: TaskFactory = traced_create_task,
        orchestrator_router: Any | None = None,
    ) -> None:
        self._room_center = room_center
        self._orchestrator_router = orchestrator_router
        self._hitl_manager = hitl_manager
        self._run_lifecycle = run_lifecycle
        self._run_reader = run_reader
        self._active_run_reader = run_reader
        self._cancellation_state = cancellation_state
        self._hitl_message_cancellation = hitl_message_cancellation
        self._agent_task_cleanup = agent_task_cleanup
        self._event_publisher = event_publisher
        self._run_event_enabled = run_event_enabled
        self._client_request_id_resolver = client_request_id_resolver
        cancellation_finalizer = CancellationFinalizer(
            run_store=orchestration_run_store,
            project_status=self._project_orchestration_status,
            broadcast_cancellation=cancellation_state.cancel_message_and_broadcast,
            get_active_token=cancellation_state.get_active_token,
            release_active_token=cancellation_state.release_active_token,
            clear_cancellation=cancellation_state.clear_cancellation,
            cancel_hitl=hitl_message_cancellation.cancel_requests_for_message,
            project_public_terminal=self._project_public_terminal_status,
            cleanup_agent_tasks=agent_task_cleanup.cleanup_cancelled_message_tasks,
            mark_reconciled=cancellation_repository.mark_reconciled,
            get_public_run=getattr(
                run_reader,
                "get_run_strict",
                run_reader.get_run,
            ),
        )
        self._cancellation_service = CancellationService(
            repository=cancellation_repository,
            finalizer=cancellation_finalizer,
            message_reader=cancellation_message_reader,
        )
        self._task_factory = task_factory
        self._inflight: set[asyncio.Task] = set()

    def bind_orchestrator_router(self, router: Any) -> None:
        """Attach the orchestrator ingress adapter after composition."""
        self._orchestrator_router = router

    def bind_active_run_reader(self, run_reader: Any) -> None:
        """Use the canonical aggregate for public and send-time active Runs."""
        self._active_run_reader = run_reader

    async def route_webhook(
        self,
        *,
        message_id: str,
        payload: dict[str, Any],
        token: str,
    ) -> None:
        """Record an authenticated orchestrator-owned webhook observation."""
        router = self._orchestrator_router
        if router is None:
            raise OrchestratorRoutingError("orchestrator webhook ingress is not bound")
        await router.route_webhook(message_id=message_id, payload=payload, token=token)

    @staticmethod
    def _prepare_request_idempotency(
        request: ExecutionRequest,
    ) -> tuple[ExecutionRequest, _RequestIdempotency]:
        if not isinstance(request.client_request_id, str):
            return request, _RequestIdempotency()
        client_request_id = normalize_client_request_id(request.client_request_id)
        if client_request_id != request.client_request_id:
            request = request.model_copy(
                update={"client_request_id": client_request_id}
            )
        if not client_request_id:
            return request, _RequestIdempotency()
        return request, _RequestIdempotency(
            client_request_id=client_request_id,
            fingerprint=build_execution_request_fingerprint(request),
            fingerprint_version=IDEMPOTENCY_FINGERPRINT_VERSION,
        )

    async def _lookup_idempotent_ack(
        self,
        *,
        request: ExecutionRequest,
        idempotency: _RequestIdempotency,
    ) -> ExecutionAck | None:
        if (
            idempotency.client_request_id is None
            or idempotency.fingerprint is None
            or idempotency.fingerprint_version is None
        ):
            return None
        response = await self._room_center.get_idempotent_user_message(
            room_id=request.room_id,
            client_request_id=idempotency.client_request_id,
            idempotency_fingerprint=idempotency.fingerprint,
            idempotency_fingerprint_version=idempotency.fingerprint_version,
        )
        return (
            room_response_to_execution_ack(response) if response is not None else None
        )

    async def _replay_or_rejection(
        self,
        *,
        request: ExecutionRequest,
        idempotency: _RequestIdempotency,
        rejection: ExecutionAck,
    ) -> ExecutionAck:
        replay_ack = await self._lookup_idempotent_ack(
            request=request,
            idempotency=idempotency,
        )
        return replay_ack or rejection

    async def _reject_if_hitl_pending(
        self,
        request: ExecutionRequest,
    ) -> ExecutionAck | None:
        try:
            pending_requests = await self._hitl_manager.get_pending_requests(
                request.room_id
            )
        except Exception:
            logger.warning("pending HITL lookup failed before execute", exc_info=True)
            return None
        if not pending_requests:
            return None
        return ExecutionAck(
            room_id=request.room_id,
            success=False,
            error="Room is waiting for your input before it can process another message.",
            status_code=409,
            should_start_orchestration=False,
        )

    async def _reject_if_room_has_active_run(
        self,
        request: ExecutionRequest,
    ) -> ExecutionAck | None:
        try:
            active_runs = await self._active_run_reader.get_runs_for_room(
                request.room_id
            )
        except Exception:
            logger.warning(
                "active room run lookup failed before execute", exc_info=True
            )
            return None
        if not active_runs:
            return None
        return ExecutionAck(
            room_id=request.room_id,
            success=False,
            error="Room is already processing another message.",
            status_code=409,
            should_start_orchestration=False,
        )

    async def _emit_room_preflight_processing_status(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not ack.message_id:
            return
        lifecycle_message_id = ack.dispatch_root_message_id or ack.message_id
        await emit_room_processing_status(
            room_id=ack.room_id or request.room_id,
            status=SSEProcessingStatus.PROCESSING,
            message_id=ack.message_id,
            lifecycle_message_id=lifecycle_message_id,
            run_lifecycle=self._run_lifecycle,
            event_publisher=self._event_publisher,
            run_event_enabled=self._run_event_enabled,
            client_request_id_resolver=self._client_request_id_resolver,
            record_lifecycle=False,
            client_request_id=request.client_request_id,
        )

    def _terminal_preflight_status(
        self,
        ack: ExecutionAck,
    ) -> SSEProcessingStatus | None:
        if ack.should_start_orchestration:
            return None
        status_by_outcome = {
            "completed": SSEProcessingStatus.COMPLETED,
            "canceled": SSEProcessingStatus.CANCELED,
            "failed": SSEProcessingStatus.FAILED,
        }
        if ack.preflight_outcome in status_by_outcome:
            return status_by_outcome[ack.preflight_outcome]
        if ack.message_id and not ack.success:
            return SSEProcessingStatus.FAILED
        return None

    async def _emit_room_preflight_terminal_status(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        status = self._terminal_preflight_status(ack)
        if status is None or not ack.message_id:
            return
        lifecycle_message_id = ack.dispatch_root_message_id or ack.message_id
        await emit_room_processing_status(
            room_id=ack.room_id or request.room_id,
            status=status,
            message_id=ack.message_id,
            lifecycle_message_id=lifecycle_message_id,
            run_lifecycle=self._run_lifecycle,
            event_publisher=self._event_publisher,
            run_event_enabled=self._run_event_enabled,
            client_request_id_resolver=self._client_request_id_resolver,
            record_lifecycle=False,
            client_request_id=request.client_request_id,
            details=ack.preflight_details or ack.error,
        )

    async def _emit_room_preflight_statuses(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        try:
            await self._emit_room_preflight_processing_status(request, ack)
            await self._emit_room_preflight_terminal_status(request, ack)
        except Exception:
            logger.warning(
                "room preflight status emission failed after persistence",
                exc_info=True,
            )

    @staticmethod
    def _room_request_extend_info(request: ExecutionRequest) -> dict[str, Any]:
        return {
            "execution_mode": request.mode,
            "agent_scope": request.agent_scope.model_dump(mode="json"),
        }

    @staticmethod
    def _scope_routing(request: ExecutionRequest) -> tuple[str, list[str] | None]:
        scope = request.agent_scope
        if scope.source == "mention":
            return "room_team", list(scope.agent_ids)
        if scope.source == "all_agents":
            return "all_agents", None
        if scope.source == "saved_group":
            return scope.group_id, None
        return "room_team", None

    async def execute(self, request: ExecutionRequest) -> ExecutionAck:
        request, idempotency = self._prepare_request_idempotency(request)
        room_request = RoomCenterUserMessageRequest(
            room_id=request.room_id,
            user_id=request.sender_id,
            user_name=request.sender_name,
            message=request.message,
            attachments=request.attachments,
            inline_file_ids=request.inline_file_ids,
            client_request_id=idempotency.client_request_id,
            extend_info=self._room_request_extend_info(request),
        )
        replay_ack = await self._lookup_idempotent_ack(
            request=request,
            idempotency=idempotency,
        )
        if replay_ack is not None:
            return replay_ack

        hitl_rejection = await self._reject_if_hitl_pending(request)
        if hitl_rejection is not None:
            return await self._replay_or_rejection(
                request=request,
                idempotency=idempotency,
                rejection=hitl_rejection,
            )

        active_run_rejection = await self._reject_if_room_has_active_run(request)
        if active_run_rejection is not None:
            return await self._replay_or_rejection(
                request=request,
                idempotency=idempotency,
                rejection=active_run_rejection,
            )

        target_group, mentioned_agent_ids = self._scope_routing(request)
        (
            persisted_response,
            preflight_context,
        ) = await self._room_center.persist_message_to_room(
            room_request,
            target_group,
            mentioned_agent_ids,
            idempotency_fingerprint=idempotency.fingerprint,
            idempotency_fingerprint_version=idempotency.fingerprint_version,
        )
        if preflight_context is None:
            return room_response_to_execution_ack(persisted_response)
        try:
            persisted_ack = room_response_to_execution_ack(persisted_response)
            try:
                await self._emit_room_preflight_processing_status(
                    request, persisted_ack
                )
            except Exception:
                logger.warning(
                    "room preflight processing status emission failed after persistence",
                    exc_info=True,
                )
            response = await self._room_center.run_message_preflight_to_room(
                preflight_context
            )
            ack = room_response_to_execution_ack(response)
            try:
                await self._emit_room_preflight_terminal_status(request, ack)
            except Exception:
                logger.warning(
                    "room preflight terminal status emission failed after preflight",
                    exc_info=True,
                )
            return ack
        except BaseException:
            try:
                self._room_center.discard_message_preflight(preflight_context)
            except BaseException:
                logger.warning(
                    "room preflight cleanup failed while preserving original error",
                    exc_info=True,
                )
            raise

    async def _route_orchestration(
        self,
        request: ExecutionRequest,
        orchestration_request: OrchestrationRequest,
    ) -> None:
        router = self._orchestrator_router
        if router is None:
            raise OrchestratorRoutingError("orchestrator router is not bound")
        await router.process_room_user_message(orchestration_request)

    async def start_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not ack.success or not ack.message_id or not ack.should_start_orchestration:
            return
        orchestration_request = OrchestrationRequest(
            room_id=request.room_id,
            room_user_message_id=ack.message_id,
            room_related_message_id=request.parent_message_id,
            user_id=request.sender_id,
            client_request_id=request.client_request_id,
            mode=request.mode,
            agent_scope=request.agent_scope.model_dump(mode="json"),
        )
        with bind_log_context(
            client_request_id=request.client_request_id,
            room_id=request.room_id,
            user_message_id=ack.message_id,
            message_id=ack.message_id,
        ):
            task = self._spawn_orchestration(
                self._route_orchestration(request, orchestration_request),
                name=f"execution-orchestrate-{ack.message_id}",
            )
        await task

    def schedule_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not ack.success or not ack.message_id or not ack.should_start_orchestration:
            return
        orchestration_request = OrchestrationRequest(
            room_id=request.room_id,
            room_user_message_id=ack.message_id,
            room_related_message_id=request.parent_message_id,
            user_id=request.sender_id,
            client_request_id=request.client_request_id,
            mode=request.mode,
            agent_scope=request.agent_scope.model_dump(mode="json"),
        )
        with bind_log_context(
            client_request_id=request.client_request_id,
            room_id=request.room_id,
            user_message_id=ack.message_id,
            message_id=ack.message_id,
        ):
            self._spawn_orchestration(
                self._route_orchestration(request, orchestration_request),
                name=f"execution-orchestrate-{ack.message_id}",
            )

    def _spawn_orchestration(
        self,
        coro,
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        task = self._task_factory(coro, name=name)
        self._inflight.add(task)

        def _on_done(done: asyncio.Task) -> None:
            self._inflight.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                logger.error(
                    "execution orchestration task failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task

    async def _project_orchestration_status(
        self,
        *,
        room_id: str,
        message_id: str,
        status: OrchestrationStatus,
    ) -> bool:
        try:
            return bool(
                await self._room_center.update_user_message_orchestration_status(
                    message_id,
                    status.value,
                )
            )
        except Exception:
            logger.warning(
                "failed to project orchestration status",
                extra={
                    "message_id": message_id,
                    "room_id": room_id,
                    "status": status.value,
                },
                exc_info=True,
            )
            return False

    async def _project_public_terminal_status(
        self,
        *,
        room_id: str,
        message_id: str,
        status: OrchestrationStatus,
    ) -> None:
        target_state = {
            OrchestrationStatus.COMPLETED: RunState.COMPLETED,
            OrchestrationStatus.CANCELED: RunState.CANCELED,
            OrchestrationStatus.FAILED: RunState.FAILED,
            OrchestrationStatus.BUDGET_EXHAUSTED: RunState.FAILED,
        }[status]
        projected = await self._run_lifecycle.project_run_state(
            room_id=room_id,
            run_id=message_id,
            trigger_message_id=message_id,
            target_state=target_state,
            terminal_reason=(
                "request canceled" if target_state == RunState.CANCELED else None
            ),
            causation_id=f"orchestration-terminal-repair:{message_id}:{status.value}",
        )
        if projected is None:
            strict_get_run = getattr(
                self._run_reader,
                "get_run_strict",
                self._run_reader.get_run,
            )
            public_run = await strict_get_run(message_id)
            public_state = getattr(
                getattr(public_run, "state", None),
                "value",
                getattr(public_run, "state", None),
            )
            if public_state != target_state.value:
                raise RuntimeError("public terminal lifecycle projection failed")

    async def finalize_pending_cancellation(
        self,
        *,
        room_id: str,
        message_id: str,
        settle_no_run: bool = False,
    ) -> CancellationFinalizationResult:
        return await self._cancellation_service.finalize(
            room_id=room_id,
            message_id=message_id,
            settle_no_run=settle_no_run,
        )

    @property
    def cancellation_service(self) -> CancellationService:
        return self._cancellation_service

    async def cancel(
        self,
        room_id: str,
        message_id: str,
        *,
        requested_by_user_id: str,
    ) -> bool | CancellationAck:
        router = self._orchestrator_router
        if router is not None:
            return await router.route_cancellation_by_user_message(
                message_id,
                reason=f"user:{requested_by_user_id}",
                post_claim_cleanup=lambda: (
                    self._hitl_message_cancellation.cancel_requests_for_message(
                        message_id
                    )
                ),
            )
        return await self._cancellation_service.cancel(
            room_id=room_id,
            message_id=message_id,
            requested_by_user_id=requested_by_user_id,
        )

    async def get_run(self, run_id: str) -> RunInfo | None:
        return await self._run_reader.get_run(run_id)

    async def get_runs_for_room(self, room_id: str) -> list[RunInfo]:
        return await self._active_run_reader.get_runs_for_room(room_id)

    async def get_latest_runs_for_rooms(
        self, room_ids: list[str]
    ) -> dict[str, RunInfo]:
        reader = self._active_run_reader
        bulk = getattr(reader, "get_latest_runs_for_rooms", None)
        if bulk is not None:
            return await bulk(room_ids)
        return await self._run_reader.get_latest_runs_for_rooms(room_ids)

    async def cancel_inflight_tasks(self) -> int:
        """Interrupt local execution without terminalizing durable runs.

        Graceful process shutdown is an infrastructure interruption, not a user
        cancellation. Non-terminal orchestration remains recoverable after the
        next process starts, so this method must not emit a public terminal state.
        """
        tasks = {task for task in set(self._inflight) if not task.done()}
        for task in tasks:
            task.cancel(GRACEFUL_SHUTDOWN_CANCEL_REASON)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return sum(task.cancelled() for task in tasks)

    async def heal_diverged_runs(self, limit: int = 500) -> int:
        return await self._run_lifecycle.heal_diverged_runs(limit=limit)

    async def resolve_hitl_batch(
        self,
        room_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        responder_id: str,
        client_request_id: str | None = None,
    ) -> HITLResponse:
        router = self._orchestrator_router
        if router is not None:
            try:
                await router.route_hitl_answer(
                    interaction_id=interaction_id,
                    answers=answers,
                    responder_id=responder_id,
                    room_id=room_id,
                )
            except OrchestratorHITLNotOwnedError:
                # Supervisor ask_user interactions live in the unified HITL
                # aggregate, not the orchestrator A2A call ledger.
                pass
            except KeyError as exc:
                # Only fall through when the interaction is absent from the
                # orchestrator store; other KeyErrors from resume stay fatal.
                if exc.args != (interaction_id,):
                    raise
            else:
                return HITLResponse(
                    request_id=interaction_id,
                    status="accepted",
                    responder_id=responder_id,
                    client_request_id=client_request_id,
                )
        result = await self._hitl_manager.handle_batch_response(
            room_id=room_id,
            interaction_id=interaction_id,
            answers=answers,
            user_id=responder_id,
            client_request_id=client_request_id,
        )
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        result.setdefault("request_id", interaction_id)
        result.setdefault("responder_id", responder_id)
        if client_request_id:
            result.setdefault("client_request_id", client_request_id)
        return hitl_response_dict_to_common(result)

    async def get_pending_hitl(self, room_id: str) -> list[HITLRequest]:
        requests = await self._hitl_manager.get_pending_requests(room_id)
        public_requests = [
            model_hitl_request_to_common(request) for request in requests
        ]

        if self._orchestrator_router is not None:
            orchestrator_requests = await self._orchestrator_router.get_pending_hitl(
                room_id
            )
            # Prefer orchestrator rows when legacy and orchestrator both surface
            # the same question during dual-runtime overlap.
            merged: dict[tuple[str, str], HITLRequest] = {
                (item.interaction_id or "", item.request_id): item
                for item in public_requests
            }
            for item in orchestrator_requests:
                merged[(item.interaction_id or "", item.request_id)] = item
            return list(merged.values())

        return public_requests

    async def cancel_hitl_interaction(
        self,
        room_id: str,
        interaction_id: str,
        expected_version: int,
    ) -> int:
        router = self._orchestrator_router
        if router is not None:
            try:
                return await router.cancel_hitl_interaction(
                    room_id=room_id,
                    interaction_id=interaction_id,
                    expected_version=expected_version,
                )
            except KeyError as exc:
                if exc.args != (interaction_id,):
                    raise
        return await self._hitl_manager.cancel_interaction_by_user(
            interaction_id,
            room_id,
            expected_version=expected_version,
        )


__all__ = [
    "ExecutionFacade",
]
