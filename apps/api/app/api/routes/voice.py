"""Loopback-only voice profile, enrollment, and correction endpoints."""
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.routes.audio import MAX_UPLOAD_BYTES
from app.api.routes.meetings import (
    get_meeting_database_settings, get_meeting_repository, get_meeting_settings, require_loopback,
)
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


def require_profile(repository: VoiceRepository, profile_id: str) -> None:
    if repository._get_profile(profile_id) is None:
        raise HTTPException(404, 'Voice profile not found')


@router.post('/voice-profiles', response_model=VoiceProfileResponse, status_code=201)
def create_profile(body: VoiceProfileRequest, repository: Repository) -> VoiceProfile:
    return repository.create_profile(body.display_name)


@router.get('/voice-profiles', response_model=list[VoiceProfileResponse])
def list_profiles(repository: Repository) -> list[VoiceProfile]:
    return repository.list_profiles()


@router.patch('/voice-profiles/{profile_id}', response_model=VoiceProfileResponse)
def rename_profile(profile_id: str, body: VoiceProfileRequest, repository: Repository) -> VoiceProfile:
    profile = repository.rename_profile(profile_id, body.display_name)
    if profile is None:
        raise HTTPException(404, 'Voice profile not found')
    return profile


@router.delete('/voice-profiles/{profile_id}', status_code=204)
def delete_profile(profile_id: str, repository: Repository, settings: Annotated[Settings, Depends(get_meeting_settings)]) -> None:
    keys = repository.delete_profile(profile_id)
    if keys is None:
        raise HTTPException(404, 'Voice profile not found')
    for key in keys:
        audio_path(settings.meeting_data_dir, key).unlink(missing_ok=True)


@router.get('/voice-profiles/{profile_id}/samples', response_model=list[VoiceSampleResponse])
def list_samples(profile_id: str, repository: Repository) -> list[VoiceSampleResponse]:
    require_profile(repository, profile_id)
    return [VoiceSampleResponse(**sample.__dict__, fifo_position=index)
            for index, sample in enumerate(repository.list_samples(profile_id), 1)]


@router.delete('/voice-profiles/{profile_id}/samples/{sample_id}', status_code=204)
def delete_sample(profile_id: str, sample_id: int, repository: Repository) -> None:
    if not repository.delete_sample(profile_id, sample_id):
        raise HTTPException(404, 'Voice sample not found')


@router.post('/voice-profiles/{profile_id}/samples/upload', response_model=EnrollmentJobResponse, status_code=202)
async def upload_sample(
    profile_id: str, repository: Repository,
    settings: Annotated[Settings, Depends(get_meeting_settings)],
    database: Annotated[DatabaseSettings, Depends(get_meeting_database_settings)],
    file: Annotated[UploadFile, File()],
) -> EnrollmentJob:
    try:
        require_profile(repository, profile_id)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, 'Audio upload exceeds 25 MiB')
        if not data:
            raise HTTPException(400, 'Audio upload must not be empty')
        try:
            return await run_in_threadpool(ingest_enrollment_upload, database, settings.meeting_data_dir, profile_id, data)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(404, 'Voice profile not found') from None
    finally:
        await file.close()


@router.post('/voice-profiles/{profile_id}/samples/from-meeting', response_model=EnrollmentJobResponse, status_code=202)
def enroll_meeting(profile_id: str, body: MeetingEnrollmentRequest, repository: Repository) -> EnrollmentJob:
    require_profile(repository, profile_id)
    if MeetingRepository(repository.connection).get_meeting(body.meeting_id) is None:
        raise HTTPException(404, 'Meeting not found')
    try:
        return repository.enqueue_from_meeting(profile_id, body.meeting_id, body.speaker)
    except EnrollmentError:
        raise HTTPException(409, 'Enrollment requires a completed meeting containing that speaker') from None
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, 'Voice profile or meeting not found') from None


@router.get('/voice-enrollment-jobs/{job_id}', response_model=EnrollmentJobResponse)
def get_job(job_id: str, repository: Repository) -> EnrollmentJob:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(404, 'Enrollment job not found')
    return job


@router.put('/meetings/{meeting_id}/speaker-identity', response_model=SpeakerIdentityResponse)
def correct_identity(meeting_id: str, body: SpeakerIdentityRequest, repository: Repository) -> SpeakerIdentity:
    try:
        return repository.set_meeting_identity(meeting_id, body.speaker, body.profile_id)
    except LookupError:
        raise HTTPException(404, 'Meeting or voice profile not found') from None
    except MeetingStateConflict:
        raise HTTPException(409, 'Correction requires a completed meeting containing that speaker') from None
