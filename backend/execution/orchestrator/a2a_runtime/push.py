"""A2A push-notification registration and authenticated push ingress.

Canonical dispatch may register a push-notification config with an Agent whose
card advertises ``pushNotifications``. Each dispatch attempt sends a fresh
high-entropy token; only its SHA-256 digest is stored on the call ledger record.

A push is authenticated against that digest in constant time and is accepted
only for a known, non-terminal call. A terminal ``Task`` snapshot is recorded as
call evidence through the existing observation ingress, and the observation
processor then applies it to the ledger and the Run-addressed sink, so Execution
stays the single owner of A2A response ingestion. Status and artifact updates
are deltas without the full result, so a terminal or input-requesting update
only makes the call due for the canonical ``tasks/get`` poll. Polling remains
the fallback for every call.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import SecretStr, ValidationError

from common.observability import events as E
from common.utils.logger import get_logger
from execution.ports import A2APushOutcome, A2APushReceipt

from .errors import RecoverableAdapterError
from .ingress import A2AObservationProcessor, ObservationIngressError
from .ledger import TERMINAL_AGENT_CALL_STATES
from .models import (
    A2APushFrame,
    A2APushNotificationConfig,
    A2ARuntimePolicy,
    AgentCallLedgerRecord,
)
from .ports import (
    AgentCallLedgerStore,
    NormalizedObservationRecorder,
    ObservationIngressAuthenticator,
)

logger = get_logger(__name__)

# Keys of the credential mapping the push ingress hands its authenticator. The
# call key comes from the URL path, never from a request header.
PUSH_TOKEN_HEADER = "x-a2a-notification-token"
PUSH_CALL_HEADER = "x-hybro-push-call-record-id"
PUSH_ROUTE_PATH = "/a2a/push"

PushNotificationReader = Callable[..., Awaitable[dict[str, Any]]]

_DUMMY_DIGEST = sha256(b"hybro-a2a-push-unknown-call").hexdigest()


class _Rejected(Exception):
    def __init__(self, outcome: A2APushOutcome) -> None:
        super().__init__(outcome)
        self.outcome: A2APushOutcome = outcome


def push_token_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class A2APushRegistration:
    """One dispatch attempt's push config and the digest the ledger keeps."""

    config: A2APushNotificationConfig
    token_digest: str


class A2APushRegistrar:
    """Issue per-attempt callback URLs and tokens for canonical dispatch."""

    def __init__(self, *, callback_base_url: str, api_prefix: str) -> None:
        if not callback_base_url:
            raise ValueError("push registration requires a callback base URL")
        self._prefix = f"{callback_base_url.rstrip('/')}{api_prefix}{PUSH_ROUTE_PATH}"

    def issue(self, call_record_id: str) -> A2APushRegistration:
        token = secrets.token_urlsafe(32)
        return A2APushRegistration(
            A2APushNotificationConfig(
                url=f"{self._prefix}/{call_record_id}",
                token=SecretStr(token),
            ),
            push_token_digest(token),
        )


class LedgerPushTokenAuthenticator(ObservationIngressAuthenticator):
    """Verify a push token against the call ledger's stored digest."""

    def __init__(self, ledger: AgentCallLedgerStore) -> None:
        self.ledger = ledger

    async def authenticate(
        self,
        *,
        source_kind: str,
        headers: dict[str, str],
        body: bytes,
    ) -> str:
        del body
        call_record_id = headers.get(PUSH_CALL_HEADER, "")
        token = headers.get(PUSH_TOKEN_HEADER, "")
        call = (
            await self.ledger.load_by_record_id(call_record_id)
            if call_record_id
            else None
        )
        stored = call.push_token_digest if call is not None else None
        # Compare even when the call is unknown so timing does not reveal it.
        matches = hmac.compare_digest(push_token_digest(token), stored or _DUMMY_DIGEST)
        if (
            source_kind != "webhook"
            or not token
            or call is None
            or stored is None
            or call.state in TERMINAL_AGENT_CALL_STATES
            or not matches
        ):
            raise PermissionError("push notification is not authorized")
        return call.call_record_id


class A2APushNotificationIngress:
    """Authenticated push ingress; satisfies ``A2APushNotificationReceiver``."""

    def __init__(
        self,
        *,
        ledger: AgentCallLedgerStore,
        authenticator: ObservationIngressAuthenticator,
        observations: NormalizedObservationRecorder,
        processor: A2AObservationProcessor,
        read_push: PushNotificationReader,
        policy: A2ARuntimePolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.authenticator = authenticator
        self.observations = observations
        self.processor = processor
        self.read_push = read_push
        self.policy = policy or A2ARuntimePolicy()

    @property
    def max_body_bytes(self) -> int:
        return self.policy.max_callback_body_bytes

    async def authenticate(
        self, call_record_id: str, *, token: str | None
    ) -> A2APushReceipt:
        """Check the path call and token alone, before any body is read."""
        try:
            await self._authenticated_call(call_record_id, token=token)
        except Exception as exc:
            return _rejection_receipt(exc, call_record_id=call_record_id)
        return A2APushReceipt("accepted")

    async def receive(
        self, call_record_id: str, *, token: str | None, body: bytes
    ) -> A2APushReceipt:
        """Re-authenticate and accept one push before any evidence is recorded."""
        if len(body) > self.max_body_bytes:
            return A2APushReceipt("too_large")
        try:
            observation_id = await self._receive(call_record_id, token=token, body=body)
        except Exception as exc:
            return _rejection_receipt(exc, call_record_id=call_record_id)
        return A2APushReceipt("accepted", observation_id)

    async def _authenticated_call(
        self, call_record_id: str, *, token: str | None
    ) -> AgentCallLedgerRecord:
        try:
            authenticated = await self.authenticator.authenticate(
                source_kind="webhook",
                headers={
                    PUSH_TOKEN_HEADER: token or "",
                    PUSH_CALL_HEADER: call_record_id,
                },
                body=b"",
            )
        except PermissionError:
            raise _Rejected("unauthorized") from None
        call = await self.ledger.load_by_record_id(authenticated)
        if call is None or call.state in TERMINAL_AGENT_CALL_STATES:
            raise _Rejected("unauthorized")
        return call

    async def _receive(
        self, call_record_id: str, *, token: str | None, body: bytes
    ) -> str | None:
        # The call may have turned terminal since the route's authenticate().
        call = await self._authenticated_call(call_record_id, token=token)
        if call.a2a_task_id is None:
            # The dispatch receipt has not bound the remote task yet, so the
            # push cannot be correlated; polling reconciles the call.
            raise _Rejected("invalid")
        try:
            payload = json.loads(body)
            frame = A2APushFrame.model_validate(
                await self.read_push(
                    payload,
                    call_record_id=call.call_record_id,
                    endpoint_scope=call.dispatch_snapshot.endpoint_scope,
                    agent_id=call.agent_id,
                    conversation_id=call.conversation_id,
                    room_epoch=call.room_epoch,
                    message_id=call.dispatch_snapshot.message_id,
                    expected_task_id=call.a2a_task_id,
                    expected_context_id=call.a2a_context_id,
                )
            )
        except (ValueError, ValidationError):
            raise _Rejected("invalid") from None
        if not _correlates(call, frame):
            raise _Rejected("invalid")
        if frame.observation is not None:
            try:
                _outcome, row = await self.observations.record(frame.observation)
            except ObservationIngressError:
                raise _Rejected("invalid") from None
            return row.observation_id
        if frame.event_kind in {"terminal", "input_required", "auth_required"}:
            await self._expedite_poll(call)
        return None

    async def process(self, observation_id: str) -> None:
        """Apply a recorded push now; inbox recovery retries any failure."""
        try:
            await self.processor.process(observation_id)
        except Exception as exc:
            logger.emit(
                E.A2A_PUSH_PROCESSING_DEFERRED, exc=exc, observation_id=observation_id
            )

    async def _expedite_poll(self, call: AgentCallLedgerRecord) -> None:
        now = datetime.now(UTC)
        if (
            call.state != "working"
            or (call.claim_expires_at is not None and call.claim_expires_at > now)
            or (call.next_attempt_at is not None and call.next_attempt_at <= now)
        ):
            return
        due = call.model_copy(
            update={
                "next_attempt_at": now,
                "state_version": call.state_version + 1,
                "updated_at": now,
            }
        )
        # A lost race means another owner already advanced the call.
        await self.ledger.cas(due, expected_state_version=call.state_version)


def _rejection_receipt(exc: Exception, *, call_record_id: str) -> A2APushReceipt:
    if isinstance(exc, _Rejected):
        return A2APushReceipt(exc.outcome)
    if not isinstance(exc, RecoverableAdapterError | TimeoutError):
        # Stores translate only transient driver errors to
        # RecoverableAdapterError; at this external boundary any other store or
        # adapter failure still answers 503 instead of escaping as a 500.
        logger.emit(E.A2A_PUSH_INGRESS_FAILED, exc=exc, call_record_id=call_record_id)
    return A2APushReceipt("unavailable")


def _correlates(call: AgentCallLedgerRecord, frame: A2APushFrame) -> bool:
    if frame.task_id != call.a2a_task_id:
        return False
    if (
        frame.context_id is not None
        and call.a2a_context_id is not None
        and frame.context_id != call.a2a_context_id
    ):
        return False
    observation = frame.observation
    return observation is None or (
        observation.call_record_id == call.call_record_id
        and observation.task_id == call.a2a_task_id
        and observation.source_kind == "webhook"
    )


__all__ = [
    "PUSH_ROUTE_PATH",
    "PUSH_TOKEN_HEADER",
    "A2APushNotificationIngress",
    "A2APushRegistrar",
    "A2APushRegistration",
    "LedgerPushTokenAuthenticator",
    "push_token_digest",
]
