from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from starlette.responses import StreamingResponse

from api_gateway.dependencies import (
    get_file_storage,
    get_room_ownership_reader,
    get_room_store,
)
from api_gateway.errors import require_chat_room
from api_gateway.owner_session import get_current_user
from api_gateway.registry import mark_declared_owner as _mark_declared_owner
from common.auth import ClerkUser
from common.errors import FileStoragePlatformError
from common.protocols import (
    FileStorage,
    PreparedFileStream,
    RoomOwnershipReader,
    RoomRouteReader,
)
from models.file_upload import FileUploadResponse
from room_files import normalize_file_id, normalize_mime_type

router = APIRouter(prefix="/files", tags=["files"])
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024


class _PreparedStreamingResponse(StreamingResponse):
    def __init__(self, content: PreparedFileStream, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._prepared_content = content

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._prepared_content.aclose()


@router.post("/upload")
async def upload_file(
    file: UploadFile,
    conversation_id: str = Form(...),
    user: ClerkUser = Depends(get_current_user),
    storage: FileStorage = Depends(get_file_storage),
    room_ownership: RoomOwnershipReader = Depends(get_room_ownership_reader),
    store: RoomRouteReader = Depends(get_room_store),
):
    """Upload a file for attachment to a room message.

    Accepts multipart/form-data with:
    - file: The file to upload
    - conversation_id: The room this file belongs to

    Returns FileUploadResponse with a stable authenticated content URL.
    """

    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")
    owner_id = await room_ownership.get_room_owner(conversation_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if owner_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to access this room",
        )
    require_chat_room(await store.get_room_by_room_id(conversation_id))

    try:
        file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {MAX_UPLOAD_BYTES}-byte upload limit",
            )
        uploaded = await storage.upload(
            file_bytes=file_bytes,
            filename=file.filename or "unnamed",
            owner_id=user.user_id,
            conversation_id=conversation_id,
            content_type=file.content_type,
        )
    except FileStoragePlatformError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    return FileUploadResponse(
        file_id=uploaded.file_id,
        file_url=uploaded.url or "",
        mime_type=uploaded.mime_type,
        file_name=uploaded.file_name,
        size_bytes=uploaded.size_bytes,
    )


@router.get("/{file_id}/content")
async def download_file(
    file_id: str,
    user: ClerkUser = Depends(get_current_user),
    storage: FileStorage = Depends(get_file_storage),
):
    try:
        normalized = normalize_file_id(file_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    prepared = await storage.prepare_download(
        normalized,
        owner_id=user.user_id,
        chunk_size=DOWNLOAD_CHUNK_SIZE,
    )
    if prepared is None:
        raise HTTPException(status_code=404, detail="File not found")
    metadata, content = prepared

    filename = metadata.file_name
    mime_type = normalize_mime_type(metadata.mime_type)
    size_bytes = metadata.size_bytes
    encoded_name = quote(filename, safe="")
    return _PreparedStreamingResponse(
        content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Content-Length": str(size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


_mark_declared_owner(router, __name__)
