from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent import AgentFacade, AgentMongoRepository
from agent.facade import AgentRepository
from api_gateway.dependencies import (
    APIGatewayDeps,
    bind_api_gateway_deps,
    missing_required_deps,
)
from chat import ChatFacade
from common.config.loader import settings
from common.dto import AgentMessageFinal, DeliveryEmitStatus
from common.eventing import (
    BoundedInternalEventBus,
    EventingConfig,
    EventModelRegistry,
    InternalEventBus,
    InternalEventPublisher,
)
from common.health_check import RuntimeHealthCheck
from common.observability import (
    MetricsCollector,
    get_instance_id,
    get_logger,
    traced_create_task,
)
from common.protocols import (
    AgentCardResolver,
    AgentExclusionReader,
    AgentMatcher,
    AgentRegistry,
    EventPublisher,
    ExecutionEngine,
    FileStorage,
    HITLManager,
    LeaderElector,
    MongoCollection,
    MongoDAL,
    RedisKV,
    RedisPubSub,
    RoomDistributedLock,
    RoomHistoryReader,
    RoomOwnershipReader,
)
from common.utils.time import utcnow
from context_memory import (
    ContentStorageMongoRepository,
    ContextMemoryFacade,
    MemoryMongoRepository,
)
from context_memory.config import CompactionConfig
from context_memory.ports import ContentStorageRepository, MemoryRepository
from conversation import ConversationFacade, MessageMongoRepository, RoomMongoRepository
from conversation.attachments import AttachmentMetadataReader
from conversation.membership import RoomMembershipSeedSource
from conversation.membership_source import RepositoryRoomMembershipSeedSource
from conversation.repository import RoomQuoteMongoRepository
from delivery.config import DeliveryConfig
from delivery.event_bus import CrossInstanceEventBus
from delivery.event_publisher import EventPublisherImpl
from delivery.facade import DeliveryFacade
from delivery.sse.deduplication import TerminalStatusDeduplicator
from delivery.sse.manager import SSETransportImpl
from delivery.types import SSETransport, TaskRunner
from execution.adapters.canary_metrics import collect_metrics
from execution.orchestrator.a2a_runtime.call_counting import AgentCallCounter
from execution.orchestrator.persistence import TERMINAL_RUN_STATUSES
from execution.orchestrator.public_text import sanitize_public_text
from execution.repository.active_runs import CanonicalActiveRunRoomReader
from jobs.cleanup_orphaned_uploads import (
    OrphanedUploadCleanerDeps,
    orphaned_upload_cleaner,
)
from jobs.compaction_sweep import CompactionSweepDeps, compaction_sweep
from jobs.constants import AGENT_HEALTH_CHECKER, ALL_JOB_NAMES
from jobs.orchestrator_workers import (
    OrchestratorCanaryDeps,
    OrchestratorProjectionDeps,
    OrchestratorRecoveryDeps,
    orchestrator_canary_job,
    orchestrator_projection_job,
    orchestrator_recovery_job,
)
from jobs.stale_task_checker import stale_task_checker
from models.room import UserAttachment
from orchestrator_composition import (
    OrchestratorCompositionError,
    configured_public_secret_values,
    create_orchestrator_runtime,
    validate_orchestrator_runtime,
)
from room_files import LocalFileContentStore, RoomFiles, RoomLifecycleStore
from session import MongoSessionRepository, SessionFacade, SessionWaiter
from session.protocols import RoomManagement, RoomMessageStore, RoomRegistry

if TYPE_CHECKING:
    from dal.redis.streams import RedisStreamsImpl

logger = get_logger(__name__)


# Pure function — trivially testable without lifespan/DB
def check_multi_worker_safety(
    *,
    is_gunicorn: bool,
    delivery_pubsub_connected: bool,
    eventing_connected: bool = True,
    delivery_kv_connected: bool,
    redis_service_connected: bool,
    mongo_supports_transactions: bool,
) -> None:
    """Refuse to start under gunicorn without Redis or Mongo transactions.

    Gunicorn workers are separate processes. Without Redis:
    - SSE broadcast is local-only (cross-worker delivery fails)
    - Background jobs run N times (no leader election)
    - Room locks use asyncio.Lock only (no cross-process coordination)
    Without Mongo transactions (a replica set or mongos), room events fall back
    to a non-transactional write path that concurrent workers can interleave.

    Raises:
        RuntimeError: if gunicorn detected and any Redis service is not connected
            or MongoDB cannot run transactions
    """
    if not is_gunicorn:
        return

    problems = []
    if not delivery_pubsub_connected:
        problems.append("Delivery Pub/Sub not connected")
    if not eventing_connected:
        problems.append("Internal eventing Pub/Sub not connected")
    if not delivery_kv_connected:
        problems.append("Delivery KV not connected")
    if not redis_service_connected:
        problems.append("RedisService (key-value) not connected")
    if not mongo_supports_transactions:
        problems.append(
            "MongoDB does not support transactions "
            "(not a replica set or mongos, or the probe failed)"
        )

    if problems:
        raise RuntimeError(
            "Running under gunicorn requires all DAL Redis services and a "
            "MongoDB replica set or mongos. Issues: " + "; ".join(problems) + ". "
            "Fix: set REDIS_URL to a running Redis instance and run MongoDB as a "
            "replica set, or use 'uvicorn main:app' for single-process mode."
        )
    logger.info("Multi-worker safety check passed: gunicorn + Redis + transactions")


@dataclass
class ApplicationRuntime:
    settings: Any
    _lifespan_context: Any | None = None


_runtime_cleanup_tasks: dict[asyncio.Task[Any], str] = {}


def _runtime_cleanup_done(task: asyncio.Task[Any]) -> None:
    name = _runtime_cleanup_tasks.pop(task, None)
    if name is None or task.cancelled():
        return
    try:
        task.result()
    except BaseException:
        logger.warning(
            "detached runtime cleanup task failed: %s",
            name,
            exc_info=True,
        )


async def _run_cleanup_steps(
    steps: list[tuple[str, Callable[[], Awaitable[Any]]]],
    *,
    timeout_seconds: float | None = None,
) -> BaseException | None:
    """Run cleanup in order without joining cancellation-resistant timeouts."""
    first_error: BaseException | None = None
    for name, cleanup in steps:
        try:
            if timeout_seconds is None:
                await cleanup()
                continue

            task = asyncio.create_task(cleanup(), name=f"runtime-cleanup:{name}")
            _runtime_cleanup_tasks[task] = name
            try:
                done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
            except BaseException:
                task.cancel()
                task.add_done_callback(_runtime_cleanup_done)
                raise
            if not done:
                task.cancel()
                task.add_done_callback(_runtime_cleanup_done)
                error = TimeoutError(f"runtime cleanup step timed out: {name}")
                if first_error is None:
                    first_error = error
                logger.warning(
                    "runtime cleanup step timed out and remains owned: %s",
                    name,
                )
                continue
            _runtime_cleanup_tasks.pop(task, None)
            task.result()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            logger.warning("runtime cleanup step failed: %s", name, exc_info=True)
    return first_error


def create_application_runtime(app_settings: Any = settings) -> ApplicationRuntime:
    return ApplicationRuntime(settings=app_settings)


def create_health_check_service(
    *,
    redis_url: str,
    compute_health_status: Callable[..., dict[str, Any]],
) -> Any:
    return RuntimeHealthCheck(
        redis_url=redis_url,
        compute_health_status=compute_health_status,
    )


async def startup_runtime(app: Any, runtime: ApplicationRuntime) -> None:
    if runtime._lifespan_context is not None:
        raise RuntimeError("Application runtime has already been started")
    context = _runtime_lifespan(app, runtime)
    runtime._lifespan_context = context
    try:
        await context.__aenter__()
    except BaseException:
        runtime._lifespan_context = None
        raise


async def shutdown_runtime(app: Any, runtime: ApplicationRuntime) -> None:
    del app
    context = runtime._lifespan_context
    if context is None:
        return
    try:
        await context.__aexit__(None, None, None)
    finally:
        runtime._lifespan_context = None


def validate_runtime_bindings(
    app: Any, runtime: ApplicationRuntime | None = None
) -> None:
    del runtime
    errors: list[str] = []

    from chat.service import chat_service

    conversation_runtime = getattr(app.state, "conversation_runtime", None)
    if conversation_runtime is None:
        errors.append("app.state.conversation_runtime")
    else:
        for missing in conversation_runtime.missing_required_bindings():
            errors.append(f"conversation.{missing}")
    for missing in chat_service.missing_required_bindings():
        errors.append(f"chat.{missing}")

    if getattr(app.state, "delivery_facade", None) is None:
        errors.append("app.state.delivery_facade")

    if getattr(app.state, "execution_deps", None) is None:
        errors.append("app.state.execution_deps")
    if getattr(app.state, "orchestrator_runtime", None) is None:
        errors.append("app.state.orchestrator_runtime")
    execution_facade = getattr(app.state, "execution_facade", None)
    if (
        execution_facade is None
        or getattr(execution_facade, "_orchestrator_router", None) is None
    ):
        errors.append("app.state.execution_facade.orchestrator_router")

    api_gateway_deps = getattr(app.state, "api_gateway_deps", None)
    for missing in missing_required_deps(api_gateway_deps):
        errors.append(f"api_gateway.{missing}")

    if errors:
        raise RuntimeError(
            "Startup binding incomplete - missing: "
            + ", ".join(errors)
            + ". Cannot serve traffic."
        )

    logger.info("All startup bindings verified")


def _latest_canonical_parent_id(records: list[dict[str, object]]) -> str | None:
    """Recover the latest durable semantic parent after process restart."""

    latest = max(records, key=lambda item: int(item.get("room_seq") or 0), default=None)
    if latest is None:
        return None
    return str(latest.get("room_event_id") or "") or None


def _canonical_final_message_end_parent_id(
    records: list[dict[str, object]], message_id: str
) -> str | None:
    """Resolve the exact durable final message_end, independent of cache order."""

    parent = next(
        (
            record
            for record in sorted(
                records,
                key=lambda item: int(item.get("room_seq") or 0),
                reverse=True,
            )
            if isinstance(record.get("payload_public"), dict)
            and record["payload_public"].get("type") == "message_end"
            and isinstance(record["payload_public"].get("payload"), dict)
            and record["payload_public"]["payload"].get("message_id") == message_id
            and record["payload_public"]["payload"].get("disposition") == "final"
        ),
        None,
    )
    return str(parent.get("room_event_id") or "") or None if parent else None


async def terminalize_canonical_hitl_run(
    orchestrator_runtime: Any,
    request: Any,
    *,
    terminal_status: str,
    reason: str,
    session_listener: Callable[[Any], Awaitable[None]],
) -> bool:
    """Settle the canonical Run that owns a terminal HITL interaction.

    Returns False when the request is not owned by a canonical Run. Agent calls
    of the Run are canceled locally before it ends, so parallel calls do not
    stay non-terminal; while that cancellation is pending the call raises and
    the HITL reconciler retries.
    """
    from execution.orchestrator.a2a_runtime.ledger import TERMINAL_AGENT_CALL_STATES
    from execution.orchestrator.lifecycle import SessionEvent

    run_id = request.orchestration_run_id
    if orchestrator_runtime is None or not run_id:
        return False
    run = await orchestrator_runtime.run_store.load(run_id)
    if run is None:
        return False
    if run.tool_catalog is None:
        raise RuntimeError("canonical HITL owner has no Tool catalog")
    if terminal_status == "canceled":
        command_id = run.cancellation_command_id
        if run.status != "canceling" or command_id is None:
            return run.status == "canceled"
        results = await orchestrator_runtime.cancellation_coordinator.cancel_run(
            run_id, reason="canonical_hitl_canceled"
        )
        if any(state not in TERMINAL_AGENT_CALL_STATES for state in results.values()):
            return True
        await orchestrator_runtime.session_host.reconcile_cancellation(run)
        await orchestrator_runtime.session_host.signal_run_cancellation(run, command_id)
        return True

    results = await orchestrator_runtime.cancellation_coordinator.cancel_run(
        run_id, reason="canonical_hitl_failed"
    )
    if any(state not in TERMINAL_AGENT_CALL_STATES for state in results.values()):
        raise RuntimeError("local Agent-call cancellation remains pending")

    async def emit(event_type, current, payload):
        await session_listener(
            SessionEvent(
                event_type=event_type,
                session_id=f"hitl-terminal:{current.run_id}",
                run_id=current.run_id,
                causation_id=request.request_id,
                sequence=current.state_version,
                timestamp=datetime.now(UTC),
                payload=payload,
                conversation_id=current.conversation_id,
                user_message_id=current.request.user_message_id,
                client_request_id=current.client_request_id,
            )
        )

    await orchestrator_runtime.kernel_factory(run.tool_catalog).terminalize(
        run_id,
        status="failed",
        reason=reason,
        lifecycle=emit,
    )
    return True


@asynccontextmanager
async def _runtime_lifespan(app: Any, runtime: ApplicationRuntime):  # noqa: C901
    """Lifespan context manager to handle startup and shutdown events.

    Startup is split into two phases with a multi-worker safety guard:
      Phase 1 — Infrastructure (DB + Redis, no background work)
      Guard   — Fail if gunicorn without fully connected Redis
      Phase 2 — Background services (only after guard passes)

    Cleanup is split into two separate paths:
      Startup failure — tears down only what was opened, without entering
          the normal SSE draining path.
      Normal shutdown — full teardown including SSE draining
    """
    _redis_runtime = None
    _redis_service = None
    _redis_streams_service = None
    _leader = None
    _agent_deps = None
    _delivery_facade = None
    _delivery_config = None
    _eventing_bus = None
    _eventing_deps = None
    _execution_deps = None
    _mongo_dal = None
    _local_agent_service = None
    _local_agent_card_resolver = None
    _bundled_agents = None
    _bundled_card_resolver = None
    _provider_runtime = None
    _browser_setup = None
    _bg_started = False
    agent_health_service = None
    redis_kv_ready = False
    redis_streams_ready = False

    try:
        # ── Phase 1: Infrastructure (DB + Redis, no background work) ──

        mongo_dal = create_mongo_dal()
        _mongo_dal = mongo_dal
        app.state.mongo_dal = mongo_dal
        await mongo_dal.connect()

        if await mongo_dal.ping():
            from a2a_adapter import AgentCardResolverImpl
            from a2a_adapter import artifact_storage as a2a_artifact_storage
            from agent.capability_issue import (
                CapabilityIssueExclusionReader,
            )
            from agent.catalog import AgentCatalogService
            from agent.health import AgentHealthService
            from agent.inspection import AgentInspectionService
            from agent.liveness import AgentLivenessService
            from agent.matcher import AgentMatcher
            from agent.selection_service import AgentSelectionService
            from chat.service import chat_service
            from common.config.runtime_store import current_store
            from common.utils.a2a_helpers import bind_a2a_artifact_files
            from conversation.deletion import RoomDeletionService
            from conversation.runtime import ConversationRuntime
            from conversation.timeline_projection import RoomTimelineProjector
            from conversation.user_message_persistence import UserMessageCommitService
            from execution.repository.orchestrator.epoch_bindings import (
                EpochScopedOrchestratorCleanup,
                bind_room_epoch_store,
                require_room_epoch_store,
                reset_room_epoch_store,
            )
            from execution.repository.orchestrator.event_store import (
                MongoOrchestratorEventStore,
            )
            from execution.repository.orchestrator.stores import (
                MongoAgentCallLedgerStore,
                MongoAgentToolBindingStore,
                MongoObservationConflictStore,
                MongoObservationInboxStore,
                MongoRoomEpochStore,
            )
            from llm_gateway import LLMGatewayImpl, ModelRegistryImpl
            from llm_gateway.config import LLMGatewayConfig
            from llm_gateway.provider_runtime import ProviderRuntime
            from llm_gateway.services import AgentSelectionLLMService
            from local_agents import (
                HostPortScanner,
                LocalAgentCardProbe,
                LocalAgentDiscoveryConfig,
                LocalAgentService,
            )

            # Each lifespan builds its own conversation runtime. The chat service
            # is still process-global, so clear bindings from a previous lifespan.
            conversation_runtime = ConversationRuntime()
            app.state.conversation_runtime = conversation_runtime
            chat_service.reset_bindings()
            reset_room_epoch_store()

            bind_room_epoch_store(
                MongoRoomEpochStore(
                    mongo_dal.collection("orchestrator_room_epochs").raw_collection
                )
            )

            room_files_collection = mongo_dal.collection("room_files")
            file_storage = create_file_storage(
                room_files_collection=room_files_collection,
                room_lifecycle=RoomMongoRepository(mongo_dal),
                room_messages_collection=mongo_dal.collection("room_user_messages"),
                room_agent_messages_collection=mongo_dal.collection(
                    "room_agent_messages"
                ),
                file_dir=runtime.settings.hybro_file_dir,
                content_url_prefix=f"{runtime.settings.api_prefix.rstrip('/')}/files",
            )

            a2a_artifact_storage.bind_artifact_files(file_storage)
            bind_a2a_artifact_files(a2a_artifact_storage)
            index_readiness = await ensure_runtime_indexes(mongo=mongo_dal)
            app.state.agent_search_index_ready = index_readiness[
                "agent_search_index_ready"
            ]

            _delivery_config = create_delivery_config(runtime.settings)
            runtime_instance_id = get_instance_id()
            delivery_redis_kv, delivery_redis_pubsub = create_delivery_redis_clients(
                redis_url=runtime.settings.redis_url,
                config=_delivery_config,
            )
            # ── Room event log + snapshot materialization ────────────────
            # (Room Stream Snapshot plan §5). The snapshot service serves the
            # per-connect snapshot and the per-connection resync provider;
            # the store is also exposed on app.state for the replay endpoint.
            from delivery.snapshot import SnapshotService

            _room_event_store = await _create_ready_room_event_store(mongo=mongo_dal)
            app.state.room_event_store = _room_event_store
            _room_snapshot_service = SnapshotService(store=_room_event_store)
            app.state.room_snapshot_service = _room_snapshot_service

            async def _room_seq_reader(conversation_id: str) -> int | None:
                return await _room_event_store.latest_seq(conversation_id)

            async def _snapshot_provider(conversation_id: str) -> dict[str, Any] | None:
                try:
                    return await _room_snapshot_service.snapshot(conversation_id)
                except Exception:
                    logger.warning(
                        "resync snapshot build failed",
                        extra={"conversation_id": conversation_id},
                        exc_info=True,
                    )
                    return None

            _delivery_facade = create_delivery_facade(
                redis_kv=delivery_redis_kv,
                redis_pubsub=delivery_redis_pubsub,
                config=_delivery_config,
                instance_id=runtime_instance_id,
                room_events=_room_event_store,
                snapshot_provider=_snapshot_provider,
                room_seq_reader=_room_seq_reader,
            )
            await _delivery_facade.start()
            _delivery_deps = create_delivery_deps(_delivery_facade)
            app.state.delivery_facade = _delivery_facade
            app.state.delivery_deps = _delivery_deps

            _eventing_bus = create_internal_event_bus(
                redis_url=runtime.settings.redis_url,
                instance_id=_delivery_facade.instance_id,
                app_settings=runtime.settings,
            )
            register_internal_event_models(_eventing_bus.registry)
            _eventing_deps = create_eventing_deps(_eventing_bus)
            app.state.eventing_bus = _eventing_bus
            app.state.eventing_deps = _eventing_deps
            app.state.eventing_connected = False

            from approval.factory import create_hitl_service

            agent_capability_issue_repository = (
                create_agent_capability_issue_repository(mongo_dal)
            )
            agent_capability_issue_service = create_agent_capability_issue_service(
                repository=agent_capability_issue_repository
            )
            from approval.adapters import HITLDeliveryAdapter
            from conversation.attachment_projection import (
                AttachmentProjectionService,
            )
            from execution.adapters.hitl import HITLMessageCancellationAdapter
            from execution.client_request_id import SSEClientRequestIdResolver

            # One holder owns the live Provider pair for every model consumer.
            _provider_runtime = ProviderRuntime(current_store())
            app.state.provider_runtime = _provider_runtime
            llm_gateway_config = LLMGatewayConfig.from_settings(runtime.settings)
            model_registry = ModelRegistryImpl(
                runtime.settings, provider_runtime=_provider_runtime
            )
            llm_provider = LLMGatewayImpl(
                model_registry=model_registry,
                config=llm_gateway_config,
                settings_obj=runtime.settings,
                provider_runtime=_provider_runtime,
            )
            from llm_gateway.agent_proxy import AgentLLMProxy

            app.state.agent_llm_proxy = AgentLLMProxy(
                llm_provider, runtime.settings.default_agent_llm_token
            )
            from llm_gateway.catalog import model_choices
            from llm_gateway.setup_api import BrowserSetup, OAuthLogins
            from llm_gateway.setup_bindings import verify_selection
            from llm_gateway.setup_service import SetupService

            _browser_setup = BrowserSetup(
                _provider_runtime,
                SetupService(_provider_runtime.store, model_choices, verify_selection),
                OAuthLogins(host=runtime.settings.oauth_callback_host),
                auth_mode=runtime.settings.auth_mode,
                admin_user_ids=frozenset(runtime.settings.admin_user_ids),
            )
            app.state.setup_access = _browser_setup
            agent_selection_llm_service = AgentSelectionLLMService(
                llm_provider=llm_provider
            )
            conversation_runtime.bind_capability_issue_reader(
                agent_capability_issue_service
            )
            agent_card_resolver = AgentCardResolverImpl()
            _agent_deps = create_agent_deps(
                mongo=mongo_dal,
                card_resolver=agent_card_resolver,
                exclusion_reader=CapabilityIssueExclusionReader(
                    agent_capability_issue_service
                ),
            )
            _agent_facade = _agent_deps.agent_registry
            local_agent_config = LocalAgentDiscoveryConfig(
                enabled=runtime.settings.local_agent_discovery_enabled,
                host=runtime.settings.local_agent_discovery_host,
                port_start=runtime.settings.local_agent_discovery_port_start,
                port_end=runtime.settings.local_agent_discovery_port_end,
                excluded_ports=runtime.settings.local_agent_discovery_excluded_ports,
                interval_seconds=(
                    runtime.settings.local_agent_discovery_interval_seconds
                ),
                connect_timeout_seconds=(
                    runtime.settings.local_agent_discovery_connect_timeout_seconds
                ),
                probe_timeout_seconds=(
                    runtime.settings.local_agent_discovery_probe_timeout_seconds
                ),
            )
            _local_agent_card_resolver = AgentCardResolverImpl(
                cache_ttl=0,
                timeout=local_agent_config.probe_timeout_seconds,
                log_failures=False,
            )
            _local_agent_service = LocalAgentService(
                config=local_agent_config,
                scanner=HostPortScanner(
                    host=local_agent_config.host,
                    port_start=local_agent_config.port_start,
                    port_end=local_agent_config.port_end,
                    connect_timeout_seconds=(
                        local_agent_config.connect_timeout_seconds
                    ),
                    excluded_ports=local_agent_config.excluded_ports,
                ),
                card_probe=LocalAgentCardProbe(
                    host=local_agent_config.host,
                    resolver=_local_agent_card_resolver,
                ),
                writer=_agent_deps.agent_registry_writer,
            )
            app.state.local_agent_service = _local_agent_service
            if runtime.settings.bundled_agents_manifest:
                _bundled_card_resolver = AgentCardResolverImpl(
                    cache_ttl=0, timeout=5, log_failures=False
                )
                _bundled_agents = create_bundled_agent_service(
                    runtime.settings,
                    repository=_agent_deps.agent_repository,
                    card_resolver=_bundled_card_resolver,
                    provider_runtime=_provider_runtime,
                )
            agent_matcher = AgentMatcher(facade=_agent_facade)
            agent_selection_service = AgentSelectionService(
                matcher=agent_matcher,
                llm_reranker=agent_selection_llm_service,
            )
            agent_health_service = AgentHealthService(
                repository=_agent_deps.agent_repository
            )
            agent_catalog = AgentCatalogService(
                _agent_facade,
                liveness_checker=AgentLivenessService(
                    health_service=agent_health_service
                ),
            )
            route_inspection_center = AgentInspectionService()
            membership_source = RepositoryRoomMembershipSeedSource(
                agent_registry=_agent_facade
            )
            _room_deps = create_room_deps(
                mongo=mongo_dal,
                agent_registry=_agent_deps.agent_registry,
                membership_source=membership_source,
                attachment_metadata_reader=file_storage,
                epoch_store=require_room_epoch_store(),
            )
            _room_facade = _room_deps.room_registry
            # Module-owned stores: each is built over its own collections and bound
            # below through focused SimpleNamespace ports.
            from agent.repository import AgentRuntimeStore

            agent_runtime_store = AgentRuntimeStore(
                agent_groups=mongo_dal.collection("agent_groups"),
                agent_repository=_agent_deps.agent_repository,
            )
            from conversation.repository import (
                ClientRequestIdStore,
                ConversationMessageStore,
                ConversationRoomStore,
            )

            message_store = ConversationMessageStore(
                room_agent_messages=mongo_dal.collection("room_agent_messages"),
                room_user_messages=mongo_dal.collection("room_user_messages"),
                message_repository=_room_deps.message_repository,
            )
            room_store = ConversationRoomStore(
                room_repository=_room_deps.room_repository
            )
            client_request_id_store = ClientRequestIdStore(message_store=message_store)
            from approval.repository import HITLLifecycleStore, HITLRequestStore

            hitl_store = HITLRequestStore(
                hitl_requests=mongo_dal.collection("hitl_requests"),
                message_writer=message_store,
            )
            hitl_lifecycle_store = HITLLifecycleStore(
                interactions=mongo_dal.collection("hitl_interactions"),
                resume_commands=mongo_dal.collection("hitl_resume_commands"),
                hitl_requests=mongo_dal.collection("hitl_requests"),
            )
            # Room deletion asks each owning module to delete its own rows.
            # Orchestrator collections are cleaned per room epoch instead.
            file_storage.bind_room_state_owners(
                (
                    _room_deps.message_repository,
                    RoomQuoteMongoRepository(mongo_dal),
                    MemoryMongoRepository(mongo=mongo_dal),
                    ContentStorageMongoRepository(mongo=mongo_dal),
                    hitl_store,
                    hitl_lifecycle_store,
                )
            )
            # P3 runtime adapters keep startup wiring narrow. These SimpleNamespace
            # boundaries are intentionally constrained to container assembly.
            hitl_runtime_store = SimpleNamespace(
                count_hitl_requests_for_message=(
                    hitl_store.count_hitl_requests_for_message
                ),
                create_hitl_request=hitl_store.create_hitl_request,
                update_agent_message_task_state=(
                    hitl_store.update_agent_message_task_state
                ),
                persist_hitl_request_id_on_message=(
                    hitl_store.persist_hitl_request_id_on_message
                ),
                find_pending_hitl_request_for_agent_message=(
                    hitl_store.find_pending_hitl_request_for_agent_message
                ),
                create_or_reuse_pending_hitl_request=(
                    hitl_store.create_or_reuse_pending_hitl_request
                ),
                claim_hitl_open_projection=hitl_store.claim_hitl_open_projection,
                complete_hitl_open_projection=(
                    hitl_store.complete_hitl_open_projection
                ),
                release_hitl_open_projection=hitl_store.release_hitl_open_projection,
                persist_pending_hitl_on_agent_message=(
                    hitl_store.persist_pending_hitl_on_agent_message
                ),
                get_room_user_message_by_message_id=(
                    message_store.get_room_user_message_by_message_id
                ),
                resolve_client_request_id_for_message_id=(
                    client_request_id_store.resolve_client_request_id_for_message_id
                ),
                persist_hitl_user_answer=hitl_store.persist_hitl_user_answer,
                persist_hitl_interaction_metadata=(
                    hitl_store.persist_hitl_interaction_metadata
                ),
                get_hitl_request=hitl_store.get_hitl_request,
                update_hitl_request=hitl_store.update_hitl_request,
                claim_hitl_request=hitl_store.claim_hitl_request,
                fenced_update_hitl_request=hitl_store.fenced_update_hitl_request,
                reset_last_notified_state=message_store.reset_last_notified_state,
                get_room_agent_message_by_message_id=(
                    message_store.get_room_agent_message_by_message_id
                ),
                get_pending_hitl_requests=hitl_store.get_pending_hitl_requests,
                get_pending_hitl_requests_strict=(
                    hitl_store.get_pending_hitl_requests_strict
                ),
                get_pending_hitl_requests_for_message=(
                    hitl_store.get_pending_hitl_requests_for_message
                ),
                get_pending_hitl_requests_for_message_strict=(
                    hitl_store.get_pending_hitl_requests_for_message_strict
                ),
                cas_update_hitl_request=hitl_store.cas_update_hitl_request,
                cas_update_hitl_request_strict=(
                    hitl_store.cas_update_hitl_request_strict
                ),
            )
            room_runtime_store = SimpleNamespace(
                add_room_agent_message=message_store.add_room_agent_message,
                get_agent_by_agent_id=agent_runtime_store.get_agent_by_agent_id,
                get_agent_group_by_id=agent_runtime_store.get_agent_group_by_id,
                get_all_active_agents=agent_runtime_store.get_all_active_agents,
                get_room_by_room_id=room_store.get_room_by_room_id,
                get_room_user_message_by_message_id=(
                    message_store.get_room_user_message_by_message_id
                ),
                update_room_user_message_by_message_id=(
                    message_store.update_room_user_message_by_message_id
                ),
            )

            membership_source.bind_store(agent_runtime_store)
            execution_client_request_id_resolver = SSEClientRequestIdResolver(
                resolver=client_request_id_store,
            )
            app.state.execution_client_request_id_resolver = (
                execution_client_request_id_resolver
            )
            from approval.adapters import HITLTerminalLifecycleAdapter
            from approval.application import HITLApplicationCoordinator
            from approval.reconciler import HITLLifecycleReconciler

            hitl_application = HITLApplicationCoordinator(
                lifecycle=hitl_lifecycle_store,
            )

            async def publish_canonical_hitl_control(
                kind: str,
                interaction: dict[str, Any],
                request_ids: list[str],
            ) -> None:
                from common.dto import RunEventNotification

                orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
                run_id = interaction.get("orchestration_run_id")
                if orchestrator_runtime is None or not isinstance(run_id, str):
                    return
                run = await orchestrator_runtime.run_store.load(run_id)
                if run is None or not run.client_request_id:
                    return
                if kind == "run_resumed" and run.status in {
                    "canceling",
                    *TERMINAL_RUN_STATUSES,
                }:
                    return
                boundary_at = datetime.now(UTC)
                payload = (
                    {
                        "interaction_id": interaction["interaction_id"],
                        "request_ids": request_ids,
                        "requested_at": boundary_at,
                    }
                    if kind == "run_waiting_input"
                    else {
                        "interaction_id": interaction["interaction_id"],
                        "resolved_request_ids": request_ids,
                        "resumed_at": boundary_at,
                    }
                )
                event_id = f"public:{run_id}:{kind}:{interaction['interaction_id']}"
                records = await _read_canonical_run_events(run.conversation_id, run_id)
                parent_event_id = _latest_canonical_parent_id(
                    records
                ) or _canonical_parent_ids.get(run_id)
                (
                    status,
                    room_event_id,
                ) = await _delivery_deps.event_publisher.emit_checked_identified(
                    RunEventNotification(
                        conversation_id=run.conversation_id,
                        event_id=event_id,
                        run_id=run_id,
                        seq=run.state_version,
                        run_event_type=kind,
                        payload=payload,
                        correlation_id=run.client_request_id,
                    ),
                    parent_event_id=parent_event_id,
                )
                if room_event_id is None and status not in {
                    DeliveryEmitStatus.ALREADY_DELIVERED,
                }:
                    raise RuntimeError(
                        f"canonical HITL control event {kind} was not persisted"
                    )
                if room_event_id is not None:
                    _canonical_parent_ids[run_id] = room_event_id
                await _emit_canonical_processing_adapter(
                    run,
                    "awaiting_input" if kind == "run_waiting_input" else "processing",
                )

            async def publish_orchestrator_hitl_control(
                kind: str,
                run_id: str,
                interaction_id: str,
                request_ids: list[str],
            ) -> None:
                await publish_canonical_hitl_control(
                    kind,
                    {
                        "orchestration_run_id": run_id,
                        "interaction_id": interaction_id,
                    },
                    request_ids,
                )

            async def project_supervisor_run_answer(
                hitl_result,
                response,
            ) -> None:
                """Aggregate-owned run-answer projection for ask_user.

                Validates the supervisor-run route so ``run_projection_status``
                converges to applied and the durable APPLIED transition can
                complete. The canonical ``run_resumed`` control itself is
                published after ``finalize_applied`` (hitl_response events
                first) via ``emit_canonical_resumed_control``, whose
                deterministic identity collapses reconciler replays.
                """
                from common.dto.hitl import HITLApplicationRoute

                del response
                interaction_id = hitl_result.get("interaction_id")
                if not isinstance(interaction_id, str) or not interaction_id:
                    return
                interaction = await hitl_lifecycle_store.get_interaction_strict(
                    interaction_id
                )
                if interaction is None or interaction.get("application_route") != (
                    HITLApplicationRoute.SUPERVISOR_RUN.value
                ):
                    return

            hitl_application.bind_run_answer_projector(project_supervisor_run_answer)

            async def read_canonical_run_state(run_id: str) -> str:
                orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
                if orchestrator_runtime is None:
                    return "missing"
                run = await orchestrator_runtime.run_store.load(run_id)
                return run.status if run is not None else "missing"

            async def request_canonical_hitl_cancellation(run_id: str) -> str:
                orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
                if orchestrator_runtime is None:
                    return "missing"
                run = await orchestrator_runtime.run_store.load(run_id)
                if run is None:
                    return "missing"
                command_id = f"cancel:{run_id}:user_requested"
                for _attempt in range(4):
                    result = await orchestrator_runtime.run_store.request_cancellation(
                        run_id,
                        expected_state_version=run.state_version,
                        command_id=command_id,
                        cause="user_requested",
                        requested_at=datetime.now(UTC),
                    )
                    if result.run is None:
                        return "missing"
                    run = result.run
                    if result.outcome in {"accepted", "replayed"} or run.status not in {
                        "queued",
                        "running",
                        "waiting_external",
                        "awaiting_user",
                    }:
                        return run.status
                raise RuntimeError("canonical HITL cancellation CAS did not converge")

            async def terminalize_canonical_hitl_run_port(
                request: Any,
                *,
                terminal_status: str,
                reason: str,
            ) -> bool:
                return await terminalize_canonical_hitl_run(
                    getattr(app.state, "orchestrator_runtime", None),
                    request,
                    terminal_status=terminal_status,
                    reason=reason,
                    session_listener=_orchestrator_session_listener,
                )

            async def request_supervisor_input_port(
                *,
                run,
                interaction_id,
                call_id,
                question,
                choices,
            ):
                """Kernel-facing port: ask_user → unified Execution HITL."""
                from common.dto.hitl import (
                    HITLApplicationRoute,
                    HITLEvidenceOrigin,
                    HITLPublicSource,
                    HITLRouteSnapshot,
                )

                snapshot = HITLRouteSnapshot(
                    route=HITLApplicationRoute.SUPERVISOR_RUN,
                    orchestration_run_id=run.run_id,
                )
                requests = await hitl_manager.request_interaction(
                    conversation_id=run.conversation_id,
                    user_message_id=run.request.user_message_id,
                    interaction_id=interaction_id,
                    application_route=HITLApplicationRoute.SUPERVISOR_RUN,
                    public_source=HITLPublicSource.SUPERVISOR,
                    evidence_origin=HITLEvidenceOrigin.SUPERVISOR,
                    route_snapshot=snapshot,
                    questions=[
                        {
                            "prompt": question,
                            "prompt_type": "single_choice" if choices else "text",
                            "choices": list(choices),
                            "source_step_id": call_id,
                        }
                    ],
                    orchestration_run_id=run.run_id,
                )
                if not requests:
                    raise RuntimeError(
                        "supervisor input interaction was not materialized"
                    )

            async def resume_supervisor_hitl_port(
                run_id,
                *,
                call_id,
                answers,
            ):
                """HITL-facing port: recorded answer → suspended kernel Run."""
                from approval.service import ContinuationLostError
                from execution.orchestrator.kernel import (
                    supervisor_answer_observation,
                )

                orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
                if orchestrator_runtime is None:
                    raise ContinuationLostError("orchestrator runtime is unavailable")
                run = await orchestrator_runtime.run_store.load(run_id)
                if run is None:
                    raise ContinuationLostError("orchestration run is missing")
                if run.status in {"canceling", *TERMINAL_RUN_STATUSES}:
                    return False
                try:
                    observation = supervisor_answer_observation(
                        run_id,
                        call_id,
                        answers,
                        datetime.now(UTC),
                    )
                    session = orchestrator_runtime.session_host.get_session(
                        run.conversation_id
                    )
                    if session is not None and session.owns_run(run_id):
                        resume_result = (
                            await orchestrator_runtime.session_host.observe_tool(
                                run.conversation_id, observation
                            )
                        )
                    else:
                        # Suspended Runs may outlive their process-local
                        # session (restart/recovery). The run-addressed sink
                        # re-enters the same kernel observation path with a
                        # fresh lifecycle emitter.
                        resume_result = (
                            await orchestrator_runtime.observation_sink.deliver(
                                run_id, observation
                            )
                        )
                    if getattr(resume_result, "outcome", None) == (
                        "cancellation_pending"
                    ):
                        return False
                except Exception as exc:
                    logger.warning(
                        "supervisor ask_user resume failed",
                        extra={
                            "run_id": run_id,
                            "call_id": call_id,
                            "conversation_id": run.conversation_id,
                            "resume_error": f"{type(exc).__name__}: {exc}",
                        },
                        exc_info=True,
                    )
                    raise
                return True

            hitl_manager = create_hitl_service(
                persistence=hitl_runtime_store,
                delivery=HITLDeliveryAdapter(_delivery_deps.event_publisher),
                terminal_lifecycle=HITLTerminalLifecycleAdapter(
                    canonical_terminalizer=terminalize_canonical_hitl_run_port,
                ),
                lifecycle=hitl_lifecycle_store,
                application=hitl_application,
                room_files=file_storage,
                canonical_control_publisher=publish_canonical_hitl_control,
                canonical_run_state_reader=read_canonical_run_state,
                supervisor_resume=resume_supervisor_hitl_port,
                canonical_cancellation_requester=request_canonical_hitl_cancellation,
                public_secret_values=configured_public_secret_values(runtime.settings),
                public_text_sanitizer=sanitize_public_text,
                approval_deadline_hours=runtime.settings.approval_deadline_hours,
            )

            hitl_reconciler = HITLLifecycleReconciler(
                lifecycle=hitl_lifecycle_store,
                service=hitl_manager,
                application=hitl_application,
            )
            route_room_reader = SimpleNamespace(
                get_room_by_room_id=room_store.get_room_by_room_id,
            )
            sse_state_reader = SimpleNamespace(
                get_room_by_room_id=room_store.get_room_by_room_id,
                get_room_user_message_by_message_id=(
                    message_store.get_room_user_message_by_message_id
                ),
            )
            conversation_runtime.bind_store(room_runtime_store)
            conversation_runtime.bind_facade(_room_facade)
            conversation_runtime.bind_user_message_commit(
                UserMessageCommitService(
                    writer=SimpleNamespace(
                        ensure_user_message_id=_room_facade.ensure_user_message_id,
                        persist_user_message=_room_facade.persist_user_message,
                    ),
                    files=SimpleNamespace(
                        write_lease=file_storage.write_lease,
                        claim_references=file_storage.claim_references,
                        commit_references=file_storage.commit_references,
                        release_references=file_storage.release_references,
                    ),
                    internal_event_publisher=(_eventing_deps.internal_event_publisher),
                )
            )
            conversation_runtime.bind_attachment_metadata_reader(file_storage)
            conversation_runtime.bind_timeline_projector(
                RoomTimelineProjector(
                    hitl_reader=SimpleNamespace(
                        get_hitl_request=hitl_store.get_hitl_request,
                    ),
                    attachment_metadata_reader=SimpleNamespace(
                        get_for_room_file=file_storage.get_for_room_file,
                    ),
                )
            )
            chat_service.bind_facade(_room_deps.chat_facade)
            chat_service.bind_message_reader(
                conversation_runtime.inquiry_room_messages_by_room_id
            )
            context_memory_facade = create_context_memory_facade(
                mongo=mongo_dal,
                room_history_reader=_room_deps.room_history_reader,
            )
            room_deletion = RoomDeletionService(
                room_lifecycle=SimpleNamespace(
                    get_room_owner=_room_facade.get_room_owner,
                    cleanup_room_owned_data=_room_facade.cleanup_room_owned_data,
                    delete_room=_room_facade.delete_room,
                ),
                file_lifecycle=SimpleNamespace(
                    begin_room_deletion=file_storage.begin_room_deletion,
                    wait_for_room_writes=file_storage.wait_for_room_writes,
                    set_deletion_phase=file_storage.set_deletion_phase,
                    delete_for_room=file_storage.delete_for_room,
                    delete_room_state=file_storage.delete_room_state,
                ),
                memory_cleanup=context_memory_facade,
                epoch_store=require_room_epoch_store(),
                orchestrator_epoch_cleanup=EpochScopedOrchestratorCleanup(
                    bindings=MongoAgentToolBindingStore(
                        mongo_dal.collection(
                            "orchestrator_agent_tool_bindings"
                        ).raw_collection
                    ),
                    calls=MongoAgentCallLedgerStore(
                        mongo_dal.collection("orchestrator_agent_calls").raw_collection
                    ),
                    observations=MongoObservationInboxStore(
                        mongo_dal.collection(
                            "orchestrator_a2a_observations"
                        ).raw_collection
                    ),
                    conflicts=MongoObservationConflictStore(
                        mongo_dal.collection(
                            "orchestrator_a2a_observation_conflicts"
                        ).raw_collection
                    ),
                    runs=mongo_dal.collection("orchestrator_runs").raw_collection,
                    run_events=MongoOrchestratorEventStore(
                        mongo_dal.collection("orchestrator_run_events").raw_collection
                    ),
                ),
            )
            chat_service.bind_room_deletion(room_deletion)

            execution_facade = create_execution_facade(
                room_center=conversation_runtime,
                hitl_manager=hitl_manager,
                hitl_message_cancellation=HITLMessageCancellationAdapter(hitl_manager),
                event_publisher=_delivery_deps.event_publisher,
                client_request_id_resolver=execution_client_request_id_resolver,
            )
            _execution_deps = create_execution_deps(execution_facade)

            app.state.execution_facade = execution_facade
            app.state.execution_deps = _execution_deps

            register_context_memory_event_handlers(
                event_bus=_eventing_deps.event_bus,
                context_memory_facade=context_memory_facade,
            )

            await _eventing_deps.event_bus.start()
            await _eventing_deps.event_bus.refresh_health()
            app.state.eventing_connected = _eventing_deps.event_bus.is_connected
        else:
            raise RuntimeError("MongoDAL ping failed after connect")

        if _execution_deps is None:
            raise RuntimeError("ExecutionDeps have not been bound")
        await hitl_store.ensure_indexes()
        await hitl_lifecycle_store.ensure_indexes()

        # Init DAL Redis subsystems before the guard. Delivery-owned
        # Pub/Sub/KV clients are constructed through container.py above.
        _redis_runtime = create_redis_runtime_deps(
            redis_url=runtime.settings.redis_url,
            instance_id=(
                _delivery_facade.instance_id if _delivery_facade is not None else None
            ),
        )
        _redis_service = _redis_runtime.command_client
        if _redis_service:
            redis_kv_ready = await _redis_service.ping()
            if redis_kv_ready:
                logger.info("DAL Redis KV connected (leader election enabled)")
            else:
                logger.warning("DAL Redis KV unavailable; Redis KV features disabled")
        else:
            logger.info("DAL Redis disabled (REDIS_URL not set)")
        app.state.redis_runtime = _redis_runtime

        _redis_streams_service = _redis_runtime.streams_client
        if _redis_streams_service:
            redis_streams_ready = await _redis_streams_service.ping()
            if redis_streams_ready:
                logger.info("DAL Redis Streams connected")
            else:
                logger.warning("DAL Redis Streams unavailable")

        # ── Guard: fail if gunicorn without fully connected Redis ──
        check_multi_worker_safety(
            is_gunicorn=runtime.settings.is_gunicorn,
            delivery_pubsub_connected=bool(
                _delivery_facade and _delivery_facade.delivery_pubsub_connected
            ),
            eventing_connected=(
                not bool(runtime.settings.redis_url)
                or bool(_eventing_bus and _eventing_bus.is_connected)
            ),
            delivery_kv_connected=bool(
                _delivery_facade and _delivery_facade.delivery_kv_connected
            ),
            redis_service_connected=redis_kv_ready,
            mongo_supports_transactions=(
                await mongo_dal.supports_transactions()
                if runtime.settings.is_gunicorn
                else True
            ),
        )

        # ── Phase 2: Background services (only after guard passes) ──

        if redis_kv_ready:
            _leader = _redis_runtime.leader
            logger.info("Leader election enabled for background jobs")

        if agent_health_service is None:
            raise RuntimeError("Agent health service has not been initialized")
        agent_health_service.set_leader_election(
            _leader, lock_name=AGENT_HEALTH_CHECKER
        )
        stale_task_checker.set_leader_election(_leader)
        if _execution_deps is not None:
            from jobs.stale_task_checker import StaleHITLDeps

            stale_task_checker.set_hitl_deps(
                StaleHITLDeps(
                    reconcile_lifecycle=hitl_reconciler.reconcile_lifecycle,
                )
            )
        compaction_sweep.set_leader_election(_leader)
        canonical_active_run_rooms = CanonicalActiveRunRoomReader(mongo_dal)
        compaction_sweep.set_sweep_deps(
            CompactionSweepDeps(
                list_room_ids_with_memory=(
                    context_memory_facade.memory_repository.list_room_ids_with_memory
                ),
                get_room_ids_with_non_terminal_runs=(
                    canonical_active_run_rooms.get_room_ids_with_non_terminal_runs
                ),
                context_compaction=context_memory_facade,
            )
        )
        orphaned_upload_cleaner.set_leader_election(_leader)
        orphaned_upload_cleaner.set_cleanup_deps(
            OrphanedUploadCleanerDeps(
                room_files=file_storage,
            )
        )

        _bg_started = True
        if _provider_runtime is not None:
            await _provider_runtime.start()
        await agent_health_service.start()
        if _local_agent_service is not None:
            await _local_agent_service.start()
        if _bundled_agents is not None:
            # Registration waits for agent cards in the background; health and
            # the rest of startup never wait for it.
            _bundled_agents.start()

        await stale_task_checker.start()
        await stale_task_checker.check_stale_tasks()

        await compaction_sweep.start()
        await orphaned_upload_cleaner.start()

        # Sessions bind to the conversation container, Execution (engine and
        # approvals), and the durable room event log for the bounded wait.
        session_repository = MongoSessionRepository(mongo_dal)
        await session_repository.ensure_indexes()
        session_facade = SessionFacade(
            store=session_repository,
            rooms=_room_deps.room_management,
            registry=_room_deps.room_registry,
            history=_room_deps.room_history_reader,
            messages=_room_deps.room_message_store,
            engine=_execution_deps.execution_engine,
            approvals=_execution_deps.hitl_manager,
            waiter=SessionWaiter(
                _room_event_store,
                start_grace_seconds=runtime.settings.session_run_start_grace_seconds,
            ),
        )

        bind_api_gateway_deps(
            app,
            APIGatewayDeps(
                agent_catalog=agent_catalog,
                agent_service=_agent_deps.agent_registry,
                agent_matcher=_agent_deps.agent_matcher,
                capability_issue_service=agent_capability_issue_service,
                agent_group_store=agent_runtime_store,
                file_storage=file_storage,
                room_ownership_reader=_room_deps.room_registry,
                hitl_manager=_execution_deps.hitl_manager,
                inspection_center=route_inspection_center,
                chat_rooms=chat_service,
                room_store=route_room_reader,
                agent_selection_service=agent_selection_service,
                execution_engine=_execution_deps.execution_engine,
                sse_store=sse_state_reader,
                sse_transport=_delivery_facade,
                session_facade=session_facade,
                local_agent_discovery=_local_agent_service,
            ),
        )

        # ── Orchestrator runtime composition ──
        #
        # The canonical orchestrator is the only execution path. It is built
        # eagerly so a misconfigured model route, prompt asset, or profile
        # parameter fails startup instead of the first request.
        from common.dto import RunEventNotification
        from execution.orchestrator.projection import public_terminal_status
        from execution.orchestrator.public_projection import (
            PublicProjectionTranslator,
            canonical_settlement_payload,
        )
        from execution.orchestrator.public_summaries import PublicSummaryRegistry

        canonical_public_secrets = configured_public_secret_values(runtime.settings)
        canonical_summary_registry = PublicSummaryRegistry(
            secret_values=canonical_public_secrets
        )
        canonical_projection_translator = PublicProjectionTranslator(
            summary_registry=canonical_summary_registry,
            secret_values=canonical_public_secrets,
        )
        # Causal inspection links for canonical events. Durability and ordering
        # do not rely on this process-local cache; deterministic event IDs and
        # room-event readback remain authoritative after restart.
        _canonical_parent_ids: dict[str, str] = {}

        async def _emit_canonical_processing_adapter(run, status: str) -> None:
            from delivery.producer_policy import canonical_processing_status_adapter

            if _delivery_deps is None or not run.client_request_id:
                return
            emitted = await _delivery_deps.event_publisher.emit_checked(
                canonical_processing_status_adapter(
                    conversation_id=run.conversation_id,
                    user_message_id=run.request.user_message_id,
                    client_request_id=run.client_request_id,
                    status=status,
                )
            )
            if emitted not in {
                DeliveryEmitStatus.DELIVERED,
                DeliveryEmitStatus.ALREADY_DELIVERED,
            }:
                raise RuntimeError(
                    "canonical processing_status event was not persisted"
                )

        async def publish_orchestrator_projection_status(run, intent) -> None:
            # SSE is non-authoritative: it is published only after the durable
            # public run/processing projection has completed AND the Run
            # reached projection_state == "settled" (all mandatory intents
            # durable). The status is derived from the settled Run, never from
            # the intent payload, so intent completion order cannot leak an
            # early terminal event.
            del intent
            status = public_terminal_status(str(run.status or ""))
            if status is None or _delivery_deps is None:
                return
            payload = canonical_settlement_payload(run)
            settlement_parent_id = _canonical_parent_ids.get(run.run_id)
            if settlement_parent_id is None:
                records = await _read_canonical_run_events(
                    run.conversation_id, run.run_id
                )
                final_message_id = run.proposed_final_message_id
                parent = next(
                    (
                        record
                        for record in reversed(records)
                        if record.get("kind") == "agent_response"
                        and isinstance(record.get("payload_public"), dict)
                        and record["payload_public"].get("message_id")
                        == final_message_id
                    ),
                    records[-1] if records else None,
                )
                if parent is not None:
                    settlement_parent_id = (
                        str(parent.get("room_event_id") or "") or None
                    )
            (
                settlement_status,
                settlement_event_id,
            ) = await _delivery_deps.event_publisher.emit_checked_identified(
                RunEventNotification(
                    conversation_id=run.conversation_id,
                    event_id=f"public:{run.run_id}:run_settled",
                    delivery_id=f"orchestrator:{run.run_id}:run_settled",
                    run_id=run.run_id,
                    seq=run.state_version,
                    run_event_type="run_settled",
                    payload=payload,
                    correlation_id=run.client_request_id,
                ),
                parent_event_id=settlement_parent_id,
            )
            if settlement_event_id is None and settlement_status not in {
                DeliveryEmitStatus.ALREADY_DELIVERED,
            }:
                raise RuntimeError("canonical run_settled was not durably persisted")
            await _emit_canonical_processing_adapter(
                run,
                "failed" if run.status == "budget_exhausted" else run.status,
            )

        # Lifecycle events are dispatched concurrently, so a per-run lock
        # re-serializes canonical room-event publication into durable order.
        _orchestrator_session_locks: dict[str, asyncio.Lock] = {}

        async def _orchestrator_session_listener(event: Any) -> None:
            lock = _orchestrator_session_locks.setdefault(event.run_id, asyncio.Lock())
            async with lock:
                await _orchestrator_session_event(event)
                return

        async def _orchestrator_session_event(event: Any) -> None:  # noqa: C901
            # Tool lifecycle remains solely in the canonical Run/event fold;
            # no task-backed RoomAgentMessage status projection is created.
            runtime = getattr(app.state, "orchestrator_runtime", None)

            if _delivery_deps is None:
                return
            current_run = (
                await runtime.run_store.load(event.run_id)
                if runtime is not None
                else None
            )
            if current_run is None:
                raise RuntimeError(
                    f"canonical lifecycle event has no durable Run for run_id={event.run_id}"
                )
            # Decision-visibility projection (Room Stream Snapshot plan §6,
            # Phase 1): public run_event payload types for LLM calls, retries,
            # orchestrator decisions, and tool calls. Payloads are produced
            # exclusively by the PublicProjectionTranslator (redaction).
            if event.conversation_id:
                if current_run.tool_catalog is not None:
                    canonical_summary_registry.register_catalog(
                        current_run.tool_catalog
                    )
                public_event = canonical_projection_translator.translate(
                    event, catalog=current_run.tool_catalog
                )
                if public_event is not None:
                    notification = RunEventNotification(
                        conversation_id=event.conversation_id,
                        event_id=public_event.event_id,
                        run_id=public_event.run_id,
                        seq=public_event.seq,
                        run_event_type=public_event.kind,
                        payload=public_event.payload,
                        correlation_id=public_event.client_request_id,
                    )
                    parent_event_id = _canonical_parent_ids.get(public_event.run_id)
                    if parent_event_id is None:
                        records = await _read_canonical_run_events(
                            public_event.conversation_id, public_event.run_id
                        )
                        parent_event_id = _latest_canonical_parent_id(records)
                        if parent_event_id is not None:
                            _canonical_parent_ids[public_event.run_id] = parent_event_id
                    (
                        status,
                        room_event_id,
                    ) = await _delivery_deps.event_publisher.emit_checked_identified(
                        notification,
                        parent_event_id=(
                            None
                            if public_event.kind == "run_started"
                            else parent_event_id
                        ),
                    )
                    if room_event_id is None and status not in {
                        DeliveryEmitStatus.ALREADY_DELIVERED,
                    }:
                        raise RuntimeError(
                            f"canonical lifecycle event was not durably persisted: status={status}, "
                            f"event={public_event.kind}, parent={parent_event_id}"
                        )
                    if room_event_id is not None:
                        _canonical_parent_ids[public_event.run_id] = room_event_id
                    if (
                        public_event.kind == "run_started"
                        and current_run is not None
                        and status == DeliveryEmitStatus.DELIVERED
                    ):
                        await _emit_canonical_processing_adapter(
                            current_run, "processing"
                        )

            if event.event_type == "run_final_answer_ready" and runtime is not None:
                # Deliver the final answer immediately instead of waiting for
                # the next projection-outbox tick (the <=10s dead stop). Runs
                # after the work-log 'Preparing…' entry so the frontend shows
                # the synthesis step before the terminal delivery.
                try:
                    await runtime.projection_worker.run_once(due_at=datetime.now(UTC))
                except Exception:
                    logger.warning(
                        "orchestrator projection nudge failed", exc_info=True
                    )

        try:

            async def _orchestrator_user_message_text(
                message_id: str,
            ) -> str | None:
                message = await message_store.get_room_user_message_by_message_id(
                    message_id
                )
                if message is None:
                    return None
                content = getattr(message, "message_content", None)
                text = getattr(content, "message_text", None)
                return text if isinstance(text, str) and text.strip() else None

            async def _deliver_orchestrator_final_message(
                run: Any,
                final: Any,
                content: str,
            ) -> bool:
                # The final checkpoint is causally owned by its exact
                # message_end, never by a process-local latest-event cache
                # that may already point at turn_end. Readback also keeps
                # this parent stable after restart.
                records = await _read_canonical_run_events(
                    run.conversation_id, run.run_id
                )
                final_parent_id = _canonical_final_message_end_parent_id(
                    records, final.message_id
                )
                if final_parent_id is None:
                    raise RuntimeError(
                        f"canonical final message_end parent is not durable for message_id={final.message_id}, "
                        f"records_count={len(records)}"
                    )
                (
                    status,
                    room_event_id,
                ) = await _delivery_deps.event_publisher.emit_checked_identified(
                    AgentMessageFinal(
                        conversation_id=run.conversation_id,
                        message_id=final.message_id,
                        agent_id="system:hybro",
                        content={
                            "content": content,
                            "related_message_id": run.request.user_message_id,
                            "client_request_id": run.client_request_id,
                        },
                        delivery_id=(
                            f"orchestrator:{run.run_id}:final:{final.message_id}"
                        ),
                    ),
                    parent_event_id=final_parent_id,
                )
                if room_event_id is None:
                    records = await _read_canonical_run_events(
                        run.conversation_id, run.run_id
                    )
                    persisted = next(
                        (
                            record
                            for record in reversed(records)
                            if record.get("kind") == "agent_response"
                            and isinstance(record.get("payload_public"), dict)
                            and record["payload_public"].get("message_id")
                            == final.message_id
                        ),
                        None,
                    )
                    if persisted is not None:
                        room_event_id = (
                            str(persisted.get("room_event_id") or "") or None
                        )
                if room_event_id is not None:
                    _canonical_parent_ids[run.run_id] = room_event_id
                # Persisted room-event identity is the durable delivery
                # boundary. A transient fanout miss self-heals through the
                # heartbeat watermark/snapshot path and must not duplicate the
                # outbox event.
                if room_event_id is not None:
                    return True
                return status in {
                    DeliveryEmitStatus.DELIVERED,
                    DeliveryEmitStatus.ALREADY_DELIVERED,
                }

            async def _read_canonical_run_events(
                conversation_id: str, run_id: str
            ) -> list[dict[str, object]]:
                if _room_event_store is None:
                    return []
                records: list[dict[str, object]] = []
                after = -1
                while True:
                    page = await _room_event_store.read_range(
                        conversation_id,
                        after=after,
                        limit=500,
                        include_skipped=False,
                    )
                    if not page:
                        break
                    records.extend(
                        record for record in page if record.get("run_id") == run_id
                    )
                    after = max(int(record.get("room_seq") or 0) for record in page)
                    if len(page) < 500:
                        break
                return records

            app.state.orchestrator_runtime = create_orchestrator_runtime(
                mongo=mongo_dal,
                settings_obj=runtime.settings,
                llm_gateway=llm_provider,
                model_registry=model_registry,
                agent_registry=_agent_deps.agent_registry,
                agent_call_counter=_agent_deps.agent_call_counter,
                exclusion_reader=CapabilityIssueExclusionReader(
                    agent_capability_issue_service
                ),
                room_ownership_reader=_room_deps.room_registry,
                epoch_store=require_room_epoch_store(),
                room_files=file_storage,
                message_writer=message_store,
                projection_listener=publish_orchestrator_projection_status,
                session_listener=_orchestrator_session_listener,
                user_message_text_reader=_orchestrator_user_message_text,
                hitl_delivery=(
                    None if _delivery_deps is None else _delivery_deps.event_publisher
                ),
                final_message_delivery=_deliver_orchestrator_final_message,
                final_message_memory_projection=(
                    context_memory_facade.project_message_for_event
                ),
                canonical_event_reader=_read_canonical_run_events,
                canonical_hitl_control=publish_orchestrator_hitl_control,
                supervisor_hitl=request_supervisor_input_port,
                supervisor_hitl_cancellation=(hitl_manager.cancel_requests_for_message),
            )
            missing = validate_orchestrator_runtime(app.state.orchestrator_runtime)
            if missing:
                raise OrchestratorCompositionError(
                    "Orchestrator runtime composition incomplete: " + ", ".join(missing)
                )
            # Served by api_gateway.routes.a2a_push_routes.
            app.state.a2a_push_receiver = app.state.orchestrator_runtime.push_ingress
            from conversation.agent_call_detail import CanonicalAgentCallDetailService

            app.state.canonical_agent_call_detail_reader = (
                CanonicalAgentCallDetailService(
                    app.state.orchestrator_runtime.run_store,
                    artifact_metadata_reader=file_storage,
                )
            )
            # Quotes of canonical Agent Cards validate against the call record.
            _room_facade.bind_agent_call_reader(
                app.state.canonical_agent_call_detail_reader
            )
            logger.info("Canonical orchestrator runtime composition ready")
        except OrchestratorCompositionError as exc:
            raise RuntimeError("Canonical orchestrator runtime is required") from exc

        # ── Orchestrator ingress adapter (single execution path) ──
        # The execution facade and API routes reach the orchestrator through
        # this adapter; it is attached here after the composition is ready.
        _orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
        if _orchestrator_runtime is not None:
            from execution.orchestrator.session import DefaultRunFactory
            from execution.orchestrator_routing import (
                DualRuntimeRouter,
                RoomMessageEnvelopeResolver,
            )

            async def _list_room_agent_ids(conversation_id: str) -> list[str]:
                room = await room_store.get_room_by_room_id(conversation_id)
                return list((room.room_agent_set or {}).keys()) if room else []

            async def _list_group_agent_ids(group_id: str) -> list[str]:
                group = await agent_runtime_store.get_agent_group_by_id(group_id)
                return list(getattr(group, "agents", []) or [])

            async def _list_all_active_agent_ids(
                user_id: str | None = None,
            ) -> list[str]:
                # Same visibility-filtered listing the all_agents scope uses.
                agents = await agent_runtime_store.get_all_active_agents(user_id)
                return [agent.agent_id for agent in agents or []]

            # PDF projection reuses the attachment projection service;
            # text/* attachments are decoded directly. Bounded to keep the
            # kernel turn within its context window.
            attachment_projection = AttachmentProjectionService(
                content_reader=file_storage
            )
            _MAX_ATTACHMENT_TEXT_CHARS = 120_000
            _MAX_TEXT_BYTES = 1_000_000

            async def _attachment_text(
                attachment: Any,
            ) -> str | None:
                mime_type = attachment.mime_type or ""
                if mime_type == "application/pdf":
                    _ref, payload = await attachment_projection.ensure_projection(
                        UserAttachment(
                            file_id=attachment.file_id,
                            mime_type=mime_type,
                            file_name="",
                            size_bytes=attachment.size_bytes,
                        )
                    )
                    return payload.text if payload is not None else None
                if mime_type.startswith("text/"):
                    data = await file_storage.get_bytes(
                        attachment.file_id, max_bytes=_MAX_TEXT_BYTES
                    )
                    if not data:
                        return None
                    return data.decode("utf-8", errors="replace")[
                        :_MAX_ATTACHMENT_TEXT_CHARS
                    ]
                return None

            envelope_source = RoomMessageEnvelopeResolver(
                get_user_message=message_store.get_room_user_message_by_message_id,
                list_room_agent_ids=_list_room_agent_ids,
                list_group_agent_ids=_list_group_agent_ids,
                list_all_active_agent_ids=_list_all_active_agent_ids,
                attachment_text_reader=_attachment_text,
            )
            orchestrator_router = DualRuntimeRouter(
                runtime=_orchestrator_runtime,
                envelope_source=envelope_source,
                run_factory=DefaultRunFactory(),
                room_memory_reader=(
                    context_memory_facade.memory_repository.get_room_memory
                ),
            )
            execution_facade.bind_orchestrator_router(orchestrator_router)

            class _CanonicalActiveRunReader:
                _TERMINAL = list(TERMINAL_RUN_STATUSES)

                @staticmethod
                def _to_run_info(document: dict[str, Any]):
                    from common.dto import RunInfo, RunState

                    status = str(document.get("status") or "")
                    state = {
                        "queued": RunState.QUEUED,
                        "awaiting_user": RunState.AWAITING_INPUT,
                        "canceling": "canceling",
                    }.get(status, RunState.PROCESSING)
                    request = document.get("request") or {}
                    return RunInfo(
                        run_id=str(document.get("run_id") or ""),
                        conversation_id=str(document.get("room_id") or ""),
                        state=state,
                        trigger_message_id=request.get("user_message_id"),
                        seq=int(document.get("state_version") or 0),
                        created_at=document.get("created_at"),
                        updated_at=document.get("updated_at"),
                    )

                async def _read(self, room_ids: list[str]) -> list[dict[str, Any]]:
                    if not room_ids:
                        return []
                    cursor = _orchestrator_runtime.run_store.collection.aggregate(
                        [
                            {
                                "$match": {
                                    "room_id": {"$in": room_ids},
                                    "status": {"$nin": self._TERMINAL},
                                }
                            },
                            {"$sort": {"updated_at": -1}},
                            {
                                "$project": {
                                    "_id": 0,
                                    "run_id": 1,
                                    "room_id": 1,
                                    "status": 1,
                                    "request.user_message_id": 1,
                                    "state_version": 1,
                                    "created_at": 1,
                                    "updated_at": 1,
                                }
                            },
                        ]
                    )
                    return await cursor.to_list(length=None)

                async def get_runs_for_room(self, conversation_id: str):
                    documents = await self._read([conversation_id])
                    return [self._to_run_info(document) for document in documents]

                async def get_latest_runs_for_rooms(self, room_ids: list[str]):
                    documents = await self._read(room_ids)
                    latest = {}
                    for document in documents:
                        conversation_id = str(document.get("room_id") or "")
                        if conversation_id and conversation_id not in latest:
                            latest[conversation_id] = self._to_run_info(document)
                    return latest

            execution_facade.bind_active_run_reader(_CanonicalActiveRunReader())
            logger.info("Orchestrator ingress adapter ready")

        # ── Mandatory canonical orchestrator background workers ──
        if _orchestrator_runtime is not None:
            orchestrator_recovery_job.set_leader_election(_leader)
            orchestrator_recovery_job.interval_seconds = (
                runtime.settings.orchestrator_worker_interval_seconds
            )
            orchestrator_recovery_job.set_recovery_deps(
                OrchestratorRecoveryDeps(
                    recover_once=_orchestrator_runtime.recovery_cycle.run_once
                )
            )
            orchestrator_projection_job.set_leader_election(_leader)
            orchestrator_projection_job.interval_seconds = (
                runtime.settings.orchestrator_worker_interval_seconds
            )
            orchestrator_projection_job.set_projection_deps(
                OrchestratorProjectionDeps(
                    project_once=_orchestrator_runtime.projection_worker.run_once
                )
            )

            async def collect_orchestrator_canary() -> dict[str, Any]:
                return await collect_metrics(
                    _orchestrator_runtime.run_store.collection,
                    _orchestrator_runtime.call_ledger.collection,
                    _orchestrator_runtime.observation_conflicts.collection,
                    recovery_cycle_last_run_at=(
                        orchestrator_recovery_job.last_completed_at
                    ),
                    window_seconds=(
                        runtime.settings.orchestrator_canary_run_failure_window_seconds
                    ),
                )

            orchestrator_canary_job.set_leader_election(_leader)
            orchestrator_canary_job.interval_seconds = (
                runtime.settings.orchestrator_worker_interval_seconds
            )
            orchestrator_canary_job.set_canary_deps(
                OrchestratorCanaryDeps(collect=collect_orchestrator_canary)
            )
            await orchestrator_recovery_job.start()
            await orchestrator_projection_job.start()
            await orchestrator_canary_job.start()

    except BaseException:
        # Startup cleanup never replaces the original failure. Every opened
        # stage is attempted even if an earlier close fails.
        startup_steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        # Roll back in dependency-reverse order.
        if _bg_started:
            startup_steps.extend(
                [
                    ("orchestrator-canary", orchestrator_canary_job.stop),
                    ("orchestrator-projection", orchestrator_projection_job.stop),
                    ("orchestrator-recovery", orchestrator_recovery_job.stop),
                    ("orphan-upload-cleaner", orphaned_upload_cleaner.stop),
                    ("compaction-sweep", compaction_sweep.stop),
                    ("stale-task-checker", stale_task_checker.stop),
                ]
            )
            if _local_agent_service is not None:
                startup_steps.append(("local-agent", _local_agent_service.stop))
            if _bundled_agents is not None:
                startup_steps.append(("bundled-agents", _bundled_agents.stop))
            if agent_health_service is not None:
                startup_steps.append(("agent-health", agent_health_service.stop))
            if _provider_runtime is not None:
                startup_steps.append(("provider-runtime", _provider_runtime.stop))
            if _browser_setup is not None:
                startup_steps.append(("browser-setup", _browser_setup.aclose))
        if _leader:
            startup_steps.append(
                ("leader-locks", lambda: _leader.release_all(ALL_JOB_NAMES))
            )
        startup_steps.append(
            ("redis-runtime", lambda: close_redis_runtime_deps(_redis_runtime))
        )
        if _eventing_bus is not None:
            startup_steps.append(("eventing", _eventing_bus.stop))
        if _local_agent_card_resolver is not None:
            startup_steps.append(
                ("local-agent-card-resolver", _local_agent_card_resolver.aclose)
            )
        if _bundled_card_resolver is not None:
            startup_steps.append(
                ("bundled-agent-card-resolver", _bundled_card_resolver.aclose)
            )
        if _delivery_facade is not None:
            startup_steps.append(("delivery", _delivery_facade.stop))
        if _mongo_dal is not None:
            startup_steps.append(("mongo", _mongo_dal.close))
        await _run_cleanup_steps(
            startup_steps,
            timeout_seconds=runtime.settings.eventing_shutdown_timeout_seconds,
        )
        app.state.eventing_bus = None
        app.state.eventing_connected = False
        app.state.delivery_facade = None
        app.state.mongo_dal = None
        app.state.agent_llm_proxy = None
        raise

    # ── Phase 3: Serve + Normal Shutdown ──
    try:
        yield
    finally:
        body_error = sys.exc_info()[1]

        async def cancel_execution() -> None:
            orchestrator_runtime = getattr(app.state, "orchestrator_runtime", None)
            if orchestrator_runtime is not None:
                # Cancels in-process kernel tasks without persisting terminal
                # state; recovery workers re-enter the Runs later.
                await orchestrator_runtime.session_host.shutdown()
            execution_deps = app.state.execution_deps
            cancelled = await execution_deps.execution_engine.cancel_inflight_tasks()
            if cancelled:
                logger.info(
                    "shutdown: cancelled %s in-flight execution task(s)",
                    cancelled,
                )

        async def drain_delivery() -> None:
            if _delivery_facade is not None:
                _delivery_facade.set_draining(True)
            await asyncio.sleep(
                _delivery_config.shutdown_drain_seconds
                if _delivery_config is not None
                else runtime.settings.shutdown_drain_seconds
            )

        shutdown_steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        shutdown_steps.extend(
            [
                ("orchestrator-canary", orchestrator_canary_job.stop),
                ("orchestrator-projection", orchestrator_projection_job.stop),
                ("orchestrator-recovery", orchestrator_recovery_job.stop),
                ("stale-task-checker", stale_task_checker.stop),
                ("compaction-sweep", compaction_sweep.stop),
                ("orphan-upload-cleaner", orphaned_upload_cleaner.stop),
            ]
        )
        if agent_health_service is not None:
            shutdown_steps.append(("agent-health", agent_health_service.stop))
        if _local_agent_service is not None:
            shutdown_steps.append(("local-agent", _local_agent_service.stop))
        if _bundled_agents is not None:
            shutdown_steps.append(("bundled-agents", _bundled_agents.stop))
        if _local_agent_card_resolver is not None:
            shutdown_steps.append(
                ("local-agent-card-resolver", _local_agent_card_resolver.aclose)
            )
        if _bundled_card_resolver is not None:
            shutdown_steps.append(
                ("bundled-agent-card-resolver", _bundled_card_resolver.aclose)
            )
        if _provider_runtime is not None:
            shutdown_steps.append(("provider-runtime", _provider_runtime.stop))
        if _browser_setup is not None:
            shutdown_steps.append(("browser-setup", _browser_setup.aclose))
        if _leader:
            shutdown_steps.append(
                ("leader-locks", lambda: _leader.release_all(ALL_JOB_NAMES))
            )
        shutdown_steps.extend(
            [("execution", cancel_execution), ("delivery-drain", drain_delivery)]
        )
        if _eventing_bus is not None:
            shutdown_steps.append(("eventing", _eventing_bus.stop))
        if _delivery_facade is not None:
            shutdown_steps.append(("delivery", _delivery_facade.stop))
        shutdown_steps.append(
            ("redis-runtime", lambda: close_redis_runtime_deps(_redis_runtime))
        )
        if _mongo_dal is not None:
            shutdown_steps.append(("mongo", _mongo_dal.close))

        cleanup_error = await _run_cleanup_steps(shutdown_steps)
        app.state.eventing_bus = None
        app.state.eventing_connected = False
        app.state.delivery_facade = None
        app.state.mongo_dal = None
        app.state.agent_llm_proxy = None
        if cleanup_error is not None and body_error is None:
            raise cleanup_error


@dataclass(frozen=True)
class AgentDeps:
    agent_registry: AgentRegistry
    agent_matcher: AgentMatcher
    agent_management: AgentFacade
    agent_registry_writer: AgentFacade
    agent_call_counter: AgentCallCounter
    agent_repository: AgentRepository


@dataclass(frozen=True)
class RoomDeps:
    room_registry: RoomRegistry
    room_management: RoomManagement
    room_message_store: RoomMessageStore
    room_history_reader: RoomHistoryReader
    room_ownership_reader: RoomOwnershipReader
    room_repository: Any
    message_repository: Any
    chat_facade: ChatFacade
    room_quote_repository: Any | None = None


@dataclass(frozen=True)
class DeliveryDeps:
    event_publisher: EventPublisher
    sse_transport: SSETransport


@dataclass(frozen=True)
class EventingDeps:
    event_bus: InternalEventBus
    internal_event_publisher: InternalEventPublisher


@dataclass(frozen=True)
class ExecutionDeps:
    execution_engine: ExecutionEngine
    hitl_manager: HITLManager


@dataclass(frozen=True)
class RedisRuntimeDeps:
    command_client: RedisKV | None
    streams_client: RedisStreamsImpl | None
    leader: LeaderElector | None
    room_lock: RoomDistributedLock | None


def create_redis_runtime_deps(
    *,
    redis_url: str,
    instance_id: str | None = None,
) -> RedisRuntimeDeps:
    if not redis_url:
        return RedisRuntimeDeps(
            command_client=None,
            streams_client=None,
            leader=None,
            room_lock=None,
        )

    from dal.redis.kv import RedisKVImpl
    from dal.redis.lock import LeaderElectorImpl, RoomRedisDistributedLock
    from dal.redis.streams import RedisStreamsImpl

    command_client = RedisKVImpl(url=redis_url)
    shared_command_client = command_client._ensure_client()
    streams_client = RedisStreamsImpl(url=redis_url)
    return RedisRuntimeDeps(
        command_client=command_client,
        streams_client=streams_client,
        leader=(
            LeaderElectorImpl(client=shared_command_client, instance_id=instance_id)
            if instance_id is not None
            else None
        ),
        room_lock=RoomRedisDistributedLock(client=shared_command_client),
    )


async def close_redis_runtime_deps(redis_runtime: RedisRuntimeDeps | None) -> None:
    if redis_runtime is None:
        return

    closed: set[int] = set()
    for attr in ("streams_client", "command_client", "leader", "room_lock"):
        client = getattr(redis_runtime, attr, None)
        close = getattr(client, "close", None)
        close_target = getattr(client, "_client", None) or client
        if close is None or id(close_target) in closed:
            continue
        closed.add(id(close_target))
        await close()


def create_mongo_dal() -> MongoDAL:
    from dal.mongo import MongoDALImpl

    return MongoDALImpl()


async def ensure_runtime_indexes(*, mongo: MongoDAL) -> dict[str, bool]:
    """Ask each collection owner to create its indexes, in dependency order."""
    from agent.repository import AgentRuntimeStore
    from execution.repository.orchestrator.indexes import (
        ensure_orchestrator_indexes,
    )
    from room_files import ensure_room_file_indexes

    agent_repository = AgentMongoRepository(mongo)
    agent_search_index_ready = await agent_repository.ensure_indexes()
    await AgentRuntimeStore(
        agent_groups=mongo.collection("agent_groups"),
        agent_repository=agent_repository,
    ).ensure_indexes()
    await ContentStorageMongoRepository(mongo=mongo).ensure_indexes()
    await MemoryMongoRepository(mongo=mongo).ensure_indexes()
    await create_agent_capability_issue_repository(mongo).ensure_indexes()
    await ensure_orchestrator_indexes(mongo)
    await RoomQuoteMongoRepository(mongo).ensure_indexes()
    await RoomMongoRepository(mongo).ensure_indexes()
    await MessageMongoRepository(mongo).ensure_indexes()
    await ensure_room_file_indexes(mongo)
    return {
        "agent_search_index_ready": agent_search_index_ready,
    }


def create_agent_capability_issue_repository(mongo: MongoDAL) -> Any:
    from agent.repository.capability_issue_mongo import (
        AgentCapabilityIssueMongoRepository,
    )

    return AgentCapabilityIssueMongoRepository(mongo=mongo)


def create_agent_capability_issue_service(*, repository: Any) -> Any:
    from agent.capability_issue import AgentCapabilityIssueService

    return AgentCapabilityIssueService(repository=repository)


def create_file_storage(
    *,
    room_files_collection: MongoCollection,
    room_lifecycle: RoomLifecycleStore,
    room_messages_collection: MongoCollection,
    room_agent_messages_collection: MongoCollection,
    file_dir: str = "",
    max_upload_bytes: int = 5 * 1024 * 1024,
    content_url_prefix: str = "/api/v1/files",
) -> FileStorage:
    from platformdirs import user_data_path

    root = file_dir or str(user_data_path("hybro", appauthor=False) / "files")
    return RoomFiles(
        metadata=room_files_collection,
        content=LocalFileContentStore(root),
        room_lifecycle=room_lifecycle,
        messages=room_messages_collection,
        agent_messages=room_agent_messages_collection,
        lease_writes=True,
        max_upload_bytes=max_upload_bytes,
        content_url_prefix=content_url_prefix,
    )


async def _create_ready_room_event_store(*, mongo: MongoDAL):
    from delivery.room_events import MongoRoomEventStore

    store = MongoRoomEventStore(mongo=mongo)
    await store.ensure_indexes()
    return store


def create_delivery_config(app_settings: Any = settings) -> DeliveryConfig:
    defaults = DeliveryConfig()
    values = {
        field: getattr(app_settings, field, getattr(defaults, field))
        for field in DeliveryConfig.__dataclass_fields__
    }
    terminal_statuses = values["terminal_processing_statuses"]
    if isinstance(terminal_statuses, str):
        values["terminal_processing_statuses"] = [
            status.strip() for status in terminal_statuses.split(",") if status.strip()
        ]
    return DeliveryConfig(**values)


def create_delivery_redis_clients(
    *,
    redis_url: str,
    config: DeliveryConfig,
) -> tuple[RedisKV | None, RedisPubSub | None]:
    if not redis_url:
        return None, None

    from dal.redis.kv import RedisKVImpl
    from dal.redis.pubsub import RedisPubSubImpl

    return (
        RedisKVImpl(url=redis_url),
        RedisPubSubImpl(
            url=redis_url,
            max_connections=config.redis_max_connections,
        ),
    )


def create_internal_event_bus(
    *,
    redis_url: str,
    instance_id: str,
    app_settings: Any = settings,
) -> BoundedInternalEventBus:
    transport = None
    if redis_url:
        from dal.redis.internal_eventing import RedisInternalEventTransport
        from dal.redis.pubsub import RedisPubSubImpl

        transport = RedisInternalEventTransport(
            redis_pubsub=RedisPubSubImpl(
                url=redis_url,
                max_connections=app_settings.redis_max_connections,
            ),
            channel=app_settings.eventing_redis_channel,
            dead_letter_channel=app_settings.eventing_redis_dead_letter_channel,
            reconnect_delay=app_settings.redis_reconnect_delay,
            reconnect_max_delay=app_settings.redis_reconnect_max_delay,
            subscription_ready_timeout=(
                app_settings.redis_room_subscription_ready_timeout_seconds
            ),
            io_timeout=app_settings.eventing_redis_io_timeout_seconds,
        )
    return BoundedInternalEventBus(
        registry=EventModelRegistry(),
        instance_id=instance_id,
        now=utcnow,
        transport=transport,
        config=EventingConfig(
            handler_queue_maxsize=app_settings.eventing_handler_queue_maxsize,
            auxiliary_task_maxsize=app_settings.eventing_auxiliary_task_maxsize,
            enqueue_timeout_seconds=app_settings.eventing_enqueue_timeout_seconds,
            shutdown_timeout_seconds=app_settings.eventing_shutdown_timeout_seconds,
            dead_letter_memory_maxlen=(app_settings.eventing_dead_letter_memory_maxlen),
        ),
    )


def register_internal_event_models(registry: EventModelRegistry) -> None:
    from common.dto import MessageCommitted, RunStateChanged

    registry.register("message_committed", MessageCommitted)
    registry.register("run_state_changed", RunStateChanged)


def create_eventing_deps(event_bus: InternalEventBus) -> EventingDeps:
    return EventingDeps(
        event_bus=event_bus,
        internal_event_publisher=event_bus,
    )


def create_delivery_facade(
    *,
    redis_kv: RedisKV | None = None,
    redis_pubsub: RedisPubSub | None = None,
    config: DeliveryConfig | None = None,
    now: Callable[[], Any] | None = None,
    id_factory: Callable[[], str] | None = None,
    instance_id: str | None = None,
    task_runner: TaskRunner | None = None,
    metrics: MetricsCollector | None = None,
    room_events: Any | None = None,
    snapshot_provider: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    room_seq_reader: Callable[[str], Awaitable[int | None]] | None = None,
) -> DeliveryFacade:
    resolved_config = config or DeliveryConfig()
    resolved_now = now or utcnow
    resolved_id_factory = id_factory or (lambda: uuid4().hex)
    resolved_instance_id = instance_id or (
        get_instance_id() if id_factory is None else resolved_id_factory()
    )
    resolved_task_runner = task_runner or traced_create_task

    event_bus = CrossInstanceEventBus(
        redis_pubsub=redis_pubsub,
        config=resolved_config,
        instance_id=resolved_instance_id,
        task_runner=resolved_task_runner,
        now=resolved_now,
    )
    sse_transport = SSETransportImpl(
        event_bus=event_bus,
        config=resolved_config,
        now=resolved_now,
        id_factory=resolved_id_factory,
        instance_id=resolved_instance_id,
        task_runner=resolved_task_runner,
        metrics=metrics,
        room_seq_reader=room_seq_reader,
        snapshot_provider=snapshot_provider,
    )
    deduplicator = TerminalStatusDeduplicator(
        redis_kv=redis_kv,
        config=resolved_config,
    )
    event_publisher = EventPublisherImpl(
        sse_transport=sse_transport,
        event_bus=event_bus,
        deduplicator=deduplicator,
        config=resolved_config,
        now=resolved_now,
        instance_id=resolved_instance_id,
        metrics=metrics,
        room_events=room_events,
        single_instance=redis_pubsub is None,
    )
    event_bus.set_sse_callback(sse_transport.broadcast_frame_to_room)

    return DeliveryFacade(
        event_publisher=event_publisher,
        sse_transport=sse_transport,
        event_bus=event_bus,
        redis_kv=redis_kv,
        config=resolved_config,
        instance_id=resolved_instance_id,
    )


def create_delivery_deps(facade: DeliveryFacade) -> DeliveryDeps:
    return DeliveryDeps(
        event_publisher=facade.event_publisher,
        sse_transport=facade.sse_transport,
    )


def create_execution_facade(**kwargs: Any):
    from execution.facade import ExecutionFacade

    return ExecutionFacade(**kwargs)


def create_execution_deps(facade) -> ExecutionDeps:
    return ExecutionDeps(
        execution_engine=facade,
        hitl_manager=facade,
    )


def create_bundled_agent_service(
    settings: Any,
    *,
    repository: AgentRepository,
    card_resolver: AgentCardResolver,
    provider_runtime: Any,
) -> Any:
    """Bundled agent registration, re-gated on every Provider pair swap.

    An unreadable or invalid manifest fails startup.
    """
    from agent.bundled import BundledAgentService, ProviderAvailability, load_manifest

    def availability() -> ProviderAvailability:
        pair = provider_runtime.current
        models = pair.config.models
        return ProviderAvailability(
            configured=pair.state == "configured",
            image_model=bool(models and models.image),
        )

    service = BundledAgentService(
        repository=repository,
        card_resolver=card_resolver,
        agents=load_manifest(Path(settings.bundled_agents_manifest)),
        provider_id=settings.default_agent_provider_id,
        availability=availability,
        id_factory=lambda: uuid4().hex,
        now=utcnow,
    )
    provider_runtime.add_listener(lambda _pair: service.on_provider_change())
    return service


def create_agent_deps(
    *,
    mongo: MongoDAL,
    card_resolver: AgentCardResolver,
    exclusion_reader: AgentExclusionReader | None = None,
) -> AgentDeps:
    repository = AgentMongoRepository(mongo=mongo)
    facade = AgentFacade(
        repository=repository,
        card_resolver=card_resolver,
        exclusion_reader=exclusion_reader,
        id_factory=lambda: uuid4().hex,
        now=utcnow,
    )
    return AgentDeps(
        agent_registry=facade,
        agent_matcher=facade,
        agent_management=facade,
        agent_registry_writer=facade,
        agent_call_counter=facade,
        agent_repository=repository,
    )


def create_room_deps(
    *,
    mongo: MongoDAL,
    agent_registry: AgentRegistry,
    membership_source: RoomMembershipSeedSource,
    attachment_metadata_reader: AttachmentMetadataReader | None = None,
    epoch_store: Any | None = None,
) -> RoomDeps:
    repository = RoomMongoRepository(mongo=mongo)
    message_repository = MessageMongoRepository(mongo=mongo)
    quote_repository = RoomQuoteMongoRepository(mongo=mongo)
    facade = ConversationFacade(
        repository=repository,
        message_repository=message_repository,
        agent_registry=agent_registry,
        membership_source=membership_source,
        quote_repository=quote_repository,
        attachment_metadata_reader=attachment_metadata_reader,
        id_factory=lambda: uuid4().hex,
        now=utcnow,
        epoch_store=epoch_store,
    )
    return RoomDeps(
        room_registry=facade,
        room_management=facade,
        room_message_store=facade,
        room_history_reader=facade,
        room_ownership_reader=facade,
        room_repository=repository,
        message_repository=message_repository,
        chat_facade=ChatFacade(conversation=facade, repository=repository),
        room_quote_repository=quote_repository,
    )


def create_context_memory_facade(
    *,
    mongo: MongoDAL,
    room_history_reader: RoomHistoryReader,
    memory_repository: MemoryRepository | None = None,
    content_repository: ContentStorageRepository | None = None,
    compaction_config: CompactionConfig | None = None,
) -> ContextMemoryFacade:
    memory_repository = memory_repository or MemoryMongoRepository(mongo=mongo)
    content_repository = content_repository or ContentStorageMongoRepository(
        mongo=mongo
    )
    compaction_config = compaction_config or CompactionConfig(
        enabled=settings.compaction_enabled,
        max_full_turns=settings.compaction_max_full_turns,
        max_total_tokens=settings.compaction_max_total_tokens,
        preserve_recent_turns=settings.compaction_preserve_recent,
        content_ttl_days=settings.compaction_content_ttl_days,
        concurrency=settings.compaction_concurrency,
    )
    return ContextMemoryFacade(
        memory_repository=memory_repository,
        content_repository=content_repository,
        room_history_reader=room_history_reader,
        id_factory=lambda: str(uuid4()),
        now=utcnow,
        compaction_config=compaction_config,
    )


def register_context_memory_event_handlers(
    *,
    event_bus: InternalEventBus,
    context_memory_facade: ContextMemoryFacade,
):
    from context_memory.events import ContextMemoryEventHandler

    handler = ContextMemoryEventHandler(projection=context_memory_facade)
    event_bus.register_handler(
        "message_committed",
        handler.handle_message_committed,
    )
    return handler
