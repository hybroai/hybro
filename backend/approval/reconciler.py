from __future__ import annotations

from approval.validation import validate_command_route_consistency
from common.observability import events as E
from common.utils.logger import get_logger
from common.utils.time import utcnow
from models.hitl import (
    HITLEventType,
    HITLInteractionStatus,
    HITLRequest,
    HITLResumeCommandStatus,
    HITLStatus,
)

logger = get_logger(__name__)


def _interaction_ids(interaction: dict) -> dict[str, object]:
    return {
        "interaction_id": interaction.get("interaction_id"),
        "conversation_id": interaction.get("room_id"),
        "run_id": interaction.get("orchestration_run_id"),
    }


class HITLLifecycleReconciler:
    """Bounded, independently-fenced repair passes for durable HITL state."""

    def __init__(
        self,
        *,
        lifecycle,
        service,
        application,
        limit: int = 100,
    ) -> None:
        self._lifecycle = lifecycle
        self._service = service
        self._application = application
        self._limit = limit

    async def reconcile_lifecycle(self) -> dict[str, int]:
        counts = {
            "materializing": 0,
            "expired": 0,
            "answers": 0,
            "applications": 0,
            "commands": 0,
            "terminal_interactions": 0,
            "terminal_requests": 0,
            "open_projections": 0,
            "errors": 0,
        }
        passes = (
            ("materializing", self._resume_materializing),
            ("expired", self._expire_due),
            ("answers", self._repair_answers),
            ("applications", self._resume_applications),
            ("commands", self._reconcile_commands),
            ("terminal_interactions", self._reconcile_terminal_interactions),
            ("terminal_requests", self._reconcile_terminal_requests),
            ("open_projections", self._recover_open_projections),
        )
        for name, operation in passes:
            try:
                counts[name] = await operation()
            except Exception as exc:
                counts["errors"] += 1
                logger.emit(
                    E.HITL_RECONCILIATION_PASS_FAILED,
                    exc=exc,
                    reconciliation_pass=name,
                )
        return counts

    async def _resume_materializing(self) -> int:
        repaired = 0
        async for interaction in self._lifecycle.iter_materializing_interactions(
            limit=self._limit
        ):
            try:
                requests = await self._service.resume_materializing_interaction(
                    interaction
                )
                repaired += int(requests is not None)
            except Exception as exc:
                logger.emit(
                    E.HITL_MATERIALIZATION_RESUME_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired

    async def _expire_due(self) -> int:
        repaired = 0
        async for interaction in self._lifecycle.iter_due_interactions(
            utcnow(), limit=self._limit
        ):
            try:
                await self._application.expire_interaction(self._service, interaction)
                repaired += 1
            except Exception as exc:
                logger.emit(
                    E.HITL_INTERACTION_EXPIRY_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired

    async def _repair_answers(self) -> int:
        repaired = 0
        async for interaction in self._lifecycle.iter_active_interactions(
            limit=self._limit
        ):
            try:
                before = list(interaction.get("answer_refs") or [])
                current = await self._application.repair_persisted_answer_refs(
                    self._service, interaction
                )
                if list(current.get("answer_refs") or []) != before:
                    repaired += 1
                if (
                    current.get("status")
                    == HITLInteractionStatus.ANSWERS_RECORDED.value
                ):
                    await self._application.apply_interaction(self._service, current)
            except Exception as exc:
                logger.emit(
                    E.HITL_ANSWER_REFS_REPAIR_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired

    async def _resume_applications(self) -> int:
        repaired = 0
        async for interaction in self._lifecycle.iter_stale_applications(
            utcnow(), limit=self._limit
        ):
            try:
                if interaction.get("status") == (
                    HITLInteractionStatus.DELIVERY_UNCERTAIN.value
                ):
                    command = (
                        await self._lifecycle.get_resume_command_for_interaction_strict(
                            interaction["interaction_id"],
                            int(interaction.get("application_revision") or 0),
                        )
                    )
                    if (
                        command is not None
                        and command.get("kind") != "supervisor_resume"
                    ):
                        continue
                    if command is not None:
                        await self._lifecycle.mark_resume_command_state(
                            command["command_id"],
                            claim_id=None,
                            expected_statuses=[
                                HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value
                            ],
                            status=HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                            error_code="supervisor_effect_replay",
                            error_message="Replaying idempotent supervisor effect",
                            retry_after_seconds=0,
                        )
                    # Without a remote A2A command no remote delivery could have begun;
                    # reclaim the orphaned aggregate without inventing a resend.
                    resumed = await self._lifecycle.resume_uncertain_interaction(
                        interaction["interaction_id"],
                        claim_id=f"orphan-recovery:{interaction['interaction_id']}",
                    )
                    if resumed is None:
                        continue
                    interaction = resumed
                await self._application.apply_interaction(self._service, interaction)
                repaired += 1
            except Exception as exc:
                logger.emit(
                    E.HITL_APPLICATION_RESUME_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired

    async def _finish_confirmed_command(self, command: dict) -> None:
        interaction = await self._lifecycle.get_interaction_strict(
            command["interaction_id"]
        )
        if interaction is None:
            return
        if interaction.get("status") == HITLInteractionStatus.DELIVERY_UNCERTAIN.value:
            resumed = await self._lifecycle.resume_uncertain_interaction(
                interaction["interaction_id"],
                claim_id=f"confirmed:{command['command_id']}",
            )
            if resumed is not None:
                interaction = resumed
        if interaction.get("status") == HITLInteractionStatus.APPLIED.value:
            await self._application.apply_interaction(self._service, interaction)
            return
        if interaction.get("status") == HITLInteractionStatus.APPLYING.value:
            await self._application.apply_interaction(self._service, interaction)

    async def _reconcile_commands(self) -> int:  # noqa: C901
        repaired = 0
        async for command in self._lifecycle.iter_due_resume_commands(
            utcnow(), limit=self._limit
        ):
            try:
                interaction = await self._lifecycle.get_interaction_strict(
                    command["interaction_id"]
                )
                if interaction is None:
                    continue
                await self._application._request_rows(
                    self._service, interaction, require_answers=False
                )
                validate_command_route_consistency(interaction, command)
                status = command.get("status")
                if status == HITLResumeCommandStatus.DELIVERING.value:
                    retryable = await self._lifecycle.reclaim_stale_resume_command(
                        command["command_id"],
                        observed_claim_id=command.get("claim_id"),
                        observed_version=int(command["version"]),
                        observed_lease_expires_at=command["lease_expires_at"],
                        now=utcnow(),
                        status=HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                        error_code="supervisor_worker_lost",
                        error_message="Replaying idempotent supervisor effect",
                        retry_after_seconds=0,
                    )
                    if retryable is not None:
                        repaired += 1
                    continue
                if status in {
                    HITLResumeCommandStatus.ACKNOWLEDGED.value,
                    HITLResumeCommandStatus.PROJECTED.value,
                }:
                    if status == HITLResumeCommandStatus.ACKNOWLEDGED.value:
                        projected = await self._lifecycle.mark_resume_command_state(
                            command["command_id"],
                            claim_id=None,
                            expected_statuses=[status],
                            status=HITLResumeCommandStatus.PROJECTED.value,
                            response_snapshot=command.get("response_snapshot"),
                        )
                        if projected is None:
                            continue
                        command = projected
                    await self._finish_confirmed_command(command)
                    repaired += 1
                    continue
                if status == HITLResumeCommandStatus.PERMANENT_FAILURE.value:
                    await self._application.fail_interaction(
                        self._service,
                        interaction,
                        reason=(
                            command.get("error_message")
                            or "Remote continuation permanently failed"
                        ),
                        application_claim_id=interaction.get("application_claim_id"),
                    )
                    repaired += 1
                    continue
                if status == HITLResumeCommandStatus.RETRYABLE_ERROR.value:
                    await self._application.apply_interaction(
                        self._service, interaction
                    )
                    repaired += 1
                    continue
            except Exception as exc:
                logger.emit(
                    E.HITL_RESUME_COMMAND_RECONCILE_DEFERRED,
                    exc=exc,
                    command_id=command.get("command_id"),
                    interaction_id=command.get("interaction_id"),
                    run_id=command.get("orchestration_run_id"),
                )
        return repaired

    async def _reconcile_terminal_interactions(self) -> int:
        repaired = 0
        async for (
            interaction
        ) in self._lifecycle.iter_unreconciled_terminal_interactions(limit=self._limit):
            try:
                await self._service.reconcile_terminal_interaction(interaction)
                repaired += 1
            except Exception as exc:
                logger.emit(
                    E.HITL_TERMINAL_INTERACTION_RECONCILE_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired

    async def _reconcile_terminal_requests(self) -> int:
        repaired = 0
        async for row in self._lifecycle.iter_unreconciled_terminal_requests(
            limit=self._limit
        ):
            try:
                request = HITLRequest(**{k: v for k, v in row.items() if k != "_id"})
                event_type = (
                    HITLEventType.INPUT_CANCELED
                    if request.status == HITLStatus.CANCELED
                    else HITLEventType.INPUT_EXPIRED
                )
                await self._service._reconcile_terminal_request(
                    request, event_type=event_type
                )
                repaired += 1
            except Exception as exc:
                logger.emit(
                    E.HITL_TERMINAL_REQUEST_RECONCILE_DEFERRED,
                    exc=exc,
                    hitl_request_id=row.get("request_id"),
                    interaction_id=row.get("interaction_id"),
                    conversation_id=row.get("room_id"),
                )
        return repaired

    async def _recover_open_projections(self) -> int:
        repaired = 0
        async for interaction in self._lifecycle.iter_active_interactions(
            limit=self._limit
        ):
            if interaction.get("status") not in {
                HITLInteractionStatus.OPEN.value,
                HITLInteractionStatus.PARTIALLY_ANSWERED.value,
            }:
                continue
            try:
                repaired += await self._service.recover_open_interaction_projection(
                    interaction
                )
            except Exception as exc:
                logger.emit(
                    E.HITL_OPEN_PROJECTION_RECOVERY_DEFERRED,
                    exc=exc,
                    **_interaction_ids(interaction),
                )
        return repaired


__all__ = ["HITLLifecycleReconciler"]
