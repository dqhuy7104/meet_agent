"""Public voice metadata and correction contracts."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class VoiceProfileRequest(BaseModel):
    display_name: DisplayName


class VoiceProfileResponse(BaseModel):
    id: str
    display_name: str
    sample_count: int


class VoiceSampleResponse(BaseModel):
    id: int
    source: Literal['upload', 'meeting']
    source_meeting_id: str | None
    source_speaker: str | None
    fingerprint: str
    created_at: datetime
    fifo_position: int


class EnrollmentJobResponse(BaseModel):
    id: str
    status: Literal['queued', 'processing', 'completed', 'failed']
    failure_code: str | None


class MeetingEnrollmentRequest(BaseModel):
    meeting_id: str
    speaker: str


class SpeakerIdentityRequest(BaseModel):
    speaker: str
    profile_id: str | None


class SpeakerIdentityResponse(BaseModel):
    speaker: str
    profile_id: str | None
    display_name: str | None
    similarity: float | None
    origin: Literal['automatic', 'manual']
