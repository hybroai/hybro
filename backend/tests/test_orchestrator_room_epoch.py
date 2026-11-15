from __future__ import annotations

from tests.orchestrator_harness.a2a_in_memory import InMemoryRoomEpochStore

from ._orchestrator_helpers import NOW


async def test_room_epoch_is_monotonic_and_creation_identity_replays():
    store = InMemoryRoomEpochStore()
    outcome, first = await store.activate("room-1", "create-1", activated_at=NOW)
    assert outcome == "accepted" and first.epoch == 1
    assert (await store.activate("room-1", "create-1", activated_at=NOW))[
        0
    ] == "replayed"
    assert (await store.activate("room-1", "different", activated_at=NOW))[
        0
    ] == "conflict"
    assert (await store.deactivate("room-1", 1, "delete-1", deactivated_at=NOW))[
        0
    ] == "accepted"
    outcome, second = await store.activate("room-1", "create-2", activated_at=NOW)
    assert outcome == "accepted" and second.epoch == 2
    assert second.high_water_mark == 2


async def test_cleanup_authority_is_exact_and_never_active_authority():
    store = InMemoryRoomEpochStore()
    await store.activate("room-1", "create-1", activated_at=NOW)
    await store.deactivate("room-1", 1, "delete-1", deactivated_at=NOW)
    assert not await store.verify_active("room-1", 1)
    assert await store.verify_cleanup_epoch("room-1", 1, "delete-1")
    assert not await store.verify_cleanup_epoch("room-1", 1, "wrong")
