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
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from approval.exceptions import (
    ContinuationLostError,
    HITLConflictError,
    HITLNotFoundError,
    HITLRequestProjectionError,
    HITLRoomMismatchError,
    HITLRoutingFailedError,
)
from approval.validation import (
    HITLAggregateCorruptionError,
    deterministic_request_id,
    validate_exact_member_inventory,
    validate_route_classifications,
)
from common.dto import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
    HITLRequestEvent,
    HITLResolvedEvent,
    HITLRouteSnapshot,
)
from common.observability import events as E
from common.public_prompt import (
    GENERIC_AGENT_INPUT_PROMPT,
    concrete_agent_input_prompt,
)
from common.run_status import TERMINAL_RUN_STATUSES
from common.utils.a2a_helpers import (
    is_authoritative_a2a_id as _is_authoritative_a2a_id,
)
from common.utils.logger import get_logger
from common.utils.time import utcnow
from models.hitl import (
    HITLEventType,
    HITLInteraction,
    HITLInteractionStatus,
    HITLPromptType,
    HITLRequest,
    HITLStatus,
)

if TYPE_CHECKING:
    from approval.protocols import (
        HITLDeliveryPort,
        HITLLifecyclePersistencePort,
        HITLPersistencePort,
        HITLTerminalLifecyclePort,
    )

logger = get_logger(__name__)


def _room_write_fenced(method):
    @wraps(method)
    async def fenced(self, *args, **kwargs):
        conversation_id = kwargs.get("conversation_id") or (args[0] if args else None)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise TypeError("conversation_id is required")
        if self._room_files is None:
            return await method(self, *args, **kwargs)
        async with self._room_files.write_lease(
            conversation_id, f"hitl:{method.__name__}"
        ):
            return await method(self, *args, **kwargs)

    return fenced


def _normalized_prompt(prompt: str | None) -> str:
    return " ".join(str(prompt or "").split()).strip().casefold()


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
        terminal_lifecycle=None,
        lifecycle=None,
        application=None,
        room_files=None,
        canonical_control_publisher=None,
        canonical_run_state_reader=None,
        supervisor_resume=None,
        canonical_cancellation_requester=None,
        public_secret_values=(),
        approval_deadline_hours: float | None = None,
        public_text_sanitizer: Callable[..., str] | None = None,
    ) -> None:
        self._persistence: HITLPersistencePort | None = None
        self._delivery: HITLDeliveryPort | None = None
        self._terminal_lifecycle: HITLTerminalLifecyclePort | None = terminal_lifecycle
        self._lifecycle: HITLLifecyclePersistencePort | None = lifecycle
        self._application = application
        self._room_files = room_files
        self._canonical_control_publisher = canonical_control_publisher
        self._canonical_run_state_reader = canonical_run_state_reader
        self._supervisor_resume = supervisor_resume
        self._canonical_cancellation_requester = canonical_cancellation_requester
        self._public_secret_values = tuple(
            value for value in public_secret_values if isinstance(value, str) and value
        )
        # None means interactions wait for the owner without a deadline.
        self._approval_deadline_hours = approval_deadline_hours
        self._public_text_sanitizer = public_text_sanitizer

    def _require_public_text_sanitizer(self) -> Callable[..., str]:
        if self._public_text_sanitizer is None:
            raise RuntimeError("HITL public text sanitizer has not been bound")
        return self._public_text_sanitizer

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

    # ------------------------------------------------------------------
    # Create HITL request
    # ------------------------------------------------------------------

    @_room_write_fenced
    async def request_interaction(
        self,
        *,
        conversation_id: str,
        user_message_id: str,
        interaction_id: str,
        application_route: HITLApplicationRoute,
        public_source: HITLPublicSource,
        evidence_origin: HITLEvidenceOrigin,
        route_snapshot: HITLRouteSnapshot,
        questions: list[dict[str, Any]],
        orchestration_run_id: str | None = None,
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
        shared_expiry = (
            utcnow() + timedelta(hours=self._approval_deadline_hours)
            if self._approval_deadline_hours is not None
            else None
        )
        interaction = HITLInteraction(
            schema_version=3,
            interaction_id=interaction_id,
            conversation_id=conversation_id,
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
                conversation_id=interaction["room_id"],
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
        conversation_id: str,
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
                logger.emit(
                    E.HITL_REQUEST_REJECTED,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    run_id=orchestration_run_id,
                    public_source=source,
                    reason=(
                        "invalid_interactive_prompt"
                        if concrete_prompt is None
                        else "invalid_a2a_continuation"
                    ),
                    agent_id=agent_id,
                )
                return None
            prompt = concrete_prompt
            prompt_type = HITLPromptType(prompt_type)
        resolved_display_message_id = display_message_id
        if source == "agent" and resolved_display_message_id is None:
            resolved_display_message_id = continuation_message_id

        if source == "agent" and not resolved_display_message_id:
            logger.emit(
                E.HITL_REQUEST_REJECTED,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                run_id=orchestration_run_id,
                public_source=source,
                reason="display_message_id_missing",
                user_message_id=user_message_id,
                agent_id=agent_id,
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
                "room_id": conversation_id,
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
                logger.emit(
                    E.HITL_REQUEST_REJECTED,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    run_id=orchestration_run_id,
                    public_source=source,
                    reason="request_identity_mismatched",
                    hitl_request_id=request_id,
                )
                return None

        if continuation_message_id:
            # Count one complete interaction as one clarification round.
            if question_index == 0 and existing_request_doc is None:
                existing = await self.persistence.count_hitl_requests_for_message(
                    continuation_message_id
                )
                if existing >= MAX_HITL_ROUNDS:
                    logger.emit(
                        E.HITL_ROUND_LIMIT_REACHED,
                        conversation_id=conversation_id,
                        interaction_id=interaction_id,
                        run_id=orchestration_run_id,
                        continuation_message_id=continuation_message_id,
                        round_count=existing,
                        max_round_count=MAX_HITL_ROUNDS,
                    )
                    return None

        request_data = dict(
            schema_version=3,
            conversation_id=conversation_id,
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
            expires_at=expires_at,
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
                logger.emit(
                    E.HITL_REQUEST_PERSIST_FAILED,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    run_id=orchestration_run_id,
                    hitl_request_id=request.request_id,
                    public_source=source,
                    display_message_id=request.display_message_id,
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
                    logger.emit(
                        E.HITL_REQUEST_REJECTED,
                        conversation_id=conversation_id,
                        interaction_id=interaction_id,
                        run_id=orchestration_run_id,
                        public_source=source,
                        reason="reused_request_mismatched",
                        hitl_request_id=request_id_to_repair,
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
                        logger.emit(
                            E.REUSED_HITL_REQUEST_REPAIR_FAILED,
                            conversation_id=conversation_id,
                            interaction_id=interaction_id,
                            run_id=orchestration_run_id,
                            hitl_request_id=request_id_to_repair,
                        )
                        return None
                    persisted_doc.update(repair_update)

            if not _is_actionable_agent_hitl_document(persisted_doc):
                # Keep malformed persisted data non-actionable without silently
                # terminalizing it here. Pending hydration filters it. Any
                # cancellation/failure must use
                # the lifecycle reconciler rather than bypassing owning-run cleanup.
                logger.emit(
                    E.HITL_REQUEST_REJECTED,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    run_id=orchestration_run_id,
                    public_source=source,
                    reason="persisted_request_malformed",
                    hitl_request_id=persisted_doc.get("request_id"),
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
                    logger.emit(
                        E.HITL_REQUEST_PERSIST_FAILED,
                        conversation_id=conversation_id,
                        interaction_id=interaction_id,
                        run_id=orchestration_run_id,
                        hitl_request_id=request.request_id,
                        public_source=source,
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
                    conversation_id=request.conversation_id,
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
        conversation_id: str,
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
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            answers=answers,
            user_id=user_id,
            client_request_id=client_request_id,
        )

    async def _project_completed_hitl_display(
        self,
        *,
        display_message_id: Any,
        user_input: Any,
        request_id: str | None = None,
        conversation_id: str | None = None,
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
        except Exception as exc:
            logger.emit(
                E.HITL_DISPLAY_PROJECTION_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                hitl_request_id=request_id,
                display_message_id=display_message_id,
            )
            return False
        if not answer_projected or not state_projected:
            logger.emit(
                E.HITL_DISPLAY_PROJECTION_FAILED,
                conversation_id=conversation_id,
                hitl_request_id=request_id,
                display_message_id=display_message_id,
                answer_projected=bool(answer_projected),
                state_projected=bool(state_projected),
            )
            return False
        return True

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

    async def get_pending_requests(self, conversation_id: str) -> list[HITLRequest]:
        """Get all pending HITL requests for a room (SSE reconnect catch-up)."""
        strict_reader = getattr(
            self.persistence, "get_pending_hitl_requests_strict", None
        )
        if callable(strict_reader) and inspect.iscoroutinefunction(strict_reader):
            docs = await strict_reader(conversation_id)
        else:
            docs = await self.persistence.get_pending_hitl_requests(conversation_id)
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

    async def _reconcile_terminal_members(
        self,
        members: list[tuple[dict[str, Any], HITLRequest]],
        *,
        event_type: HITLEventType,
    ) -> None:
        errors: list[Exception] = []
        for row, member in members:
            if row.get("cancellation_reconciled") is True:
                continue
            try:
                await self._emit_hitl_event(
                    conversation_id=member.conversation_id,
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

    async def cancel_interaction_by_user(
        self,
        interaction_id: str,
        conversation_id: str,
        *,
        expected_version: int,
    ) -> int:
        if self._lifecycle is None:
            raise HITLRoutingFailedError("HITL lifecycle persistence is required")
        interaction = await self._lifecycle.get_interaction_strict(interaction_id)
        if interaction is None:
            raise HITLNotFoundError("HITL interaction not found")
        if interaction.get("room_id") != conversation_id:
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
            await self.cancel_request(request_ids[0], conversation_id=conversation_id)
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
        conversation_id: str | None = None,
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
        if conversation_id is not None and request.conversation_id != conversation_id:
            raise HITLRoomMismatchError("Room mismatch")
        if request.status == HITLStatus.CANCELED:
            await self._reconcile_terminal_request(
                request, event_type=HITLEventType.INPUT_CANCELED
            )
            return
        if request.status not in {
            HITLStatus.PENDING,
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
            except Exception as exc:
                logger.emit(
                    E.CLIENT_REQUEST_ID_RESOLUTION_FAILED,
                    exc=exc,
                    message_id=user_message_id,
                    lookup="user_message",
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
                except Exception as exc:
                    logger.emit(
                        E.CLIENT_REQUEST_ID_RESOLUTION_FAILED,
                        exc=exc,
                        message_id=message_id,
                        lookup="fallback",
                    )
        return None

    async def canonical_run_allows_resume(self, interaction: dict[str, Any]) -> bool:
        state = await self._canonical_run_state(interaction.get("orchestration_run_id"))
        return state is None or state not in {"canceling", *TERMINAL_RUN_STATUSES}

    async def _is_canonical_run(self, run_id: object) -> bool:
        return await self._canonical_run_state(run_id) is not None

    async def _canonical_run_state(self, run_id: object) -> str | None:
        """Return the orchestrator Run status, or None when no Run owns run_id."""
        if (
            not isinstance(run_id, str)
            or not run_id
            or self._canonical_run_state_reader is None
        ):
            return None
        try:
            state = self._canonical_run_state_reader(run_id)
            if inspect.isawaitable(state):
                state = await state
        except Exception as exc:
            logger.emit(E.HITL_RUN_STATE_READ_FAILED, exc=exc, run_id=run_id)
            return None
        return None if state == "missing" else state

    async def _emit_hitl_event(
        self,
        conversation_id: str,
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
                sanitize_public_text = self._require_public_text_sanitizer()
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
                    conversation_id=conversation_id,
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
                conversation_id=conversation_id,
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
