"""Durable prepared-invocation reconstruction from generic Run snapshots."""

from __future__ import annotations

from typing import Protocol

from ..models import ToolInvocation
from ..ports import OrchestratorRunStore
from .errors import RecoverableCheckpointError
from .models import AgentCallLedgerRecord, PreparedInvocationSnapshot
from .ports import AgentToolBindingStore
from .resources import freeze_call_manifest


class AcceptedCallRecoveryRuntime(Protocol):
    async def recover_dispatch(
        self,
        record: AgentCallLedgerRecord,
        invocation: ToolInvocation,
    ) -> None: ...


class RunPreparedInvocationSnapshotReader:
    def __init__(
        self,
        *,
        run_store: OrchestratorRunStore,
        binding_store: AgentToolBindingStore,
    ) -> None:
        self.run_store = run_store
        self.binding_store = binding_store

    async def read_prepared(
        self, invocation: ToolInvocation
    ) -> PreparedInvocationSnapshot | None:
        run = await self.run_store.load(invocation.run_id)
        if run is None or run.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }:
            return None
        binding = await self.binding_store.load(invocation.tool.binding.binding_id)
        if (
            binding is None
            or binding.run_id != run.run_id
            or binding.binding_digest != invocation.tool.binding.binding_digest
            or binding.tool_name != invocation.tool.definition.name
            or binding.room_id != run.room_id
            or binding.room_epoch != run.request.room_epoch
        ):
            return None
        manifest = freeze_call_manifest(
            arguments=invocation.arguments,
            run_manifest=run.resource_manifest,
            binding=binding,
            source_room_id=run.room_id,
            source_room_epoch=run.request.room_epoch,
            root_context_source_message_id=run.request.user_message_id,
        )
        return PreparedInvocationSnapshot(
            run_id=run.run_id,
            invocation_id=invocation.invocation_id,
            room_id=run.room_id,
            room_epoch=run.request.room_epoch,
            requesting_subject_id=run.request.requesting_subject_id,
            binding=binding,
            resource_manifest=manifest,
        )

    async def read_invocation(
        self, *, run_id: str, invocation_id: str
    ) -> ToolInvocation | None:
        """Reload the exact durable invocation for background dispatch recovery."""

        run = await self.run_store.load(run_id)
        if run is None or run.status in {
            "canceling",
            "completed",
            "failed",
            "canceled",
            "budget_exhausted",
        }:
            return None
        matches = [
            entry.invocation
            for batch in run.tool_batches
            for entry in batch.entries
            if entry.call_id == invocation_id and entry.invocation is not None
        ]
        if len(matches) != 1:
            return None
        return matches[0]


class RunBackedDispatchRecovery:
    """Production recovery adapter from durable Run invocation to Tool runtime."""

    def __init__(
        self,
        *,
        prepared_reader: RunPreparedInvocationSnapshotReader,
        runtime: AcceptedCallRecoveryRuntime,
    ) -> None:
        self.prepared_reader = prepared_reader
        self.runtime = runtime

    async def __call__(self, record: AgentCallLedgerRecord) -> None:
        invocation = await self.prepared_reader.read_invocation(
            run_id=record.run_id,
            invocation_id=record.invocation_id,
        )
        if invocation is None:
            raise RecoverableCheckpointError(
                "durable invocation is unavailable for call recovery"
            )
        await self.runtime.recover_dispatch(record, invocation)
