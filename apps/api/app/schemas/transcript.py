"""Transcript and audio processing response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SpeechSegment(BaseModel):
    """A speech region identified by VAD, in original audio seconds."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)


class SpeakerTurn(SpeechSegment):
    """A speaker turn identified by diarization."""

    speaker: str


class TranscriptSegment(SpeakerTurn):
    """PhoWhisper transcription assigned to the most overlapping speaker."""

    text: str


class AudioProcessResponse(BaseModel):
    """Combined output from VAD, PhoWhisper, and pyannote."""

    filename: str
    duration_seconds: float = Field(ge=0)
    transcript: str
    vad_segments: list[SpeechSegment]
    diarization_segments: list[SpeakerTurn]
    transcript_segments: list[TranscriptSegment]
