from __future__ import annotations

from enum import StrEnum


class HITLDeliveryDisposition(StrEnum):
    """What the command journal may safely do after a delivery failure."""

    RETRYABLE = "retryable"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    PERMANENT = "permanent"


class HITLDeliveryPhase(StrEnum):
    PRE_SEND = "pre_send"
    IN_FLIGHT = "in_flight"
    REMOTE_RESPONSE = "remote_response"
    POST_SEND_PERSISTENCE = "post_send_persistence"


class HITLDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        disposition: HITLDeliveryDisposition,
        phase: HITLDeliveryPhase,
        error_code: str,
    ) -> None:
        super().__init__(message)
        self.disposition = disposition
        self.phase = phase
        self.error_code = error_code


__all__ = [
    "HITLDeliveryDisposition",
    "HITLDeliveryError",
    "HITLDeliveryPhase",
]
