"""The sign-in attempt limit: a growing delay, never a lockout."""

from __future__ import annotations

from account.attempts import AttemptLimiter


class _Monotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _limiter(**overrides) -> tuple[AttemptLimiter, _Monotonic]:
    clock = _Monotonic()
    options = {"base_seconds": 1.0, "max_seconds": 8.0, "clock": clock}
    options.update(overrides)
    return AttemptLimiter(**options), clock


def _attempt(limiter: AttemptLimiter, clock: _Monotonic, *keys: str) -> float:
    """One attempt that waits out its delay, as a request does."""
    delay = limiter.begin(*keys)
    clock.now += delay
    return delay


def test_the_first_attempt_is_not_delayed():
    limiter, _ = _limiter()

    assert limiter.begin("account:a") == 0.0


def test_each_failed_attempt_doubles_the_delay_up_to_the_cap():
    limiter, clock = _limiter()

    delays = [_attempt(limiter, clock, "account:a") for _ in range(7)]

    assert delays == [0.0, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_attempts_sent_at_once_take_turns_up_to_the_cap():
    limiter, _ = _limiter()

    waits = [limiter.begin("account:a") for _ in range(50)]

    # Each waits for the one before it, then its own delay; none waits
    # longer than the cap, and none is refused.
    assert waits[:5] == [0.0, 1.0, 3.0, 7.0, 8.0]
    assert max(waits) == 8.0


def test_a_very_long_run_of_attempts_keeps_its_delay():
    limiter, clock = _limiter()

    delays = [_attempt(limiter, clock, "account:a") for _ in range(1100)]

    assert delays[-1] == 8.0


def test_the_delay_is_the_largest_of_the_keys():
    limiter, clock = _limiter()
    for _ in range(3):
        _attempt(limiter, clock, "address:1.2.3.4")

    assert _attempt(limiter, clock, "account:a", "address:1.2.3.4") == 4.0
    # The account key was counted once by that attempt.
    assert _attempt(limiter, clock, "account:a") == 1.0


def test_a_success_clears_its_keys_only():
    limiter, clock = _limiter()
    for _ in range(3):
        _attempt(limiter, clock, "account:a", "address:1.2.3.4")

    limiter.succeeded("account:a")

    assert _attempt(limiter, clock, "account:a") == 0.0
    assert _attempt(limiter, clock, "address:1.2.3.4") == 4.0


def test_failures_are_forgotten_after_the_quiet_period():
    limiter, clock = _limiter(forget_after_seconds=60.0)
    for _ in range(4):
        _attempt(limiter, clock, "account:a")

    clock.now += 61.0

    assert limiter.begin("account:a") == 0.0


def test_the_number_of_tracked_keys_is_bounded():
    limiter, clock = _limiter(max_keys=3)
    for index in range(10):
        _attempt(limiter, clock, f"address:{index}")
        _attempt(limiter, clock, f"address:{index}")

    assert limiter.tracked() == 3
    # The most recent keys are the ones kept.
    assert _attempt(limiter, clock, "address:9") == 2.0
    assert _attempt(limiter, clock, "address:0") == 0.0
