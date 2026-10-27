"""Loopback-only voice profile, enrollment, and correction endpoints."""
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.routes.audio import MAX_UPLOAD_BYTES
from app.api.routes.meetings import (
    get_meeting_database_settings, get_meeting_repository, get_meeting_settings, require_loopback,
)
from app.api.auth import CurrentAccount, require_account, require_csrf
from app.services.access.repository import AccessRepository
from app.services.access.policy import require_meeting, require_project
from app.core.config import Settings
from app.core.database_config import DatabaseSettings
from app.schemas.voice import (
    EnrollmentJobResponse, MeetingEnrollmentRequest, SpeakerIdentityRequest,
    SpeakerIdentityResponse, VoiceProfileRequest, VoiceProfileResponse, VoiceSampleResponse,
)
from app.services.meetings.repository import MeetingRepository, MeetingStateConflict
from app.services.meetings.storage import audio_path
from app.services.voice.enrollment import EnrollmentError
from app.services.voice.repository import EnrollmentJob, SpeakerIdentity, VoiceProfile, VoiceRepository
from app.services.voice.storage import ingest_enrollment_upload

router = APIRouter(tags=['voice'], dependencies=[Depends(require_loopback)])


def get_voice_repository(repository: Annotated[MeetingRepository, Depends(get_meeting_repository)]) -> VoiceRepository:
    return VoiceRepository(repository.connection)


Repository = Annotated[VoiceRepository, Depends(get_voice_repository)]
AccountDependency = Annotated[CurrentAccount, Depends(require_account)]


def require_voice_access(account: CurrentAccount, repository: VoiceRepository, *, edit: bool = False,
                         delete: bool = False) -> None:
    if account.is_system_admin and not account.is_disabled:
        return
    access = AccessRepository(repository.connection)
    projects = access.visible_projects(account)
    if not projects or delete or (edit and not any(
        access.role(account, project.id) == 'project_manager' for project in projects
    )):
        raise HTTPException(403, 'Action not permitted')


def require_profile(repository: VoiceRepository, profile_id: str) -> None:
    if repository._get_profile(profile_id) is None:
        raise HTTPException(404, 'Voice profile not found')


@router.post('/voice-profiles', response_model=VoiceProfileResponse, status_code=201, dependencies=[Depends(require_csrf)])
def create_profile(body: VoiceProfileRequest, repository: Repository, account: AccountDependency) -> VoiceProfile:
    require_voice_access(account, repository, edit=True)
    return repository.create_profile(body.display_name)


@router.get('/voice-profiles', response_model=list[VoiceProfileResponse])
def list_profiles(repository: Repository, account: AccountDependency) -> list[VoiceProfile]:
    require_voice_access(account, repository)
    return repository.list_profiles()


@router.patch('/voice-profiles/{profile_id}', response_model=VoiceProfileResponse, dependencies=[Depends(require_csrf)])
def rename_profile(profile_id: str, body: VoiceProfileRequest, repository: Repository, account: AccountDependency) -> VoiceProfile:
    require_voice_access(account, repository, edit=True)
    profile = repository.rename_profile(profile_id, body.display_name)
    if profile is None:
        raise HTTPException(404, 'Voice profile not found')
    return profile


@router.delete('/voice-profiles/{profile_id}', status_code=204, dependencies=[Depends(require_csrf)])
def delete_profile(profile_id: str, repository: Repository, account: AccountDependency, settings: Annotated[Settings, Depends(get_meeting_settings)]) -> None:
    require_voice_access(account, repository, delete=True)
    keys = repository.delete_profile(profile_id)
    if keys is None:
        raise HTTPException(404, 'Voice profile not found')
    for key in keys:
        audio_path(settings.meeting_data_dir, key).unlink(missing_ok=True)


@router.get('/voice-profiles/{profile_id}/samples', response_model=list[VoiceSampleResponse])
def list_samples(profile_id: str, repository: Repository, account: AccountDependency) -> list[VoiceSampleResponse]:
    require_voice_access(account, repository)
    require_profile(repository, profile_id)
    samples = []
    access = AccessRepository(repository.connection)
    for sample in repository.list_samples(profile_id):
        if sample.source_meeting_id:
            try:
                require_meeting(account, sample.source_meeting_id, 'read', access)
            except HTTPException as error:
                if error.status_code == 404:
                    continue
                raise
        samples.append(sample)
    return [VoiceSampleResponse(**sample.__dict__, fifo_position=index)
            for index, sample in enumerate(samples, 1)]


@router.delete('/voice-profiles/{profile_id}/samples/{sample_id}', status_code=204, dependencies=[Depends(require_csrf)])
def delete_sample(profile_id: str, sample_id: int, repository: Repository, account: AccountDependency) -> None:
    require_voice_access(account, repository, delete=True)
    if not repository.delete_sample(profile_id, sample_id):
        raise HTTPException(404, 'Voice sample not found')


@router.post('/voice-profiles/{profile_id}/samples/upload', response_model=EnrollmentJobResponse, status_code=202, dependencies=[Depends(require_csrf)])
async def upload_sample(
    profile_id: str, repository: Repository, account: AccountDependency,
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    database: Annotated[DatabaseSettings, Depends(get_meeting_database_settings)],
    file: Annotated[UploadFile, File()],
) -> EnrollmentJob:
    try:
        require_voice_access(account, repository, edit=True)
        require_profile(repository, profile_id)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, 'Audio upload exceeds 25 MiB')
        if not data:
            raise HTTPException(400, 'Audio upload must not be empty')
        try:
            return await run_in_threadpool(ingest_enrollment_upload, database, settings.meeting_data_dir, profile_id, data, account.id)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(404, 'Voice profile not found') from None
    finally:
        await file.close()


@router.post('/voice-profiles/{profile_id}/samples/from-meeting', response_model=EnrollmentJobResponse, status_code=202, dependencies=[Depends(require_csrf)])
def enroll_meeting(profile_id: str, body: MeetingEnrollmentRequest, repository: Repository, account: AccountDependency) -> EnrollmentJob:
    require_profile(repository, profile_id)
    meeting = require_meeting(account, body.meeting_id, 'identify', AccessRepository(repository.connection))
    try:
        return repository.enqueue_from_meeting(profile_id, body.meeting_id, body.speaker, account.id, meeting.project_id)
    except EnrollmentError:
        raise HTTPException(409, 'Enrollment requires a completed meeting containing that speaker') from None
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, 'Voice profile or meeting not found') from None


@router.get('/voice-enrollment-jobs/{job_id}', response_model=EnrollmentJobResponse)
def get_job(job_id: str, repository: Repository, account: AccountDependency) -> EnrollmentJob:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(404, 'Enrollment job not found')
    if job.source_meeting_id:
        require_meeting(account, job.source_meeting_id, 'read', AccessRepository(repository.connection))
    elif job.project_id:
        require_project(account, job.project_id, 'read', AccessRepository(repository.connection))
    elif not account.is_system_admin and job.created_by_account_id != account.id:
        raise HTTPException(404, 'Enrollment job not found')
    return job


@router.put('/meetings/{meeting_id}/speaker-identity', response_model=SpeakerIdentityResponse, dependencies=[Depends(require_csrf)])
def correct_identity(meeting_id: str, body: SpeakerIdentityRequest, repository: Repository, account: AccountDependency) -> SpeakerIdentity:
    require_meeting(account, meeting_id, 'identify', AccessRepository(repository.connection))
    try:
        return repository.set_meeting_identity(meeting_id, body.speaker, body.profile_id)
    except LookupError:
        raise HTTPException(404, 'Meeting or voice profile not found') from None
    except MeetingStateConflict:
        raise HTTPException(409, 'Correction requires a completed meeting containing that speaker') from None
