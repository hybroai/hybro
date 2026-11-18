"""Whether an orchestrator Run exists for a room user message."""

from __future__ import annotations

from typing import Any

from execution.orchestrator.persistence import ORCHESTRATOR_RUNS_COLLECTION


class UserMessageRunReader:
    """Looks a Run up by room first, so the ``room_id`` index serves it.

    Any stored Run counts, whatever its schema version or status. Lookup errors
    propagate.
    """

    def __init__(self, mongo: Any) -> None:
        self._runs = mongo.collection(ORCHESTRATOR_RUNS_COLLECTION)

    async def has_run(self, conversation_id: str, user_message_id: str) -> bool:
        run = await self._runs.find_one(
            {"room_id": conversation_id, "request.user_message_id": user_message_id},
            projection={"_id": 1},
        )
        return run is not None
