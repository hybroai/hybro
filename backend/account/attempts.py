"""The attempt limit of sign-in and setup: a growing delay, never a lockout.

Counts live in this process's memory; one backend worker is the only
supported setup. Each attempt is counted when it begins and its keys are
cleared when it succeeds. A key's delay starts after the first attempt and
doubles up to ``max_seconds``.

Attempts on one key take turns: each waits for the one before it and then its
own delay. No attempt waits longer than ``max_seconds`` and none is refused,
which is what keeps wrong passwords from blocking the right one. The price:
attempts sent at once beyond that horizon are checked together when it ends,
so the limit on simultaneous guesses is the number of requests the service
holds, not one per delay.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Past this the delay is far above any cap; it only keeps the power finite.
_MAX_DOUBLINGS = 32


class AttemptLimiter:
    def __init__(
        self,
        *,
        base_seconds: float = 0.5,
        max_seconds: float = 10.0,
        forget_after_seconds: float = 15 * 60,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_seconds
        self._max = max_seconds
        self._forget_after = forget_after_seconds
        self._max_keys = max_keys
        self._clock = clock
        # key -> (attempts not followed by a success, when the last one is
        # checked); insertion order is the order of last use.
        self._attempts: dict[str, tuple[int, float]] = {}

    def begin(self, *keys: str) -> float:
        """Count one attempt on each key; return the seconds to wait first."""
        now = self._clock()
        wait = 0.0
        counts: dict[str, int] = {}
        for key in keys:
            counts[key], key_wait = self._next(key, now)
            wait = max(wait, min(key_wait, self._max))
        for key, count in counts.items():
            self._attempts.pop(key, None)
            self._attempts[key] = (count + 1, now + wait)
        while len(self._attempts) > self._max_keys:
            del self._attempts[next(iter(self._attempts))]
        return wait

    def _next(self, key: str, now: float) -> tuple[int, float]:
        """The key's count, and how long its next attempt waits for its turn."""
        count, checked_at = self._attempts.get(key, (0, now))
        if now - checked_at > self._forget_after:
            count, checked_at = 0, now
        if not count:
            return 0, 0.0
        delay = min(self._base * 2 ** min(count - 1, _MAX_DOUBLINGS), self._max)
        return count, max(checked_at, now) - now + delay

    def succeeded(self, *keys: str) -> None:
        for key in keys:
            self._attempts.pop(key, None)

    def tracked(self) -> int:
        return len(self._attempts)


__all__ = ["AttemptLimiter"]
