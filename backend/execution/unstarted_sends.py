"""Fail accepted sends whose Run never started.

A send is accepted when execute logs its ``processing`` status. The send that
created the message then schedules its Run. If the process stops before the
Run exists, nothing ends the message and it stays ``processing``. This sweep
finds those sends in the durable room event log and logs FAILED for each, with
the same details as a start that failed in-process.

The durable condition decides alone: the send is older than ``min_age``, has
no Run, and the room logged no terminal status for it. It works across
workers, since each check reads Mongo. A send whose orchestration task still
runs in this process is skipped as an extra guard. Sends older than
``max_age`` are left alone, so the first sweep does not fail old messages
that never had a Run record.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol

from common.a2a_constants import SSEProcessingStatus
from common.observability import events as E
from common.protocols import EventPublisher
from common.utils.logger import get_logger
from common.utils.time import utcnow
from execution.events import START_FAILED_DETAILS, emit_room_processing_status
from execution.ports import ClientRequestIdResolver

logger = get_logger(__name__)


class AcceptedSendLog(Protocol):
    async def list_accepted_sends(
        self,
        *,
        oldest: datetime,
        newest: datetime,
        after: tuple[str, str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    async def has_status(
        self, conversation_id: str, message_id: str, statuses: Iterable[str]
    ) -> bool: ...


class UserMessageRuns(Protocol):
    async def has_run(self, conversation_id: str, user_message_id: str) -> bool: ...


class UnstartedSendSweep:
    def __init__(
        self,
        *,
        sends: AcceptedSendLog,
        runs: UserMessageRuns,
        event_publisher: EventPublisher,
        client_request_id_resolver: ClientRequestIdResolver,
        is_orchestration_pending: Callable[[str], bool],
        terminal_statuses: Iterable[str],
        min_age: timedelta = timedelta(minutes=10),
        max_age: timedelta = timedelta(days=1),
        page_size: int = 200,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sends = sends
        self._runs = runs
        self._event_publisher = event_publisher
        self._client_request_id_resolver = client_request_id_resolver
        self._is_orchestration_pending = is_orchestration_pending
        self._terminal_statuses = frozenset(terminal_statuses)
        self._min_age = min_age
        self._max_age = max_age
        self._page_size = page_size
        self._now = now

    async def sweep(self) -> int:
        """Log FAILED for each unstarted send in the window; return the count."""
        now = self._now()
        oldest, newest = now - self._max_age, now - self._min_age
        failed = 0
        after: tuple[str, str] | None = None
        while True:
            page = await self._sends.list_accepted_sends(
                oldest=oldest, newest=newest, after=after, limit=self._page_size
            )
            for record in page:
                if await self._fail_if_unstarted(record):
                    failed += 1
            if len(page) < self._page_size:
                return failed
            last = page[-1]
            after = (str(last["ts"]), str(last["room_event_id"]))

    async def _fail_if_unstarted(self, record: dict[str, Any]) -> bool:
        conversation_id = record.get("conversation_id")
        payload = record.get("payload_public") or {}
        message_id = payload.get("message_id")
        if not isinstance(conversation_id, str) or not isinstance(message_id, str):
            return False
        try:
            if self._is_orchestration_pending(message_id):
                return False
            if await self._runs.has_run(conversation_id, message_id):
                return False
            if await self._sends.has_status(
                conversation_id, message_id, self._terminal_statuses
            ):
                return False
            await emit_room_processing_status(
                conversation_id=conversation_id,
                status=SSEProcessingStatus.FAILED,
                message_id=message_id,
                lifecycle_message_id=payload.get("related_message_id") or message_id,
                event_publisher=self._event_publisher,
                client_request_id_resolver=self._client_request_id_resolver,
                client_request_id=payload.get("client_request_id"),
                details=START_FAILED_DETAILS,
            )
        except Exception as exc:
            # One send must not stop the sweep; the next sweep retries it.
            logger.emit(
                E.UNSTARTED_SEND_CHECK_FAILED,
                exc=exc,
                conversation_id=conversation_id,
                user_message_id=message_id,
            )
            return False
        logger.emit(
            E.UNSTARTED_SEND_FAILED,
            conversation_id=conversation_id,
            user_message_id=message_id,
        )
        return True


__all__ = ["UnstartedSendSweep"]
