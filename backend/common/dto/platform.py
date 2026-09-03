from datetime import datetime

from pydantic import Field, JsonValue

from common.dto.base import FrozenDTO


class FileMetadata(FrozenDTO):
    file_id: str
    room_id: str
    owner_id: str
    source: str
    mime_type: str
    file_name: str
    size_bytes: int
    sha256: str
    status: str
    created_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class FileInfo(FrozenDTO):
    file_id: str
    file_name: str
    mime_type: str
    size_bytes: int
    url: str | None = None


__all__ = [
    "FileInfo",
    "FileMetadata",
]
