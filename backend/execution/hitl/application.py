from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.dto.hitl import HITLApplicationRoute
from common.utils.logger import get_logger
from common.utils.time import ensure_utc, utcnow
from execution.hitl.delivery import (
    HITLDeliveryDisposition,
    HITLDeliveryError,
    HITLDeliveryPhase,
)
from execution.hitl.exceptions import (
    HITLConflictError,
    HITLDeliveryUncertainError,
    HITLError,
    HITLExpiredError,
    HITLNotFoundError,
    HITLRoomMismatchError,
    HITLRoutingFailedError,
)
from execution.hitl.validation import (
    HITLAggregateCorruptionError,
    validate_command_route_consistency,
    validate_exact_member_inventory,
    validate_route_classifications,
)
from models.hitl import (
    HITLEventType,
    HITLInteractionStatus,
    HITLRequest,
    HITLResumeCommand,
    HITLResumeCommandStatus,
    HITLStatus,
    HITLSupervisorEffectCommand,
)

logger = get_logger(__name__)


def _as_utc_datetime(value: Any) -> datetime:
    """Require BSON-native datetimes in the R1 persisted schema."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    raise TypeError(f"unsupported HITL datetime value: {type(value).__name__}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _command_identity(interaction_id: str, revision: int) -> tuple[str, str]:
    seed = f"{interaction_id}:{revision}:a2a_resume"
    token = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"hitl-resume-{token}", f"hitl-message-{token}"


def _supervisor_command_identity(interaction_id: str, revision: int) -> str:
    seed = f"{interaction_id}:{revision}:supervisor_resume"
    return f"hitl-supervisor-{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"


class HITLApplicationCoordinator:
    """Recoverable answer application across request, aggregate, and A2A journal."""

    LEASE_SECONDS = 300
    HEARTBEAT_SECONDS = 15

    def __init__(self, *, lifecycle) -> None:
        self._lifecycle = lifecycle
        self._run_answer_projector = None

    def bind_run_answer_projector(self, projector) -> None:
        self._run_answer_projector = projector

    async def _renew_leases_once(
        self,
        *,
        interaction_id: str,
        application_claim_id: str,
        command_id: str | None = None,
        command_claim_id: str | None = None,
    ) -> None:
        renewed = await self._lifecycle.renew_interaction_application(
            interaction_id,
            claim_id=application_claim_id,
            lease_seconds=self.LEASE_SECONDS,
        )
        if not renewed:
            raise RuntimeError("lost HITL application lease")
        if command_id and command_claim_id:
            command_renewed = await self._lifecycle.renew_resume_command(
                command_id,
                claim_id=command_claim_id,
                lease_seconds=self.LEASE_SECONDS,
            )
            if not command_renewed:
                raise RuntimeError("lost HITL resume-command lease")

    async def _heartbeat_leases(
        self,
        *,
        interaction_id: str,
        application_claim_id: str,
        command_id: str | None = None,
        command_claim_id: str | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_SECONDS)
            await self._renew_leases_once(
                interaction_id=interaction_id,
                application_claim_id=application_claim_id,
                command_id=command_id,
                command_claim_id=command_claim_id,
            )

    async def _run_fenced_effect(
        self,
        effect,
        *,
        interaction_id: str,
        application_claim_id: str,
        command_id: str,
        command_claim_id: str,
    ):
        # Prove ownership immediately before any externally visible effect.
        await self._renew_leases_once(
            interaction_id=interaction_id,
            application_claim_id=application_claim_id,
            command_id=command_id,
            command_claim_id=command_claim_id,
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_leases(
                interaction_id=interaction_id,
                application_claim_id=application_claim_id,
                command_id=command_id,
                command_claim_id=command_claim_id,
            )
        )
        effect_task = asyncio.create_task(effect())
        done, _pending = await asyncio.wait(
            {effect_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat in done:
            effect_task.cancel()
            await asyncio.gather(effect_task, return_exceptions=True)
            await heartbeat
        try:
            return await effect_task
        finally:
            await self._stop_heartbeat(heartbeat)

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def expire_interaction(
        self,
        service,
        interaction: dict[str, Any],
        *,
        reason: str = "Human input deadline expired",
    ) -> None:
        await self._request_rows(service, interaction, require_answers=False)
        interaction_id = interaction["interaction_id"]
        terminal = await self._lifecycle.terminalize_interaction(
            interaction_id,
            expected_statuses=[
                HITLInteractionStatus.OPEN.value,
                HITLInteractionStatus.PARTIALLY_ANSWERED.value,
                HITLInteractionStatus.ANSWERS_RECORDED.value,
            ],
            status=HITLInteractionStatus.EXPIRED.value,
            reason=reason,
            member_status=HITLStatus.EXPIRED.value,
            owning_run_terminal_status="failed",
        )
        terminal = terminal or await self._lifecycle.get_interaction_strict(
            interaction_id
        )
        if terminal is not None:
            await service.reconcile_terminal_interaction(terminal)

    async def fail_interaction(
        self,
        service,
        interaction: dict[str, Any],
        *,
        reason: str,
        application_claim_id: str | None = None,
    ) -> None:
        await self._request_rows(service, interaction, require_answers=False)
        if application_claim_id:
            terminal = await self._lifecycle.mark_interaction_application_state(
                interaction["interaction_id"],
                claim_id=application_claim_id,
                status=HITLInteractionStatus.FAILED.value,
                error=reason,
            )
        else:
            terminal = await self._lifecycle.terminalize_interaction(
                interaction["interaction_id"],
                expected_statuses=[
                    HITLInteractionStatus.ANSWERS_RECORDED.value,
                    HITLInteractionStatus.APPLYING.value,
                    HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
                ],
                status=HITLInteractionStatus.FAILED.value,
                reason=reason,
                member_status=HITLStatus.CANCELED.value,
                owning_run_terminal_status="failed",
            )
        if terminal is None:
            raise HITLRoutingFailedError(
                "Lost application fence while failing HITL interaction"
            )
        # mark_interaction_application_state predates aggregate terminal outcome
        # metadata, so persist it with a fenced terminal CAS when no application
        # claim owns the failure. Claimed failures still converge from this
        # in-memory proven outcome and retries/reconciliation reread members.
        terminal = {
            **terminal,
            "member_terminal_status": HITLStatus.CANCELED.value,
            "owning_run_terminal_status": "failed",
            "terminal_reason": reason,
        }
        await service.reconcile_terminal_interaction(terminal)

    async def _project_run_answers(
        self,
        interaction: dict[str, Any],
        rows: list[dict[str, Any]],
        combined_input: str,
        application_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._run_answer_projector is None:
            return interaction
        if interaction.get("run_projection_status") == "applied":
            return interaction
        claim_id = uuid4().hex
        claimed = await self._lifecycle.claim_run_answer_projection(
            interaction["interaction_id"],
            application_revision=int(interaction["application_revision"]),
            claim_id=claim_id,
            lease_seconds=self.LEASE_SECONDS,
        )
        if claimed is None:
            latest = await self._lifecycle.get_interaction_strict(
                interaction["interaction_id"]
            )
            if latest and latest.get("run_projection_status") == "applied":
                return latest
            raise HITLRoutingFailedError("Run answer projection is already claimed")
        projection_result = self._result(rows[0], status="applied", interaction=claimed)
        snapshot = validate_route_classifications(claimed)
        # Projection authority comes from the aggregate and immutable route
        # snapshot, never from a mutable member row.
        projection_result.update(
            {
                "room_id": claimed.get("room_id"),
                "user_message_id": claimed.get("user_message_id"),
                "orchestration_run_id": claimed.get("orchestration_run_id"),
                "interaction_id": claimed.get("interaction_id"),
                "request_ids": list(claimed.get("request_ids") or []),
                "continuation_message_id": snapshot.continuation_message_id,
                "a2a_task_id": snapshot.task_id,
                "a2a_context_id": snapshot.context_id,
                "agent_id": snapshot.agent_id,
            }
        )
        if application_result:
            projection_result.update(application_result)
        projection_result["answer_records"] = [
            {"request_id": row["request_id"], "response": row["user_input"]}
            for row in rows
        ]
        try:
            await self._run_answer_projector(
                hitl_result=projection_result,
                response=combined_input,
            )
        except Exception as exc:
            await self._lifecycle.mark_run_answer_projection(
                interaction["interaction_id"],
                claim_id=claim_id,
                status="failed",
                error=str(exc),
            )
            raise HITLRoutingFailedError(
                f"Run answer projection failed: {exc}"
            ) from exc
        projected = await self._lifecycle.mark_run_answer_projection(
            interaction["interaction_id"],
            claim_id=claim_id,
            status="applied",
        )
        if projected is None:
            raise HITLRoutingFailedError("Run answer projection finalization failed")
        return projected

    async def _interaction_for_request(
        self, service, request_doc: dict[str, Any]
    ) -> dict[str, Any]:
        interaction_id = request_doc.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id:
            raise HITLRoutingFailedError("HITL request has no persisted interaction")
        interaction = await self._lifecycle.get_interaction_strict(interaction_id)
        if interaction is None:
            raise HITLRoutingFailedError("Persisted HITL interaction is missing")
        rows = await self._request_rows(service, interaction, require_answers=False)
        if request_doc.get("request_id") not in {row.get("request_id") for row in rows}:
            raise HITLRoutingFailedError("HITL request is not an aggregate member")
        return interaction

    async def handle_response(  # noqa: C901
        self,
        service,
        *,
        room_id: str,
        request_id: str,
        user_input: str,
        user_id: str,
    ) -> dict[str, Any]:
        doc = await service.persistence.get_hitl_request(request_id)
        if not doc:
            raise HITLNotFoundError("HITL request not found")
        if doc.get("room_id") != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        interaction = await self._interaction_for_request(service, doc)
        expires_at = doc.get("expires_at")
        if expires_at is not None and _as_utc_datetime(expires_at) <= utcnow():
            await self.expire_interaction(service, interaction)
            raise HITLExpiredError("HITL request has expired")

        current_status = doc.get("status")
        if current_status in {
            HITLStatus.ANSWER_RECORDED.value,
            HITLStatus.RESPONDED.value,
        }:
            if doc.get("user_input") != user_input:
                raise HITLConflictError(f"Request already {current_status}")
            aggregate_expiry = interaction.get("expires_at")
            if (
                current_status != HITLStatus.RESPONDED.value
                and aggregate_expiry is not None
                and _as_utc_datetime(aggregate_expiry) <= utcnow()
            ):
                await self.expire_interaction(service, interaction)
                raise HITLExpiredError("HITL interaction has expired")
            if current_status == HITLStatus.RESPONDED.value:
                return self._result(doc, status="applied")
            return await self.apply_interaction(service, interaction)
        if current_status != HITLStatus.PENDING.value:
            raise HITLConflictError(f"Request already {current_status or 'unknown'}")

        answer_digest = _digest(user_input)
        claimed = await service.persistence.claim_hitl_request(
            request_id,
            status=HITLStatus.ANSWER_RECORDED.value,
            claim_id=None,
            user_input=user_input,
            answer_digest=answer_digest,
            answered_at=utcnow(),
            responded_at=None,
            responded_by_user_id=user_id,
        )
        if not claimed:
            latest = await service.persistence.get_hitl_request(request_id)
            if latest and latest.get("expires_at") is not None:
                if _as_utc_datetime(latest["expires_at"]) <= utcnow():
                    await self.expire_interaction(service, interaction)
                    raise HITLExpiredError("HITL request has expired")
            if (
                latest
                and latest.get("user_input") == user_input
                and latest.get("status")
                in {
                    HITLStatus.ANSWER_RECORDED.value,
                    HITLStatus.RESPONDED.value,
                }
            ):
                return await self.apply_interaction(service, interaction)
            raise HITLConflictError(
                f"Request already {(latest or {}).get('status', 'unknown')}"
            )

        # claim_hitl_request returns the pre-update document with Motor's default.
        doc.update(
            {
                "status": HITLStatus.ANSWER_RECORDED.value,
                "user_input": user_input,
                "answer_digest": answer_digest,
                "answered_at": utcnow(),
                "responded_by_user_id": user_id,
            }
        )
        recorded = await self._lifecycle.record_interaction_answer(
            interaction["interaction_id"],
            request_id=request_id,
            answer_digest=answer_digest,
        )
        if recorded is None:
            latest_interaction = await self._lifecycle.get_interaction_strict(
                interaction["interaction_id"]
            )
            aggregate_expiry = (latest_interaction or interaction).get("expires_at")
            if (
                aggregate_expiry is not None
                and _as_utc_datetime(aggregate_expiry) <= utcnow()
            ):
                await self.expire_interaction(
                    service, latest_interaction or interaction
                )
                raise HITLExpiredError("HITL interaction has expired")
            # The request answer is durable, but an aggregate CAS failure must be
            # recovered before any application is attempted.
            raise HITLRoutingFailedError("Failed to record interaction answer")
        if recorded.get("status") != HITLInteractionStatus.ANSWERS_RECORDED.value:
            return self._result(doc, status="accepted", interaction=recorded)
        return await self.apply_interaction(service, recorded)

    async def handle_batch_response(  # noqa: C901
        self,
        service,
        *,
        room_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        user_id: str,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably record a complete questionnaire, then apply it exactly once.

        Request rows may be written over multiple CAS operations, but execution is
        never resumed until the aggregate proves that every required answer is
        present. A retry repairs any partially-recorded batch idempotently.
        """
        interaction = await self._lifecycle.get_interaction_strict(interaction_id)
        if interaction is None:
            raise HITLNotFoundError("HITL interaction not found")
        if interaction.get("room_id") != room_id:
            raise HITLRoomMismatchError("Room mismatch")

        expected_ids = list(interaction.get("request_ids") or [])
        required_ids = list(interaction.get("required_request_ids") or [])
        submitted = {answer["request_id"]: answer["user_input"] for answer in answers}
        if (
            not expected_ids
            or len(submitted) != len(answers)
            or set(required_ids) != set(expected_ids)
            or set(submitted) != set(required_ids)
        ):
            raise HITLConflictError(
                "Batch answers must exactly match the interaction's required questions"
            )

        preflight_rows: dict[str, dict[str, Any]] = {}
        authoritative_client_ids: set[str] = set()
        for request_id in expected_ids:
            row = await service.persistence.get_hitl_request(request_id)
            if row is None:
                raise HITLRoutingFailedError("Interaction request is missing")
            if row.get("room_id") != room_id:
                raise HITLRoomMismatchError("Room mismatch")
            if row.get("interaction_id") != interaction_id:
                raise HITLRoutingFailedError("Interaction request identity mismatch")
            row_client_id = row.get("client_request_id")
            if isinstance(row_client_id, str) and row_client_id.strip():
                authoritative_client_ids.add(row_client_id.strip())
            preflight_rows[request_id] = row
        try:
            validate_exact_member_inventory(
                interaction, [preflight_rows[request_id] for request_id in expected_ids]
            )
        except HITLAggregateCorruptionError as exc:
            raise HITLRoutingFailedError(str(exc)) from exc
        if len(authoritative_client_ids) > 1:
            raise HITLRoutingFailedError(
                "Interaction contains conflicting client_request_id values"
            )
        authoritative_client_id = next(iter(authoritative_client_ids), None)
        if (
            client_request_id
            and authoritative_client_id
            and client_request_id != authoritative_client_id
        ):
            raise HITLConflictError("client_request_id does not match interaction")

        expires_at = interaction.get("expires_at")
        if expires_at is not None and _as_utc_datetime(expires_at) <= utcnow():
            await self.expire_interaction(service, interaction)
            raise HITLExpiredError("HITL interaction has expired")
        if interaction.get("status") in {
            HITLInteractionStatus.CANCELED.value,
            HITLInteractionStatus.EXPIRED.value,
            HITLInteractionStatus.FAILED.value,
        }:
            raise HITLConflictError(
                f"Interaction already {interaction.get('status', 'unavailable')}"
            )

        current = interaction
        for request_id in expected_ids:
            user_input = submitted[request_id].strip()
            if not user_input:
                raise HITLConflictError("Every required answer must be non-empty")
            row = preflight_rows[request_id]
            answer_digest = _digest(user_input)
            status = row.get("status")
            if status == HITLStatus.PENDING.value:
                claimed = await service.persistence.claim_hitl_request(
                    request_id,
                    status=HITLStatus.ANSWER_RECORDED.value,
                    claim_id=None,
                    user_input=user_input,
                    answer_digest=answer_digest,
                    answered_at=utcnow(),
                    responded_at=None,
                    responded_by_user_id=user_id,
                )
                if not claimed:
                    row = await service.persistence.get_hitl_request(request_id)
                    status = (row or {}).get("status")
                else:
                    row.update(
                        {
                            "status": HITLStatus.ANSWER_RECORDED.value,
                            "user_input": user_input,
                            "answer_digest": answer_digest,
                            "responded_by_user_id": user_id,
                        }
                    )
                    status = HITLStatus.ANSWER_RECORDED.value

            if status in {
                HITLStatus.ANSWER_RECORDED.value,
                HITLStatus.RESPONDED.value,
            }:
                if (
                    row.get("user_input") != user_input
                    or row.get("answer_digest") != answer_digest
                ):
                    raise HITLConflictError(
                        f"Request {request_id} already has a different answer"
                    )
            else:
                raise HITLConflictError(
                    f"Request {request_id} already {status or 'unavailable'}"
                )

            if request_id not in (current.get("answer_request_ids") or []):
                recorded = await self._lifecycle.record_interaction_answer(
                    interaction_id,
                    request_id=request_id,
                    answer_digest=answer_digest,
                )
                if recorded is None:
                    current = await self._lifecycle.get_interaction_strict(
                        interaction_id
                    )
                    if current and request_id in (
                        current.get("answer_request_ids") or []
                    ):
                        continue
                    aggregate_expiry = (current or interaction).get("expires_at")
                    if (
                        aggregate_expiry is not None
                        and _as_utc_datetime(aggregate_expiry) <= utcnow()
                    ):
                        await self.expire_interaction(service, current or interaction)
                        raise HITLExpiredError("HITL interaction has expired")
                    raise HITLRoutingFailedError(
                        "Failed to record questionnaire answer"
                    )
                current = recorded

        current = await self._lifecycle.get_interaction_strict(interaction_id)
        if current is None:
            raise HITLRoutingFailedError("HITL interaction disappeared")
        if current.get("status") == HITLInteractionStatus.APPLIED.value:
            result = await self.apply_interaction(service, current)
            result["client_request_id"] = authoritative_client_id
            return result
        if current.get("status") not in {
            HITLInteractionStatus.ANSWERS_RECORDED.value,
            HITLInteractionStatus.APPLYING.value,
        }:
            raise HITLRoutingFailedError(
                "Questionnaire answers are durable but application is not ready"
            )
        result = await self.apply_interaction(service, current)
        result["client_request_id"] = authoritative_client_id
        return result

    async def repair_persisted_answer_refs(
        self, service, interaction: dict[str, Any]
    ) -> dict[str, Any]:
        """Repair aggregate answer references from authoritative member rows."""

        rows = await self._request_rows(service, interaction, require_answers=False)
        current = interaction
        for row in rows:
            if row.get("status") not in {
                HITLStatus.ANSWER_RECORDED.value,
                HITLStatus.RESPONDED.value,
            }:
                continue
            user_input = row.get("user_input")
            answer_digest = row.get("answer_digest")
            if (
                not isinstance(user_input, str)
                or not isinstance(answer_digest, str)
                or answer_digest != _digest(user_input)
            ):
                raise HITLRoutingFailedError("Interaction answer digest mismatch")
            recorded = await self._lifecycle.record_interaction_answer(
                interaction["interaction_id"],
                request_id=row["request_id"],
                answer_digest=answer_digest,
            )
            if recorded is None:
                latest = await self._lifecycle.get_interaction_strict(
                    interaction["interaction_id"]
                )
                refs = {
                    ref.get("request_id"): ref.get("digest")
                    for ref in (latest or {}).get("answer_refs") or []
                    if isinstance(ref, dict)
                }
                if refs.get(row["request_id"]) != answer_digest:
                    raise HITLRoutingFailedError(
                        "Failed to repair interaction answer reference"
                    )
                current = latest
            else:
                current = recorded
        return current

    async def apply_interaction(  # noqa: C901
        self, service, interaction: dict[str, Any]
    ) -> dict[str, Any]:
        allows_resume = getattr(service, "canonical_run_allows_resume", None)
        if callable(allows_resume) and not await allows_resume(interaction):
            rows = await self._request_rows(service, interaction)
            return self._result(rows[0], status="canceled", interaction=interaction)
        interaction = await self.repair_persisted_answer_refs(service, interaction)
        interaction_id = interaction["interaction_id"]
        status = interaction.get("status")
        if status == HITLInteractionStatus.APPLIED.value:
            rows = await self._request_rows(service, interaction)
            recovery_result = None
            if (
                interaction.get("application_route")
                == HITLApplicationRoute.A2A_RESUME.value
            ):
                command = (
                    await self._lifecycle.get_resume_command_for_interaction_strict(
                        interaction["interaction_id"],
                        int(interaction["application_revision"]),
                    )
                )
                recovery_result = dict((command or {}).get("response_snapshot") or {})
            interaction = await self._project_run_answers(
                interaction,
                rows,
                self._combined_input(rows),
                recovery_result,
            )
            await self._mark_command_aggregate_applied(interaction)
            responses_projected = await self.finalize_applied(service, interaction)
            if not responses_projected:
                raise HITLRoutingFailedError(
                    "HITL response projection remains incomplete"
                )
            # Crash-recovery replays re-publish the resumed control after the
            # durable response events; the deterministic event identity
            # collapses onto the original room event.
            if interaction.get("application_route") == (
                HITLApplicationRoute.SUPERVISOR_RUN.value
            ):
                emit_resumed = getattr(service, "emit_canonical_resumed_control", None)
                if callable(emit_resumed):
                    await emit_resumed(interaction)
            result = self._result(rows[0], status="applied", interaction=interaction)
            result["answer_records"] = [
                {"request_id": row["request_id"], "response": row.get("user_input")}
                for row in rows
            ]
            return result
        if status == HITLInteractionStatus.DELIVERY_UNCERTAIN.value:
            raise HITLDeliveryUncertainError(
                "Answer delivery is uncertain; status reconciliation is required"
            )
        if status not in {
            HITLInteractionStatus.ANSWERS_RECORDED.value,
            HITLInteractionStatus.APPLYING.value,
        }:
            rows = await self._request_rows(service, interaction)
            return self._result(rows[0], status="accepted", interaction=interaction)

        claim_id = uuid4().hex
        claimed = await self._lifecycle.claim_interaction_application(
            interaction_id,
            claim_id=claim_id,
            lease_seconds=self.LEASE_SECONDS,
        )
        if claimed is None:
            latest = await self._lifecycle.get_interaction_strict(interaction_id)
            if latest and latest.get("status") == HITLInteractionStatus.APPLIED.value:
                await self.finalize_applied(service, latest)
                rows = await self._request_rows(service, latest)
                return self._result(rows[0], status="applied", interaction=latest)
            rows = await self._request_rows(service, latest or interaction)
            return self._result(rows[0], status="accepted", interaction=latest)

        rows = await self._request_rows(service, claimed)
        if not rows or any(row.get("user_input") is None for row in rows):
            await self._lifecycle.mark_interaction_application_state(
                interaction_id,
                claim_id=claim_id,
                status=HITLInteractionStatus.ANSWERS_RECORDED.value,
                error="recorded answers are incomplete",
            )
            raise HITLRoutingFailedError("Recorded HITL answers are incomplete")
        request = HITLRequest(**{k: v for k, v in rows[0].items() if k != "_id"})
        combined_input = self._combined_input(rows)
        route_result: dict[str, Any] = {}
        try:
            snapshot = validate_route_classifications(claimed)
            # Public continuation is ordered answer received → run resumed →
            # route effect for both A2A and supervisor-backed canonical Runs.
            # Fail closed if the durable response projection is incomplete.
            responses_projected = await self.finalize_applied(
                service,
                claimed,
                reconcile_terminal=False,
            )
            if not responses_projected:
                raise HITLRoutingFailedError(
                    "HITL response projection remains incomplete"
                )
            emit_resumed = getattr(service, "emit_canonical_resumed_control", None)
            if callable(emit_resumed):
                await emit_resumed(claimed)
            if snapshot.route == HITLApplicationRoute.A2A_RESUME:
                route_result = await self._apply_agent(
                    service,
                    request=request,
                    interaction=claimed,
                    claim_id=claim_id,
                    user_input=combined_input,
                )
            elif snapshot.route == HITLApplicationRoute.SUPERVISOR_RUN:
                route_result = await self._apply_supervisor(
                    service,
                    request=request,
                    interaction=claimed,
                    claim_id=claim_id,
                    user_input=combined_input,
                )
            claimed = await self._project_run_answers(
                claimed, rows, combined_input, route_result
            )
        except HITLDeliveryError as exc:
            target_status = {
                HITLDeliveryDisposition.RETRYABLE: HITLInteractionStatus.APPLYING.value,
                HITLDeliveryDisposition.DELIVERY_UNCERTAIN: (
                    HITLInteractionStatus.DELIVERY_UNCERTAIN.value
                ),
                HITLDeliveryDisposition.PERMANENT: HITLInteractionStatus.FAILED.value,
            }[exc.disposition]
            if exc.disposition == HITLDeliveryDisposition.PERMANENT:
                await self.fail_interaction(
                    service,
                    claimed,
                    reason="Remote continuation permanently failed",
                    application_claim_id=claim_id,
                )
            else:
                await self._lifecycle.mark_interaction_application_state(
                    interaction_id,
                    claim_id=claim_id,
                    status=target_status,
                    error="remote_continuation_delivery_error",
                )
            if exc.disposition == HITLDeliveryDisposition.DELIVERY_UNCERTAIN:
                raise HITLDeliveryUncertainError(str(exc)) from exc
            raise HITLRoutingFailedError(str(exc)) from exc
        except TimeoutError as exc:
            await self._lifecycle.mark_interaction_application_state(
                interaction_id,
                claim_id=claim_id,
                status=HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
                error="answer_delivery_timeout",
            )
            raise HITLDeliveryUncertainError(
                "Answer may have been delivered; automatic resend is disabled"
            ) from exc
        except (ConnectionRefusedError, ConnectionError) as exc:
            await self._lifecycle.mark_interaction_application_state(
                interaction_id,
                claim_id=claim_id,
                status=HITLInteractionStatus.APPLYING.value,
                error="agent_unreachable",
            )
            raise HITLRoutingFailedError("Remote agent was not reached") from exc
        except Exception as exc:
            await self._lifecycle.mark_interaction_application_state(
                interaction_id,
                claim_id=claim_id,
                status=HITLInteractionStatus.APPLYING.value,
                error="hitl_apply_failed",
            )
            if isinstance(exc, HITLError):
                raise
            logger.exception(
                "Failed to apply HITL answers",
                extra={
                    "interaction_id": interaction_id,
                    "claim_id": claim_id,
                },
            )
            raise HITLRoutingFailedError("Failed to apply HITL answers") from exc

        applied = await self._lifecycle.mark_interaction_application_state(
            interaction_id,
            claim_id=claim_id,
            status=HITLInteractionStatus.APPLIED.value,
        )
        if applied is None:
            raise HITLRoutingFailedError(
                "HITL application completed but durable finalization is pending"
            )
        await self._mark_command_aggregate_applied(applied)
        await self.finalize_applied(service, applied)
        result = self._result(rows[0], status="applied", interaction=applied)
        result["answer_records"] = [
            {"request_id": row["request_id"], "response": row.get("user_input")}
            for row in rows
        ]
        result.update(route_result)
        return result

    async def _apply_supervisor(  # noqa: C901
        self,
        service,
        *,
        request: HITLRequest,
        interaction: dict[str, Any],
        claim_id: str,
        user_input: str,
    ) -> dict[str, Any]:
        revision = int(interaction["application_revision"])
        command_id = _supervisor_command_identity(
            interaction["interaction_id"], revision
        )
        rows = await self._request_rows(service, interaction)
        answer_digest = _digest(
            "\n".join(f"{row['request_id']}:{row['answer_digest']}" for row in rows)
        )
        snapshot = validate_route_classifications(interaction)
        command = HITLSupervisorEffectCommand(
            schema_version=3,
            command_id=command_id,
            interaction_id=interaction["interaction_id"],
            application_revision=revision,
            orchestration_run_id=snapshot.orchestration_run_id or "",
            answer_request_ids=[row["request_id"] for row in rows],
            answer_digest=answer_digest,
        ).model_dump(mode="python")
        validate_command_route_consistency(interaction, command)
        durable_command = await self._lifecycle.create_resume_command(command)
        validate_command_route_consistency(interaction, durable_command)
        status = durable_command.get("status")
        if status in {
            HITLResumeCommandStatus.ACKNOWLEDGED.value,
            HITLResumeCommandStatus.PROJECTED.value,
        }:
            if status == HITLResumeCommandStatus.ACKNOWLEDGED.value:
                projected = await self._lifecycle.mark_resume_command_state(
                    command_id,
                    claim_id=None,
                    expected_statuses=[status],
                    status=HITLResumeCommandStatus.PROJECTED.value,
                    response_snapshot=durable_command.get("response_snapshot"),
                )
                if projected is None:
                    raise HITLRoutingFailedError(
                        "Supervisor effect projection remains pending"
                    )
                durable_command = projected
            return dict(durable_command.get("response_snapshot") or {})
        if status == HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value:
            retryable = await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=None,
                expected_statuses=[status],
                status=HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                error_code="supervisor_effect_replay",
                error_message="Replaying idempotent supervisor effect",
                retry_after_seconds=0,
            )
            if retryable is None:
                raise HITLRoutingFailedError("Supervisor effect recovery is claimed")

        command_claim_id = uuid4().hex
        claimed_command = await self._lifecycle.claim_resume_command(
            command_id,
            claim_id=command_claim_id,
            lease_seconds=self.LEASE_SECONDS,
        )
        if claimed_command is None:
            raise HITLRoutingFailedError("Supervisor effect is already being applied")
        try:
            request.orchestration_run_id = snapshot.orchestration_run_id
            await self._run_fenced_effect(
                lambda: service._handle_supervisor_response(
                    request,
                    user_input,
                    effect_id=command_id,
                ),
                interaction_id=interaction["interaction_id"],
                application_claim_id=claim_id,
                command_id=command_id,
                command_claim_id=command_claim_id,
            )
        except Exception as exc:
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                error_code="supervisor_effect_failed",
                error_message=str(exc),
                retry_after_seconds=0,
            )
            raise
        snapshot = {
            "supervisor_effect_id": command_id,
            "supervisor_effect_applied": True,
        }
        acknowledged = await self._lifecycle.mark_resume_command_state(
            command_id,
            claim_id=command_claim_id,
            expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
            status=HITLResumeCommandStatus.ACKNOWLEDGED.value,
            response_snapshot=snapshot,
        )
        if acknowledged is None:
            raise HITLRoutingFailedError(
                "Supervisor effect completed but acknowledgement is pending"
            )
        projected = await self._lifecycle.mark_resume_command_state(
            command_id,
            claim_id=None,
            expected_statuses=[HITLResumeCommandStatus.ACKNOWLEDGED.value],
            status=HITLResumeCommandStatus.PROJECTED.value,
            response_snapshot=snapshot,
        )
        if projected is None:
            raise HITLRoutingFailedError("Supervisor effect projection remains pending")
        return snapshot

    async def _apply_agent(  # noqa: C901
        self,
        service,
        *,
        request: HITLRequest,
        interaction: dict[str, Any],
        claim_id: str,
        user_input: str,
    ) -> dict[str, Any]:
        revision = int(interaction["application_revision"])
        command_id, outbound_message_id = _command_identity(
            interaction["interaction_id"], revision
        )
        rows = await self._request_rows(service, interaction)
        answer_digest = _digest(
            "\n".join(f"{row['request_id']}:{row['answer_digest']}" for row in rows)
        )
        snapshot = validate_route_classifications(interaction)
        command = HITLResumeCommand(
            schema_version=3,
            command_id=command_id,
            interaction_id=interaction["interaction_id"],
            application_revision=revision,
            task_id=snapshot.task_id or "",
            context_id=snapshot.context_id or "",
            continuation_message_id=snapshot.continuation_message_id or "",
            agent_id=snapshot.agent_id or "",
            display_message_id=request.display_message_id,
            outbound_message_id=outbound_message_id,
            answer_request_ids=[row["request_id"] for row in rows],
            answer_digest=answer_digest,
        ).model_dump(mode="python")
        validate_command_route_consistency(interaction, command)
        durable_command = await self._lifecycle.create_resume_command(command)
        validate_command_route_consistency(interaction, durable_command)
        command_status = durable_command.get("status")
        if command_status in {
            HITLResumeCommandStatus.ACKNOWLEDGED.value,
            HITLResumeCommandStatus.PROJECTED.value,
        }:
            if command_status == HITLResumeCommandStatus.ACKNOWLEDGED.value:
                projected = await self._lifecycle.mark_resume_command_state(
                    command_id,
                    claim_id=None,
                    expected_statuses=[HITLResumeCommandStatus.ACKNOWLEDGED.value],
                    status=HITLResumeCommandStatus.PROJECTED.value,
                    response_snapshot=durable_command.get("response_snapshot"),
                )
                if projected is None:
                    raise HITLRoutingFailedError(
                        "Remote result projection remains pending"
                    )
                durable_command = projected
            response_snapshot = dict(durable_command.get("response_snapshot") or {})
            if response_snapshot.get("reconciled_from_get_task") is True:
                return await service._project_reconciled_agent_response(
                    request,
                    response_snapshot,
                )
            return response_snapshot
        if command_status == HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value:
            raise TimeoutError("previous delivery remains uncertain")
        if command_status == HITLResumeCommandStatus.PERMANENT_FAILURE.value:
            raise HITLDeliveryError(
                durable_command.get("error_message")
                or "Remote continuation permanently failed",
                disposition=HITLDeliveryDisposition.PERMANENT,
                phase=HITLDeliveryPhase.REMOTE_RESPONSE,
                error_code=durable_command.get("error_code")
                or "remote_permanent_failure",
            )
        command_claim_id = uuid4().hex
        claimed_command = await self._lifecycle.claim_resume_command(
            command_id,
            claim_id=command_claim_id,
            lease_seconds=self.LEASE_SECONDS,
        )
        if claimed_command is None:
            raise HITLRoutingFailedError(
                "Remote continuation is already being delivered"
            )
        try:
            request.a2a_task_id = snapshot.task_id
            request.a2a_context_id = snapshot.context_id
            request.continuation_message_id = snapshot.continuation_message_id
            request.agent_id = snapshot.agent_id
            route_result = await self._run_fenced_effect(
                lambda: service._handle_agent_response(
                    request,
                    user_input,
                    outbound_message_id=outbound_message_id,
                ),
                interaction_id=interaction["interaction_id"],
                application_claim_id=claim_id,
                command_id=command_id,
                command_claim_id=command_claim_id,
            )
        except HITLDeliveryError as exc:
            command_status = {
                HITLDeliveryDisposition.RETRYABLE: (
                    HITLResumeCommandStatus.RETRYABLE_ERROR.value
                ),
                HITLDeliveryDisposition.DELIVERY_UNCERTAIN: (
                    HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value
                ),
                HITLDeliveryDisposition.PERMANENT: (
                    HITLResumeCommandStatus.PERMANENT_FAILURE.value
                ),
            }[exc.disposition]
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=command_status,
                error_code=exc.error_code,
                error_message=str(exc),
                retry_after_seconds=(
                    5 if exc.disposition == HITLDeliveryDisposition.RETRYABLE else None
                ),
            )
            raise
        except TimeoutError as exc:
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value,
                error_code="delivery_uncertain",
                error_message=str(exc),
            )
            raise
        except (ConnectionRefusedError, ConnectionError) as exc:
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=HITLResumeCommandStatus.RETRYABLE_ERROR.value,
                error_code="connection_failed",
                error_message=str(exc),
                retry_after_seconds=5,
            )
            raise
        except Exception as exc:
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value,
                error_code="post_send_local_failure",
                error_message=str(exc),
            )
            raise HITLDeliveryError(
                str(exc),
                disposition=HITLDeliveryDisposition.DELIVERY_UNCERTAIN,
                phase=HITLDeliveryPhase.POST_SEND_PERSISTENCE,
                error_code="post_send_local_failure",
            ) from exc
        acknowledged = await self._lifecycle.mark_resume_command_state(
            command_id,
            claim_id=command_claim_id,
            expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
            status=HITLResumeCommandStatus.ACKNOWLEDGED.value,
            response_snapshot=route_result,
        )
        if acknowledged is None:
            await self._lifecycle.mark_resume_command_state(
                command_id,
                claim_id=command_claim_id,
                expected_statuses=[HITLResumeCommandStatus.DELIVERING.value],
                status=HITLResumeCommandStatus.DELIVERY_UNCERTAIN.value,
                error_code="ack_persistence_uncertain",
                error_message="Remote acknowledgement persistence failed",
            )
            raise HITLDeliveryError(
                "Remote acknowledgement persistence failed",
                disposition=HITLDeliveryDisposition.DELIVERY_UNCERTAIN,
                phase=HITLDeliveryPhase.POST_SEND_PERSISTENCE,
                error_code="ack_persistence_uncertain",
            )
        projected = await self._lifecycle.mark_resume_command_state(
            command_id,
            claim_id=None,
            expected_statuses=[HITLResumeCommandStatus.ACKNOWLEDGED.value],
            status=HITLResumeCommandStatus.PROJECTED.value,
            response_snapshot=route_result,
        )
        if projected is None:
            raise HITLRoutingFailedError("Remote result projection remains pending")
        return route_result

    async def _mark_command_aggregate_applied(
        self, interaction: dict[str, Any]
    ) -> None:
        command = await self._lifecycle.get_resume_command_for_interaction_strict(
            interaction["interaction_id"],
            int(interaction["application_revision"]),
        )
        if command and command.get("status") == HITLResumeCommandStatus.PROJECTED.value:
            await self._lifecycle.mark_resume_command_aggregate_applied(
                command["command_id"]
            )

    async def finalize_applied(
        self,
        service,
        interaction: dict[str, Any],
        *,
        reconcile_terminal: bool = True,
    ) -> bool:
        rows = await self._request_rows(service, interaction)
        all_projected = True
        for row in rows:
            request_id = row["request_id"]
            if row.get("status") == HITLStatus.ANSWER_RECORDED.value:
                finalized = await service.persistence.cas_update_hitl_request_strict(
                    request_id,
                    expected_status=HITLStatus.ANSWER_RECORDED.value,
                    status=HITLStatus.RESPONDED.value,
                    responded_at=utcnow(),
                    application_projected=False,
                )
                if not finalized:
                    all_projected = False
                    continue
                row["status"] = HITLStatus.RESPONDED.value
                row["application_projected"] = False
            if row.get("status") != HITLStatus.RESPONDED.value:
                all_projected = False
                continue
            if row.get("application_projected") is True:
                continue
            request = HITLRequest(**{k: v for k, v in row.items() if k != "_id"})
            request.interaction_status = HITLInteractionStatus.APPLIED
            request.application_status = HITLInteractionStatus.APPLIED.value
            request.application_error = None
            projection_ok = True
            if request.display_message_id:
                projection_ok = await service._project_completed_hitl_display(
                    display_message_id=request.display_message_id,
                    user_input=request.user_input,
                    request_id=request.request_id,
                    room_id=request.room_id,
                )
            try:
                await service._emit_hitl_event(
                    room_id=request.room_id,
                    event_type=HITLEventType.INPUT_RECEIVED,
                    request=request,
                )
            except Exception:
                projection_ok = False
                logger.warning(
                    "Failed to emit durable HITL resolved event",
                    extra={"hitl_request_id": request.request_id},
                    exc_info=True,
                )
            if projection_ok:
                projection_ok = await service.persistence.update_hitl_request(
                    request.request_id,
                    application_projected=True,
                )
            all_projected = all_projected and bool(projection_ok)
        if (
            reconcile_terminal
            and all_projected
            and not interaction.get("terminal_reconciled")
        ):
            await self._lifecycle.mark_interaction_terminal_reconciled(
                interaction["interaction_id"],
                version=int(interaction["version"]),
            )
        return all_projected

    async def _request_rows(
        self,
        service,
        interaction: dict[str, Any],
        *,
        require_answers: bool = True,
    ) -> list[dict[str, Any]]:
        expected_ids = list(interaction.get("request_ids") or [])
        required_ids = list(interaction.get("required_request_ids") or [])
        if (
            not expected_ids
            or len(set(expected_ids)) != len(expected_ids)
            or set(required_ids) != set(expected_ids)
        ):
            raise HITLRoutingFailedError("Invalid interaction request inventory")
        rows: list[dict[str, Any]] = []
        for request_id in expected_ids:
            row = await service.persistence.get_hitl_request(request_id)
            if row is None:
                raise HITLRoutingFailedError("Interaction request is missing")
            if require_answers and row.get("user_input") is None:
                raise HITLRoutingFailedError("Interaction answer is missing")
            if require_answers:
                digest = row.get("answer_digest")
                if not isinstance(digest, str) or digest != _digest(
                    str(row["user_input"])
                ):
                    raise HITLRoutingFailedError("Interaction answer digest mismatch")
            rows.append(row)
        try:
            validate_exact_member_inventory(interaction, rows)
        except HITLAggregateCorruptionError as exc:
            raise HITLRoutingFailedError(str(exc)) from exc
        if not require_answers:
            return rows
        answer_refs = {
            ref.get("request_id"): ref.get("digest")
            for ref in interaction.get("answer_refs") or []
            if isinstance(ref, dict)
        }
        if set(answer_refs) != set(expected_ids) or any(
            answer_refs[row["request_id"]] != row["answer_digest"] for row in rows
        ):
            raise HITLRoutingFailedError("Interaction answer references are invalid")
        return rows

    @staticmethod
    def _combined_input(rows: list[dict[str, Any]]) -> str:
        if len(rows) == 1:
            return str(rows[0].get("user_input") or "")
        return "\n\n".join(
            f"Q: {row.get('prompt', '')}\nA: {row.get('user_input', '')}"
            for row in rows
        )

    @staticmethod
    def _result(
        request: dict[str, Any],
        *,
        status: str,
        interaction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "request_id": request["request_id"],
            "room_id": request.get("room_id"),
            "user_message_id": request.get("user_message_id"),
            "orchestration_run_id": request.get("orchestration_run_id"),
            "source": request.get("public_source"),
            "response": request.get("user_input"),
            "user_input": request.get("user_input"),
            "responder_id": request.get("responded_by_user_id"),
            "display_message_id": request.get("display_message_id"),
            "continuation_message_id": request.get("continuation_message_id"),
            "a2a_task_id": request.get("a2a_task_id"),
            "a2a_context_id": request.get("a2a_context_id"),
            "agent_id": request.get("agent_id"),
            "agent_name": request.get("agent_name"),
            "client_request_id": request.get("client_request_id"),
            "interaction_id": request.get("interaction_id"),
            "interaction_status": (interaction or {}).get("status"),
            "request_ids": list((interaction or {}).get("request_ids") or []),
            "application_revision": (interaction or {}).get("application_revision"),
            "run_projection_status": (interaction or {}).get("run_projection_status"),
            "resolved_at": utcnow() if status == "applied" else None,
        }
