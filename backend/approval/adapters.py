from __future__ import annotations


class HITLDeliveryAdapter:
    def __init__(self, event_publisher) -> None:
        self._event_publisher = event_publisher

    async def emit(self, event) -> None:
        await self._event_publisher.emit(event)


class HITLTerminalLifecycleAdapter:
    """Terminalize the canonical Run that owns a terminated HITL interaction."""

    def __init__(self, *, canonical_terminalizer=None) -> None:
        self._canonical_terminalizer = canonical_terminalizer

    async def terminalize_owning_run(
        self,
        request,
        *,
        terminal_status: str,
        reason: str,
    ) -> None:
        # A run the canonical terminalizer does not own has no public mirror
        # left to project; there is nothing further to converge.
        if request.orchestration_run_id and self._canonical_terminalizer is not None:
            await self._canonical_terminalizer(
                request,
                terminal_status=terminal_status,
                reason=reason,
            )


__all__ = [
    "HITLTerminalLifecycleAdapter",
    "HITLDeliveryAdapter",
]
