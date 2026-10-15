"""Contracts for the local meeting library."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.schemas.transcript import SpeakerTurn, SpeechSegment, TranscriptSegment


class MeetingAcceptedResponse(BaseModel):
    meeting_id: str
    title: str | None
    original_filename: str
    status: Literal["queued", "processing", "completed", "failed"]
    created_at: str
    updated_at: str


class MeetingMetadataResponse(BaseModel):
    id: str
    title: str | None
    original_filename: str
    created_at: str
    updated_at: str
    status: Literal["queued", "processing", "completed", "failed"]
    attempt_count: int
    duration_seconds: float | None
    failure_code: str | None


class MeetingListResponse(BaseModel):
    items: list[MeetingMetadataResponse]
    total: int
    limit: int
    offset: int


class MeetingDetailResponse(MeetingMetadataResponse):
    transcript: str | None
    vad_segments: list[SpeechSegment]
    diarization_segments: list[SpeakerTurn]
    transcript_segments: list[TranscriptSegment]


class SearchHitResponse(BaseModel):
    meeting_id: str
    title: str | None
    speaker: str
    start: float
    end: float
    text: str


class MeetingSearchResponse(BaseModel):
    items: list[SearchHitResponse]
    total: int
    limit: int
    offset: int
