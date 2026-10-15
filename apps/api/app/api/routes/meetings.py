"""Local-only meeting library endpoints."""

from __future__ import annotations

import hashlib
import ipaddress
import sqlite3
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.api.routes.audio import MAX_UPLOAD_BYTES
from app.core.config import Settings, get_settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.schemas.meeting import (
    MeetingAcceptedResponse, MeetingDetailResponse, MeetingListResponse, MeetingMetadataResponse,
    MeetingSearchResponse, SearchHitResponse,
)
from app.services.meetings.repository import (
    IdempotencyConflict, MeetingRecord, MeetingRepository, MeetingStateConflict,
)
from app.services.meetings.storage import audio_path, audio_publication_lock, publish_audio


async def require_loopback(request: Request) -> None:
    """Restrict meeting access to the same local browser origin and peer."""
    try:
        is_local = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        is_local = False
    if not is_local:
        raise HTTPException(status_code=403, detail="Meeting library is available only on loopback")
    host = request.url.hostname
    try:
        is_local_host = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        is_local_host = False
    if not is_local_host:
        raise HTTPException(status_code=403, detail="Meeting library requires a local Host")
    origin = request.headers.get("origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
        expected_origin = f"{request.url.scheme}://{request.headers['host']}"
        if origin != expected_origin:
            raise HTTPException(status_code=403, detail="Meeting library requires a matching Origin")


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
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _metadata(record: MeetingRecord) -> MeetingMetadataResponse:
    return MeetingMetadataResponse(
        id=record.id, title=record.title, original_filename=record.original_filename,
        created_at=record.created_at, updated_at=record.updated_at, status=record.status,
        attempt_count=record.attempt_count, duration_seconds=record.duration_seconds,
        failure_code=record.failure_code,
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
    if idempotency_key == "":
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be empty")
    checksum = hashlib.sha256(data).hexdigest()
    if idempotency_key:
        existing = repository.find_by_idempotency_key(idempotency_key)
        if existing:
            if existing.checksum != checksum:
                raise HTTPException(status_code=409, detail="Idempotency key belongs to different audio")
            return _accepted(existing)

    with audio_publication_lock(settings.meeting_data_dir):
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


@router.get("", response_model=MeetingListResponse)
async def list_meetings(
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MeetingListResponse:
    """List stored meetings from newest to oldest."""
    return MeetingListResponse(
        items=[_metadata(record) for record in repository.list_meetings(limit, offset)],
        total=repository.count_meetings(), limit=limit, offset=offset,
    )


@router.get("/search", response_model=MeetingSearchResponse)
async def search_meetings(
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MeetingSearchResponse:
    """Search completed meeting transcripts using plain text."""
    if not q.strip():
        raise HTTPException(status_code=422, detail="Search query must not be blank")
    if "\x00" in q:
        raise HTTPException(status_code=422, detail="Search query must not contain NUL")
    hits, total = repository.search_segments(q, limit, offset)
    return MeetingSearchResponse(
        items=[SearchHitResponse(**hit.__dict__) for hit in hits],
        total=total, limit=limit, offset=offset,
    )


@router.get("/{meeting_id}", response_model=MeetingDetailResponse)
async def get_meeting(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
) -> MeetingDetailResponse:
    """Return metadata and completed transcript results for one meeting."""
    detail = repository.get_detail(meeting_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return MeetingDetailResponse(
        **_metadata(detail.meeting).model_dump(),
        transcript=detail.meeting.transcript if detail.meeting.status == "completed" else None,
        vad_segments=detail.vad_segments,
        diarization_segments=detail.diarization_segments,
        transcript_segments=detail.transcript_segments,
    )


@router.get("/{meeting_id}/audio", response_class=FileResponse)
async def get_meeting_audio(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> FileResponse:
    """Serve the original audio, including byte range requests."""
    record = repository.get_meeting(meeting_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    path = audio_path(settings.meeting_data_dir, record.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Meeting audio unavailable")
    filename = Path(record.original_filename.replace("\\", "/")).name
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> None:
    """Delete a non-processing meeting and its original audio."""
    try:
        storage_key = repository.delete(meeting_id)
    except MeetingStateConflict as error:
        raise HTTPException(status_code=409, detail="Processing meeting cannot be deleted") from error
    if storage_key is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    audio_path(settings.meeting_data_dir, storage_key).unlink(missing_ok=True)


@router.post("/{meeting_id}/retry", response_model=MeetingAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_meeting(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
) -> MeetingAcceptedResponse:
    """Requeue a failed meeting using its existing ID and audio."""
    try:
        record = repository.retry(meeting_id)
    except MeetingStateConflict as error:
        raise HTTPException(status_code=409, detail="Only failed meetings can be retried") from error
    if record is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _accepted(record)
