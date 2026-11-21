"""Narrow injected ports for the orchestrator contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from .models import (
    AssistantMessage,
    CancellationCause,
    CompactionResult,
    ModelStreamEvent,
    ModelTurnRequest,
    OrchestratorRunState,
    PreparedResourceRef,
    ProjectionIntent,
    ResolvedTool,
    SessionNotice,
    ToolAcceptance,
    ToolDefinition,
    ToolExecutionOutcome,
    ToolInvocation,
    ToolResult,
)

if TYPE_CHECKING:
    # Annotation only: ``run_state`` and ``kernel`` import this module.
    from .budget import BudgetPolicy
    from .kernel import IDFactory
    from .run_state import (
        KernelLifecycle,
        KernelRunResult,
        SupervisorHITLPort,
        SystemClock,
    )

StoreOutcome = Literal["accepted", "replayed", "conflict", "error"]


class InvalidParkedInteractionTarget(RuntimeError):
    """Publication was rejected without accepting a public interaction."""


@dataclass(frozen=True, slots=True)
class ParkedInteractionCloseout:
    """Durable exact-child winner returned by parked closeout."""

    result: ToolResult
    local_cancellation_won: bool


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


class AgentCallDenied(Exception):
    """``accept`` refused a new Agent call because a check rule denies it.

    It carries the id of the rule that decided and no other text, so nothing
    an owner wrote and no exception detail reaches the model or the client.
    """

    def __init__(self, rule_id: str | None) -> None:
        super().__init__("agent call denied by a check rule")
        self.rule_id = rule_id


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
    ) -> ParkedInteractionCloseout: ...


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


class ModelLoop(Protocol):
    """One model turn of a Run, as the Run lifecycle sees it."""

    async def step(
        self,
        run: OrchestratorRunState,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None,
    ) -> KernelRunResult | None:
        """Take one model turn; None means take another loop iteration."""
        ...

    async def resume_overflow(
        self,
        run: OrchestratorRunState,
        notice: SessionNotice,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None,
    ) -> OrchestratorRunState | KernelRunResult:
        """Finish a context-overflow closure and compaction after a lost ack."""
        ...


class ModelLoopHost(Protocol):
    """The Run lifecycle members the model loop reads at call time."""

    clock: SystemClock
    id_factory: IDFactory
    tool_catalog: ToolCatalog
    budget_policy: BudgetPolicy
    supervisor_hitl: SupervisorHITLPort | None
    model_runtime: ModelRuntime
    # Opaque to the lifecycle side; the model loop's ``ContextCompiler``.
    context_compiler: object
    context_compactor: ContextCompactor | None
    public_secret_values: tuple[str, ...]
    artifact_metadata_reader: (
        Callable[..., Awaitable[PreparedResourceRef | None]] | None
    )

    async def _load(self, run_id: str) -> OrchestratorRunState: ...

    async def _checkpoint(
        self,
        run: OrchestratorRunState,
        *,
        updates: dict[str, object],
        command_id: str,
    ) -> OrchestratorRunState: ...

    async def _emit(
        self,
        lifecycle: KernelLifecycle | None,
        event_type: str,
        run: OrchestratorRunState,
        payload: dict[str, object],
    ) -> None: ...

    async def _terminate(
        self,
        run: OrchestratorRunState,
        *,
        status: Literal["failed", "canceled", "budget_exhausted"],
        reason: str,
        cancellation_cause: (
            Literal[
                "user_requested", "room_closed", "shutdown", "policy", "owner_refused"
            ]
            | None
        ) = None,
    ) -> KernelRunResult: ...

    async def _close_active_attempt(
        self,
        run: OrchestratorRunState,
        *,
        disposition: Literal["error", "aborted"],
        error_summary: str | None = None,
    ) -> tuple[OrchestratorRunState, str | None]: ...

    async def _ensure_tool_batch(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState: ...

    async def _execute_tool_batch(
        self,
        run: OrchestratorRunState,
        assistant: AssistantMessage,
        signal: CancellationSignal,
        *,
        lifecycle: KernelLifecycle | None = None,
    ) -> KernelRunResult | Literal["retry", "decide"] | None: ...

    async def _finalize_canonical_answer(
        self,
        run: OrchestratorRunState,
        assistant: AssistantMessage,
        *,
        lifecycle: KernelLifecycle | None,
    ) -> KernelRunResult: ...

    def _tool_label(self, run: OrchestratorRunState, tool_name: str) -> str | None: ...

    def _stable_id(self, run: OrchestratorRunState, *parts: object) -> str: ...

    async def _record_model_event(
        self,
        run: OrchestratorRunState,
        turn_id: str,
        event: ModelStreamEvent,
        *,
        message_id: str | None = None,
    ) -> OrchestratorRunState: ...

    async def _append_assistant(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState: ...

    async def _reserve_model_retry(
        self, run: OrchestratorRunState
    ) -> OrchestratorRunState | KernelRunResult: ...


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

    async def schedule_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        next_attempt_at: datetime,
    ) -> RunStoreResult: ...

    async def claim_recovery(
        self,
        run_id: str,
        *,
        expected_state_version: int,
        owner_id: str,
        lease_expires_at: datetime,
        claimed_at: datetime,
        allow_scheduled: bool = False,
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
