from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

ProcessingStatusLike = str | Enum


A2APushOutcome = Literal[
    "accepted", "unauthorized", "invalid", "too_large", "unavailable"
]


@dataclass(frozen=True, slots=True)
class A2APushReceipt:
    """Result of one push notification.

    An unknown, terminal, or token-less call and a wrong token all report
    ``unauthorized``, so the outcome never reveals whether a call exists.
    ``observation_id`` names recorded evidence still to be processed.
    """

    outcome: A2APushOutcome
    observation_id: str | None = None


class A2APushNotificationReceiver(Protocol):
    """Execution-owned A2A push ingress served by the API Gateway."""

    @property
    def max_body_bytes(self) -> int: ...

    async def authenticate(
        self, call_record_id: str, *, token: str | None
    ) -> A2APushReceipt: ...

    async def receive(
        self, call_record_id: str, *, token: str | None, body: bytes
    ) -> A2APushReceipt: ...

    async def process(self, observation_id: str) -> None: ...


class TaskFactory(Protocol):
    def __call__(
        self,
        coro: Awaitable[Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]: ...


class HITLMessageCancellationPort(Protocol):
    async def cancel_requests_for_message(self, message_id: str) -> None: ...


class ClientRequestIdResolver(Protocol):
    async def resolve_client_request_id(
        self,
        message_id: str | None,
        provided_client_request_id: str | None,
    ) -> str | None: ...
