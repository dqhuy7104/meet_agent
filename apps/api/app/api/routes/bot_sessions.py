"""Local-only Google Meet bot control endpoints."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.api.routes.meetings import get_meeting_settings, require_loopback
from app.core.config import Settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.schemas.bot_session import BotSessionCreate, BotSessionResponse
from app.services.meetings.bot_repository import BotSession, BotSessionRepository, BotStateConflict
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path, audio_publication_lock


router = APIRouter(prefix="/bot-sessions", tags=["bot-sessions"], dependencies=[Depends(require_loopback)])
_MEET_CODE = re.compile(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}\Z")


def canonical_meet_url(url: str) -> str:
    """Accept only the regular HTTPS meeting-code form on Google's exact host."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "meet.google.com"
                or not _MEET_CODE.fullmatch(parsed.path)):
            raise ValueError("Invalid Meet URL")
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Enter a standard https://meet.google.com/xxx-xxxx-xxx link") from error
    return f"https://meet.google.com{parsed.path}"


async def get_bot_repository(
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> AsyncIterator[BotSessionRepository]:
    initialize_meeting_store(settings.meeting_data_dir)
    connection = open_meeting_db(settings.meeting_data_dir)
    try:
        yield BotSessionRepository(connection)
    finally:
        connection.close()


def _response(repository: BotSessionRepository, session: BotSession) -> BotSessionResponse:
    if session.meeting_id:
        count = len(MeetingRepository(repository.connection).get_audio_files(session.meeting_id))
    else:
        count = len(repository.parts(session.id))
    return BotSessionResponse(
        id=session.id, status=session.status, failure_code=session.failure_code,
        created_at=session.created_at, started_at=session.started_at, ended_at=session.ended_at,
        finalized_part_count=count, capture_incomplete=session.capture_incomplete,
        meeting_id=session.meeting_id, stop_requested=session.stop_requested,
    )


def _get(repository: BotSessionRepository, session_id: str) -> BotSession:
    session = repository.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Bot session not found")
    return session


@router.post("", response_model=BotSessionResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_bot_session(
    body: BotSessionCreate,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
) -> BotSessionResponse:
    url = canonical_meet_url(body.url)
    if idempotency_key == "":
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be empty")
    try:
        session = repository.create(url, body.title, idempotency_key)
    except BotStateConflict as error:
        raise HTTPException(status_code=409, detail="A bot session is already active or the key belongs to another link") from error
    return _response(repository, session)


@router.get("/active", response_model=BotSessionResponse | None)
async def get_active_bot_session(
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse | Response:
    session = repository.get_active()
    return _response(repository, session) if session else Response(status_code=204)


@router.get("/{session_id}", response_model=BotSessionResponse)
async def get_bot_session(
    session_id: str, repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse:
    return _response(repository, _get(repository, session_id))


@router.post("/{session_id}/stop", response_model=BotSessionResponse)
async def stop_bot_session(
    session_id: str, repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse:
    session = _get(repository, session_id)
    if session.status not in {"queued", "joining", "waiting_admission", "recording"}:
        raise HTTPException(status_code=409, detail="Bot session cannot be stopped")
    stopped = repository.request_stop(session_id)
    return _response(repository, stopped)  # type: ignore[arg-type]


@router.post("/{session_id}/process-parts", response_model=BotSessionResponse)
async def process_bot_parts(
    session_id: str,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> BotSessionResponse:
    session = _get(repository, session_id)
    if session.status == "completed" and session.capture_incomplete:
        return _response(repository, session)
    if session.status != "interrupted" or not repository.parts(session_id):
        raise HTTPException(status_code=409, detail="No interrupted recording parts are available")
    try:
        with audio_publication_lock(settings.meeting_data_dir):
            repository.finalize(session_id, partial=True)
    except BotStateConflict as error:
        raise HTTPException(status_code=409, detail="Recording parts cannot be processed") from error
    return _response(repository, _get(repository, session_id))


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_bot_session(
    session_id: str,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> None:
    _get(repository, session_id)
    try:
        with audio_publication_lock(settings.meeting_data_dir):
            keys = repository.discard(session_id)
            for key in keys:
                audio_path(settings.meeting_data_dir, key).unlink(missing_ok=True)
    except BotStateConflict as error:
        raise HTTPException(status_code=409, detail="Bot session cannot be discarded") from error
