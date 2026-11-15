from __future__ import annotations

from datetime import timedelta

import pytest

from execution.orchestrator.budget import (
    BudgetExceeded,
    BudgetPolicy,
)
from execution.orchestrator.models import BudgetState
from tests._orchestrator_helpers import NOW, profile


def budget(**updates):
    values = {"deadline_at": NOW + timedelta(seconds=60)}
    values.update(updates)
    return BudgetState(**values)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"deadline_at": NOW}, "deadline"),
        ({"provider_retries_used": 5}, "provider_retries"),
        ({"input_tokens": 10000}, "input_tokens"),
        ({"output_tokens": 5000}, "output_tokens"),
        ({"model_turns_used": 5, "grace_turns_used": 1}, "model_turns"),
    ],
)
def test_model_turn_limits_fail_typed(updates, reason):
    with pytest.raises(BudgetExceeded) as caught:
        BudgetPolicy().before_model_turn(budget(**updates), profile(), now=NOW)
    assert caught.value.reason == reason


def test_wrap_up_is_idempotent_and_disables_new_tools_during_grace():
    policy = BudgetPolicy()
    wrapped = policy.request_wrap_up(budget())
    assert policy.request_wrap_up(wrapped) is wrapped
    with pytest.raises(BudgetExceeded, match="grace_tools_disabled"):
        policy.before_tool_call(wrapped, profile(), now=NOW)


def test_compaction_uses_its_own_budget_without_incrementing_normal_turns():
    policy = BudgetPolicy()
    compacted = policy.record_compaction(budget())
    assert compacted.compactions_used == 1
    assert compacted.model_turns_used == 0
    with pytest.raises(BudgetExceeded, match="compactions"):
        policy.before_model_turn(compacted, profile(), now=NOW, purpose="compaction")


def test_kernel_reschedule_and_gateway_attempts_share_idempotent_retry_budget():
    policy = BudgetPolicy()
    configured = profile().model_copy(update={"max_provider_retries_total": 2})
    current = policy.record_provider_attempt(
        budget(), configured, attempt_key="turn:assistant:1", retry=False
    )
    current = policy.record_provider_attempt(
        current, configured, attempt_key="turn:assistant:2", retry=True
    )
    current = policy.record_provider_attempt(
        current, configured, attempt_key="kernel-retry:turn:assistant", retry=True
    )
    assert current.provider_retries_used == 2
    assert policy.remaining_provider_retries(current, configured) == 0
    # An ambiguous checkpoint acknowledgement replays the SAME reservation,
    # even at the limit. The ensuing attempt 1 is not charged again.
    assert (
        policy.record_provider_attempt(
            current, configured, attempt_key="kernel-retry:turn:assistant", retry=True
        )
        is current
    )
    current = policy.record_provider_attempt(
        current, configured, attempt_key="next:assistant-next:1", retry=False
    )
    assert current.provider_retries_used == 2
    with pytest.raises(BudgetExceeded, match="provider_retries"):
        policy.record_provider_attempt(
            current, configured, attempt_key="next:assistant-next:2", retry=True
        )
