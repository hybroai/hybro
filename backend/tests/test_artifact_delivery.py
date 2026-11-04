import logging

import pytest

from common.errors import (
    FileStoragePlatformError,
    RetryableFileStoragePlatformError,
)
from common.types import (
    Artifact,
    DataPart,
    FileContent,
    FilePart,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from common.utils.artifact_delivery import (
    OUTPUT_DELIVERY_FAILURE_CODE,
    mark_task_output_delivery_failed,
    new_materialization_report,
    output_delivery_failed,
)


@pytest.mark.asyncio
async def test_materialization_reports_storage_failure_without_logging_payload(caplog):
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class FailingStorage:
        async def store_agent_artifact(self, **_kwargs):
            raise OSError("SECRET_STORAGE_DETAIL")

    bind_artifact_files(FailingStorage())
    secret_artifact_id = "SECRET_ARTIFACT_ID"
    artifact = Artifact(
        artifact_id=secret_artifact_id,
        parts=[
            Part(
                root=FilePart(
                    file=FileContent(
                        bytes="aGVsbG8=",
                        mimeType="image/png",
                        name="image.png",
                    )
                )
            )
        ],
    )
    report = new_materialization_report()

    converted = await materialize_artifacts(
        [artifact], "room-1", "message-1", report=report
    )

    assert converted == 0
    assert report["attempted"] == 1
    assert report["stored"] == 0
    assert report["unavailable"] == 1
    assert report["failures"][0]["code"] == "storage_failed"
    assert report["failures"][0]["exception_type"] == "OSError"
    assert "SECRET_STORAGE_DETAIL" not in caplog.text
    assert secret_artifact_id not in caplog.text
    assert artifact.parts[0].root.data["type"] == "file_unavailable"


@pytest.mark.asyncio
async def test_materialization_retries_transient_platform_storage_failure(caplog):
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class TransientStorage:
        def __init__(self):
            self.calls = 0

        async def store_agent_artifact(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise FileStoragePlatformError(409, "Artifact could not be finalized")
            return {
                "file_id": "file-1",
                "file_name": "image.png",
                "mime_type": "image/png",
                "size_bytes": 5,
                "sha256": "digest",
            }

        def content_url(self, file_id):
            return f"/api/v1/files/{file_id}/content"

    storage = TransientStorage()
    bind_artifact_files(storage)
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(
                root=FilePart(
                    file=FileContent(
                        bytes="aGVsbG8=", mimeType="image/png", name="image.png"
                    )
                )
            )
        ],
    )
    report = new_materialization_report()

    caplog.set_level(logging.WARNING, logger="a2a_adapter.artifact_storage")
    converted = await materialize_artifacts(
        [artifact], "room-1", "message-1", report=report
    )

    assert storage.calls == 2
    [record] = [
        r
        for r in caplog.records
        if r.getMessage() == "artifact_materialization_store_retried"
    ]
    assert record.attempt == 1
    assert record.platform_status == 409
    assert converted == 1
    assert report["stored"] == 1
    assert report["unavailable"] == 0
    assert artifact.parts[0].root.file.uri == "/api/v1/files/file-1/content"


@pytest.mark.parametrize(
    "error",
    [
        RetryableFileStoragePlatformError(
            503, {"message": "storage backend unavailable"}
        )
    ],
)
def test_transient_storage_backend_errors_are_retryable(error):
    from a2a_adapter.artifact_storage import _is_retryable_store_error

    assert _is_retryable_store_error(error) is True


@pytest.mark.asyncio
async def test_materialization_retries_transient_content_store_failure():
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class TransientStorage:
        def __init__(self):
            self.calls = 0

        async def store_agent_artifact(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RetryableFileStoragePlatformError(
                    503, {"message": "temporary filesystem error"}
                )
            return {
                "file_id": "file-1",
                "file_name": "image.png",
                "mime_type": "image/png",
                "size_bytes": 5,
                "sha256": "digest",
            }

        def content_url(self, file_id):
            return f"/api/v1/files/{file_id}/content"

    storage = TransientStorage()
    bind_artifact_files(storage)
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(
                root=FilePart(
                    file=FileContent(
                        bytes="aGVsbG8=", mimeType="image/png", name="image.png"
                    )
                )
            )
        ],
    )

    converted = await materialize_artifacts([artifact], "room-1", "message-1")

    assert storage.calls == 2
    assert converted == 1


@pytest.mark.asyncio
async def test_materialization_does_not_retry_non_transient_platform_failure():
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class RejectedStorage:
        def __init__(self):
            self.calls = 0

        async def store_agent_artifact(self, **_kwargs):
            self.calls += 1
            raise FileStoragePlatformError(413, "Artifact exceeds size limit")

    storage = RejectedStorage()
    bind_artifact_files(storage)
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=FilePart(file=FileContent(bytes="aGVsbG8=", name="image.png")))
        ],
    )

    await materialize_artifacts([artifact], "room-1", "message-1")

    assert storage.calls == 1
    assert artifact.parts[0].root.data["type"] == "file_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Room unavailable for artifact write",
        "Artifact origin conflicts with existing content",
    ],
)
async def test_materialization_does_not_retry_deterministic_conflicts(message):
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class ConflictStorage:
        def __init__(self):
            self.calls = 0

        async def store_agent_artifact(self, **_kwargs):
            self.calls += 1
            raise FileStoragePlatformError(409, message)

    storage = ConflictStorage()
    bind_artifact_files(storage)
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=FilePart(file=FileContent(bytes="aGVsbG8=", name="image.png")))
        ],
    )

    await materialize_artifacts([artifact], "room-1", "message-1")

    assert storage.calls == 1
    assert artifact.parts[0].root.data["type"] == "file_unavailable"


@pytest.mark.asyncio
async def test_invalid_base64_has_specific_internal_failure_code():
    from a2a_adapter.artifact_storage import materialize_artifacts

    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=FilePart(file=FileContent(bytes="not-base64", name="image.png")))
        ],
    )
    report = new_materialization_report()

    await materialize_artifacts([artifact], "room-1", "message-1", report=report)

    assert report["failures"][0]["code"] == "invalid_base64"
    assert artifact.parts[0].root.data["reason"] == "invalid_content"


@pytest.mark.asyncio
async def test_storage_error_containing_limit_stays_storage_failure():
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class FailingStorage:
        async def store_agent_artifact(self, **_kwargs):
            raise OSError("filesystem limit reached")

    bind_artifact_files(FailingStorage())
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=FilePart(file=FileContent(bytes="aGVsbG8=", name="image.png")))
        ],
    )
    report = new_materialization_report()

    await materialize_artifacts([artifact], "room-1", "message-1", report=report)

    assert report["failures"][0]["code"] == "storage_failed"
    assert artifact.parts[0].root.data["reason"] == "invalid_content"


@pytest.mark.asyncio
async def test_malformed_storage_result_is_not_recorded_as_success():
    from a2a_adapter.artifact_storage import bind_artifact_files, materialize_artifacts

    class MalformedStorage:
        async def store_agent_artifact(self, **_kwargs):
            return {"file_id": "file-1"}

    bind_artifact_files(MalformedStorage())
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=FilePart(file=FileContent(bytes="aGVsbG8=", name="image.png")))
        ],
    )
    report = new_materialization_report()

    converted = await materialize_artifacts(
        [artifact], "room-1", "message-1", report=report
    )

    assert converted == 0
    assert report["stored"] == 0
    assert report["unavailable"] == 1
    assert report["failures"][0]["code"] == "storage_failed"
    assert artifact.parts[0].root.data["type"] == "file_unavailable"


def test_failed_file_only_output_projects_local_failure_and_preserves_remote_state():
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(
                root=DataPart(
                    data={
                        "type": "file_unavailable",
                        "reason": "invalid_content",
                    }
                )
            )
        ],
    )
    report = {
        "attempted": 1,
        "stored": 0,
        "unavailable": 1,
        "failures": [{"code": "invalid_content"}],
    }
    task = Task(
        id="task-1",
        context_id="context-1",
        status=TaskStatus(state=TaskState.completed),
        artifacts=[artifact],
    )

    assert output_delivery_failed(task.artifacts, report) is True
    mark_task_output_delivery_failed(task)

    assert task.status.state == TaskState.failed
    assert task.metadata["output_failure_code"] == OUTPUT_DELIVERY_FAILURE_CODE
    assert task.metadata["remote_task_state"] == TaskState.completed.value
    assert artifact.parts[0].root.data["type"] == "file_unavailable"

    from common.a2a_task_projection import public_persisted_task_data

    projected = public_persisted_task_data(task)
    assert projected["metadata"] == {
        "output_failure_code": OUTPUT_DELIVERY_FAILURE_CODE,
        "remote_task_state": TaskState.completed.value,
    }
    assert projected["artifacts"][0]["parts"][0]["data"]["type"] == ("file_unavailable")


def test_meaningful_status_text_makes_unavailable_file_partial_output():
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[Part(root=DataPart(data={"type": "file_unavailable"}))],
    )
    report = {
        "attempted": 1,
        "stored": 0,
        "unavailable": 1,
        "failures": [{"code": "invalid_content"}],
    }

    assert output_delivery_failed([artifact], report, text="Useful summary") is False
    assert output_delivery_failed([artifact], report, text="  ") is True


def test_empty_data_does_not_make_unavailable_file_partial_output():
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=DataPart(data={"type": "file_unavailable"})),
            Part(root=DataPart(data={})),
        ],
    )
    report = {
        "attempted": 1,
        "stored": 0,
        "unavailable": 1,
        "failures": [{"code": "invalid_content"}],
    }

    assert output_delivery_failed([artifact], report) is True


def test_partial_useful_output_does_not_fail_completed_task():
    artifact = Artifact(
        artifact_id="artifact-1",
        parts=[
            Part(root=DataPart(data={"type": "file_unavailable"})),
            Part(root=DataPart(data={"result": "usable"})),
        ],
    )
    report = {
        "attempted": 1,
        "stored": 0,
        "unavailable": 1,
        "failures": [{"code": "invalid_content"}],
    }

    assert output_delivery_failed([artifact], report) is False


def test_completed_task_without_advertised_files_remains_completed():
    assert output_delivery_failed([], new_materialization_report()) is False
