from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from logging import ERROR, WARNING
from typing import Any

from pydantic import BaseModel

from common.eventing.eventing_config import EventingConfig
from common.eventing.models import EventDeadLetter, EventEnvelope
from common.eventing.protocols import EventHandler, InternalEventTransport
from common.eventing.registry import EventModelRegistry
from common.observability import events as E
from common.observability import get_current_trace_id, get_logger, trace_id_context
from common.observability.catalog import CatalogViolation

log = get_logger(__name__)

# Stages where the dead letter means a handler did not run to completion.
_ERROR_STAGES = frozenset({"handler", "worker"})


@dataclass(slots=True)
class _QueuedEvent:
    event: BaseModel
    trace_id: str | None
    completion: asyncio.Future[None]


@dataclass(slots=True)
class _HandlerState:
    event_type: str
    handler: EventHandler
    queue: asyncio.Queue[_QueuedEvent]
    task: asyncio.Task[None] | None = None
    current_item: _QueuedEvent | None = None


_DEAD_LETTER_MAX_JSON_BYTES = 8192
_METADATA_MAX_FIELDS = 24
_METADATA_KEY_MAX_CHARS = 64
_METADATA_VALUE_MAX_CHARS = 256
_IDENTIFIER_MAX_FIELDS = 32
_IDENTIFIER_VALUE_MAX_CHARS = 256
_PAYLOAD_KEY_MAX_FIELDS = 64
_PAYLOAD_KEY_MAX_CHARS = 64
_SAFE_IDENTIFIER_KEYS = frozenset(
    {
        "id",
        "conversation_id",
        # Internal event payloads keep the stored ``room_id`` key for rolling
        # deploys (see ``InternalDomainEvent``).
        "room_id",
        "run_id",
        "message_id",
        "user_message_id",
        "related_message_id",
        "task_id",
        "agent_id",
        "hub_id",
        "journal_id",
        "idempotency_key",
        "client_request_id",
        "correlation_id",
        "trace_id",
        "event_id",
        "request_id",
        "context_id",
        "turn_id",
        "slot_id",
    }
)
_SAFE_PAYLOAD_KEYS = _SAFE_IDENTIFIER_KEYS | frozenset(
    {
        "event_type",
        "timestamp",
        "kind",
        "origin",
        "payload",
        "metadata",
        "is_terminal",
        "status",
        "operation",
        "handler",
        "value",
    }
)


class BoundedInternalEventBus:
    """Process-local ordered handlers with optional generic remote fan-out."""

    def __init__(
        self,
        *,
        registry: EventModelRegistry,
        instance_id: str,
        now: Callable[[], datetime],
        config: EventingConfig | None = None,
        transport: InternalEventTransport | None = None,
    ) -> None:
        self.registry = registry
        self.instance_id = instance_id
        self.config = config or EventingConfig()
        self.transport = transport
        self._now = now
        self._handlers: dict[str, list[_HandlerState]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._admission_condition = asyncio.Condition()
        self._active_publications = 0
        self._accepting = False
        self._started = False
        self._starting = False
        self._stopping = False
        self._auxiliary_tasks: dict[asyncio.Task[Any], str] = {}
        self.dead_letters: deque[EventDeadLetter] = deque(
            maxlen=self.config.dead_letter_memory_maxlen
        )

    @property
    def is_connected(self) -> bool:
        return bool(self.transport and self.transport.is_connected)

    @property
    def worker_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(
            state.task
            for states in self._handlers.values()
            for state in states
            if state.task is not None
        )

    def register_handler(self, event_type: str, handler: EventHandler) -> None:
        if self.registry.frozen or self._started:
            raise RuntimeError(
                "Internal event handlers must be registered before start"
            )
        if not callable(handler):
            raise TypeError("handler must be callable")
        state = _HandlerState(
            event_type=event_type,
            handler=handler,
            queue=asyncio.Queue(maxsize=self.config.handler_queue_maxsize),
        )
        self._handlers.setdefault(event_type, []).append(state)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started and self._accepting:
                return
            await self._set_accepting(False)
            self._prune_done_auxiliary_tasks()
            self._raise_if_lifecycle_tasks_pending()
            self._stopping = False
            self._starting = True
            self._started = True
            self.registry.freeze()
            for states in self._handlers.values():
                for index, state in enumerate(states):
                    if state.task is not None and not state.task.done():
                        continue
                    task = asyncio.create_task(
                        self._worker(state),
                        name=f"eventing-handler-{state.event_type}-{index}",
                    )
                    state.task = task
                    task.add_done_callback(
                        lambda done, owned_state=state: self._worker_done(
                            owned_state, done
                        )
                    )
            try:
                if self.transport is not None:
                    await self.transport.start(self.handle_remote_message)
                    await self.transport.refresh_health()
                await self._complete_start(success=True)
            except BaseException:
                await self._complete_start(success=False)
                deadline = (
                    asyncio.get_running_loop().time()
                    + self.config.shutdown_timeout_seconds
                )
                if self.transport is not None:
                    try:
                        await self._await_bounded(
                            self.transport.stop(),
                            timeout=self._remaining(deadline),
                            operation="transport_start_rollback",
                            allow_over_capacity=True,
                        )
                    except BaseException:
                        pass
                await self._cancel_workers(deadline)
                await self._cancel_auxiliary_tasks(deadline)
                raise

    def _raise_if_lifecycle_tasks_pending(self) -> None:
        live_worker = any(
            state.task is not None and not state.task.done()
            for states in self._handlers.values()
            for state in states
        )
        if live_worker:
            raise RuntimeError("eventing handler cleanup is still pending")
        if self._auxiliary_tasks:
            operations = ", ".join(sorted(set(self._auxiliary_tasks.values())))
            raise RuntimeError(
                f"eventing auxiliary cleanup is still pending: {operations}"
            )

    async def stop(self) -> None:
        cancelled: asyncio.CancelledError | None = None
        async with self._lifecycle_lock:
            cleanup = asyncio.create_task(
                self._stop_cleanup(),
                name="eventing-stop-cleanup",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
            await asyncio.gather(cleanup, return_exceptions=True)
            cleanup_error = cleanup.exception()
        if cleanup_error is not None:
            raise cleanup_error
        if cancelled is not None:
            raise cancelled

    async def refresh_health(self) -> None:
        if self.transport is not None:
            await self.transport.refresh_health()

    async def publish(
        self,
        event: BaseModel,
        *,
        wait_for_handlers: bool = False,
        fanout: bool = True,
    ) -> None:
        if not await self._admit_publication():
            raise RuntimeError("Internal event bus is not running")
        completions: list[asyncio.Future[None]] = []
        try:
            event_type = self.registry.event_type_for(event)
            trace_id = get_current_trace_id()
            deadline = self._publication_deadline()
            completions = await self._enqueue_handlers(
                event_type,
                event,
                trace_id,
                deadline=deadline,
            )
            if fanout and self.transport is not None:
                envelope = EventEnvelope(
                    origin=self.instance_id,
                    event_type=event_type,
                    event=event.model_dump(mode="json"),
                    trace_id=trace_id,
                    timestamp=self._now(),
                )
                try:
                    await self._await_bounded(
                        self.transport.publish(envelope.model_dump_json()),
                        timeout=self._remaining(deadline),
                        operation="fanout_publish",
                    )
                except Exception as exc:
                    await self._safe_dead_letter(
                        "fanout",
                        event,
                        exc,
                        event_type=event_type,
                        timeout=self._remaining(deadline),
                    )
        finally:
            await asyncio.shield(self._finish_publication())
        if wait_for_handlers and completions:
            await asyncio.gather(*completions)

    async def handle_remote_message(self, message: str) -> None:
        envelope: EventEnvelope | None = None
        try:
            envelope = EventEnvelope.model_validate_json(message)
            if envelope.kind != "internal_event":
                raise ValueError("Unexpected internal event envelope kind")
            if envelope.origin == self.instance_id:
                return
            event = self.registry.deserialize(envelope.event_type, envelope.event)
            if getattr(event, "event_type", None) != envelope.event_type:
                raise ValueError("Internal event envelope type mismatch")
        except Exception as exc:
            with trace_id_context(envelope.trace_id if envelope else None):
                await self._safe_dead_letter(
                    "deserialization",
                    envelope.model_dump(mode="json") if envelope else message,
                    exc,
                    event_type=envelope.event_type if envelope else None,
                )
            return
        if not await self._admit_remote_publication():
            return
        try:
            with trace_id_context(envelope.trace_id):
                await self._enqueue_handlers(
                    envelope.event_type,
                    event,
                    envelope.trace_id,
                    deadline=self._publication_deadline(),
                )
        finally:
            await asyncio.shield(self._finish_publication())

    async def _enqueue_handlers(
        self,
        event_type: str,
        event: BaseModel,
        trace_id: str | None,
        *,
        deadline: float,
    ) -> list[asyncio.Future[None]]:
        completions: list[asyncio.Future[None]] = []
        for state in self._handlers.get(event_type, []):
            completion = asyncio.get_running_loop().create_future()
            completions.append(completion)
            item = _QueuedEvent(event=event, trace_id=trace_id, completion=completion)
            try:
                await asyncio.wait_for(
                    state.queue.put(item),
                    timeout=self._remaining(deadline),
                )
            except TimeoutError as exc:
                await self._safe_dead_letter(
                    "queue_full",
                    event,
                    exc,
                    event_type=event_type,
                    metadata={"handler": self._handler_name(state.handler)},
                    timeout=self._remaining(deadline),
                )
                completion.set_result(None)
        return completions

    async def _worker(self, state: _HandlerState) -> None:
        worker_task = asyncio.current_task()
        while True:
            try:
                item = await state.queue.get()
            except BaseException as exc:
                if self._is_worker_cancellation(exc):
                    raise
                await self._safe_dead_letter(
                    "worker",
                    {"handler": self._handler_name(state.handler)},
                    exc,
                    event_type=state.event_type,
                )
                await asyncio.sleep(0)
                continue
            state.current_item = item
            try:
                await self._process_item(state, item)
            finally:
                if state.current_item is item:
                    state.current_item = None
            if state.task is not worker_task or self._stopping or not self._started:
                return

    def _worker_done(
        self,
        state: _HandlerState,
        task: asyncio.Task[None],
    ) -> None:
        self._consume_task_result(task)
        if state.task is task:
            state.task = None

    async def _process_item(
        self,
        state: _HandlerState,
        item: _QueuedEvent,
    ) -> None:
        try:
            with trace_id_context(item.trace_id):
                try:
                    result = state.handler(item.event)
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    if self._is_worker_cancellation(exc):
                        raise
                    await self._safe_dead_letter(
                        "handler",
                        item.event,
                        exc,
                        event_type=state.event_type,
                        metadata={"handler": self._handler_name(state.handler)},
                    )
        except BaseException as exc:
            if self._is_worker_cancellation(exc):
                raise
            await self._safe_dead_letter(
                "worker",
                item.event,
                exc,
                event_type=state.event_type,
                metadata={"handler": self._handler_name(state.handler)},
            )
        finally:
            if not item.completion.done():
                item.completion.set_result(None)
            try:
                state.queue.task_done()
            except BaseException as exc:
                if self._is_worker_cancellation(exc):
                    raise
                await self._safe_dead_letter(
                    "worker",
                    item.event,
                    exc,
                    event_type=state.event_type,
                    metadata={"handler": self._handler_name(state.handler)},
                )

    async def _safe_dead_letter(
        self,
        stage: str,
        payload: Any,
        exc: BaseException,
        *,
        event_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        publish: bool = True,
    ) -> None:
        try:
            await self._dead_letter(
                stage,
                payload,
                exc,
                event_type=event_type,
                metadata=metadata,
                timeout=timeout,
                publish=publish,
            )
        except CatalogViolation:
            # Raised only in strict mode (tests): a bad emit must fail loudly.
            raise
        except BaseException as dead_letter_exc:
            if self._is_worker_cancellation(dead_letter_exc):
                raise

    async def _dead_letter(
        self,
        stage: str,
        payload: Any,
        exc: BaseException,
        *,
        event_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        publish: bool = True,
    ) -> None:
        dead_letter = self._record_memory_dead_letter(
            stage,
            payload,
            exc,
            event_type=event_type,
            metadata=metadata,
        )
        if self.transport is None or not publish:
            return
        try:
            await self._await_bounded(
                self.transport.publish_dead_letter(dead_letter.model_dump_json()),
                timeout=(
                    self.config.enqueue_timeout_seconds
                    if timeout is None
                    else max(timeout, 0.001)
                ),
                operation="dead_letter_publish",
            )
        except BaseException as publish_exc:
            if self._is_worker_cancellation(publish_exc):
                raise
            log.emit(
                E.EVENTING_DEAD_LETTER_PUBLISH_FAILED,
                exc=publish_exc,
                failure_stage=dead_letter.failure_stage,
                event_type=dead_letter.event_type,
            )

    def _record_memory_dead_letter(
        self,
        stage: str,
        payload: Any,
        exc: BaseException,
        *,
        event_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EventDeadLetter:
        projection = self._redacted_payload_projection(payload)
        resolved_event_type = event_type or getattr(payload, "event_type", None)
        current_trace_id = get_current_trace_id()
        dead_letter = EventDeadLetter(
            origin=self.instance_id[:128],
            failure_stage=stage[:64],
            event_type=(
                str(resolved_event_type)[:128]
                if resolved_event_type is not None
                else None
            ),
            trace_id=current_trace_id[:128] if current_trace_id else None,
            payload=projection,
            exception_class=exc.__class__.__name__[:128],
            exception_message=self._exception_summary(exc),
            timestamp=self._now(),
            metadata=self._bounded_metadata(metadata),
        )
        if (
            len(dead_letter.model_dump_json().encode("utf-8"))
            > _DEAD_LETTER_MAX_JSON_BYTES
        ):
            dead_letter = dead_letter.model_copy(
                update={
                    "payload": {
                        "payload_size_bytes": projection["payload_size_bytes"],
                        "payload_sha256": projection["payload_sha256"],
                    },
                    "metadata": {"dead_letter_truncated": True},
                }
            )
        self.dead_letters.append(dead_letter)
        # Metadata keys never shadow emit's own parameters or the fixed fields.
        fields = {
            key: value
            for key, value in dead_letter.metadata.items()
            if key not in {"level", "exc"}
        }
        fields.update(
            failure_stage=dead_letter.failure_stage,
            event_type=dead_letter.event_type,
            exception_class=dead_letter.exception_class,
            origin=dead_letter.origin,
        )
        log.emit(
            E.EVENTING_DEAD_LETTER,
            level=ERROR if stage in _ERROR_STAGES else WARNING,
            **fields,
        )
        return dead_letter

    async def _set_accepting(self, accepting: bool) -> None:
        async with self._admission_condition:
            self._accepting = accepting
            self._admission_condition.notify_all()

    async def _is_accepting(self) -> bool:
        async with self._admission_condition:
            return self._started and self._accepting and not self._stopping

    async def _complete_start(self, *, success: bool) -> None:
        async with self._admission_condition:
            self._starting = False
            self._started = success
            self._accepting = success
            self._admission_condition.notify_all()

    async def _admit_publication(self) -> bool:
        async with self._admission_condition:
            if not self._started or not self._accepting or self._stopping:
                return False
            self._active_publications += 1
            return True

    async def _admit_remote_publication(self) -> bool:
        async with self._admission_condition:
            while self._starting and not self._stopping:
                await self._admission_condition.wait()
            if not self._started or not self._accepting or self._stopping:
                return False
            self._active_publications += 1
            return True

    async def _finish_publication(self) -> None:
        async with self._admission_condition:
            self._active_publications -= 1
            if self._active_publications == 0:
                self._admission_condition.notify_all()

    async def _wait_for_active_publications(self) -> None:
        async with self._admission_condition:
            while self._active_publications:
                await self._admission_condition.wait()

    async def _stop_cleanup(self) -> None:
        self._stopping = True
        deadline = (
            asyncio.get_running_loop().time() + self.config.shutdown_timeout_seconds
        )
        try:
            await self._set_accepting(False)
            await self._stop_transport_ingress(self._remaining(deadline))
            # Publications are intrinsically bounded by their own deadline.
            # Waiting for every admitted publication prevents any queue put or
            # fanout from racing worker cancellation.
            await self._wait_for_active_publications()
            await self._stop_transport(self._remaining(deadline))
            await self._drain_handler_queues(self._remaining(deadline))
        finally:
            await self._cancel_workers(deadline)
            await self._cancel_auxiliary_tasks(deadline)
            self._started = False

    async def _stop_transport_ingress(self, timeout: float) -> None:
        if self.transport is None:
            return
        stop_ingress = getattr(self.transport, "stop_ingress", None)
        if not callable(stop_ingress):
            return
        try:
            await self._await_bounded(
                stop_ingress(),
                timeout=timeout,
                operation="transport_stop_ingress",
                allow_over_capacity=True,
            )
        except TimeoutError:
            pass
        except Exception:
            pass

    async def _stop_transport(self, timeout: float) -> None:
        if self.transport is None:
            return
        try:
            await self._await_bounded(
                self.transport.stop(),
                timeout=timeout,
                operation="transport_stop",
                allow_over_capacity=True,
            )
        except TimeoutError:
            pass
        except Exception:
            pass

    async def _drain_handler_queues(self, timeout: float) -> None:
        queues = [state.queue for states in self._handlers.values() for state in states]
        if not queues:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in queues)),
                timeout=timeout,
            )
        except TimeoutError:
            pass

    async def _cancel_workers(self, deadline: float) -> None:
        states = [state for group in self._handlers.values() for state in group]
        owned_tasks = [
            (state, state.task) for state in states if state.task is not None
        ]
        tasks = [task for _state, task in owned_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self._remaining(deadline),
            )
        else:
            done, pending = set(), set()
        for task in done:
            self._consume_task_result(task)
        try:
            self._settle_worker_shutdown(owned_tasks, pending)
        finally:
            self._settle_queued_completions(states)

    def _settle_worker_shutdown(
        self,
        owned_tasks: list[tuple[_HandlerState, asyncio.Task[None]]],
        pending: set[asyncio.Task[None]],
    ) -> None:
        for state, task in owned_tasks:
            if task not in pending:
                if state.task is task:
                    state.task = None
                continue
            item = state.current_item
            if item is not None and not item.completion.done():
                item.completion.set_result(None)
            self._record_memory_dead_letter(
                "shutdown_handler_timeout",
                {"event_type": state.event_type},
                TimeoutError("event handler ignored shutdown cancellation"),
                event_type=state.event_type,
                metadata={
                    "handler": self._handler_name(state.handler),
                    "task_name": task.get_name(),
                    "queue_size": state.queue.qsize(),
                },
            )

    @staticmethod
    def _settle_queued_completions(states: list[_HandlerState]) -> None:
        for state in states:
            while not state.queue.empty():
                item = state.queue.get_nowait()
                if not item.completion.done():
                    item.completion.set_result(None)
                state.queue.task_done()

    async def _await_bounded(
        self,
        awaitable,
        *,
        timeout: float,
        operation: str,
        allow_over_capacity: bool = False,
    ):
        self._prune_done_auxiliary_tasks()
        if allow_over_capacity and any(
            existing_operation == operation
            for existing_operation in self._auxiliary_tasks.values()
        ):
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError(f"eventing {operation} is already pending")
        if (
            not allow_over_capacity
            and len(self._auxiliary_tasks) >= self.config.auxiliary_task_maxsize
        ):
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            error = RuntimeError("eventing auxiliary task capacity exhausted")
            self._record_memory_dead_letter(
                "auxiliary_task_capacity",
                {"operation": operation},
                error,
                metadata={
                    "operation": operation,
                    "capacity": self.config.auxiliary_task_maxsize,
                },
            )
            raise error

        task = asyncio.ensure_future(awaitable)
        self._auxiliary_tasks[task] = operation
        task.add_done_callback(self._auxiliary_done)
        try:
            done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.001))
        except BaseException:
            task.cancel()
            raise
        if not done:
            task.cancel()
            raise TimeoutError
        return task.result()

    def _prune_done_auxiliary_tasks(self) -> None:
        for task in tuple(self._auxiliary_tasks):
            if task.done():
                self._auxiliary_done(task)

    def _auxiliary_done(self, task: asyncio.Task[Any]) -> None:
        self._auxiliary_tasks.pop(task, None)
        self._consume_task_result(task)

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _cancel_auxiliary_tasks(self, deadline: float) -> None:
        tasks = tuple(self._auxiliary_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self._remaining(deadline),
            )
        else:
            done, pending = set(), set()
        for task in done:
            self._consume_task_result(task)
        for task in pending:
            operation = self._auxiliary_tasks.get(task, "unknown")
            self._record_memory_dead_letter(
                "auxiliary_task_timeout",
                {"operation": operation},
                TimeoutError("bounded eventing operation ignored cancellation"),
                metadata={"operation": operation, "task_name": task.get_name()},
            )

    @staticmethod
    def _payload_json(payload: Any) -> bytes:
        serialized = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        try:
            return json.dumps(
                serialized,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        except Exception:
            return repr(type(serialized).__name__).encode("utf-8")

    @classmethod
    def _redacted_payload_projection(cls, payload: Any) -> dict[str, Any]:
        trusted_model = isinstance(payload, BaseModel)
        serialized = payload.model_dump(mode="json") if trusted_model else payload
        raw = cls._payload_json(serialized)
        projection: dict[str, Any] = {
            "payload_size_bytes": len(raw),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
        }
        if isinstance(serialized, dict):
            safe_keys = [
                str(key)
                for key in list(serialized)[:_PAYLOAD_KEY_MAX_FIELDS]
                if str(key) in _SAFE_PAYLOAD_KEYS
            ]
            if safe_keys:
                projection["payload_keys"] = [
                    key[:_PAYLOAD_KEY_MAX_CHARS] for key in safe_keys
                ]

        if trusted_model and isinstance(serialized, dict):
            identifiers = {
                key: str(value)[:_IDENTIFIER_VALUE_MAX_CHARS]
                for key, value in serialized.items()
                if key in _SAFE_IDENTIFIER_KEYS
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
            if identifiers:
                projection["identifiers"] = dict(
                    list(identifiers.items())[:_IDENTIFIER_MAX_FIELDS]
                )
        return projection

    @staticmethod
    def _exception_summary(exc: BaseException) -> str:
        raw = str(exc).encode("utf-8", errors="replace")
        message_hash = hashlib.sha256(raw).hexdigest()
        fingerprint = hashlib.sha256(
            exc.__class__.__name__.encode("utf-8") + b":" + raw
        ).hexdigest()
        return (
            f"redacted:size_bytes={len(raw)}:sha256={message_hash}:"
            f"fingerprint={fingerprint}"
        )

    @staticmethod
    def _bounded_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        bounded: dict[str, Any] = {}
        for key, value in list((metadata or {}).items())[:_METADATA_MAX_FIELDS]:
            bounded_key = str(key)[:_METADATA_KEY_MAX_CHARS]
            if value is None or isinstance(value, (bool, int, float)):
                bounded[bounded_key] = value
            else:
                bounded[bounded_key] = str(value)[:_METADATA_VALUE_MAX_CHARS]
        return bounded

    def _publication_deadline(self) -> float:
        timeout = min(
            self.config.enqueue_timeout_seconds,
            max(self.config.shutdown_timeout_seconds / 2, 0.001),
        )
        return asyncio.get_running_loop().time() + timeout

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(deadline - asyncio.get_running_loop().time(), 0.001)

    @staticmethod
    def _is_worker_cancellation(exc: BaseException) -> bool:
        if not isinstance(exc, asyncio.CancelledError):
            return False
        task = asyncio.current_task()
        return bool(task and task.cancelling())

    @staticmethod
    def _handler_name(handler: EventHandler) -> str:
        return getattr(handler, "__qualname__", handler.__class__.__name__)


__all__ = ["BoundedInternalEventBus"]
