"""Narrow injected ports for the orchestrator contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from .models import (
    CancellationCause,
    CompactionResult,
    ModelStreamEvent,
    ModelTurnRequest,
    OrchestratorEvent,
    OrchestratorRunState,
    ProjectionIntent,
    ResolvedTool,
    ToolAcceptance,
    ToolDefinition,
    ToolExecutionOutcome,
    ToolInvocation,
)

StoreOutcome = Literal["accepted", "replayed", "conflict", "error"]


class RunStoreResult(Protocol):
    @property
    def outcome(self) -> StoreOutcome: ...

    @property
    def run(self) -> OrchestratorRunState | None: ...


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


class ModelRuntime(Protocol):
    def stream_turn(
        self,
        request: ModelTurnRequest,
        *,
        signal: CancellationSignal,
    ) -> AsyncIterator[ModelStreamEvent]: ...


class ToolCatalog(Protocol):
    def list_tools(self, run: OrchestratorRunState) -> list[ToolDefinition]: ...

    def resolve(self, run: OrchestratorRunState, tool_name: str) -> ResolvedTool: ...


class ToolRuntime(Protocol):
    async def accept(self, invocation: ToolInvocation) -> ToolAcceptance: ...

    async def execute(
        self,
        invocation: ToolInvocation,
        acceptance: ToolAcceptance,
        *,
        signal: CancellationSignal,
    ) -> ToolExecutionOutcome: ...

    async def dispatch_model_reply(
        self,
        invocation: ToolInvocation,
        *,
        parent_call_record_id: str,
        interaction_fingerprint: str | None,
        signal: CancellationSignal,
    ) -> ToolExecutionOutcome: ...

    async def publish_parked_interaction(
        self,
        *,
        call_record_id: str,
        interaction_id: str,
    ) -> None: ...

    async def abandon_parked_interaction(
        self,
        *,
        call_record_id: str,
        interaction_id: str,
        terminal_state: str,
    ) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IDFactory(Protocol):
    def new_id(self, prefix: str) -> str: ...


class ProjectionDriver(Protocol):
    async def settle(self, run_id: str) -> OrchestratorRunState: ...


class ContextCompactor(Protocol):
    async def compact(
        self,
        messages: list[object],
        *,
        turn_id: str,
        remaining_provider_retries: int,
        deadline_at: datetime,
        on_event: Callable[[ModelStreamEvent], Awaitable[None]],
        signal: CancellationSignal,
    ) -> CompactionResult: ...


class OrchestratorRunStore(Protocol):
    async def create(
        self, run: OrchestratorRunState, *, command_id: str
    ) -> RunStoreResult: ...

    async def load(self, run_id: str) -> OrchestratorRunState | None: ...

    async def cas_mutate(
        self,
        run: OrchestratorRunState,
        *,
        expected_state_version: int,
        command_id: str,
    ) -> RunStoreResult: ...

    async def request_cancellation(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        command_id: str,
        cause: CancellationCause,
        requested_at: datetime,
    ) -> RunStoreResult: ...

    async def repair_canceling_recovery(self, *, limit: int) -> int: ...

    async def claim_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
    ) -> RunStoreResult: ...

    async def renew_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> RunStoreResult: ...

    async def release_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime | None,
        failure_count: int = 0,
        quarantined_at: datetime | None = None,
        quarantine_reason: Literal["terminal_invariant_conflict"] | None = None,
    ) -> RunStoreResult: ...

    async def list_due_runs(
        self, *, due_at: datetime, limit: int
    ) -> list[OrchestratorRunState]: ...

    async def claim_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> RunStoreResult: ...

    async def complete_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
    ) -> RunStoreResult: ...

    async def block_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        reason: str,
    ) -> RunStoreResult: ...

    async def release_projection_intent(
        self,
        run_id: str,
        intent_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> RunStoreResult: ...

    async def list_due_projection_intents(
        self, *, due_at: datetime, limit: int
    ) -> list[tuple[str, ProjectionIntent]]: ...


class OrchestratorEventStore(Protocol):
    async def append(self, event: OrchestratorEvent) -> StoreOutcome: ...

    async def read(
        self, run_id: str, *, after_sequence: int = 0
    ) -> list[OrchestratorEvent]: ...


class InvocationCheckpointReader(Protocol):
    async def is_acceptance_checkpointed(
        self,
        run_id: str,
        invocation_id: str,
        acceptance_id: str,
        idempotency_key: str,
        binding_digest: str,
    ) -> bool: ...

    async def is_suspension_checkpointed(
        self,
        run_id: str,
        invocation_id: str,
        status: str,
    ) -> bool: ...


class InvocationOutcomeCheckpointReader(Protocol):
    async def is_run_terminal(self, run_id: str) -> bool: ...

    async def is_outcome_checkpointed(
        self,
        run_id: str,
        invocation_id: str,
        outcome_digest: str,
    ) -> bool: ...

    async def has_processed_observation(
        self,
        run_id: str,
        invocation_id: str,
        observation_id: str,
    ) -> bool: ...


class EventProjector(Protocol):
    async def project(self, intent: ProjectionIntent) -> StoreOutcome: ...
