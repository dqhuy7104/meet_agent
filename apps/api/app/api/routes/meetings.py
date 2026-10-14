"""Local-only meeting library endpoints."""

from __future__ import annotations

import hashlib
import ipaddress
import sqlite3
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status

from app.api.routes.audio import MAX_UPLOAD_BYTES
from app.core.config import Settings, get_settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.schemas.meeting import MeetingAcceptedResponse
from app.services.meetings.repository import IdempotencyConflict, MeetingRecord, MeetingRepository
from app.services.meetings.storage import audio_path, publish_audio


async def require_loopback(request: Request) -> None:
    """Reject clients whose actual connection peer is not loopback."""
    try:
        is_local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        is_local = False
    if not is_local:
        raise HTTPException(status_code=403, detail="Meeting library is available only on loopback")


router = APIRouter(prefix="/meetings", tags=["meetings"], dependencies=[Depends(require_loopback)])


async def get_meeting_settings() -> Settings:
    """Return process settings without a thread-pool dependency hop."""
    return get_settings()


async def get_meeting_repository(settings: Annotated[Settings, Depends(get_meeting_settings)]) -> AsyncIterator[MeetingRepository]:
    """Open and close one SQLite connection for this request."""
    initialize_meeting_store(settings.meeting_data_dir)
    connection = open_meeting_db(settings.meeting_data_dir)
    try:
        yield MeetingRepository(connection)
    finally:
        connection.close()


def _accepted(record: MeetingRecord) -> MeetingAcceptedResponse:
    return MeetingAcceptedResponse(
        meeting_id=record.id,
        title=record.title,
        original_filename=record.original_filename,
        status="queued",
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.post("", response_model=MeetingAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_meeting(
    file: Annotated[UploadFile, File(description="Original meeting audio, up to 25 MiB")],
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    title: Annotated[str | None, Form(max_length=200)] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
) -> MeetingAcceptedResponse:
    """Store audio and queue one meeting without running inference in the API."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio file exceeds 25 MiB")
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if not data:
        raise HTTPException(status_code=400, detail="Audio file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio file exceeds 25 MiB")
    checksum = hashlib.sha256(data).hexdigest()
    if idempotency_key:
        existing = repository.find_by_idempotency_key(idempotency_key)
        if existing:
            if existing.checksum != checksum:
                raise HTTPException(status_code=409, detail="Idempotency key belongs to different audio")
            return _accepted(existing)

    storage_key = publish_audio(settings.meeting_data_dir, data)
    try:
        record = repository.create_meeting(
            uuid.uuid4().hex, title, file.filename or "audio", storage_key, checksum, idempotency_key
        )
    except IdempotencyConflict as error:
        audio_path(settings.meeting_data_dir, storage_key).unlink(missing_ok=True)
        existing = repository.find_by_idempotency_key(idempotency_key)
        if existing and existing.checksum == checksum:
            return _accepted(existing)
        raise HTTPException(status_code=409, detail="Idempotency key belongs to different audio") from error
    except (sqlite3.Error, OSError):
        audio_path(settings.meeting_data_dir, storage_key).unlink(missing_ok=True)
        raise
    return _accepted(record)
