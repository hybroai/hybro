"""Run checkpoint reader used by the A2A process managers."""

from __future__ import annotations

from hashlib import sha256

from ..models import ToolResult
from ..persistence import TERMINAL_RUN_STATUSES
from ..ports import OrchestratorRunStore


class RunCheckpointReader:
    """Read-only generic Run proof used by A2A process managers."""

    def __init__(self, run_store: OrchestratorRunStore) -> None:
        self.run_store = run_store

    async def is_acceptance_checkpointed(
        self,
        run_id: str,
        invocation_id: str,
        acceptance_id: str,
        idempotency_key: str,
        binding_digest: str,
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and entry.acceptance is not None
            and entry.acceptance.acceptance_id == acceptance_id
            and entry.acceptance.idempotency_key == idempotency_key
            and entry.invocation.idempotency_key == idempotency_key
            and entry.invocation.tool.binding.binding_digest == binding_digest
            for batch in run.tool_batches
            for entry in batch.entries
        )

    async def is_suspension_checkpointed(
        self, run_id: str, invocation_id: str, status: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and entry.state == status
            for batch in run.tool_batches
            for entry in batch.entries
        )

    async def is_run_terminal(self, run_id: str) -> bool:
        run = await self.run_store.load(run_id)
        return run is not None and run.status in TERMINAL_RUN_STATUSES

    async def is_outcome_checkpointed(
        self, run_id: str, invocation_id: str, outcome_digest: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        for batch in run.tool_batches:
            for entry in batch.entries:
                result = entry.buffered_terminal_result
                if (
                    entry.invocation is not None
                    and entry.invocation.invocation_id == invocation_id
                    and isinstance(result, ToolResult)
                    and sha256(result.model_dump_json().encode()).hexdigest()
                    == outcome_digest
                ):
                    return True
        return False

    async def has_processed_observation(
        self, run_id: str, invocation_id: str, observation_id: str
    ) -> bool:
        run = await self.run_store.load(run_id)
        if run is None:
            return False
        return any(
            entry.invocation is not None
            and entry.invocation.invocation_id == invocation_id
            and observation_id in entry.processed_observation_ids
            for batch in run.tool_batches
            for entry in batch.entries
        )
