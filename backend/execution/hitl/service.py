"""Human-in-the-Loop (HITL) service — manages the HITL request/response lifecycle.

Responsibilities:
1. Create HITL requests for supervisor or agent input-required lifecycles
2. Persist requests to MongoDB
3. Emit SSE events to notify the frontend
4. Handle user responses (route to A2A agent or supervisor context)
5. Clean up expired/canceled requests

See docs/HITL_DESIGN.md §6 for full design details.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from common.a2a_constants import FAILURE_STATES
from common.dto import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
    HITLRequestEvent,
    HITLResolvedEvent,
    HITLRouteSnapshot,
)
from common.dto.hitl import A2AInteractionSpec, HITLAnswerKind, HITLInteractionKind
from common.utils.a2a_helpers import (
    is_authoritative_a2a_id as _is_authoritative_a2a_id,
)
from common.utils.logger import get_logger
from common.utils.time import utcnow
from execution.dispatch.agent_ingress_router import (
    UNSUPPORTED_INTERACTION_CODE,
    UNSUPPORTED_INTERACTION_MESSAGE,
)
from execution.hitl.exceptions import (
    ContinuationLostError,
    HITLConflictError,
    HITLNotFoundError,
    HITLRequestProjectionError,
    HITLRoomMismatchError,
    HITLRoutingFailedError,
)
from execution.hitl.public_prompt import (
    GENERIC_AGENT_INPUT_PROMPT,
    concrete_agent_input_prompt,
)
from execution.hitl.validation import (
    HITLAggregateCorruptionError,
    deterministic_interaction_id,
    deterministic_request_id,
    validate_exact_member_inventory,
    validate_route_classifications,
)
from models.hitl import (
    HITLEventType,
    HITLInteraction,
    HITLInteractionStatus,
    HITLPromptType,
    HITLRequest,
    HITLStatus,
)

if TYPE_CHECKING:
    from execution.ports import (
        HITLAgentReplyPort,
        HITLContinuationPort,
        HITLDeliveryPort,
        HITLLifecyclePersistencePort,
        HITLPersistencePort,
        HITLTaskNotificationPort,
        HITLTerminalLifecyclePort,
    )

logger = get_logger(__name__)


def _room_write_fenced(method):
    @wraps(method)
    async def fenced(self, *args, **kwargs):
        room_id = kwargs.get("room_id") or (args[0] if args else None)
        if not isinstance(room_id, str) or not room_id:
            raise TypeError("room_id is required")
        if self._room_files is None:
            return await method(self, *args, **kwargs)
        async with self._room_files.write_lease(room_id, f"hitl:{method.__name__}"):
            return await method(self, *args, **kwargs)

    return fenced


def _short_prompt_hash(prompt: str | None) -> str:
    prompt_hash = _prompt_hash(prompt)
    if prompt_hash is None:
        return "-"
    return prompt_hash[:12]


def _normalized_prompt(prompt: str | None) -> str:
    return " ".join(str(prompt or "").split()).strip().casefold()


def _prompt_type_for_typed_question(question: Any) -> HITLPromptType:
    if question.interaction_kind == HITLInteractionKind.AUTH_CHALLENGE:
        return HITLPromptType.AUTHENTICATION
    if question.interaction_kind == HITLInteractionKind.POLICY_DECISION:
        return HITLPromptType.APPROVAL
    return {
        HITLAnswerKind.SINGLE_CHOICE: HITLPromptType.SINGLE_CHOICE,
        HITLAnswerKind.MULTI_CHOICE: HITLPromptType.MULTI_CHOICE,
        HITLAnswerKind.CONFIRMATION: HITLPromptType.CONFIRMATION,
    }.get(question.answer_kind, HITLPromptType.TEXT)


def _prompt_hash(prompt: str | None) -> str | None:
    normalized = _normalized_prompt(prompt)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


MAX_HITL_ROUNDS = 15
MAX_HITL_GROUP_SIZE = 100
_GENERIC_AGENT_INPUT_PROMPT = GENERIC_AGENT_INPUT_PROMPT


def _is_actionable_agent_hitl_document(document: dict[str, Any]) -> bool:
    return bool(
        document.get("public_source") == HITLPublicSource.AGENT.value
        and concrete_agent_input_prompt(document.get("prompt")) is not None
        and _is_authoritative_a2a_id(document.get("a2a_task_id"))
        and _is_authoritative_a2a_id(document.get("a2a_context_id"))
    )


def _same_agent_hitl_logical_request(
    persisted: dict[str, Any], current: dict[str, Any]
) -> bool:
    if any(
        persisted.get(field) != current.get(field)
        for field in ("room_id", "user_message_id", "public_source")
    ):
        return False
    if (
        persisted.get("agent_id")
        and current.get("agent_id")
        and persisted.get("agent_id") != current.get("agent_id")
    ):
        return False
    return any(
        persisted.get(field) and persisted.get(field) == current.get(field)
        for field in ("display_message_id", "continuation_message_id")
    )


def _public_hitl_request_from_doc(document: dict[str, Any]) -> HITLRequest:
    data = {key: value for key, value in document.items() if key != "_id"}
    if data.get("public_source") == HITLPublicSource.AGENT.value:
        data["prompt"] = concrete_agent_input_prompt(data.get("prompt"))
    return HITLRequest(**data)


class HITLService:
    """Manages the human-in-the-loop interaction lifecycle."""

    def __init__(
        self,
        *,
        continuation=None,
        task_notifications=None,
        terminal_lifecycle=None,
        lifecycle=None,
        application=None,
        room_files=None,
        canonical_control_publisher=None,
        lifecycle_family_reader=None,
        canonical_run_state_reader=None,
        supervisor_resume=None,
        canonical_cancellation_requester=None,
        public_secret_values=(),
    ) -> None:
        self._persistence: HITLPersistencePort | None = None
        self._delivery: HITLDeliveryPort | None = None
        self._agent_reply: HITLAgentReplyPort | None = None
        self._continuation: HITLContinuationPort | None = continuation
        self._task_notifications: HITLTaskNotificationPort | None = task_notifications
        self._terminal_lifecycle: HITLTerminalLifecyclePort | None = terminal_lifecycle
        self._lifecycle: HITLLifecyclePersistencePort | None = lifecycle
        self._application = application
        self._room_files = room_files
        self._canonical_control_publisher = canonical_control_publisher
        self._lifecycle_family_reader = lifecycle_family_reader
        self._canonical_run_state_reader = canonical_run_state_reader
        self._supervisor_resume = supervisor_resume
        self._canonical_cancellation_requester = canonical_cancellation_requester
        self._public_secret_values = tuple(
            value for value in public_secret_values if isinstance(value, str) and value
        )

    @property
    def persistence(self):
        if self._persistence is None:
            raise RuntimeError("HITL persistence port has not been bound")
        return self._persistence

    @property
    def delivery(self):
        if self._delivery is not None:
            return self._delivery
        raise RuntimeError("HITL delivery port has not been bound")

    @property
    def agent_reply(self):
        if self._agent_reply is None:
            raise RuntimeError("HITL agent reply port has not been bound")
        return self._agent_reply

    @property
    def continuation(self):
        if self._continuation is None:
            raise RuntimeError("HITL continuation port has not been bound")
        return self._continuation

    @property
    def task_notifications(self):
        if self._task_notifications is None:
            raise RuntimeError("HITL task notification port has not been bound")
        return self._task_notifications

    # ------------------------------------------------------------------
    # Create HITL request
    # ------------------------------------------------------------------

    @_room_write_fenced
    async def request_interaction(
        self,
        *,
        room_id: str,
        user_message_id: str,
        interaction_id: str,
        application_route: HITLApplicationRoute,
        public_source: HITLPublicSource,
        evidence_origin: HITLEvidenceOrigin,
        route_snapshot: HITLRouteSnapshot,
        questions: list[dict[str, Any]],
        orchestration_run_id: str | None = None,
        expires_in_hours: float = 24.0,
    ) -> list[HITLRequest] | None:
        """Validate and create one complete ordered persisted interaction."""

        application_route = HITLApplicationRoute(application_route)
        public_source = HITLPublicSource(public_source)
        evidence_origin = HITLEvidenceOrigin(evidence_origin)
        route_snapshot = HITLRouteSnapshot.model_validate(route_snapshot)
        if application_route != route_snapshot.route:
            raise ValueError("application_route must match route_snapshot.route")
        if (
            application_route == HITLApplicationRoute.SUPERVISOR_RUN
            and orchestration_run_id != route_snapshot.orchestration_run_id
        ):
            raise ValueError(
                "supervisor orchestration_run_id must match route_snapshot"
            )
        if not interaction_id.strip():
            raise ValueError("interaction_id must not be blank")
        if not questions or len(questions) > MAX_HITL_GROUP_SIZE:
            raise ValueError("questions must contain between 1 and 100 members")
        allowed = {
            "prompt",
            "prompt_type",
            "choices",
            "agent_id",
            "agent_name",
            "source_step_id",
            "continuation_message_id",
            "display_message_id",
            "request_id",
        }
        normalized: list[dict[str, Any]] = []
        for index, raw_question in enumerate(questions):
            unknown = set(raw_question) - allowed
            if unknown:
                raise ValueError(f"unsupported HITL question fields: {sorted(unknown)}")
            prompt = raw_question.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"question {index} requires a non-blank prompt")
            question = dict(raw_question)
            question["prompt"] = prompt.strip()
            question["prompt_type"] = HITLPromptType(
                question.get("prompt_type", HITLPromptType.TEXT)
            )
            question["request_id"] = question.get("request_id") or (
                deterministic_request_id(interaction_id, index)
            )
            if public_source == HITLPublicSource.AGENT:
                concrete_prompt = concrete_agent_input_prompt(question["prompt"])
                if concrete_prompt is None:
                    raise ValueError(f"question {index} requires a concrete prompt")
                question["prompt"] = concrete_prompt
                question["agent_id"] = (
                    question.get("agent_id") or route_snapshot.agent_id
                )
                question["continuation_message_id"] = (
                    question.get("continuation_message_id")
                    or route_snapshot.continuation_message_id
                )
                question["display_message_id"] = (
                    question.get("display_message_id")
                    or question["continuation_message_id"]
                )
                if not question["display_message_id"]:
                    raise ValueError(
                        f"question {index} requires a display message identity"
                    )
            normalized.append(question)
        request_ids = [question["request_id"] for question in normalized]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("question request_ids must be unique")

        if self._lifecycle is None:
            raise HITLRequestProjectionError("HITL lifecycle persistence is required")
        shared_expiry = utcnow() + timedelta(hours=expires_in_hours)
        interaction = HITLInteraction(
            schema_version=3,
            interaction_id=interaction_id,
            room_id=room_id,
            user_message_id=user_message_id,
            orchestration_run_id=orchestration_run_id,
            application_route=application_route,
            public_source=public_source,
            evidence_origin=evidence_origin,
            route_snapshot=route_snapshot,
            route_fingerprint=route_snapshot.fingerprint,
            creation_inventory=normalized,
            expected_request_count=len(normalized),
            expires_at=shared_expiry,
        ).model_dump(mode="python")
        # The complete immutable creation inventory is the first durable write.
        # Retries must use the persisted inventory, never a partially rebuilt
        # caller payload.
        durable = await self._lifecycle.materialize_interaction(interaction)
        return await self.resume_materializing_interaction(durable)

    async def resume_materializing_interaction(
        self, interaction: dict[str, Any]
    ) -> list[HITLRequest] | None:
        """Idempotently create and attach every missing inventory member."""
        if self._lifecycle is None:
            raise HITLRequestProjectionError("HITL lifecycle persistence is required")
        try:
            snapshot = HITLRouteSnapshot.model_validate(interaction["route_snapshot"])
            validate_route_classifications(interaction)
        except (KeyError, TypeError, ValueError) as exc:
            raise HITLRequestProjectionError(str(exc)) from exc
        inventory = list(interaction.get("creation_inventory") or [])
        if len(inventory) != int(interaction.get("expected_request_count") or 0):
            raise HITLRequestProjectionError(
                "materializing interaction has incomplete creation inventory"
            )
        created: list[HITLRequest] = []
        for index, raw_question in enumerate(inventory):
            question = dict(raw_question)
            request = await self._request_interaction_member(
                room_id=interaction["room_id"],
                user_message_id=interaction["user_message_id"],
                application_route=HITLApplicationRoute(
                    interaction["application_route"]
                ),
                public_source=HITLPublicSource(interaction["public_source"]),
                evidence_origin=HITLEvidenceOrigin(interaction["evidence_origin"]),
                route_snapshot=snapshot,
                interaction_id=interaction["interaction_id"],
                question_index=index,
                question_count=len(inventory),
                orchestration_run_id=interaction.get("orchestration_run_id"),
                expires_at=interaction.get("expires_at"),
                a2a_task_id=snapshot.task_id,
                a2a_context_id=snapshot.context_id,
                continuation_message_id=question.pop("continuation_message_id", None),
                agent_id=question.pop("agent_id", None),
                **question,
            )
            if request is None:
                return None
            created.append(request)
        latest = await self._lifecycle.get_interaction_strict(
            interaction["interaction_id"]
        )
        if latest and latest.get("status") in {
            HITLInteractionStatus.OPEN.value,
            HITLInteractionStatus.PARTIALLY_ANSWERED.value,
        }:
            await self.recover_open_interaction_projection(latest)
        return created

    async def _request_interaction_member(
        self,
        room_id: str,
        user_message_id: str,
        application_route: HITLApplicationRoute,
        public_source: HITLPublicSource,
        evidence_origin: HITLEvidenceOrigin,
        route_snapshot: HITLRouteSnapshot,
        interaction_id: str,
        question_index: int,
        question_count: int,
        prompt: str,
        prompt_type: HITLPromptType = HITLPromptType.TEXT,
        choices: list[str] | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        source_step_id: str | None = None,
        a2a_task_id: str | None = None,
        a2a_context_id: str | None = None,
        continuation_message_id: str | None = None,
        display_message_id: str | None = None,
        orchestration_run_id: str | None = None,
        expires_in_hours: float = 24.0,
        expires_at: Any | None = None,
        request_id: str | None = None,
    ) -> HITLRequest | None:
        """Materialize one already-validated member of an interaction."""
        source = public_source.value
        agent_prompt_hash = _prompt_hash(prompt) if source == "agent" else None
        if source == "agent":
            concrete_prompt = concrete_agent_input_prompt(prompt)
            has_authoritative_remote_ids = bool(
                _is_authoritative_a2a_id(a2a_task_id)
                and _is_authoritative_a2a_id(a2a_context_id)
            )
            if concrete_prompt is None or not has_authoritative_remote_ids:
                logger.error(
                    "Rejecting invalid agent HITL request",
                    extra={
                        "room_id": room_id,
                        "agent_id": agent_id,
                        "error_code": (
                            "invalid_interactive_prompt"
                            if concrete_prompt is None
                            else "invalid_a2a_continuation"
                        ),
                    },
                )
                return None
            prompt = concrete_prompt
            prompt_type = HITLPromptType(prompt_type)
        resolved_display_message_id = display_message_id
        if source == "agent" and resolved_display_message_id is None:
            resolved_display_message_id = continuation_message_id

        if source == "agent" and not resolved_display_message_id:
            logger.error(
                "Agent HITL request has no display or continuation message id",
                extra={
                    "room_id": room_id,
                    "user_message_id": user_message_id,
                    "agent_id": agent_id,
                },
            )
            return None

        resolved_client_request_id = await self._resolve_hitl_client_request_id(
            user_message_id=user_message_id,
            message_id=resolved_display_message_id or continuation_message_id,
        )

        existing_request_doc = None
        if source == "supervisor" and request_id:
            existing_request_doc = await self.persistence.get_hitl_request(request_id)
            expected_identity = {
                "interaction_id": interaction_id,
                "question_index": question_index,
                "question_count": question_count,
                "room_id": room_id,
                "user_message_id": user_message_id,
                "orchestration_run_id": orchestration_run_id,
                "application_route": application_route.value,
                "public_source": public_source.value,
                "evidence_origin": evidence_origin.value,
            }
            if existing_request_doc is not None and any(
                existing_request_doc.get(field) != value
                for field, value in expected_identity.items()
            ):
                logger.error(
                    "Refusing to reuse mismatched HITL request %s",
                    request_id,
                )
                return None

        if continuation_message_id:
            # Count one complete interaction as one clarification round.
            if question_index == 0 and existing_request_doc is None:
                existing = await self.persistence.count_hitl_requests_for_message(
                    continuation_message_id
                )
                if existing >= MAX_HITL_ROUNDS:
                    logger.warning(
                        "Max HITL rounds (%d) exceeded for message %s",
                        MAX_HITL_ROUNDS,
                        continuation_message_id,
                    )
                    return None

        request_data = dict(
            schema_version=3,
            room_id=room_id,
            user_message_id=user_message_id,
            interaction_id=interaction_id,
            question_index=question_index,
            question_count=question_count,
            application_route=application_route,
            public_source=public_source,
            evidence_origin=evidence_origin,
            prompt=prompt,
            agent_prompt_hash=agent_prompt_hash,
            prompt_type=prompt_type,
            choices=choices,
            agent_id=agent_id,
            agent_name=agent_name,
            source_step_id=source_step_id,
            a2a_task_id=a2a_task_id,
            a2a_context_id=a2a_context_id,
            continuation_message_id=continuation_message_id,
            display_message_id=resolved_display_message_id,
            client_request_id=resolved_client_request_id,
            orchestration_run_id=orchestration_run_id,
            expires_at=expires_at or (utcnow() + timedelta(hours=expires_in_hours)),
        )
        if request_id:
            request_data["request_id"] = request_id
        request = HITLRequest(**request_data)

        # 1. Persist FIRST (so it survives SSE drops)
        # Keep datetimes as BSON datetimes. JSON-mode dumps turn deadlines into
        # strings, which breaks Mongo deadline queries and mixed-type comparisons
        # while attaching the request to its interaction.
        doc = request.model_dump(mode="python", exclude_none=True)
        hitl_request_created = False
        if source == "agent":
            persisted = await self.persistence.create_or_reuse_pending_hitl_request(doc)
            if not persisted:
                logger.error(
                    "Failed to create or reuse HITL request for agent message %s",
                    request.display_message_id,
                )
                return None
            persisted_doc, hitl_request_created = persisted
            persisted_doc = dict(persisted_doc)
            if not hitl_request_created:
                request_id_to_repair = persisted_doc.get("request_id")
                persisted_identity_matches = all(
                    persisted_doc.get(field) == doc.get(field)
                    for field in (
                        "interaction_id",
                        "question_index",
                        "question_count",
                        "room_id",
                        "user_message_id",
                        "orchestration_run_id",
                        "application_route",
                        "public_source",
                        "evidence_origin",
                    )
                )
                if (
                    not request_id_to_repair
                    or not _same_agent_hitl_logical_request(persisted_doc, doc)
                    or not persisted_identity_matches
                ):
                    # A uniqueness collision does not prove that the existing request
                    # is malformed. Never cancel another active interaction from this
                    # creation path; terminalization must go through the lifecycle
                    # reconciler so its owning run and projections converge.
                    logger.error(
                        "Rejecting mismatched reused agent HITL request",
                        extra={"hitl_request_id": request_id_to_repair},
                    )
                    return None

                repair_update = {
                    "prompt": prompt,
                    "agent_prompt_hash": agent_prompt_hash,
                    "prompt_type": getattr(prompt_type, "value", prompt_type),
                    "choices": choices,
                    "a2a_task_id": a2a_task_id,
                    "a2a_context_id": a2a_context_id,
                }
                if resolved_client_request_id and not persisted_doc.get(
                    "client_request_id"
                ):
                    repair_update["client_request_id"] = resolved_client_request_id
                repair_update = {
                    key: value
                    for key, value in repair_update.items()
                    if persisted_doc.get(key) != value
                }
                if repair_update:
                    repaired = await self.persistence.cas_update_hitl_request(
                        request_id_to_repair,
                        expected_status=HITLStatus.PENDING.value,
                        **repair_update,
                    )
                    if not repaired:
                        logger.error(
                            "Failed to atomically repair reused agent HITL request",
                            extra={"hitl_request_id": request_id_to_repair},
                        )
                        return None
                    persisted_doc.update(repair_update)

            if not _is_actionable_agent_hitl_document(persisted_doc):
                # Keep malformed persisted data non-actionable without silently
                # terminalizing it here. Pending hydration filters it. Any
                # cancellation/failure must use
                # the lifecycle reconciler rather than bypassing owning-run cleanup.
                logger.error(
                    "Rejecting malformed persisted agent HITL request",
                    extra={"hitl_request_id": persisted_doc.get("request_id")},
                )
                return None
            request = HITLRequest(
                **{k: v for k, v in persisted_doc.items() if k != "_id"}
            )
        else:
            saved = await self.persistence.create_hitl_request(doc)
            hitl_request_created = bool(saved)
            if not saved:
                existing_doc = existing_request_doc
                if existing_doc is None and request_id:
                    existing_doc = await self.persistence.get_hitl_request(
                        request.request_id
                    )
                if existing_doc is None:
                    logger.error(
                        "Failed to persist HITL request %s", request.request_id
                    )
                    return None
                request = HITLRequest(
                    **{k: v for k, v in existing_doc.items() if k != "_id"}
                )

        # Materialize the complete durable interaction before any user-visible
        # message projection or SSE emission.
        if self._lifecycle is None:
            raise HITLRequestProjectionError(
                "HITL lifecycle persistence is required",
                request_id=request.request_id,
            )
        if self._lifecycle is not None:
            persisted_identity = await self.persistence.get_hitl_request(
                request.request_id
            )
            if persisted_identity is None:
                raise HITLRequestProjectionError(
                    "persisted HITL request disappeared before interaction materialization",
                    request_id=request.request_id,
                )
            raw_interaction_id = persisted_identity.get("interaction_id")
            if raw_interaction_id != interaction_id:
                raise HITLRequestProjectionError(
                    "persisted HITL request interaction identity mismatch",
                    request_id=request.request_id,
                )
            materialized = await self._lifecycle.attach_interaction_request(
                interaction_id,
                request_id=request.request_id,
                required=True,
                expires_at=request.expires_at,
                question_index=request.question_index,
            )
            if materialized is None:
                raise HITLRequestProjectionError(
                    "failed to materialize HITL interaction",
                    request_id=request.request_id,
                )
            return request

    async def recover_open_interaction_projection(
        self, interaction: dict[str, Any]
    ) -> int:
        """Idempotently project every member after an interaction becomes OPEN."""
        if interaction.get("status") not in {
            HITLInteractionStatus.OPEN.value,
            HITLInteractionStatus.PARTIALLY_ANSWERED.value,
        }:
            return 0
        expected_ids = list(interaction.get("request_ids") or [])
        expected_total = int(interaction.get("expected_request_count") or 0)
        rows = [
            row
            for request_id in expected_ids
            if (row := await self.persistence.get_hitl_request(request_id)) is not None
        ]
        by_id = {row.get("request_id"): row for row in rows}
        if len(rows) != expected_total or len(by_id) != expected_total:
            raise HITLRequestProjectionError(
                "interaction requests are incomplete during projection"
            )
        try:
            validate_exact_member_inventory(interaction, rows)
        except HITLAggregateCorruptionError as exc:
            raise HITLRequestProjectionError(str(exc)) from exc
        projected_count = 0
        ordered_ids = expected_ids
        for request_id in ordered_ids:
            row = by_id[request_id]
            if row.get("open_projection_completed_at") is not None:
                continue
            claim_id = uuid4().hex
            claimed = await self.persistence.claim_hitl_open_projection(
                request_id, claim_id
            )
            if claimed is None:
                latest = await self.persistence.get_hitl_request(request_id)
                if latest and (
                    latest.get("open_projection_completed_at") is not None
                    or latest.get("open_projection_claim_id")
                ):
                    # Another materializer/reconciler owns this idempotent
                    # projection. Its active claim is pending success, not a
                    # materialization failure that should unwind creation.
                    continue
                raise HITLRequestProjectionError(
                    "HITL open projection could not be claimed",
                    request_id=request_id,
                )
            request = HITLRequest(**{k: v for k, v in row.items() if k != "_id"})
            display_id = request.display_message_id or request.continuation_message_id
            try:
                projection_ok = True
                if display_id and request.public_source == HITLPublicSource.AGENT:
                    projection_ok = bool(
                        await self.persistence.persist_pending_hitl_on_agent_message(
                            display_id,
                            request_id=request.request_id,
                            prompt=request.prompt,
                            prompt_type=request.prompt_type,
                            choices=request.choices,
                            a2a_task_id=request.a2a_task_id,
                            a2a_context_id=request.a2a_context_id,
                            interaction_id=request.interaction_id,
                            question_count=request.question_count,
                            question_index=request.question_index,
                            projection_at=request.created_at,
                        )
                    )
                elif display_id:
                    projection_ok = all(
                        (
                            await self.persistence.update_agent_message_task_state(
                                display_id, "input-required"
                            ),
                            await self.persistence.persist_hitl_user_answer(
                                display_id, None
                            ),
                            await self.persistence.persist_hitl_request_id_on_message(
                                display_id, request.request_id
                            ),
                            await self.persistence.persist_hitl_interaction_metadata(
                                display_id,
                                interaction_id=request.interaction_id,
                                question_count=request.question_count,
                                question_index=request.question_index,
                            ),
                        )
                    )
                if not projection_ok:
                    raise HITLRequestProjectionError(
                        "failed to project open HITL interaction member",
                        request_id=request.request_id,
                    )
                request.interaction_status = HITLInteractionStatus.OPEN
                request.interaction_version = int(interaction.get("version") or 1)
                request.application_status = HITLInteractionStatus.OPEN.value
                await self._emit_hitl_event(
                    room_id=request.room_id,
                    event_type=HITLEventType.INPUT_REQUESTED,
                    request=request,
                )
                completed = await self.persistence.complete_hitl_open_projection(
                    request_id, claim_id
                )
                if not completed:
                    raise HITLRequestProjectionError(
                        "failed to finalize open HITL projection marker",
                        request_id=request_id,
                    )
                projected_count += 1
            except Exception:
                await self.persistence.release_hitl_open_projection(
                    request_id, claim_id
                )
                raise
        if (
            await self._is_canonical_run(interaction.get("orchestration_run_id"))
            and self._canonical_control_publisher
        ):
            result = self._canonical_control_publisher(
                "run_waiting_input",
                interaction,
                ordered_ids,
            )
            if inspect.isawaitable(result):
                await result
        return projected_count

    async def emit_canonical_resumed_control(self, interaction: dict[str, Any]) -> None:
        if (
            not await self._is_canonical_run(interaction.get("orchestration_run_id"))
            or not self._canonical_control_publisher
        ):
            return
        result = self._canonical_control_publisher(
            "run_resumed",
            interaction,
            list(interaction.get("request_ids") or []),
        )
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------
    # Handle user response
    # ------------------------------------------------------------------

    @_room_write_fenced
    async def handle_batch_response(
        self,
        room_id: str,
        interaction_id: str,
        answers: list[dict[str, str]],
        user_id: str,
        client_request_id: str | None = None,
    ) -> dict:
        """Record a complete questionnaire and resume its interaction once."""
        if self._application is None:
            raise HITLRoutingFailedError(
                "Batch HITL responses require lifecycle-bound application"
            )
        return await self._application.handle_batch_response(
            self,
            room_id=room_id,
            interaction_id=interaction_id,
            answers=answers,
            user_id=user_id,
            client_request_id=client_request_id,
        )

    @_room_write_fenced
    async def handle_response(
        self,
        room_id: str,
        request_id: str,
        user_input: str,
        user_id: str,
    ) -> dict:
        """Record one answer through its required persisted interaction."""
        if self._application is None:
            raise HITLRoutingFailedError(
                "HITL responses require lifecycle-bound application"
            )
        return await self._application.handle_response(
            self,
            room_id=room_id,
            request_id=request_id,
            user_input=user_input,
            user_id=user_id,
        )

    async def _project_completed_hitl_display(
        self,
        *,
        display_message_id: Any,
        user_input: Any,
        request_id: str | None = None,
        room_id: str | None = None,
    ) -> bool:
        if not isinstance(display_message_id, str) or not display_message_id:
            return False
        if user_input is None:
            return False
        try:
            answer_projected = await self.persistence.persist_hitl_user_answer(
                display_message_id,
                user_input,
            )
            state_projected = await self.persistence.update_agent_message_task_state(
                display_message_id,
                "completed",
            )
        except Exception:
            logger.warning(
                "Failed to project completed HITL response onto display message",
                extra={
                    "hitl_request_id": request_id,
                    "room_id": room_id,
                    "display_message_id": display_message_id,
                },
                exc_info=True,
            )
            return False
        if not answer_projected or not state_projected:
            logger.warning(
                "Incomplete completed HITL display projection",
                extra={
                    "hitl_request_id": request_id,
                    "room_id": room_id,
                    "display_message_id": display_message_id,
                    "answer_projected": bool(answer_projected),
                    "state_projected": bool(state_projected),
                },
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Agent response routing
    # ------------------------------------------------------------------

    async def _handle_agent_response(
        self,
        request: HITLRequest,
        user_input: str,
        *,
        outbound_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Send user's reply to the waiting A2A agent.

        For push-notification agents the reply is fire-and-forget: the agent
        will POST a webhook callback that triggers ``resume_queue_from_continuation``.

        For blocking agents (non-push OR push-capable but WEBHOOK_BASE_URL
        unset) the reply returns synchronously.  We use the response directly
        (not a DB re-read) and trigger queue resume manually.

        If the blocking reply itself returns ``input_required`` again, we do
        NOT resume — the agent needs another round of user input, which the
        next HITL cycle will handle.
        """
        # Reset last_notified_state so multi-round input_required works
        await self.persistence.reset_last_notified_state(
            request.continuation_message_id
        )

        reply_result = await self.agent_reply.reply_to_task(
            message_id=request.continuation_message_id,
            task_id=request.a2a_task_id,
            context_id=request.a2a_context_id,
            user_input=user_input,
            outbound_message_id=outbound_message_id,
        )

        # reply_to_task returns {"blocking": bool, "task_state": str|None,
        # "response_text": str|None}.  When blocking=True the response is
        # already complete — use it directly instead of re-reading from DB.
        was_blocking = reply_result.get("blocking", False)
        raw_task_state = reply_result.get("task_state")
        authoritative_task_id = reply_result.get("task_id") or request.a2a_task_id
        authoritative_context_id = (
            reply_result.get("context_id") or request.a2a_context_id
        )
        if not was_blocking and raw_task_state not in {
            "failed",
            "canceled",
            "rejected",
        }:
            # Push-notification mode — agent will POST webhook → resume_queue_from_continuation
            return {
                "blocking": False,
                "resume_execution": False,
                "a2a_task_id": authoritative_task_id,
                "a2a_context_id": authoritative_context_id,
            }

        response_text = reply_result.get("response_text") or ""
        task_state = (
            str(raw_task_state).strip().lower().replace("_", "-")
            if raw_task_state
            else ("completed" if response_text.strip() else "input-required")
        )

        # If the agent asked for more input, don't resume the queue — create
        # a new HITL request so the frontend has a pending record for the next
        # answer.  Without this, multi-round blocking HITL conversations get
        # stuck after the second prompt.
        if task_state in ("input-required", "auth-required", "policy-required"):
            try:
                interaction_spec = A2AInteractionSpec.model_validate(
                    reply_result.get("_interaction_spec")
                )
            except (TypeError, ValueError):
                return {
                    "blocking": True,
                    "task_state": "failed",
                    "response_text": UNSUPPORTED_INTERACTION_MESSAGE,
                    "resume_execution": True,
                    "routing_failed": True,
                    "error_code": UNSUPPORTED_INTERACTION_CODE,
                    "a2a_task_id": authoritative_task_id,
                    "a2a_context_id": authoritative_context_id,
                }
            public_response_text = "\n\n".join(
                question.prompt for question in interaction_spec.questions
            )
            response_prompt_hash = _prompt_hash(public_response_text)
            same_raw_agent_prompt = bool(
                request.agent_prompt_hash
                and response_prompt_hash
                and request.agent_prompt_hash == response_prompt_hash
            )
            same_concrete_public_prompt = bool(
                request.agent_prompt_hash is None
                and request.prompt != _GENERIC_AGENT_INPUT_PROMPT
                and public_response_text != _GENERIC_AGENT_INPUT_PROMPT
                and _normalized_prompt(public_response_text)
                == _normalized_prompt(request.prompt)
            )
            if (
                request.orchestration_run_id
                and task_state == "input-required"
                and (same_raw_agent_prompt or same_concrete_public_prompt)
            ):
                logger.warning(
                    "hitl_agent_no_progress message_id=%s task_id=%s "
                    "prompt_hash=%s; returning control to orchestrator",
                    request.continuation_message_id,
                    request.a2a_task_id,
                    _short_prompt_hash(public_response_text),
                )
                return {
                    "blocking": True,
                    "task_state": task_state,
                    "response_text": public_response_text,
                    "resume_execution": True,
                    "agent_no_progress": True,
                    "agent_no_progress_code": "agent_repeated_input_required",
                    "agent_id": request.agent_id,
                    "agent_name": request.agent_name,
                    "display_message_id": request.display_message_id,
                    "continuation_message_id": request.continuation_message_id,
                    "a2a_task_id": authoritative_task_id,
                    "a2a_context_id": authoritative_context_id,
                }
            if request.orchestration_run_id:
                return {
                    "blocking": True,
                    "task_state": task_state,
                    "response_text": "Agent requested additional input.",
                    "resume_execution": True,
                    "agent_input_required_private": True,
                    "agent_id": request.agent_id,
                    "agent_name": request.agent_name,
                    "display_message_id": request.display_message_id,
                    "continuation_message_id": request.continuation_message_id,
                    "a2a_task_id": authoritative_task_id,
                    "a2a_context_id": authoritative_context_id,
                }
            logger.info(
                "hitl: direct continuation returned typed input_required for %s — "
                "creating the next interaction",
                request.continuation_message_id,
            )
            followup_interaction_id = deterministic_interaction_id(
                event_identity=request.continuation_message_id or request.request_id,
                round_identity=(
                    f"{request.interaction_id}:followup:{interaction_spec.interaction_id}"
                ),
            )
            new_requests = await self.request_interaction(
                room_id=request.room_id,
                user_message_id=request.user_message_id,
                interaction_id=followup_interaction_id,
                application_route=HITLApplicationRoute.A2A_RESUME,
                public_source=HITLPublicSource.AGENT,
                evidence_origin=HITLEvidenceOrigin.AGENT,
                route_snapshot=HITLRouteSnapshot(
                    route=HITLApplicationRoute.A2A_RESUME,
                    task_id=authoritative_task_id,
                    context_id=authoritative_context_id,
                    continuation_message_id=request.continuation_message_id or "",
                    agent_id=request.agent_id or "",
                ),
                questions=[
                    {
                        "prompt": question.prompt,
                        "prompt_type": _prompt_type_for_typed_question(question),
                        "choices": list(question.choices) if question.choices else None,
                        "agent_id": request.agent_id,
                        "agent_name": request.agent_name,
                        "continuation_message_id": request.continuation_message_id,
                        "display_message_id": request.display_message_id,
                    }
                    for question in interaction_spec.questions
                ],
                orchestration_run_id=request.orchestration_run_id,
            )
            if new_requests is None:
                logger.warning(
                    "hitl: request_interaction failed for %s — keeping original "
                    "HITL retryable",
                    request.continuation_message_id,
                )
                raise HITLRoutingFailedError(
                    "failed to create follow-up HITL request; "
                    "the original HITL request remains pending for retry"
                )
            if not new_requests or len(new_requests) != len(interaction_spec.questions):
                raise HITLRoutingFailedError(
                    "failed to materialize the complete follow-up HITL interaction"
                )
            new_request = new_requests[0]
            return {
                "blocking": True,
                "task_state": task_state,
                "response_text": response_text,
                "resume_execution": False,
                "followup_hitl_request_id": new_request.request_id,
                "followup_prompt": new_request.prompt,
                "followup_prompt_type": getattr(
                    new_request.prompt_type,
                    "value",
                    new_request.prompt_type,
                ),
                "agent_id": new_request.agent_id,
                "agent_name": new_request.agent_name,
                "display_message_id": new_request.display_message_id,
                "continuation_message_id": new_request.continuation_message_id,
                "a2a_task_id": new_request.a2a_task_id,
                "a2a_context_id": new_request.a2a_context_id,
                "requires_auth": task_state == "auth-required",
                "requires_policy": (
                    task_state == "policy-required"
                    or bool(reply_result.get("requires_policy"))
                    or bool(reply_result.get("policy_required"))
                ),
            }

        # Use the response text from the synchronous reply (authoritative,
        # no stale-DB risk).
        task_result_text = reply_result.get("response_text")

        # reply_to_task already persisted the full task + message_text
        # atomically via update_task_on_message.  We only need to emit the
        # SSE notification so the frontend shows the updated message.
        is_failure = task_state in ("failed", "canceled", "rejected")
        effective_state = task_state or "completed"
        if request.display_message_id:
            # Retrieve the agent message to get user_id for notification
            agent_msg = await self.persistence.get_room_agent_message_by_message_id(
                request.display_message_id
            )
            if agent_msg:
                state_map = {
                    "completed": "completed",
                    "failed": "failed",
                    "canceled": "canceled",
                    "rejected": "rejected",
                }
                notify_state = state_map.get(effective_state, "completed")
                await self.task_notifications.notify_task_update(
                    request.display_message_id,
                    notify_state,
                    room_id=request.room_id,
                    user_id=agent_msg.user_id or "",
                )

        logger.info(
            "hitl: blocking reply completed (state=%s) — triggering manual "
            "queue resume for %s",
            task_state,
            request.continuation_message_id,
        )
        if request.orchestration_run_id:
            return {
                "blocking": True,
                "task_state": task_state,
                "response_text": task_result_text,
                "resume_execution": True,
                "a2a_task_id": authoritative_task_id,
                "a2a_context_id": authoritative_context_id,
            }
        resumed = await self.continuation.resume_queue_from_continuation(
            request.continuation_message_id,
            task_result_text=task_result_text,
            failed=is_failure,
        )
        if not resumed:
            raise RuntimeError(
                f"Failed to resume queue for message {request.continuation_message_id} "
                "— continuation may have been lost or room lock timed out"
            )
        return {
            "blocking": True,
            "task_state": task_state,
            "response_text": task_result_text,
            "resume_execution": False,
            "queue_resume_triggered": True,
            "a2a_task_id": authoritative_task_id,
            "a2a_context_id": authoritative_context_id,
        }

    async def _project_reconciled_agent_response(
        self,
        request: HITLRequest,
        response_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Finish local continuation effects after GetTask confirms delivery.

        The remote send must never be repeated. Queue continuation is destructive
        but idempotent at this boundary: a missing continuation means another
        recovery worker already consumed it.
        """

        projected = dict(response_snapshot)
        task_state = str(projected.get("task_state") or "completed")
        task_result_text = projected.get("response_text")
        if not isinstance(task_result_text, str):
            task_result_text = ""
        failed = task_state in {state.value for state in FAILURE_STATES}
        if request.orchestration_run_id:
            projected["resume_execution"] = True
            return projected
        continuation_message_id = request.continuation_message_id
        if not continuation_message_id:
            raise RuntimeError("Reconciled interaction has no continuation identity")
        resumed = await self.continuation.resume_queue_from_continuation(
            continuation_message_id,
            task_result_text=task_result_text,
            failed=failed,
        )
        if not resumed and await self.continuation.has_pending_queue_continuation(
            continuation_message_id
        ):
            raise RuntimeError("Queue continuation remains pending after resume")
        projected["resume_execution"] = False
        projected["queue_resume_triggered"] = bool(resumed)
        projected["queue_resume_already_completed"] = not resumed
        return projected

    # ------------------------------------------------------------------
    # Supervisor response routing
    # ------------------------------------------------------------------

    async def _handle_supervisor_response(
        self,
        request: HITLRequest,
        user_input: str,
        *,
        effect_id: str | None = None,
    ) -> None:
        """Resume the suspended orchestrator Run with the recorded answer.

        The ask_user declaration persisted ``source_step_id`` as its tool call
        identity; the answer is delivered as the deterministic ToolObservation
        for that call, after which the kernel continues the model loop.
        """
        del effect_id
        if not request.orchestration_run_id:
            raise ContinuationLostError(
                "Supervisor HITL request is missing orchestration_run_id"
            )
        call_id = request.source_step_id
        if not call_id:
            raise ContinuationLostError(
                "Supervisor HITL request is missing its tool call identity"
            )
        if self._supervisor_resume is None:
            raise ContinuationLostError(
                "Supervisor HITL resume port has not been bound"
            )
        resumed = await self._supervisor_resume(
            run_id=request.orchestration_run_id,
            call_id=call_id,
            answers=user_input,
        )
        if resumed is False:
            raise ContinuationLostError(
                "Supervisor Run became non-resumable during HITL continuation"
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def _public_pending_requests(
        self, docs: list[dict[str, Any]]
    ) -> list[HITLRequest]:
        if self._lifecycle is None:
            eligible = [
                document
                for document in docs
                if document.get("public_source") != HITLPublicSource.AGENT.value
                or _is_actionable_agent_hitl_document(document)
            ]
            return [_public_hitl_request_from_doc(document) for document in eligible]
        interaction_ids: list[str] = []
        for document in docs:
            interaction_id = document.get("interaction_id")
            if not isinstance(interaction_id, str) or not interaction_id:
                raise HITLRequestProjectionError(
                    "persisted HITL request has no interaction",
                    request_id=document.get("request_id"),
                )
            if interaction_id not in interaction_ids:
                interaction_ids.append(interaction_id)
        public: list[HITLRequest] = []
        visible_statuses = {
            HITLInteractionStatus.OPEN.value,
            HITLInteractionStatus.PARTIALLY_ANSWERED.value,
            HITLInteractionStatus.ANSWERS_RECORDED.value,
            HITLInteractionStatus.APPLYING.value,
            HITLInteractionStatus.DELIVERY_UNCERTAIN.value,
        }
        visible_member_statuses = {
            HITLStatus.PENDING.value,
            HITLStatus.PROCESSING.value,
            HITLStatus.ANSWER_RECORDED.value,
        }
        for interaction_id in interaction_ids:
            # Aggregate visibility is authoritative. In particular,
            # MATERIALIZING must be hidden without trying to validate a partial
            # request-row scan.
            interaction = await self._lifecycle.get_interaction_strict(interaction_id)
            if interaction is None:
                raise HITLRequestProjectionError(
                    "persisted HITL interaction is missing"
                )
            if interaction.get("status") not in visible_statuses:
                continue
            ordered_rows: list[dict[str, Any]] = []
            for request_id in interaction.get("request_ids") or []:
                row = await self.persistence.get_hitl_request(request_id)
                if row is None:
                    raise HITLRequestProjectionError(
                        "persisted HITL interaction member is missing",
                        request_id=request_id,
                    )
                ordered_rows.append(row)
            try:
                validate_exact_member_inventory(interaction, ordered_rows)
            except HITLAggregateCorruptionError as exc:
                raise HITLRequestProjectionError(str(exc)) from exc
            for document in ordered_rows:
                if document.get("status") not in visible_member_statuses:
                    continue
                if (
                    document.get("public_source") == HITLPublicSource.AGENT.value
                    and not _is_actionable_agent_hitl_document(document)
                ):
                    continue
                enriched = dict(document)
                enriched["interaction_status"] = interaction.get("status")
                enriched["interaction_version"] = interaction.get("version")
                enriched["application_status"] = interaction.get("status")
                enriched["application_error"] = interaction.get("application_error")
                public.append(_public_hitl_request_from_doc(enriched))
        return public

    async def get_pending_requests(self, room_id: str) -> list[HITLRequest]:
        """Get all pending HITL requests for a room (SSE reconnect catch-up)."""
        strict_reader = getattr(
            self.persistence, "get_pending_hitl_requests_strict", None
        )
        if callable(strict_reader) and inspect.iscoroutinefunction(strict_reader):
            docs = await strict_reader(room_id)
        else:
            docs = await self.persistence.get_pending_hitl_requests(room_id)
        return await self._public_pending_requests(docs)

    async def get_pending_requests_for_message(
        self, user_message_id: str
    ) -> list[HITLRequest]:
        """Get pending HITL requests associated with a specific user message."""
        docs = await self.persistence.get_pending_hitl_requests_for_message(
            user_message_id
        )
        return await self._public_pending_requests(docs)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def _interaction_members(
        self, request: HITLRequest
    ) -> tuple[dict[str, Any], list[tuple[dict[str, Any], HITLRequest]]]:
        if self._lifecycle is None:
            raise HITLRoutingFailedError("HITL lifecycle persistence is required")
        interaction = await self._lifecycle.get_interaction_strict(
            request.interaction_id
        )
        if interaction is None:
            raise HITLRoutingFailedError("Persisted HITL interaction is missing")
        rows: list[dict[str, Any]] = []
        for member_id in interaction.get("request_ids") or []:
            row = await self.persistence.get_hitl_request(member_id)
            if row is None:
                raise HITLRoutingFailedError("Interaction request is missing")
            rows.append(row)
        try:
            validate_exact_member_inventory(interaction, rows)
        except HITLAggregateCorruptionError as exc:
            raise HITLRoutingFailedError(str(exc)) from exc
        return interaction, [
            (
                row,
                HITLRequest(
                    **{key: value for key, value in row.items() if key != "_id"}
                ),
            )
            for row in rows
        ]

    async def _clear_hitl_continuation_once(
        self,
        request: HITLRequest,
        cleared_continuation_ids: set[str],
    ) -> None:
        if not request.continuation_message_id:
            return
        if request.continuation_message_id in cleared_continuation_ids:
            return
        cleared_continuation_ids.add(request.continuation_message_id)
        await self.persistence.get_and_clear_continuation_on_message(
            request.continuation_message_id
        )
        await self.persistence.get_and_clear_continuation_on_user_message(
            request.continuation_message_id
        )

    async def _reconcile_terminal_members(
        self,
        members: list[tuple[dict[str, Any], HITLRequest]],
        *,
        event_type: HITLEventType,
    ) -> None:
        cleared_continuation_ids: set[str] = set()
        errors: list[Exception] = []
        for row, member in members:
            if row.get("cancellation_reconciled") is True:
                continue
            try:
                await self._clear_hitl_continuation_once(
                    member, cleared_continuation_ids
                )
                await self._emit_hitl_event(
                    room_id=member.room_id,
                    event_type=event_type,
                    request=member,
                )
                reconciled = await self.persistence.update_hitl_request(
                    member.request_id, cancellation_reconciled=True
                )
                if not reconciled:
                    raise RuntimeError("HITL terminal reconciliation failed")
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("HITL terminal side effects remain pending") from errors[
                0
            ]

    async def _terminalize_interaction_requests(
        self,
        request: HITLRequest,
        *,
        status: HITLStatus,
        event_type: HITLEventType,
        owning_run_terminal_status: str,
        owning_run_terminal_reason: str,
    ) -> list[HITLRequest]:
        interaction, _members = await self._interaction_members(request)
        aggregate_status = (
            HITLInteractionStatus.CANCELED.value
            if status == HITLStatus.CANCELED
            else HITLInteractionStatus.EXPIRED.value
        )
        terminal = await self._lifecycle.terminalize_interaction(
            interaction["interaction_id"],
            expected_statuses=[
                HITLInteractionStatus.MATERIALIZING.value,
                HITLInteractionStatus.OPEN.value,
                HITLInteractionStatus.PARTIALLY_ANSWERED.value,
                HITLInteractionStatus.ANSWERS_RECORDED.value,
            ],
            status=aggregate_status,
            reason=owning_run_terminal_reason,
            member_status=status.value,
            owning_run_terminal_status=owning_run_terminal_status,
        )
        if terminal is None:
            terminal = await self._lifecycle.get_interaction_strict(
                interaction["interaction_id"]
            )
        if terminal is None or terminal.get("status") != aggregate_status:
            return []

        terminal_members = await self.reconcile_terminal_interaction(terminal)
        return [member for _row, member in terminal_members]

    async def reconcile_terminal_interaction(
        self, interaction: dict[str, Any]
    ) -> list[tuple[dict[str, Any], HITLRequest]]:
        """CAS-converge every member, rereading losers before side effects."""
        aggregate_status = interaction.get("status")
        member_status = interaction.get("member_terminal_status") or (
            HITLStatus.EXPIRED.value
            if aggregate_status == HITLInteractionStatus.EXPIRED.value
            else HITLStatus.CANCELED.value
        )
        if aggregate_status not in {
            HITLInteractionStatus.CANCELED.value,
            HITLInteractionStatus.EXPIRED.value,
            HITLInteractionStatus.FAILED.value,
        }:
            return []
        owning_status = interaction.get("owning_run_terminal_status") or (
            "canceled"
            if aggregate_status == HITLInteractionStatus.CANCELED.value
            else "failed"
        )
        reason = interaction.get("terminal_reason") or "HITL interaction terminated"
        event_type = (
            HITLEventType.INPUT_EXPIRED
            if member_status == HITLStatus.EXPIRED.value
            else HITLEventType.INPUT_CANCELED
        )
        request_ids = list(interaction.get("request_ids") or [])
        preflight_rows: list[dict[str, Any]] = []
        for request_id in request_ids:
            row = await self.persistence.get_hitl_request(request_id)
            if row is None:
                raise HITLRoutingFailedError("Interaction request is missing")
            preflight_rows.append(row)
        try:
            validate_exact_member_inventory(interaction, preflight_rows)
        except HITLAggregateCorruptionError as exc:
            raise HITLRoutingFailedError(str(exc)) from exc

        proven: list[tuple[dict[str, Any], HITLRequest]] = []
        eligible = {
            HITLStatus.PENDING.value,
            HITLStatus.PROCESSING.value,
            HITLStatus.ANSWER_RECORDED.value,
            member_status,
        }
        for request_id in request_ids:
            latest: dict[str, Any] | None = None
            for _attempt in range(8):
                row = await self.persistence.get_hitl_request(request_id)
                if row is None or row.get("status") not in eligible:
                    latest = row
                    break
                matches = (
                    row.get("status") == member_status
                    and row.get("owning_run_terminal_status") == owning_status
                    and row.get("owning_run_terminal_reason") == reason
                )
                if matches:
                    latest = row
                    break
                await self.persistence.cas_update_hitl_request_strict(
                    request_id,
                    expected_status=row["status"],
                    status=member_status,
                    cancellation_reconciled=False,
                    owning_run_terminal_status=owning_status,
                    owning_run_terminal_reason=reason,
                )
                # Always reread: a truthy adapter result does not prove which
                # contender won the persisted CAS.
            if latest is None or (
                latest.get("status") in eligible
                and not (
                    latest.get("status") == member_status
                    and latest.get("owning_run_terminal_status") == owning_status
                    and latest.get("owning_run_terminal_reason") == reason
                )
            ):
                raise RuntimeError(
                    f"HITL terminal member {request_id} did not converge"
                )
            if latest.get("status") == member_status:
                proven.append(
                    (
                        latest,
                        HITLRequest(
                            **{
                                key: value
                                for key, value in latest.items()
                                if key != "_id"
                            }
                        ),
                    )
                )
        await self._reconcile_terminal_members(proven, event_type=event_type)
        reconciled_rows = [
            await self.persistence.get_hitl_request(request_id)
            for request_id in request_ids
        ]
        if reconciled_rows and all(
            row is not None and row.get("cancellation_reconciled") is True
            for row in reconciled_rows
        ):
            latest_interaction = await self._lifecycle.get_interaction_strict(
                interaction["interaction_id"]
            )
            if latest_interaction is not None:
                await self._lifecycle.mark_interaction_terminal_reconciled(
                    interaction["interaction_id"],
                    version=int(latest_interaction["version"]),
                )
            # Every member's public terminal is now durable and aggregate
            # ownership is cleared. Only now may the owning Tool/Turn/Run expose
            # its terminal sequence.
            if self._terminal_lifecycle is not None and proven:
                member = proven[0][1]
                terminal_status = member.owning_run_terminal_status or (
                    "canceled" if member.status == HITLStatus.CANCELED else "failed"
                )
                terminal_reason = member.owning_run_terminal_reason or (
                    "Human input request was canceled"
                    if terminal_status == "canceled"
                    else "Human input request expired"
                )
                try:
                    await self._terminal_lifecycle.terminalize_owning_run(
                        member,
                        terminal_status=terminal_status,
                        reason=terminal_reason,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "HITL terminal side effects remain pending"
                    ) from exc
        return proven

    async def _reconcile_terminal_request(
        self,
        request: HITLRequest,
        *,
        event_type: HITLEventType,
    ) -> None:
        del event_type
        if self._lifecycle is None:
            raise HITLRoutingFailedError("HITL lifecycle persistence is required")
        interaction = await self._lifecycle.get_interaction_strict(
            request.interaction_id
        )
        if interaction is None:
            raise HITLRoutingFailedError("Persisted HITL interaction is missing")
        await self.reconcile_terminal_interaction(interaction)

    async def cancel_interaction(
        self,
        interaction_id: str,
        room_id: str,
        *,
        failure_reason: str,
    ) -> bool:
        """Fail-closed compensation for an interaction creation/projection error."""

        if self._lifecycle is None:
            raise HITLRoutingFailedError("HITL lifecycle persistence is required")
        interaction = await self._lifecycle.get_interaction_strict(interaction_id)
        if interaction is None:
            return False
        if interaction.get("room_id") != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        terminal = await self._lifecycle.terminalize_interaction(
            interaction_id,
            expected_statuses=[
                HITLInteractionStatus.MATERIALIZING.value,
                HITLInteractionStatus.OPEN.value,
                HITLInteractionStatus.PARTIALLY_ANSWERED.value,
                HITLInteractionStatus.ANSWERS_RECORDED.value,
            ],
            status=HITLInteractionStatus.FAILED.value,
            reason=failure_reason,
            member_status=HITLStatus.CANCELED.value,
            owning_run_terminal_status="failed",
        )
        if terminal is None:
            terminal = await self._lifecycle.get_interaction_strict(interaction_id)
        if terminal is None:
            return False
        if terminal.get("status") == HITLInteractionStatus.FAILED.value:
            try:
                await self.reconcile_terminal_interaction(terminal)
            except (HITLRoutingFailedError, RuntimeError):
                # A MATERIALIZING aggregate may not have every member yet. Its
                # terminal aggregate state is still sufficient to prevent opening.
                logger.warning(
                    "HITL interaction compensation awaits member reconciliation",
                    extra={"interaction_id": interaction_id},
                )
            return True
        return False

    async def cancel_interaction_by_user(
        self,
        interaction_id: str,
        room_id: str,
        *,
        expected_version: int,
    ) -> int:
        if self._lifecycle is None:
            raise HITLRoutingFailedError("HITL lifecycle persistence is required")
        interaction = await self._lifecycle.get_interaction_strict(interaction_id)
        if interaction is None:
            raise HITLNotFoundError("HITL interaction not found")
        if interaction.get("room_id") != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        if int(interaction.get("version") or 0) != expected_version:
            raise HITLConflictError("HITL interaction changed before cancellation")
        if interaction.get("status") not in {
            HITLInteractionStatus.MATERIALIZING.value,
            HITLInteractionStatus.OPEN.value,
            HITLInteractionStatus.PARTIALLY_ANSWERED.value,
        }:
            raise HITLConflictError("HITL interaction is no longer cancelable")
        orchestration_run_id = interaction.get("orchestration_run_id")
        if orchestration_run_id and await self._is_canonical_run(orchestration_run_id):
            if self._canonical_cancellation_requester is None:
                raise HITLRoutingFailedError(
                    "canonical cancellation requester is required"
                )
            result = self._canonical_cancellation_requester(orchestration_run_id)
            if inspect.isawaitable(result):
                result = await result
            if result not in {"canceling", "canceled"}:
                raise HITLConflictError(
                    "owning Run already has another lifecycle winner"
                )
        request_ids = list(interaction.get("request_ids") or [])
        if request_ids:
            await self.cancel_request(request_ids[0], room_id=room_id)
        else:
            terminal = await self._lifecycle.terminalize_interaction(
                interaction_id,
                expected_statuses=[HITLInteractionStatus.MATERIALIZING.value],
                status=HITLInteractionStatus.CANCELED.value,
                reason="Human input interaction was canceled",
                member_status=HITLStatus.CANCELED.value,
                owning_run_terminal_status="canceled",
            )
            if terminal is None:
                raise HITLConflictError("HITL interaction could not be canceled")
        latest = await self._lifecycle.get_interaction_strict(interaction_id)
        if latest is None:
            raise HITLConflictError("HITL interaction disappeared after cancellation")
        return int(latest.get("version") or 0)

    async def cancel_request(
        self,
        request_id: str,
        room_id: str | None = None,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Cancel the aggregate that owns the public request member."""
        doc = await self.persistence.get_hitl_request(request_id)
        if not doc:
            raise HITLNotFoundError("HITL request not found")
        request = HITLRequest(
            **{key: value for key, value in doc.items() if key != "_id"}
        )
        if room_id is not None and request.room_id != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        if request.status == HITLStatus.CANCELED:
            await self._reconcile_terminal_request(
                request, event_type=HITLEventType.INPUT_CANCELED
            )
            return
        if request.status not in {
            HITLStatus.PENDING,
            HITLStatus.PROCESSING,
            HITLStatus.ANSWER_RECORDED,
        }:
            return
        await self._terminalize_interaction_requests(
            request,
            status=HITLStatus.CANCELED,
            event_type=HITLEventType.INPUT_CANCELED,
            owning_run_terminal_status="failed" if failure_reason else "canceled",
            owning_run_terminal_reason=(
                failure_reason or "Human input request was canceled"
            ),
        )

    async def expire_request(self, request_id: str, room_id: str | None = None) -> None:
        """Expire the aggregate that owns the public request member."""
        doc = await self.persistence.get_hitl_request(request_id)
        if not doc:
            raise HITLNotFoundError("HITL request not found")
        request = HITLRequest(
            **{key: value for key, value in doc.items() if key != "_id"}
        )
        if room_id is not None and request.room_id != room_id:
            raise HITLRoomMismatchError("Room mismatch")
        if request.status == HITLStatus.EXPIRED:
            await self._reconcile_terminal_request(
                request, event_type=HITLEventType.INPUT_EXPIRED
            )
            return
        if request.status not in {
            HITLStatus.PENDING,
            HITLStatus.PROCESSING,
            HITLStatus.ANSWER_RECORDED,
        }:
            return
        await self._terminalize_interaction_requests(
            request,
            status=HITLStatus.EXPIRED,
            event_type=HITLEventType.INPUT_EXPIRED,
            owning_run_terminal_status="failed",
            owning_run_terminal_reason="Human input request expired",
        )

    async def cancel_requests_for_message(
        self,
        user_message_id: str,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Cancel all pending HITL requests for a given user message."""
        strict_reader = getattr(
            self.persistence,
            "get_pending_hitl_requests_for_message_strict",
            None,
        )
        processed_request_ids: set[str] = set()
        while True:
            if callable(strict_reader) and inspect.iscoroutinefunction(strict_reader):
                docs = await strict_reader(user_message_id)
                pending = [
                    HITLRequest(**{k: v for k, v in doc.items() if k != "_id"})
                    for doc in docs
                ]
            else:
                docs = await self.persistence.get_pending_hitl_requests_for_message(
                    user_message_id
                )
                pending = [
                    HITLRequest(**{k: v for k, v in doc.items() if k != "_id"})
                    for doc in docs
                ]
            if not pending:
                return
            batch_ids = {req.request_id for req in pending}
            if batch_ids <= processed_request_ids:
                raise RuntimeError("HITL cancellation scan made no progress")
            processed_request_ids.update(batch_ids)
            for req in pending:
                await self.cancel_request(
                    req.request_id,
                    failure_reason=failure_reason,
                )

    PROCESSING_TIMEOUT_SECONDS = 600
    LEASE_HEARTBEAT_SECONDS = 120

    async def _find_pending_followup_for_stale_agent_hitl(
        self, doc: dict[str, Any]
    ) -> dict[str, Any] | None:
        if doc.get("public_source") != HITLPublicSource.AGENT.value:
            return None

        room_id = doc.get("room_id")
        display_message_id = doc.get("display_message_id")
        continuation_message_id = doc.get("continuation_message_id")
        if not room_id or not (display_message_id or continuation_message_id):
            return None

        find_pending = getattr(
            self.persistence, "find_pending_hitl_request_for_agent_message", None
        )
        if not callable(find_pending):
            return None

        try:
            maybe_pending = find_pending(
                room_id=room_id,
                display_message_id=display_message_id,
                continuation_message_id=continuation_message_id,
                agent_id=doc.get("agent_id"),
                a2a_task_id=doc.get("a2a_task_id"),
                a2a_context_id=doc.get("a2a_context_id"),
            )
            pending = (
                await maybe_pending
                if inspect.isawaitable(maybe_pending)
                else maybe_pending
            )
        except Exception:
            logger.warning(
                "Failed to check pending follow-up HITL during stale recovery",
                extra={
                    "hitl_request_id": doc.get("request_id"),
                    "room_id": room_id,
                    "display_message_id": display_message_id,
                    "continuation_message_id": continuation_message_id,
                },
                exc_info=True,
            )
            return None

        if not isinstance(pending, dict):
            return None
        if pending.get("request_id") == doc.get("request_id"):
            return None
        return pending

    async def recover_stale_processing(self) -> int:
        """Recover HITL requests stuck in 'processing' after a crash.

        For each stale request, checks the ``routing_completed_at`` field
        (set by handle_response immediately after successful routing):
        - Field is set   -> finalize to 'responded' (routing succeeded,
          only the status write was lost)
        - Field is absent -> revert to 'pending' (routing never completed,
          safe to let the user retry)

        All writes use CAS (expected_status='processing') to prevent
        overwriting a newer state. Reverts also clear ``claim_id`` so the
        original worker's fenced writes become no-ops.
        """
        from datetime import timedelta

        cutoff = utcnow() - timedelta(seconds=self.PROCESSING_TIMEOUT_SECONDS)
        recovered = 0
        async for doc in self.persistence.iter_stale_processing_hitl_requests(cutoff):
            req_id = doc.get("request_id")
            routing_done = doc.get("routing_completed_at") is not None

            if routing_done:
                ok = await self.persistence.cas_update_hitl_request(
                    req_id,
                    expected_status=HITLStatus.PROCESSING.value,
                    status=HITLStatus.RESPONDED.value,
                )
                if ok:
                    logger.warning(
                        "Finalized stale PROCESSING HITL request %s to RESPONDED "
                        "(routing_completed_at is set)",
                        req_id,
                    )
                    recovered += 1
                else:
                    logger.info(
                        "Skipped recovery of HITL request %s — status already changed",
                        req_id,
                    )
            else:
                pending_followup = (
                    await self._find_pending_followup_for_stale_agent_hitl(doc)
                )
                if pending_followup:
                    ok = await self.persistence.cas_update_hitl_request(
                        req_id,
                        expected_status=HITLStatus.PROCESSING.value,
                        status=HITLStatus.RESPONDED.value,
                        routing_completed_at=utcnow(),
                    )
                    if ok:
                        logger.warning(
                            "Finalized stale PROCESSING HITL request %s to "
                            "RESPONDED because pending follow-up request %s "
                            "already exists",
                            req_id,
                            pending_followup.get("request_id"),
                        )
                        recovered += 1
                    else:
                        logger.info(
                            "Skipped recovery of HITL request %s — status already changed",
                            req_id,
                        )
                    continue

                ok = await self.persistence.cas_update_hitl_request(
                    req_id,
                    expected_status=HITLStatus.PROCESSING.value,
                    status=HITLStatus.PENDING.value,
                    claim_id=None,
                    routing_completed_at=None,
                    user_input=None,
                    responded_at=None,
                    responded_by_user_id=None,
                )
                if ok:
                    logger.warning(
                        "Reverted stale PROCESSING HITL request %s to PENDING "
                        "(routing never completed)",
                        req_id,
                    )
                    recovered += 1
                else:
                    logger.info(
                        "Skipped recovery of HITL request %s — status already changed",
                        req_id,
                    )

        if recovered:
            logger.warning(
                "Recovered %d stale PROCESSING HITL requests (threshold: %ds)",
                recovered,
                self.PROCESSING_TIMEOUT_SECONDS,
            )
        return recovered

    # ------------------------------------------------------------------
    # SSE emission helper
    # ------------------------------------------------------------------

    async def _resolve_hitl_client_request_id(
        self,
        *,
        user_message_id: str,
        message_id: str | None,
    ) -> str | None:
        get_user_message = getattr(
            self.persistence, "get_room_user_message_by_message_id", None
        )
        user_message = None
        if callable(get_user_message):
            try:
                maybe_user_message = get_user_message(user_message_id)
                user_message = (
                    await maybe_user_message
                    if inspect.isawaitable(maybe_user_message)
                    else maybe_user_message
                )
            except Exception:
                logger.warning(
                    "Failed to resolve HITL client_request_id from user message",
                    extra={"user_message_id": user_message_id},
                    exc_info=True,
                )
        client_request_id = (
            user_message.client_request_id
            if user_message and isinstance(user_message.client_request_id, str)
            else None
        )
        if isinstance(client_request_id, str) and client_request_id.strip():
            return client_request_id.strip()

        if isinstance(message_id, str) and message_id.strip():
            resolve_fn = getattr(
                self.persistence,
                "resolve_client_request_id_for_message_id",
                None,
            )
            if callable(resolve_fn):
                try:
                    maybe_resolved = resolve_fn(message_id.strip())
                    resolved = (
                        await maybe_resolved
                        if inspect.isawaitable(maybe_resolved)
                        else maybe_resolved
                    )
                    if isinstance(resolved, str) and resolved.strip():
                        return resolved.strip()
                except Exception:
                    logger.warning(
                        "Failed to resolve HITL client_request_id from message id",
                        extra={"message_id": message_id},
                        exc_info=True,
                    )
        return None

    async def canonical_run_allows_resume(self, interaction: dict[str, Any]) -> bool:
        run_id = interaction.get("orchestration_run_id")
        if not await self._is_canonical_run(run_id):
            return True
        if self._canonical_run_state_reader is None:
            return True
        state = self._canonical_run_state_reader(run_id)
        if inspect.isawaitable(state):
            state = await state
        return state not in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
            "missing",
        }

    async def _is_canonical_run(self, run_id: object) -> bool:
        if (
            not isinstance(run_id, str)
            or not run_id
            or self._lifecycle_family_reader is None
        ):
            return False
        try:
            family = self._lifecycle_family_reader(run_id)
            if inspect.isawaitable(family):
                family = await family
            return family == "canonical"
        except Exception:
            logger.warning(
                "Failed to resolve durable HITL lifecycle family",
                extra={"run_id": run_id},
                exc_info=True,
            )
            return False

    async def _emit_hitl_event(
        self,
        room_id: str,
        event_type: HITLEventType,
        request: HITLRequest,
        error: str | None = None,
    ) -> None:
        """Emit an HITL lifecycle event via SSE."""
        data: dict = {
            "request_id": request.request_id,
            "message_id": (
                request.display_message_id
                or request.continuation_message_id
                or request.user_message_id
            ),
            "source": request.public_source.value,
            "related_message_id": request.user_message_id,
        }
        client_request_id = request.client_request_id
        if not (isinstance(client_request_id, str) and client_request_id.strip()):
            client_request_id = await self._resolve_hitl_client_request_id(
                user_message_id=request.user_message_id,
                message_id=data.get("message_id"),
            )
        if isinstance(client_request_id, str) and client_request_id.strip():
            data["client_request_id"] = client_request_id.strip()

        source = request.public_source.value
        prompt_type = getattr(request.prompt_type, "value", request.prompt_type)
        request_status = getattr(request.status, "value", request.status)

        canonical_run_id = (
            request.orchestration_run_id
            if await self._is_canonical_run(request.orchestration_run_id)
            else None
        )
        if event_type == HITLEventType.INPUT_REQUESTED:
            if canonical_run_id:
                from execution.orchestrator.public_text import sanitize_public_text

                public_prompt = sanitize_public_text(
                    request.prompt, secret_values=self._public_secret_values
                )[:4000]
                public_choices = [
                    sanitize_public_text(
                        choice, secret_values=self._public_secret_values
                    )[:500]
                    for choice in (request.choices or [])[:20]
                ]
                public_agent_label = (
                    sanitize_public_text(
                        request.agent_name,
                        secret_values=self._public_secret_values,
                    )[:160]
                    if request.agent_name
                    else None
                )
            else:
                public_prompt = request.prompt
                public_choices = request.choices
            await self._emit_delivery_event(
                HITLRequestEvent(
                    room_id=room_id,
                    run_id=canonical_run_id,
                    request_id=request.request_id,
                    message_id=data["message_id"],
                    source=source,
                    prompt=public_prompt,
                    prompt_type=prompt_type,
                    choices=public_choices,
                    agent_id=None if canonical_run_id else request.agent_id,
                    agent_name=None if canonical_run_id else request.agent_name,
                    agent_label=public_agent_label if canonical_run_id else None,
                    source_step_id=(
                        None if canonical_run_id else request.source_step_id
                    ),
                    interaction_id=request.interaction_id,
                    interaction_status=(
                        None
                        if canonical_run_id
                        else getattr(
                            request.interaction_status,
                            "value",
                            request.interaction_status,
                        )
                    ),
                    interaction_version=(
                        None if canonical_run_id else request.interaction_version
                    ),
                    application_status=(
                        None if canonical_run_id else request.application_status
                    ),
                    question_count=request.question_count,
                    question_index=request.question_index,
                    related_message_id=(
                        None if canonical_run_id else data["related_message_id"]
                    ),
                    related_user_message_id=(
                        request.user_message_id if canonical_run_id else None
                    ),
                    client_request_id=data.get("client_request_id"),
                )
            )
            return

        status_map = {
            HITLEventType.INPUT_RECEIVED: HITLStatus.RESPONDED.value,
            HITLEventType.INPUT_EXPIRED: HITLStatus.EXPIRED.value,
            HITLEventType.INPUT_CANCELED: HITLStatus.CANCELED.value,
            HITLEventType.ERROR: "error",
        }
        await self._emit_delivery_event(
            HITLResolvedEvent(
                room_id=room_id,
                run_id=canonical_run_id,
                request_id=request.request_id,
                message_id=data["message_id"],
                source=source,
                status=status_map.get(event_type, request_status),
                interaction_id=request.interaction_id,
                interaction_status=(
                    None
                    if canonical_run_id
                    else getattr(
                        request.interaction_status, "value", request.interaction_status
                    )
                ),
                interaction_version=(
                    None if canonical_run_id else request.interaction_version
                ),
                application_status=(
                    None if canonical_run_id else request.application_status
                ),
                question_count=request.question_count,
                question_index=request.question_index,
                related_message_id=(
                    None if canonical_run_id else data["related_message_id"]
                ),
                related_user_message_id=(
                    request.user_message_id if canonical_run_id else None
                ),
                error_message=None if canonical_run_id else error,
                client_request_id=data.get("client_request_id"),
            )
        )

    async def _emit_delivery_event(
        self, event: HITLRequestEvent | HITLResolvedEvent
    ) -> None:
        result = self.delivery.emit(event)
        if inspect.isawaitable(result):
            await result


class BoundHITLServiceProxy:
    def __init__(self) -> None:
        self._service: HITLService | None = None

    def bind(self, service: HITLService) -> None:
        self._service = service

    def _require_service(self) -> HITLService:
        if self._service is None:
            raise RuntimeError("HITLService has not been bound at startup")
        return self._service

    def __getattr__(self, name: str) -> Any:
        return getattr(self._require_service(), name)
