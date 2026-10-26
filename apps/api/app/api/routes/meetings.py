"""Local-only meeting library endpoints."""

from __future__ import annotations

import hashlib
import psycopg
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.api.routes.audio import MAX_UPLOAD_BYTES
from app.core.config import Settings, get_settings
from app.db.session import open_database, validate_database
from app.core.database_config import DatabaseSettings, get_database_settings
from app.services.meetings.storage import initialize_meeting_storage
from app.services.meetings.ingestion import ingest_meeting
from starlette.concurrency import run_in_threadpool
from app.schemas.meeting import (
    MeetingAcceptedResponse, MeetingDetailResponse, MeetingListResponse, MeetingMetadataResponse,
    MeetingAudioFileResponse, MeetingSearchResponse, SearchHitResponse,
)
from app.services.meetings.repository import (
    IdempotencyConflict, MeetingRecord, MeetingRepository, MeetingStateConflict,
)
from app.services.meetings.storage import audio_path, audio_publication_lock, publish_audio
from app.services.voice.repository import VoiceRepository
from app.services.meetings.worker import terminate_process


from app.api.local_access import is_local_origin, require_loopback


router = APIRouter(prefix="/meetings", tags=["meetings"], dependencies=[Depends(require_loopback)])
MAX_MEETING_FILES = 10
MAX_MEETING_UPLOAD_BYTES = 100 * 1024 * 1024


async def get_meeting_settings() -> Settings:
    """Return process settings without a thread-pool dependency hop."""
    return get_settings()


def get_meeting_database_settings() -> DatabaseSettings:
    return get_database_settings()


def get_meeting_repository(
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    database: Annotated[DatabaseSettings, Depends(get_meeting_database_settings)],
) -> Iterator[MeetingRepository]:
    with open_database(database) as connection:
        validate_database(connection)
        initialize_meeting_storage(settings.meeting_data_dir, connection)
        yield MeetingRepository(connection)


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
        failure_code=record.failure_code, pipeline_pid=record.pipeline_pid,
        source=record.source,
    )


@router.post("", response_model=MeetingAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_meeting(
    database: Annotated[DatabaseSettings, Depends(get_meeting_database_settings)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    file: Annotated[UploadFile | None, File(description="One original audio file, up to 25 MiB")] = None,
    files: Annotated[list[UploadFile] | None, File(description="Ordered meeting audio files")] = None,
    title: Annotated[str | None, Form(max_length=200)] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
) -> MeetingAcceptedResponse:
    """Store ordered audio parts and queue one meeting."""
    uploads = [file] if file is not None else files or []
    if file is not None and files:
        raise HTTPException(status_code=422, detail="Use either file or files")
    if not uploads:
        raise HTTPException(status_code=422, detail="At least one audio file is required")
    if len(uploads) > MAX_MEETING_FILES:
        raise HTTPException(status_code=413, detail="A meeting supports at most 10 audio files")
    parts: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        for upload in uploads:
            if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Audio file exceeds 25 MiB")
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            if not data:
                raise HTTPException(status_code=400, detail="Audio file is empty")
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Audio file exceeds 25 MiB")
            total_bytes += len(data)
            if total_bytes > MAX_MEETING_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Meeting audio exceeds 100 MiB")
            parts.append((upload.filename or "audio", data))
    finally:
        for upload in uploads:
            await upload.close()
    if idempotency_key == "":
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be empty")
    try:
        record = await run_in_threadpool(ingest_meeting, database, settings.meeting_data_dir, parts, title, idempotency_key)
    except IdempotencyConflict:
        raise HTTPException(status_code=409, detail="Idempotency key belongs to different audio") from None
    return _accepted(record)


@router.get("", response_model=MeetingListResponse)
def list_meetings(
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
def search_meetings(
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
def get_meeting(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
) -> MeetingDetailResponse:
    """Return metadata and completed transcript results for one meeting."""
    detail = repository.get_detail(meeting_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return MeetingDetailResponse(
        **_metadata(detail.meeting).model_dump(),
        voice_identification_status=detail.meeting.voice_identification_status,
        speaker_identities=[identity.__dict__ for identity in VoiceRepository(repository.connection).identities_for_meeting(meeting_id)],
        transcript=detail.meeting.transcript if detail.meeting.status == "completed" else None,
        vad_segments=detail.vad_segments,
        diarization_segments=detail.diarization_segments,
        transcript_segments=detail.transcript_segments,
        audio_files=[MeetingAudioFileResponse(
            ordinal=part.ordinal, original_filename=part.original_filename,
            duration_seconds=part.duration_seconds,
            start_seconds=part.start_seconds,
        ) for part in detail.audio_files],
    )


@router.get("/{meeting_id}/audio", response_class=FileResponse)
def get_meeting_audio(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> FileResponse:
    """Serve the first original audio for the legacy URL."""
    return _audio_response(meeting_id, 0, repository, settings)


@router.get("/{meeting_id}/audio/{ordinal}", response_class=FileResponse)
def get_meeting_audio_part(
    meeting_id: str,
    ordinal: int,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> FileResponse:
    """Serve one original audio part, including byte ranges."""
    return _audio_response(meeting_id, ordinal, repository, settings)


def _audio_response(
    meeting_id: str, ordinal: int, repository: MeetingRepository, settings: Settings,
) -> FileResponse:
    record = repository.get_meeting(meeting_id)
    if record is None or record.status == "cancelled":
        raise HTTPException(status_code=404, detail="Meeting not found")
    audio_files = repository.get_audio_files(meeting_id)
    part = next((part for part in audio_files if part.ordinal == ordinal), None)
    if part is None:
        raise HTTPException(status_code=404, detail="Meeting audio unavailable")
    path = audio_path(settings.meeting_data_dir, part.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Meeting audio unavailable")
    filename = Path(part.original_filename.replace("\\", "/")).name
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meeting(
    meeting_id: str,
    repository: Annotated[MeetingRepository, Depends(get_meeting_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> None:
    """Cancel a meeting, remove its audio, and retain only metadata."""
    cancelled = repository.cancel(meeting_id)
    if cancelled is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    for storage_key in (cancelled.storage_key, *cancelled.other_storage_keys):
        audio_path(settings.meeting_data_dir, storage_key).unlink(missing_ok=True)
    if cancelled.pipeline_pid is not None and cancelled.pipeline_start_ticks is not None:
        terminate_process(cancelled.pipeline_pid, cancelled.pipeline_start_ticks)


@router.post("/{meeting_id}/retry", response_model=MeetingAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
def retry_meeting(
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
