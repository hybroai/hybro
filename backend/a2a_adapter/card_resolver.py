import time

import httpx

from common.dto import AgentCardSnapshot
from common.observability import events as E
from common.observability import get_logger

from .card_data import build_agent_card
from .constants import AGENT_CARD_WELL_KNOWN_PATH, PREV_AGENT_CARD_WELL_KNOWN_PATH
from .docker_host_fallback import docker_host_fallback_url_for_error
from .translators import a2a_card_to_snapshot

logger = get_logger(__name__)


class AgentCardResolverImpl:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        cache_ttl: int = 300,
        timeout: float = 10,
        log_failures: bool = True,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._cache_ttl = cache_ttl
        self._log_failures = log_failures
        self._cache: dict[str, tuple[float, AgentCardSnapshot]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve_card(self, agent_url: str) -> AgentCardSnapshot | None:
        normalized_url = agent_url.rstrip("/")
        cached = self._cache.get(normalized_url)
        now = time.monotonic()
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]

        snapshot, last_error = await self._resolve_card_from_url(normalized_url)
        if snapshot is None and last_error is not None:
            fallback_url = docker_host_fallback_url_for_error(
                normalized_url, last_error
            )
            if fallback_url is not None:
                snapshot, last_error = await self._resolve_card_from_url(fallback_url)

        if snapshot is None and self._log_failures:
            status_fields = (
                {"status": last_error.response.status_code}
                if isinstance(last_error, httpx.HTTPStatusError)
                else {}
            )
            logger.emit(
                E.AGENT_CARD_RESOLUTION_FAILED,
                exc=last_error,
                agent_url=normalized_url,
                **status_fields,
            )
            return None

        self._cache[normalized_url] = (now, snapshot)
        return snapshot

    async def _resolve_card_from_url(
        self,
        normalized_url: str,
    ) -> tuple[AgentCardSnapshot | None, Exception | None]:
        last_error: Exception | None = None
        for path in (AGENT_CARD_WELL_KNOWN_PATH, PREV_AGENT_CARD_WELL_KNOWN_PATH):
            try:
                response = await self._client.get(f"{normalized_url}{path}")
                response.raise_for_status()
                card = build_agent_card(response.json())
                return a2a_card_to_snapshot(card, normalized_url), None
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if (
                    exc.response.status_code == 404
                    and path == AGENT_CARD_WELL_KNOWN_PATH
                ):
                    continue
                break
            except Exception as exc:
                last_error = exc
                break

        return None, last_error

    async def supports_push_notifications(self, agent_url: str) -> bool:
        card = await self.resolve_card(agent_url)
        if card is None:
            return False
        accepted = {"push_notifications", "push-notifications", "pushNotifications"}
        return bool(accepted & set(card.capabilities))

    async def supports_streaming(self, agent_url: str) -> bool:
        card = await self.resolve_card(agent_url)
        if card is None:
            return False
        accepted = {"streaming", "stream", "message/stream"}
        return bool(accepted & set(card.capabilities))


__all__ = ["AgentCardResolverImpl"]
