"""Local-only Google Meet bot control endpoints."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.api.routes.meetings import get_meeting_settings, get_meeting_database_settings, require_loopback
from app.api.auth import CurrentAccount, require_csrf
from app.api.routes.projects import AccountDependency, RepositoryDependency
from app.services.access.policy import require_project, visible_project_ids
from app.services.access.repository import AccessRepository
from app.core.config import Settings
from app.db.session import open_database, validate_database
from app.core.database_config import DatabaseSettings
from app.services.meetings.storage import initialize_meeting_storage
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


def get_bot_repository(
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    database: Annotated[DatabaseSettings, Depends(get_meeting_database_settings)],
) -> Iterator[BotSessionRepository]:
    with open_database(database) as connection:
        validate_database(connection)
        initialize_meeting_storage(settings.meeting_data_dir, connection)
        yield BotSessionRepository(connection, settings.meeting_data_dir)


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




def _authorize(session: BotSession, account: CurrentAccount, access: AccessRepository, *, mutate: bool = False) -> None:
    if session.project_id is None:
        raise HTTPException(404, 'Bot session not found')
    require_project(account, session.project_id, 'read', access)
    if not mutate:
        return
    role = access.role(account, session.project_id)
    if role in {'system_admin', 'project_manager'}:
        return
    if role == 'member' and session.started_by_account_id == account.id and session.meeting_id is None:
        return
    raise HTTPException(403, 'Action not permitted')


@router.post("", response_model=BotSessionResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_csrf)])
def create_bot_session(
    body: BotSessionCreate,
    account: AccountDependency, access: RepositoryDependency,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
) -> BotSessionResponse:
    require_project(account, body.project_id, 'start_bot', access)
    url = canonical_meet_url(body.url)
    if idempotency_key == "":
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be empty")
    try:
        session = repository.create(url, body.title, idempotency_key, body.project_id, account.id)
    except BotStateConflict as error:
        raise HTTPException(status_code=409, detail="A bot session is already active or the key belongs to another link") from error
    return _response(repository, session)


@router.get("/active", response_model=BotSessionResponse | None)
def get_active_bot_session(
    account: AccountDependency, access: RepositoryDependency,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse | Response:
    session = repository.get_active()
    if session and session.project_id not in visible_project_ids(account, access):
        session = None
    return _response(repository, session) if session else Response(status_code=204)


@router.get("/recent", response_model=BotSessionResponse | None)
def get_recent_interrupted_bot_session(
    account: AccountDependency, access: RepositoryDependency,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse | Response:
    session = repository.get_recent_interrupted(visible_project_ids(account, access))
    return _response(repository, session) if session else Response(status_code=204)


@router.get("/{session_id}", response_model=BotSessionResponse)
def get_bot_session(
    account: AccountDependency, access: RepositoryDependency,
    session_id: str, repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse:
    session = _get(repository, session_id)
    _authorize(session, account, access)
    return _response(repository, session)


@router.post("/{session_id}/stop", response_model=BotSessionResponse, dependencies=[Depends(require_csrf)])
def stop_bot_session(
    account: AccountDependency, access: RepositoryDependency,
    session_id: str, repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
) -> BotSessionResponse:
    session = _get(repository, session_id)
    _authorize(session, account, access, mutate=True)
    if session.status not in {"queued", "joining", "waiting_admission", "recording"}:
        raise HTTPException(status_code=409, detail="Bot session cannot be stopped")
    stopped = repository.request_stop(session_id)
    return _response(repository, stopped)  # type: ignore[arg-type]


@router.post("/{session_id}/process-parts", response_model=BotSessionResponse, dependencies=[Depends(require_csrf)])
def process_bot_parts(
    account: AccountDependency, access: RepositoryDependency,
    session_id: str,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> BotSessionResponse:
    session = _get(repository, session_id)
    _authorize(session, account, access, mutate=True)
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


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_csrf)])
def discard_bot_session(
    account: AccountDependency, access: RepositoryDependency,
    session_id: str,
    repository: Annotated[BotSessionRepository, Depends(get_bot_repository)],
    settings: Annotated[Settings, Depends(get_meeting_settings)],
) -> None:
    session = _get(repository, session_id)
    _authorize(session, account, access, mutate=True)
    try:
        with audio_publication_lock(settings.meeting_data_dir):
            keys = repository.discard(session_id)
            for key in keys:
                audio_path(settings.meeting_data_dir, key).unlink(missing_ok=True)
    except BotStateConflict as error:
        raise HTTPException(status_code=409, detail="Bot session cannot be discarded") from error
