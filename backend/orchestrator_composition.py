"""Production composition root for the orchestrator runtime (dark launch).

This module assembles the full ``execution.orchestrator`` runtime from Mongo
stores, typed settings, and the existing product services. It is imported only
by ``container.py`` (the composition root) and tests; no route, facade, or job
may reach it until step 7 wires dual-routing ingress.

Construction is failure-isolated at the adapter boundary: missing model routes,
missing prompt assets, or invalid profile parameters raise
``OrchestratorCompositionError`` so ``container.py`` can log and continue
serving the legacy product. Programming errors (broken wiring, wrong types,
import failures) are intentionally *not* swallowed here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from a2a_adapter.client_facade import (
    cancel_remote_task as sdk_cancel_remote_task,
)
from a2a_adapter.client_facade import (
    fetch_agent_card_with_fallback as sdk_fetch_agent_card,
)
from a2a_adapter.client_facade import send_message as sdk_send_message
from a2a_adapter.client_facade import stream_message as sdk_stream_message
from a2a_adapter.orchestrator_direct_client import OrchestratorDirectA2AClient
from a2a_adapter.remote_task import fetch_remote_task as sdk_fetch_remote_task
from common.utils.logger import get_logger
from dal.orchestrator.artifacts import GuardedRoomFileArtifactWriter
from dal.orchestrator.event_store import MongoOrchestratorEventStore
from dal.orchestrator.hitl import MongoHITLApplicationStore
from dal.orchestrator.projection import (
    MongoAppendEventProjector,
    MongoFinalMessageProjector,
)
from dal.orchestrator.run_store import MongoOrchestratorRunStore
from dal.orchestrator.stores import (
    MongoAgentCallLedgerStore,
    MongoAgentToolBindingStore,
    MongoObservationConflictStore,
    MongoObservationInboxStore,
)
from execution.adapters.agent_candidates import AgentServiceCandidateSource
from execution.adapters.authorization import MembershipAuthorizationRefresh
from execution.adapters.hitl import DurableHITLApplicationPort
from execution.adapters.profiles import (
    OrchestratorProfileResolutionError,
    OrchestratorProfileResolver,
    PromptAssetRegistry,
)
from execution.adapters.resources import RoomFilesResourceMaterializer
from execution.adapters.session_host import RoomSessionHost
from execution.orchestrator.a2a_runtime.cancellation import A2ACancellationCoordinator
from execution.orchestrator.a2a_runtime.catalog import FrozenToolCatalog
from execution.orchestrator.a2a_runtime.catalog_assembler import (
    AgentToolCatalogAssembler,
)
from execution.orchestrator.a2a_runtime.dispatch import DirectA2ADispatchAdapter
from execution.orchestrator.a2a_runtime.errors import (
    AgentCardContractError,
    RecoverableAdapterError,
    RecoverableTransportError,
)
from execution.orchestrator.a2a_runtime.hitl import A2AContinuationCoordinator
from execution.orchestrator.a2a_runtime.in_memory import RunCheckpointReader
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    A2AObservationProcessor,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.preparation import (
    RunBackedDispatchRecovery,
    RunPreparedInvocationSnapshotReader,
)
from execution.orchestrator.a2a_runtime.recovery import (
    A2AArtifactRecoveryService,
    A2ACallRecoveryService,
    A2ACancellationRecoveryService,
    A2AContinuationRecoveryService,
    A2AInboxRecoveryService,
    A2ARecoveryCycle,
)
from execution.orchestrator.a2a_runtime.runtime import A2AAgentToolRuntime
from execution.orchestrator.a2a_runtime.terminal_interactions import (
    TerminalInteractionFinalizer,
)
from execution.orchestrator.budget import BudgetPolicy
from execution.orchestrator.context import ContextCompiler
from execution.orchestrator.kernel import KernelConflict, OrchestratorKernel
from execution.orchestrator.lifecycle import SessionEvent
from execution.orchestrator.model_runtime import GatewayModelRuntime
from execution.orchestrator.models import (
    FrozenToolCatalogSnapshot,
    OrchestratorProfile,
    RecoveryClaim,
)
from execution.orchestrator.profiles import UnsupportedProviderCapabilities
from execution.orchestrator.projection import (
    ProjectionListener,
    ProjectionOutboxWorker,
    SettlingProjectionDriver,
)
from execution.orchestrator.session import DefaultRunFactory, EventCancellationSignal

logger = get_logger(__name__)


class OrchestratorCompositionError(RuntimeError):
    """Adapter-level composition failure; safe to degrade the dark launch."""


_GENERIC_RECOVERY_MAX_INVARIANT_FAILURES = 3
_GENERIC_RECOVERY_BASE_BACKOFF_SECONDS = 5
_GENERIC_RECOVERY_MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True, slots=True)
class GenericRecoveryFailureDecision:
    failure_count: int
    next_attempt_at: datetime | None
    quarantined_at: datetime | None
    quarantine_reason: Literal["terminal_invariant_conflict"] | None


def _generic_recovery_failure_decision(
    claim: RecoveryClaim, exc: Exception, *, now: datetime
) -> GenericRecoveryFailureDecision:
    """Bound persistent recovery conflicts without mutating the Run winner."""

    invariant_conflict = isinstance(exc, KernelConflict)
    failure_count = claim.failure_count + 1 if invariant_conflict else 0
    if invariant_conflict and failure_count >= _GENERIC_RECOVERY_MAX_INVARIANT_FAILURES:
        return GenericRecoveryFailureDecision(
            failure_count=failure_count,
            next_attempt_at=None,
            quarantined_at=now,
            quarantine_reason="terminal_invariant_conflict",
        )
    delay = min(
        _GENERIC_RECOVERY_BASE_BACKOFF_SECONDS * (2 ** (max(failure_count, 1) - 1)),
        _GENERIC_RECOVERY_MAX_BACKOFF_SECONDS,
    )
    return GenericRecoveryFailureDecision(
        failure_count=failure_count,
        next_attempt_at=now + timedelta(seconds=delay),
        quarantined_at=None,
        quarantine_reason=None,
    )


@dataclass(frozen=True, slots=True)
class OrchestratorRuntime:
    run_store: Any
    event_store: Any
    epoch_store: Any
    room_files: Any
    binding_store: Any
    call_ledger: Any
    observation_inbox: Any
    observation_conflicts: Any
    hitl_store: Any
    hitl_port: Any
    catalog_assembler: Any
    tool_runtime: Any
    observation_ingress: Any
    observation_processor: Any
    dispatch: Any
    profile_resolver: Any
    profiles: dict[str, OrchestratorProfile]
    session_host: Any
    observation_sink: Any
    cancellation_coordinator: Any
    continuation: Any
    hitl_delivery: Any
    kernel_factory: Callable[[FrozenToolCatalogSnapshot], OrchestratorKernel]
    projection_worker: Any
    recovery_cycle: Any
    public_secret_values: tuple[str, ...]


_RUNTIME_BINDINGS = (
    "run_store",
    "event_store",
    "epoch_store",
    "room_files",
    "binding_store",
    "call_ledger",
    "observation_inbox",
    "observation_conflicts",
    "hitl_store",
    "hitl_port",
    "catalog_assembler",
    "tool_runtime",
    "observation_ingress",
    "observation_processor",
    "dispatch",
    "profile_resolver",
    "profiles",
    "session_host",
    "observation_sink",
    "cancellation_coordinator",
    "continuation",
    "hitl_delivery",
    "kernel_factory",
    "projection_worker",
    "recovery_cycle",
    "public_secret_values",
)


def validate_orchestrator_runtime(runtime: Any) -> list[str]:
    """List missing bindings for a (possibly incomplete) composition."""
    if runtime is None:
        return ["runtime"]
    return [name for name in _RUNTIME_BINDINGS if getattr(runtime, name, None) is None]


def configured_public_secret_values(settings_obj: Any) -> tuple[str, ...]:
    """Collect configured credential values without logging or serializing them."""

    values: set[str] = set()
    for name in getattr(type(settings_obj), "model_fields", {}):
        lowered = name.lower()
        value = getattr(settings_obj, name, None)
        getter = getattr(value, "get_secret_value", None)
        if callable(getter):
            value = getter()
        if not isinstance(value, str):
            continue
        if (
            any(
                marker in lowered
                for marker in ("api_key", "secret", "token", "password", "credential")
            )
            and len(value) >= 4
        ):
            values.add(value)
        # Credentials embedded in configured DSNs are just as sensitive as
        # explicitly named secrets. urlsplit handles mongodb(+srv), redis(s),
        # AMQP, SQL, and HTTP-family schemes without maintaining a scheme list.
        if "://" in value:
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            for credential in (parsed.username, parsed.password):
                if credential is not None:
                    decoded = unquote(credential)
                    if len(decoded) >= 4:
                        values.add(decoded)
    return tuple(sorted(values, key=len, reverse=True))


async def _run_with_recovery_lease(
    *,
    run_store: Any,
    run_id: str,
    owner_id: str,
    work: Any,
    lease_duration: timedelta,
    renew_interval_seconds: float,
) -> Any:
    """Run Kernel work while periodically renewing its token-fenced lease."""

    task = asyncio.create_task(work)
    try:
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=renew_interval_seconds)
            if done:
                break
            renewed = None
            for _renew_attempt in range(4):
                current = await run_store.load(run_id)
                if current is None or current.recovery_claim.owner_id != owner_id:
                    raise RecoverableAdapterError("generic recovery lease was replaced")
                renewed = await run_store.renew_recovery(
                    run_id,
                    expected_state_version=current.state_version,
                    owner_id=owner_id,
                    lease_expires_at=datetime.now(UTC) + lease_duration,
                )
                if renewed.outcome in {"accepted", "replayed"}:
                    break
                await asyncio.sleep(0)
            if renewed is None or renewed.outcome not in {"accepted", "replayed"}:
                raise RecoverableAdapterError(
                    "generic recovery lease renewal conflicted"
                )
        return await task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


def create_orchestrator_runtime(  # noqa: C901
    *,
    mongo: Any,
    settings_obj: Any,
    llm_gateway: Any,
    model_registry: Any,
    agent_registry: Any,
    exclusion_reader: Any,
    room_ownership_reader: Any,
    epoch_store: Any,
    room_files: Any,
    observation_authenticator: Any | None = None,
    session_listener: Any | None = None,
    projection_listener: ProjectionListener | None = None,
    user_message_text_reader: Callable[[str], Any] | None = None,
    hitl_delivery: Any | None = None,
    final_message_delivery: Callable[..., Any] | None = None,
    final_message_memory_projection: Callable[[str, str], Any] | None = None,
    canonical_event_reader: Callable[[str, str], Any] | None = None,
    canonical_hitl_control: Callable[[str, str, str, list[str]], Any] | None = None,
    supervisor_hitl: Any | None = None,
) -> OrchestratorRuntime:
    """Compose the full orchestrator runtime over the registered Mongo stores.

    No Mongo or LLM calls are made during construction; this is wiring only.
    """
    public_secret_values = configured_public_secret_values(settings_obj)
    run_store = MongoOrchestratorRunStore(
        mongo.collection("orchestrator_runs").raw_collection,
        mongo.collection("orchestrator_recovery_leases").raw_collection,
    )
    # The event store is bound for the projection worker (step 6), which
    # appends durable Run events through the outbox projector.
    event_store = MongoOrchestratorEventStore(
        mongo.collection("orchestrator_run_events").raw_collection
    )
    binding_store = MongoAgentToolBindingStore(
        mongo.collection("orchestrator_agent_tool_bindings").raw_collection
    )
    call_ledger = MongoAgentCallLedgerStore(
        mongo.collection("orchestrator_agent_calls").raw_collection
    )
    observation_inbox = MongoObservationInboxStore(
        mongo.collection("orchestrator_a2a_observations").raw_collection
    )
    observation_conflicts = MongoObservationConflictStore(
        mongo.collection("orchestrator_a2a_observation_conflicts").raw_collection
    )
    hitl_store = MongoHITLApplicationStore(
        mongo.collection("orchestrator_hitl_interactions").raw_collection
    )

    profile_resolver = OrchestratorProfileResolver(
        model_registry=model_registry,
        prompt_registry=PromptAssetRegistry(),
        settings_obj=settings_obj,
    )
    try:
        profiles = {
            "fast": profile_resolver.resolve("fast"),
            "ultimate": profile_resolver.resolve("ultimate"),
        }
    except (
        OrchestratorProfileResolutionError,
        UnsupportedProviderCapabilities,
        ValueError,
    ) as exc:
        # Narrow resolution boundary only (model route, prompt asset, digest,
        # capability). Unexpected errors remain programming errors.
        raise OrchestratorCompositionError(
            f"orchestrator profile resolution failed: {exc}"
        ) from exc

    candidate_source = AgentServiceCandidateSource(
        agents=agent_registry,
        exclusion_reader=exclusion_reader,
    )
    authorization = MembershipAuthorizationRefresh(
        agents=agent_registry,
        room_ownership=room_ownership_reader,
    )
    catalog_assembler = AgentToolCatalogAssembler(
        candidate_source=candidate_source,
        binding_store=binding_store,
        room_epoch_store=epoch_store,
    )

    hitl_port = DurableHITLApplicationPort(hitl_store=hitl_store)
    terminal_finalizer = TerminalInteractionFinalizer(hitl_port)

    # External ingress stays fully rejected until step 7 wires the per-source
    # authenticators (webhook HMAC via a2a_adapter, relay identity).
    authenticator = observation_authenticator or RejectExternalIngressAuthenticator()
    observation_ingress = A2AObservationIngress(
        inbox=observation_inbox,
        conflicts=observation_conflicts,
        ledger=call_ledger,
        authenticator=authenticator,
    )

    artifact_writer = GuardedRoomFileArtifactWriter(
        room_files=room_files,
        room_epochs=epoch_store,
    )
    resources = RoomFilesResourceMaterializer(
        room_files=room_files,
        artifact_writer=artifact_writer,
        inline_artifact_reader=observation_inbox,
        context_text_reader=user_message_text_reader,
    )

    async def resolve_call_address(
        call_record_id: str,
    ) -> dict[str, Any] | None:
        record = await call_ledger.load_by_record_id(call_record_id)
        if record is None:
            return None
        return {
            "task_id": record.a2a_task_id,
            "context_id": record.a2a_context_id,
            "endpoint_scope": record.dispatch_snapshot.endpoint_scope,
            "agent_id": record.agent_id,
        }

    direct_client = OrchestratorDirectA2AClient(
        send_message=sdk_send_message,
        stream_message=sdk_stream_message,
        cancel_remote_task=sdk_cancel_remote_task,
        fetch_remote_task=sdk_fetch_remote_task,
        fetch_agent_card=sdk_fetch_agent_card,
        receipt_factory=A2ADispatchReceipt,
        observation_factory=NormalizedA2AObservation,
        recoverable_transport_error_factory=RecoverableTransportError,
        agent_card_contract_error_factory=AgentCardContractError,
        epoch_owner=artifact_writer.epoch_owner,
        call_resolver=resolve_call_address,
    )
    direct = DirectA2ADispatchAdapter(direct_client, observations=observation_ingress)
    dispatch = direct

    prepared_reader = RunPreparedInvocationSnapshotReader(
        run_store=run_store,
        binding_store=binding_store,
    )
    checkpoint_reader = RunCheckpointReader(run_store)
    tool_runtime = A2AAgentToolRuntime(
        ledger=call_ledger,
        prepared_reader=prepared_reader,
        checkpoint_reader=checkpoint_reader,
        authorization=authorization,
        room_epochs=epoch_store,
        resources=resources,
        dispatch=dispatch,
        observations=observation_ingress,
        terminal_finalizer=terminal_finalizer,
        hitl=hitl_port,
        hitl_delivery=hitl_delivery,
        run_store=run_store,
        canonical_hitl_control=canonical_hitl_control,
        public_secret_values=public_secret_values,
    )

    model_runtime = GatewayModelRuntime(llm_gateway)

    def kernel_for_catalog(
        snapshot: FrozenToolCatalogSnapshot,
    ) -> OrchestratorKernel:
        # The production projection driver never claims or completes intents
        # in-process. Terminal CAS already minted the required outbox intents;
        # the leader-elected ProjectionOutboxWorker delivers them. This driver
        # only attempts the idempotent settlement transition so the kernel
        # remains non-blocking and replay-safe.
        return OrchestratorKernel(
            run_store=run_store,
            model_runtime=model_runtime,
            tool_runtime=tool_runtime,
            tool_catalog=FrozenToolCatalog(snapshot),
            context_compiler=ContextCompiler(),
            budget_policy=BudgetPolicy(),
            projection_driver=SettlingProjectionDriver(run_store),
            public_secret_values=public_secret_values,
            canonical_event_reader=canonical_event_reader,
            artifact_metadata_reader=resources.describe_artifact,
            supervisor_hitl=supervisor_hitl,
        )

    run_factory = DefaultRunFactory()
    session_host = RoomSessionHost(
        kernel_factory=kernel_for_catalog,
        run_store=run_store,
        epoch_store=epoch_store,
        listener=session_listener,
        run_factory=run_factory,
    )

    observation_sink = session_host.observation_sink()

    async def project_terminal_run_status(intent, run):
        # Canonical Run state remains in the orchestrator aggregate. This
        # outbox intent publishes run_settled but never mirrors status into the
        # removed compatibility `runs`/task-card projection.
        if projection_listener is not None:
            # run_settled is part of this durable dependent intent. A failure
            # leaves the intent claimed/pending for lease-based retry; an append
            # followed by a crash is read back by its deterministic delivery ID.
            await projection_listener(run, intent)
        return "accepted"

    projectors = {
        "append_orchestrator_event": MongoAppendEventProjector(event_store).project,
        "deliver_final_message": MongoFinalMessageProjector(
            mongo.collection("room_agent_messages"),
            final_message_delivery,
            final_message_memory_projection,
        ).project,
        "project_terminal_run_status": project_terminal_run_status,
    }
    projection_worker = ProjectionOutboxWorker(
        run_store=run_store,
        projectors=projectors,
    )

    cancellation_coordinator = A2ACancellationCoordinator(
        ledger=call_ledger,
        room_epochs=epoch_store,
        dispatch=dispatch,
        observations=observation_ingress,
        hitl=hitl_port,
    )
    cancellation_recovery = A2ACancellationRecoveryService(
        coordinator=cancellation_coordinator,
        ledger=call_ledger,
    )
    observation_processor = A2AObservationProcessor(
        inbox=observation_inbox,
        conflicts=observation_conflicts,
        ledger=call_ledger,
        room_epochs=epoch_store,
        artifacts=resources,
        hitl=hitl_port,
        sink=observation_sink,
        checkpoint_reader=checkpoint_reader,
        outcome_reader=checkpoint_reader,
    )
    inbox_recovery = A2AInboxRecoveryService(
        processor=observation_processor,
        inbox=observation_inbox,
    )

    recover_dispatch = RunBackedDispatchRecovery(
        prepared_reader=prepared_reader,
        runtime=tool_runtime,
    )

    call_recovery = A2ACallRecoveryService(
        ledger=call_ledger,
        checkpoints=checkpoint_reader,
        room_epochs=epoch_store,
        dispatch=dispatch,
        observations=observation_ingress,
        recover_dispatch=recover_dispatch,
        hitl=hitl_port,
        hitl_delivery=hitl_delivery,
        run_store=run_store,
        canonical_hitl_control=canonical_hitl_control,
        public_secret_values=public_secret_values,
    )
    artifact_recovery = A2AArtifactRecoveryService(inbox_recovery)

    async def _recovery_noop() -> None:
        # HITL continuation and the watchdog remain separate recovery phases.
        return None

    recovery_instance = f"orchestrator-generic-runs:{uuid.uuid4().hex}"
    recovery_lease = timedelta(seconds=60)
    recovery_renew_interval = 20.0

    async def recover_generic_runs() -> None:  # noqa: C901
        now = datetime.now(UTC)
        due = await run_store.list_due_runs(due_at=now, limit=100)
        for candidate in due:
            claim_now = datetime.now(UTC)
            recovery_owner = f"{recovery_instance}:{uuid.uuid4().hex}"
            claimed = await run_store.claim_recovery(
                candidate.run_id,
                expected_state_version=candidate.state_version,
                owner_id=recovery_owner,
                lease_expires_at=claim_now + recovery_lease,
                claimed_at=claim_now,
            )
            if claimed.run is None or claimed.outcome not in {"accepted", "replayed"}:
                continue
            run = claimed.run

            async def emit(event_type, current, payload, _owner_id=recovery_owner):
                latest = await run_store.load(current.run_id)
                if latest is None or latest.recovery_claim.owner_id != _owner_id:
                    raise RecoverableAdapterError("generic recovery lease was lost")
                if session_listener is None:
                    return
                result = session_listener(
                    SessionEvent(
                        event_type=event_type,
                        session_id=f"recovery:{current.run_id}",
                        run_id=current.run_id,
                        causation_id=current.request.user_message_id,
                        sequence=current.state_version,
                        timestamp=datetime.now(UTC),
                        payload=payload,
                        room_id=current.room_id,
                        user_message_id=current.request.user_message_id,
                        client_request_id=current.client_request_id,
                        lifecycle_family=current.lifecycle_family,
                    )
                )
                if hasattr(result, "__await__"):
                    await result

            try:
                # This deterministic root is safe both before and after its
                # original append; the publisher reads back the same room row.
                await emit(
                    "run_started",
                    run,
                    {
                        "status": run.status,
                        "mode": run.profile.profile_id,
                        "started_at": run.created_at,
                    },
                )
                if run.tool_catalog is None:
                    raise RecoverableAdapterError("recoverable Run has no tool catalog")
                await _run_with_recovery_lease(
                    run_store=run_store,
                    run_id=run.run_id,
                    owner_id=recovery_owner,
                    work=kernel_for_catalog(run.tool_catalog).run(
                        run.run_id,
                        signal=EventCancellationSignal(),
                        lifecycle=emit,
                    ),
                    lease_duration=recovery_lease,
                    renew_interval_seconds=recovery_renew_interval,
                )
                current = await run_store.load(run.run_id)
                if (
                    current is not None
                    and current.recovery_claim.owner_id == recovery_owner
                ):
                    await run_store.release_recovery(
                        run.run_id,
                        expected_state_version=current.state_version,
                        owner_id=recovery_owner,
                        next_attempt_at=(
                            None
                            if current.status
                            in {"completed", "failed", "canceled", "budget_exhausted"}
                            else datetime.now(UTC) + timedelta(seconds=2)
                        ),
                    )
            except Exception as exc:
                current = await run_store.load(run.run_id)
                if (
                    current is not None
                    and current.recovery_claim.owner_id == recovery_owner
                ):
                    failure_at = datetime.now(UTC)
                    decision = _generic_recovery_failure_decision(
                        current.recovery_claim, exc, now=failure_at
                    )
                    released = await run_store.release_recovery(
                        run.run_id,
                        expected_state_version=current.state_version,
                        owner_id=recovery_owner,
                        next_attempt_at=decision.next_attempt_at,
                        failure_count=decision.failure_count,
                        quarantined_at=decision.quarantined_at,
                        quarantine_reason=decision.quarantine_reason,
                    )
                    if (
                        released.outcome in {"accepted", "replayed"}
                        and decision.quarantined_at is not None
                    ):
                        # The dedicated lease makes this warning exactly-once:
                        # quarantined rows are excluded from every future scan.
                        logger.warning(
                            "generic orchestrator Run recovery quarantined "
                            "run_id=%s reason=%s attempts=%s",
                            run.run_id,
                            decision.quarantine_reason,
                            decision.failure_count,
                        )
                    elif isinstance(exc, KernelConflict):
                        logger.debug(
                            "generic orchestrator Run recovery invariant retry "
                            "run_id=%s attempt=%s",
                            run.run_id,
                            decision.failure_count,
                        )
                    else:
                        logger.exception(
                            "generic orchestrator Run recovery failed "
                            "run_id=%s error_type=%s retry_scheduled=true: %s",
                            run.run_id,
                            type(exc).__name__,
                            exc,
                        )

    def _due_phase(recover: Callable[..., Any]) -> Callable[[], Any]:
        async def run() -> None:
            await recover(due_at=datetime.now(UTC))

        return run

    # Auth-challenge HITL answers fail closed until a real verifier is bound.
    class FailingAuthReferenceVerification:
        async def verify(self, *args: Any, **kwargs: Any) -> str:
            raise PermissionError("Auth references not implemented")

    continuation_coordinator = A2AContinuationCoordinator(
        ledger=call_ledger,
        bindings=binding_store,
        hitl=hitl_port,
        room_epochs=epoch_store,
        authorization=authorization,
        auth_references=FailingAuthReferenceVerification(),
        dispatch=dispatch,
        observations=observation_ingress,
        hitl_delivery=hitl_delivery,
        run_store=run_store,
        canonical_hitl_control=canonical_hitl_control,
        public_secret_values=public_secret_values,
    )
    continuation_recovery = A2AContinuationRecoveryService(
        coordinator=continuation_coordinator,
        ledger=call_ledger,
    )

    # Projection delivery is deliberately bound twice: as the recovery-cycle
    # projection phase AND as the standalone leader-gated projection job.
    # Both surfaces are idempotent (CAS + lease + dedupe) and re-drive the
    # same outbox; the redundancy self-heals whichever worker is behind.
    recovery_cycle = A2ARecoveryCycle(
        cancellation=_due_phase(cancellation_recovery.recover_due),
        continuation=_due_phase(continuation_recovery.recover_due),
        observations=_due_phase(inbox_recovery.recover_due),
        calls=_due_phase(call_recovery.recover_due),
        artifacts=_due_phase(artifact_recovery.recover_due),
        generic_runs=recover_generic_runs,
        projection=projection_worker.run_once,
        watchdog=_recovery_noop,
    )

    return OrchestratorRuntime(
        run_store=run_store,
        event_store=event_store,
        epoch_store=epoch_store,
        room_files=room_files,
        binding_store=binding_store,
        call_ledger=call_ledger,
        observation_inbox=observation_inbox,
        observation_conflicts=observation_conflicts,
        hitl_store=hitl_store,
        hitl_port=hitl_port,
        catalog_assembler=catalog_assembler,
        tool_runtime=tool_runtime,
        observation_ingress=observation_ingress,
        observation_processor=observation_processor,
        dispatch=dispatch,
        profile_resolver=profile_resolver,
        profiles=profiles,
        session_host=session_host,
        observation_sink=observation_sink,
        cancellation_coordinator=cancellation_coordinator,
        continuation=continuation_coordinator,
        hitl_delivery=hitl_delivery
        if hitl_delivery is not None
        else _NoopHitlDelivery(),
        kernel_factory=kernel_for_catalog,
        projection_worker=projection_worker,
        recovery_cycle=recovery_cycle,
        public_secret_values=public_secret_values,
    )


class _NoopHitlDelivery:
    async def emit(self, event: Any) -> None:
        return None


__all__ = [
    "OrchestratorCompositionError",
    "OrchestratorRuntime",
    "create_orchestrator_runtime",
    "validate_orchestrator_runtime",
]
