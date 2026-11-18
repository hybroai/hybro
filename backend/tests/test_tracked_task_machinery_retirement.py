"""C2 step 6: tracked-task machinery and pending_continuation are retired."""

from __future__ import annotations

import importlib
import inspect
from unittest.mock import AsyncMock

import pytest

RETIRED_TASK_SETTINGS = {
    "stale_check_minutes",
    "task_expiry_hours",
    "pending_task_warning_hours",
}


@pytest.mark.parametrize(
    "module",
    [
        "execution.task_tracking",
        "execution.dispatch.task_notifications",
        "delivery.task_notifier",
        # Only the retired tracked-task code imported these.
        "a2a_adapter.task_status",
        "execution.dispatch.agent_event",
        "execution.dispatch.a2a_interaction",
    ],
)
def test_tracked_task_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_execution_ports_drop_tracked_task_and_continuation_contracts():
    import execution.ports as ports
    from approval.protocols import HITLPersistencePort

    assert not hasattr(ports, "A2ATransportPort")
    assert not hasattr(ports, "A2ATaskTrackingStorePort")
    for name in (
        "get_pending_continuation_on_message",
        "save_continuation_on_user_message",
        "get_and_clear_continuation_on_message",
        "get_and_clear_continuation_on_user_message",
    ):
        assert not hasattr(HITLPersistencePort, name), name


def test_task_lifecycle_store_keeps_only_live_methods():
    from conversation.repository import ClientRequestIdStore

    public = {
        name
        for name, member in inspect.getmembers(ClientRequestIdStore)
        if callable(member) and not name.startswith("__")
    }

    assert public == {
        "resolve_client_request_id_for_agent_message",
        "resolve_client_request_id_for_message_id",
    }


def test_stale_checker_keeps_only_hitl_reconciliation():
    from jobs.stale_task_checker import StaleTaskChecker

    for name in (
        "_process_stale_task",
        "_auto_fail_expired_task",
        "_auto_fail_non_tracked_task",
        "_mark_task_failed",
        "_message_or_parent_cancelled",
        "_is_message_cancelled_for_recovery",
        "_get_task_from_agent",
        "configure_timing",
    ):
        assert not hasattr(StaleTaskChecker, name), name
    for name in RETIRED_TASK_SETTINGS:
        assert name not in inspect.signature(StaleTaskChecker).parameters


@pytest.mark.asyncio
async def test_stale_checker_iteration_runs_hitl_reconcile():
    from jobs.stale_task_checker import StaleHITLDeps, StaleTaskChecker

    hitl = StaleHITLDeps(
        reconcile_lifecycle=AsyncMock(), fail_unstarted_sends=AsyncMock()
    )
    checker = StaleTaskChecker()
    checker.set_hitl_deps(hitl)

    await checker.check_stale_tasks()

    hitl.reconcile_lifecycle.assert_awaited_once()
    assert not hasattr(StaleHITLDeps, "recover_stale_processing")


def test_conversation_facade_has_no_stale_card_auto_fail():
    from conversation.facade import ConversationFacade

    assert not hasattr(ConversationFacade, "_auto_fail_stale_agent_messages")


def test_pending_continuation_field_is_gone():
    from models.room import RoomAgentMessage, RoomUserMessage

    for model in (RoomAgentMessage, RoomUserMessage):
        assert "pending_continuation" not in model.model_fields, model


def test_hitl_service_no_longer_clears_continuations():
    from approval.service import HITLService

    assert not hasattr(HITLService, "_clear_hitl_continuation_once")


def test_task_timing_settings_are_retired():
    from common.config.loader import REMOVED_FIELDS, Settings

    assert RETIRED_TASK_SETTINGS <= REMOVED_FIELDS
    assert not RETIRED_TASK_SETTINGS & set(Settings.model_fields)


def test_hitl_reconciler_has_no_remote_command_inspection():
    from approval.reconciler import HITLLifecycleReconciler

    parameters = inspect.signature(HITLLifecycleReconciler).parameters

    assert "inspect_remote_command" not in parameters


def test_container_no_longer_wires_tracked_task_notifications():
    import container

    source = inspect.getsource(container)

    for name in (
        "bind_notification_store",
        "bind_task_notification_runtime",
        "TaskUpdateNotifier",
        "task_notification_store",
        "bind_task_processing_status_emitter",
        "inspect_uncertain_hitl_command",
        "enable_task_tracking_on_message",
        "pending_continuation",
    ):
        assert name not in source, name


def test_tracked_task_only_constants_and_queries_are_gone():
    import common.a2a_constants as constants
    from conversation.facade import MessageRepository
    from conversation.repository import ConversationMessageStore

    assert not hasattr(constants, "UNSUPPORTED_INTERACTION_CODE")
    assert not hasattr(constants, "UNSUPPORTED_INTERACTION_MESSAGE")
    for name in (
        "get_agent_task_messages_for_user",
        "get_task_messages_for_room",
        "get_pending_task_messages_for_user",
    ):
        assert not hasattr(MessageRepository, name), name
    assert not hasattr(ConversationMessageStore, "update_last_notified_state")
