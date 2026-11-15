from common.a2a_constants import (
    SSEProcessingStatus,
    normalize_task_state_value,
)
from common.types import TaskState


def test_common_a2a_constants_are_sdk_free_but_accept_enum_like_values():
    assert normalize_task_state_value(TaskState.completed) == "completed"
    assert normalize_task_state_value("completed") == "completed"


def test_processing_status_values_are_shared_strings():
    assert SSEProcessingStatus.QUEUED.value == "queued"
    assert SSEProcessingStatus.COMPLETED.value == "completed"
    assert SSEProcessingStatus.AWAITING_INPUT.value == "awaiting_input"
