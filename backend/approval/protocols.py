"""Ports describing Approval's own dependencies: HITL request and lifecycle
persistence, owning-run terminalization, and event delivery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

from common.dto import HITLRequestEvent, HITLResolvedEvent


class HITLPersistencePort(Protocol):
    async def count_hitl_requests_for_message(self, message_id: str) -> int: ...
    async def create_hitl_request(self, doc: dict[str, Any]) -> bool: ...
    async def claim_hitl_request(
        self,
        request_id: str,
        *,
        status: str,
        claim_id: str | None,
        user_input: str,
        responded_at: Any,
        responded_by_user_id: str,
        answer_digest: str | None = None,
        answered_at: Any | None = None,
        answered_by_client_id: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def get_hitl_request(self, request_id: str) -> dict[str, Any] | None: ...
    async def update_hitl_request(self, request_id: str, **updates: Any) -> bool: ...
    async def fenced_update_hitl_request(
        self,
        request_id: str,
        claim_id: str,
        *update_docs: dict[str, Any],
        **updates: Any,
    ) -> bool: ...
    async def get_pending_hitl_requests(
        self, conversation_id: str
    ) -> list[dict[str, Any]]: ...
    async def get_pending_hitl_requests_strict(
        self, conversation_id: str
    ) -> list[dict[str, Any]]: ...
    async def get_pending_hitl_requests_for_message(
        self, message_id: str
    ) -> list[dict[str, Any]]: ...
    async def update_agent_message_task_state(
        self, message_id: str, state: str
    ) -> None: ...
    async def persist_hitl_request_id_on_message(
        self,
        message_id: str,
        request_id: str | None,
    ) -> bool: ...
    async def find_pending_hitl_request_for_agent_message(
        self,
        *,
        conversation_id: str,
        display_message_id: str | None,
        continuation_message_id: str | None,
        agent_id: str | None,
        a2a_task_id: str | None,
        a2a_context_id: str | None,
    ) -> dict[str, Any] | None: ...
    async def create_or_reuse_pending_hitl_request(
        self,
        request_data: dict[str, Any],
    ) -> tuple[dict[str, Any], bool] | None: ...
    async def claim_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> dict[str, Any] | None: ...
    async def complete_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> bool: ...
    async def release_hitl_open_projection(
        self, request_id: str, claim_id: str
    ) -> bool: ...
    async def persist_pending_hitl_on_agent_message(
        self,
        message_id: str,
        *,
        request_id: str,
        prompt: str,
        prompt_type: Any,
        choices: list[str] | None,
        a2a_task_id: str | None,
        a2a_context_id: str | None,
        interaction_id: str,
        question_count: int,
        question_index: int,
        expected_request_id: str | None = None,
        projection_at: datetime | None = None,
    ) -> bool: ...
    async def persist_hitl_user_answer(
        self, message_id: str, user_input: str | None
    ) -> None: ...
    async def persist_hitl_interaction_metadata(
        self,
        message_id: str,
        *,
        interaction_id: str | None,
        question_count: int | None,
        question_index: int | None,
    ) -> None: ...
    async def get_room_agent_message_by_message_id(
        self, message_id: str
    ) -> Any | None: ...
    async def get_room_user_message_by_message_id(
        self, message_id: str
    ) -> Any | None: ...
    async def resolve_client_request_id_for_message_id(
        self, message_id: str
    ) -> str | None: ...
    async def reset_last_notified_state(self, message_id: str) -> None: ...
    async def cas_update_hitl_request(
        self,
        request_id: str,
        *,
        expected_status: str,
        **updates: Any,
    ) -> bool: ...
    async def cas_update_hitl_request_strict(
        self,
        request_id: str,
        *,
        expected_status: str,
        **updates: Any,
    ) -> bool: ...


class HITLLifecyclePersistencePort(Protocol):
    async def materialize_interaction(
        self, interaction_data: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def attach_interaction_request(
        self,
        interaction_id: str,
        *,
        request_id: str,
        required: bool,
        expires_at: Any,
        question_index: int,
    ) -> dict[str, Any] | None: ...
    async def get_interaction_strict(
        self, interaction_id: str
    ) -> dict[str, Any] | None: ...
    async def get_interaction_for_request_strict(
        self, request_id: str
    ) -> dict[str, Any] | None: ...
    async def iter_materializing_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def record_interaction_answer(
        self,
        interaction_id: str,
        *,
        request_id: str,
        answer_digest: str,
    ) -> dict[str, Any] | None: ...
    async def claim_interaction_application(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...
    async def renew_interaction_application(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool: ...
    async def resume_uncertain_interaction(
        self, interaction_id: str, *, claim_id: str
    ) -> dict[str, Any] | None: ...
    async def claim_run_answer_projection(
        self,
        interaction_id: str,
        *,
        application_revision: int,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...
    async def renew_run_answer_projection(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool: ...
    async def mark_run_answer_projection(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def mark_interaction_application_state(
        self,
        interaction_id: str,
        *,
        claim_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def terminalize_interaction(
        self,
        interaction_id: str,
        *,
        expected_statuses: list[str],
        status: str,
        reason: str,
        member_status: str | None = None,
        owning_run_terminal_status: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def mark_interaction_terminal_reconciled(
        self, interaction_id: str, *, version: int
    ) -> bool: ...
    async def create_resume_command(
        self, command_data: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def get_resume_command_for_interaction_strict(
        self, interaction_id: str, application_revision: int
    ) -> dict[str, Any] | None: ...
    async def claim_resume_command(
        self,
        command_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...
    async def get_resume_command_strict(
        self, command_id: str
    ) -> dict[str, Any] | None: ...
    async def renew_resume_command(
        self,
        command_id: str,
        *,
        claim_id: str,
        lease_seconds: int,
    ) -> bool: ...
    async def reclaim_stale_resume_command(
        self,
        command_id: str,
        *,
        observed_claim_id: str | None,
        observed_version: int,
        observed_lease_expires_at: Any,
        now: Any,
        status: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any] | None: ...
    async def mark_resume_command_state(
        self,
        command_id: str,
        *,
        claim_id: str | None,
        expected_statuses: list[str],
        status: str,
        response_snapshot: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any] | None: ...
    async def mark_resume_command_aggregate_applied(self, command_id: str) -> bool: ...
    def iter_due_interactions(
        self, now: Any, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_stale_applications(
        self, now: Any, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_run_busy_applications(
        self, run_id: str, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_due_resume_commands(
        self, now: Any, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_active_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_unreconciled_terminal_interactions(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    def iter_unreconciled_terminal_requests(
        self, *, limit: int = 100
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def ensure_indexes(self) -> None: ...


class HITLTerminalLifecyclePort(Protocol):
    async def terminalize_owning_run(
        self,
        request: Any,
        *,
        terminal_status: str,
        reason: str,
    ) -> None: ...


class HITLDeliveryPort(Protocol):
    async def emit(self, event: HITLRequestEvent | HITLResolvedEvent) -> None: ...


__all__ = [
    "HITLDeliveryPort",
    "HITLLifecyclePersistencePort",
    "HITLPersistencePort",
    "HITLTerminalLifecyclePort",
]
