from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from approval import (
    hitl_response_dict_to_common,
    model_hitl_request_to_common,
)
from common.a2a_constants import SSEProcessingStatus
from common.dto import (
    CancellationAck,
    ExecutionAck,
    ExecutionRequest,
    HITLRequest,
    HITLResponse,
    RunInfo,
)
from common.errors import NotFoundError
from common.observability import bind_log_context, traced_create_task
from common.observability import events as E
from common.protocols import EventPublisher
from common.utils.logger import get_logger
from execution.events import emit_room_processing_status
from execution.idempotency import (
    IDEMPOTENCY_FINGERPRINT_VERSION,
    build_execution_request_fingerprint,
    normalize_client_request_id,
)
from execution.orchestrator_routing import (
    OrchestratorHITLNotOwnedError,
    OrchestratorRoutingError,
)
from execution.ports import (
    ClientRequestIdResolver,
    HITLMessageCancellationPort,
    TaskFactory,
)
from execution.shutdown import GRACEFUL_SHUTDOWN_CANCEL_REASON
from execution.translators import room_response_to_execution_ack
from models.request import RoomCenterUserMessageRequest

logger = get_logger(__name__)

_ROOM_BUSY_ERROR = "Room is already processing another message."
_ROOM_STATE_UNAVAILABLE_ERROR = (
    "Could not check whether the room is free. Please try again."
)
_START_FAILED_DETAILS = "Message processing could not start."


@dataclass(frozen=True)
class _RequestIdempotency:
    client_request_id: str | None = None
    fingerprint: str | None = None
    fingerprint_version: int | None = None


@dataclass
class _RoomClaim:
    """This process admitted a send to the Room and its Run may not exist yet.

    Concurrent sends that share the claim's ``client_request_id`` are the same
    request; persistence deduplicates them, so they join the claim instead of
    being rejected.
    """

    client_request_id: str | None
    holders: int = 1
    message_id: str | None = None
    # Loop time when execute handed the claim to a send that starts
    # orchestration; the caller must then schedule it.
    admitted_at: float | None = None


@dataclass
class _PendingOrchestration:
    """An orchestration task that has not ended.

    ``pending`` until the router asks to start the Run, ``starting`` from then
    on, ``canceled`` when a user cancel arrived first, so the Run never
    starts, and ``failed`` when the task failed before any Run existed.
    """

    request: ExecutionRequest
    ack: ExecutionAck
    stage: Literal["pending", "starting", "canceled", "failed"] = "pending"


class RoomCenterPort(Protocol):
    async def get_idempotent_user_message(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        idempotency_fingerprint: str,
        idempotency_fingerprint_version: int,
    ) -> Any | None: ...

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


class HITLServicePort(Protocol):
    async def request_interaction(self, **kwargs: Any) -> list[Any] | None: ...

    async def handle_batch_response(
        self,
        conversation_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        user_id: str,
        client_request_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_pending_requests(self, conversation_id: str) -> list[Any]: ...

    async def cancel_request(
        self,
        request_id: str,
        conversation_id: str | None = None,
    ) -> Any: ...


class ExecutionFacade:
    # How often a cancel looks for a Run the router is creating right now.
    RUN_START_POLL_SECONDS = 0.05
    # An admitted send must be scheduled right after execute returns; a claim
    # whose orchestration never started is dropped after this long.
    UNSCHEDULED_CLAIM_SECONDS = 30.0

    def __init__(
        self,
        *,
        room_center: RoomCenterPort,
        hitl_manager: HITLServicePort,
        hitl_message_cancellation: HITLMessageCancellationPort,
        event_publisher: EventPublisher,
        client_request_id_resolver: ClientRequestIdResolver,
        task_factory: TaskFactory = traced_create_task,
        orchestrator_router: Any | None = None,
    ) -> None:
        self._room_center = room_center
        self._orchestrator_router = orchestrator_router
        self._hitl_manager = hitl_manager
        self._active_run_reader: Any | None = None
        self._hitl_message_cancellation = hitl_message_cancellation
        self._event_publisher = event_publisher
        self._client_request_id_resolver = client_request_id_resolver
        self._task_factory = task_factory
        self._inflight: set[asyncio.Task] = set()
        # User message ids whose orchestration task has not ended.
        self._orchestrating: dict[str, _PendingOrchestration] = {}
        # Rooms with an admitted send whose orchestration has not ended. The
        # durable active-Run check cannot see a Run that is not created yet.
        self._room_claims: dict[str, _RoomClaim] = {}

    def bind_orchestrator_router(self, router: Any) -> None:
        """Attach the orchestrator ingress adapter after composition."""
        self._orchestrator_router = router

    def bind_active_run_reader(self, run_reader: Any) -> None:
        """Use the canonical aggregate for public and send-time active Runs."""
        self._active_run_reader = run_reader

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
            conversation_id=request.conversation_id,
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
                request.conversation_id
            )
        except Exception as exc:
            logger.emit(
                E.PENDING_HITL_LOOKUP_FAILED,
                exc=exc,
                conversation_id=request.conversation_id,
                client_request_id=request.client_request_id,
            )
            return self._room_state_unavailable_rejection(request)
        if not pending_requests:
            return None
        return ExecutionAck(
            conversation_id=request.conversation_id,
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
            active_runs = await self._require_active_run_reader().get_runs_for_room(
                request.conversation_id
            )
        except Exception as exc:
            logger.emit(
                E.ACTIVE_RUN_LOOKUP_FAILED,
                exc=exc,
                conversation_id=request.conversation_id,
                client_request_id=request.client_request_id,
            )
            return self._room_state_unavailable_rejection(request)
        if not active_runs:
            return None
        return self._room_busy_rejection(request)

    @staticmethod
    def _room_state_unavailable_rejection(request: ExecutionRequest) -> ExecutionAck:
        """Refuse a send whose pending-HITL or active-Run check could not run.

        Admitting it could start a second Run or answer past a pending question.
        """
        return ExecutionAck(
            conversation_id=request.conversation_id,
            success=False,
            error=_ROOM_STATE_UNAVAILABLE_ERROR,
            status_code=503,
            should_start_orchestration=False,
        )

    @staticmethod
    def _room_busy_rejection(request: ExecutionRequest) -> ExecutionAck:
        return ExecutionAck(
            conversation_id=request.conversation_id,
            success=False,
            error=_ROOM_BUSY_ERROR,
            status_code=409,
            should_start_orchestration=False,
        )

    def _claim_room(self, request: ExecutionRequest) -> _RoomClaim | None:
        """Claim the Room for this send, or return None when it is busy.

        Runs without awaiting, so two sends cannot both find the Room free.
        """
        claim = self._room_claims.get(request.conversation_id)
        if claim is not None and self._claim_is_abandoned(claim):
            # The send was admitted but its orchestration was never scheduled.
            logger.emit(
                E.ROOM_CLAIM_DROPPED,
                conversation_id=request.conversation_id,
                user_message_id=claim.message_id,
            )
            claim = None
        if claim is None:
            claim = _RoomClaim(client_request_id=request.client_request_id)
            self._room_claims[request.conversation_id] = claim
            return claim
        if (
            request.client_request_id is not None
            and claim.client_request_id == request.client_request_id
        ):
            claim.holders += 1
            return claim
        return None

    def _claim_is_abandoned(self, claim: _RoomClaim) -> bool:
        """An admitted send whose caller never scheduled its orchestration."""
        return (
            claim.admitted_at is not None
            and claim.message_id not in self._orchestrating
            and asyncio.get_running_loop().time() - claim.admitted_at
            > self.UNSCHEDULED_CLAIM_SECONDS
        )

    def _settle_room_claim(
        self,
        conversation_id: str,
        claim: _RoomClaim,
        ack: ExecutionAck | None,
    ) -> None:
        """Keep the claim for the send that starts orchestration, else drop it."""
        claim.holders -= 1
        if ack is not None and self._starts_orchestration(ack):
            claim.message_id = ack.message_id
            claim.admitted_at = asyncio.get_running_loop().time()
            self._room_claims.setdefault(conversation_id, claim)
            return
        if (
            claim.holders == 0
            and claim.message_id is None
            and self._room_claims.get(conversation_id) is claim
        ):
            del self._room_claims[conversation_id]

    def _release_room_claim(self, conversation_id: str, message_id: str) -> None:
        claim = self._room_claims.get(conversation_id)
        if claim is not None and claim.message_id == message_id:
            del self._room_claims[conversation_id]

    @staticmethod
    def _starts_orchestration(ack: ExecutionAck) -> bool:
        return bool(ack.success and ack.message_id and ack.should_start_orchestration)

    async def _emit_room_preflight_processing_status(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not ack.message_id:
            return
        lifecycle_message_id = ack.dispatch_root_message_id or ack.message_id
        await emit_room_processing_status(
            conversation_id=ack.conversation_id or request.conversation_id,
            status=SSEProcessingStatus.PROCESSING,
            message_id=ack.message_id,
            lifecycle_message_id=lifecycle_message_id,
            event_publisher=self._event_publisher,
            client_request_id_resolver=self._client_request_id_resolver,
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
            conversation_id=ack.conversation_id or request.conversation_id,
            status=status,
            message_id=ack.message_id,
            lifecycle_message_id=lifecycle_message_id,
            event_publisher=self._event_publisher,
            client_request_id_resolver=self._client_request_id_resolver,
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
        except Exception as exc:
            logger.emit(
                E.PREFLIGHT_STATUS_EMISSION_FAILED,
                exc=exc,
                conversation_id=request.conversation_id,
                client_request_id=request.client_request_id,
                message_id=ack.message_id,
                status_stage="processing_and_terminal",
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
            conversation_id=request.conversation_id,
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

        claim = self._claim_room(request)
        if claim is None:
            return await self._replay_or_rejection(
                request=request,
                idempotency=idempotency,
                rejection=self._room_busy_rejection(request),
            )
        ack: ExecutionAck | None = None
        try:
            ack = await self._admit_claimed_send(
                request,
                room_request=room_request,
                idempotency=idempotency,
            )
            return ack
        finally:
            self._settle_room_claim(request.conversation_id, claim, ack)

    async def _admit_claimed_send(
        self,
        request: ExecutionRequest,
        *,
        room_request: RoomCenterUserMessageRequest,
        idempotency: _RequestIdempotency,
    ) -> ExecutionAck:
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
            except Exception as exc:
                logger.emit(
                    E.PREFLIGHT_STATUS_EMISSION_FAILED,
                    exc=exc,
                    conversation_id=request.conversation_id,
                    client_request_id=request.client_request_id,
                    message_id=persisted_ack.message_id,
                    status_stage="processing",
                )
            response = await self._room_center.run_message_preflight_to_room(
                preflight_context
            )
            ack = room_response_to_execution_ack(response)
            try:
                await self._emit_room_preflight_terminal_status(request, ack)
            except Exception as exc:
                logger.emit(
                    E.PREFLIGHT_STATUS_EMISSION_FAILED,
                    exc=exc,
                    conversation_id=request.conversation_id,
                    client_request_id=request.client_request_id,
                    message_id=ack.message_id,
                    status_stage="terminal",
                )
            return ack
        except BaseException:
            try:
                self._room_center.discard_message_preflight(preflight_context)
            except BaseException as exc:
                logger.emit(
                    E.PREFLIGHT_CLEANUP_FAILED,
                    exc=exc,
                    conversation_id=request.conversation_id,
                    client_request_id=request.client_request_id,
                )
            raise

    async def _route_orchestration(
        self,
        request: ExecutionRequest,
        message_id: str,
    ) -> None:
        router = self._orchestrator_router
        if router is None:
            raise OrchestratorRoutingError("orchestrator router is not bound")
        await router.process_room_user_message(
            request,
            message_id=message_id,
            before_run_start=lambda: self._admit_run_start(message_id),
        )

    def _admit_run_start(self, message_id: str) -> bool:
        """Let the router create the Run unless the user already canceled."""
        pending = self._orchestrating.get(message_id)
        if pending is None:
            return True
        if pending.stage == "canceled":
            return False
        pending.stage = "starting"
        return True

    async def _orchestrate(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        message_id = ack.message_id
        assert message_id is not None
        try:
            await self._route_orchestration(request, message_id)
        except Exception:
            # Without a Run nothing else ends this message, so the accepted
            # send would show no response and no error.
            pending = self._orchestrating.get(message_id)
            if pending is not None and await self._ended_before_run(pending):
                # Set before awaiting so a racing cancel reports the failure.
                pending.stage = "failed"
                await self._emit_orchestration_status(
                    pending,
                    SSEProcessingStatus.FAILED,
                    details=_START_FAILED_DETAILS,
                )
            raise

    async def _ended_before_run(self, pending: _PendingOrchestration) -> bool:
        if pending.stage == "canceled":
            return False
        if pending.stage == "pending":
            # Mark it now, before any await, so no cancel can claim it.
            pending.stage = "failed"
            return True
        try:
            return not await self._orchestrator_router.has_run_for_user_message(
                pending.ack.message_id
            )
        except Exception as exc:
            logger.emit(
                E.ORCHESTRATION_FAILURE_RUN_LOOKUP_FAILED,
                exc=exc,
                message_id=pending.ack.message_id,
            )
            return True

    async def _emit_orchestration_status(
        self,
        pending: _PendingOrchestration,
        status: SSEProcessingStatus,
        *,
        details: str | None = None,
    ) -> None:
        request, ack = pending.request, pending.ack
        try:
            await emit_room_processing_status(
                conversation_id=ack.conversation_id or request.conversation_id,
                status=status,
                message_id=ack.message_id,
                lifecycle_message_id=ack.dispatch_root_message_id or ack.message_id,
                event_publisher=self._event_publisher,
                client_request_id_resolver=self._client_request_id_resolver,
                client_request_id=request.client_request_id,
                details=details,
            )
        except Exception as exc:
            logger.emit(
                E.ORCHESTRATION_STATUS_EMISSION_FAILED,
                exc=exc,
                message_id=ack.message_id,
                status=status,
            )

    async def start_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not self._starts_orchestration(ack):
            return
        task = self._spawn_orchestration(request, ack)
        await task

    def schedule_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> None:
        if not self._starts_orchestration(ack):
            return
        self._spawn_orchestration(request, ack)

    def is_orchestration_pending(self, message_id: str) -> bool:
        """Whether this process still runs the message's orchestration task.

        The task creates the run, so a request without run events is not yet
        known to have failed while this is true.
        """
        return message_id in self._orchestrating

    def _spawn_orchestration(
        self,
        request: ExecutionRequest,
        ack: ExecutionAck,
    ) -> asyncio.Task[Any]:
        message_id = ack.message_id
        assert message_id is not None
        with bind_log_context(
            client_request_id=request.client_request_id,
            conversation_id=request.conversation_id,
            user_message_id=message_id,
            message_id=message_id,
        ):
            task = self._task_factory(
                self._orchestrate(request, ack),
                name=f"execution-orchestrate-{message_id}",
            )
        self._inflight.add(task)
        self._orchestrating[message_id] = _PendingOrchestration(request, ack)

        def _on_done(done: asyncio.Task) -> None:
            self._inflight.discard(done)
            self._orchestrating.pop(message_id, None)
            self._release_room_claim(request.conversation_id, message_id)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                logger.emit(E.ORCHESTRATION_TASK_FAILED, exc=exc, message_id=message_id)

        task.add_done_callback(_on_done)
        return task

    async def cancel(
        self,
        conversation_id: str,
        message_id: str,
        *,
        requested_by_user_id: str,
    ) -> bool | CancellationAck:
        router = self._orchestrator_router
        if router is None:
            raise RuntimeError("Execution orchestrator router is not bound")

        async def cancel_run() -> bool | CancellationAck:
            return await router.route_cancellation_by_user_message(
                message_id,
                reason=f"user:{requested_by_user_id}",
                post_claim_cleanup=lambda: (
                    self._hitl_message_cancellation.cancel_requests_for_message(
                        message_id
                    )
                ),
            )

        try:
            return await cancel_run()
        except KeyError as exc:
            if exc.args != (message_id,):
                raise
        return await self._cancel_before_run(message_id, cancel_run)

    async def _cancel_before_run(
        self,
        message_id: str,
        cancel_run: Callable[[], Awaitable[bool | CancellationAck]],
    ) -> bool | CancellationAck:
        """Cancel a message whose orchestration task has not created its Run."""
        while True:
            pending = self._orchestrating.get(message_id)
            if pending is None:
                # The task ended between the Run lookup and now; a Run it
                # created is found by this retry.
                try:
                    return await cancel_run()
                except KeyError as exc:
                    if exc.args != (message_id,):
                        raise
                raise NotFoundError("Message processing", message_id)
            if pending.stage == "failed":
                return CancellationAck(
                    status="failed", cancellation_applied=False, reconciled=True
                )
            if pending.stage == "pending":
                # Set before awaiting so the router's run-start check sees it.
                pending.stage = "canceled"
                await self._emit_orchestration_status(
                    pending, SSEProcessingStatus.CANCELED
                )
            if pending.stage == "canceled":
                return CancellationAck(
                    status="canceled", cancellation_applied=True, reconciled=True
                )
            # The router is creating the Run now: wait until it exists or the
            # task ends.
            await asyncio.sleep(self.RUN_START_POLL_SECONDS)
            try:
                return await cancel_run()
            except KeyError as exc:
                if exc.args != (message_id,):
                    raise

    def _require_active_run_reader(self) -> Any:
        if self._active_run_reader is None:
            raise RuntimeError("Execution active-run reader is not bound")
        return self._active_run_reader

    async def get_runs_for_room(self, conversation_id: str) -> list[RunInfo]:
        return await self._require_active_run_reader().get_runs_for_room(
            conversation_id
        )

    async def get_latest_runs_for_rooms(
        self, room_ids: list[str]
    ) -> dict[str, RunInfo]:
        return await self._require_active_run_reader().get_latest_runs_for_rooms(
            room_ids
        )

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

    async def resolve_hitl_batch(
        self,
        conversation_id: str,
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
                    conversation_id=conversation_id,
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
            conversation_id=conversation_id,
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

    async def get_pending_hitl(self, conversation_id: str) -> list[HITLRequest]:
        requests = await self._hitl_manager.get_pending_requests(conversation_id)
        public_requests = [
            model_hitl_request_to_common(request) for request in requests
        ]

        if self._orchestrator_router is not None:
            orchestrator_requests = await self._orchestrator_router.get_pending_hitl(
                conversation_id
            )
            # Prefer orchestrator rows when the Execution HITL manager and the
            # orchestrator both surface the same question.
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
        conversation_id: str,
        interaction_id: str,
        expected_version: int,
    ) -> int:
        router = self._orchestrator_router
        if router is not None:
            try:
                return await router.cancel_hitl_interaction(
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    expected_version=expected_version,
                )
            except KeyError as exc:
                if exc.args != (interaction_id,):
                    raise
        return await self._hitl_manager.cancel_interaction_by_user(
            interaction_id,
            conversation_id,
            expected_version=expected_version,
        )


__all__ = [
    "ExecutionFacade",
]
