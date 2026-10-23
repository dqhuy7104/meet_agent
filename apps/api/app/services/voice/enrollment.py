"""Extract enrollment centroids without changing stored speaker labels."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import numpy as np
import torch

from app.core.audio_models import ModelRegistry
from app.schemas.transcript import SpeakerTurn
from app.services.audio.preprocess import AudioTooLongError, InvalidAudioError, SAMPLE_RATE, decode_audio
from app.services.diarization.pyannote import diarization_turns, extract_speaker_embeddings
from app.services.meetings.repository import MeetingDetail
from app.services.meetings.storage import audio_path
from app.services.voice.matching import normalize_embedding


class EnrollmentError(ValueError):
    """A safe enrollment failure suitable for job status."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _exclusive_intervals(turns: list[SpeakerTurn], speaker: str, duration: float) -> list[tuple[float, float]]:
    boundaries = sorted({max(0.0, min(duration, value)) for turn in turns for value in (turn.start, turn.end)})
    intervals = []
    for start, end in zip(boundaries, boundaries[1:]):
        active = {turn.speaker for turn in turns if turn.start < end and turn.end > start}
        if active == {speaker} and end > start:
            intervals.append((start, end))
    return intervals


def _infer(waveform: np.ndarray, models: ModelRegistry) -> Any:
    try:
        return models.get_diarization()({
            'waveform': torch.from_numpy(waveform).unsqueeze(0), 'sample_rate': SAMPLE_RATE,
        })
    except Exception as error:
        raise EnrollmentError('model_unavailable') from error


def _vector(result: Any, speaker: str) -> list[float]:
    vector = normalize_embedding(extract_speaker_embeddings(result).get(speaker, []))
    if vector is None:
        raise EnrollmentError('model_unavailable')
    return vector


def extract_upload_embedding(data: bytes, models: ModelRegistry) -> list[float]:
    """Require one speaker and ten seconds of speech in a two-minute upload."""
    try:
        waveform = decode_audio(data, max_duration_seconds=120)
    except AudioTooLongError as error:
        raise EnrollmentError('audio_too_long') from error
    except InvalidAudioError as error:
        raise EnrollmentError('invalid_audio') from error
    result = _infer(waveform, models)
    turns = diarization_turns(result)
    speakers = {turn.speaker for turn in turns}
    if len(speakers) > 1:
        raise EnrollmentError('multiple_speakers')
    if not speakers:
        raise EnrollmentError('insufficient_speech')
    speaker = next(iter(speakers))
    intervals = _exclusive_intervals(turns, speaker, len(waveform) / SAMPLE_RATE)
    if sum(end - start for start, end in intervals) < 10:
        raise EnrollmentError('insufficient_speech')
    return _vector(result, speaker)


def extract_meeting_embedding(
    detail: MeetingDetail, speaker: str, data_dir: Path, models: ModelRegistry,
) -> list[float]:
    """Align a stored label to exclusive speech from its original audio part."""
    if detail.meeting.status != 'completed' or not any(
        turn.speaker == speaker for turn in detail.diarization_segments
    ):
        raise EnrollmentError('source_changed')
    prefix = re.match(r'^PART_(\d+)_', speaker)
    ordinal = int(prefix.group(1)) - 1 if prefix else 0
    part = next((part for part in detail.audio_files if part.ordinal == ordinal), None)
    if part is None or part.start_seconds is None or part.start_seconds < 0:
        raise EnrollmentError('source_changed')
    try:
        waveform = decode_audio(audio_path(data_dir, part.storage_key).read_bytes())
    except (OSError, InvalidAudioError) as error:
        raise EnrollmentError('source_changed') from error
    duration = len(waveform) / SAMPLE_RATE
    stored = [turn.model_copy(update={'start': turn.start - part.start_seconds,
                                      'end': turn.end - part.start_seconds})
              for turn in detail.diarization_segments]
    intervals = _exclusive_intervals(stored, speaker, duration)
    seconds = sum(end - start for start, end in intervals)
    if seconds < 10:
        raise EnrollmentError('insufficient_speech')
    result = _infer(waveform, models)
    turns = diarization_turns(result)
    scores = []
    for label in {turn.speaker for turn in turns}:
        candidate = _exclusive_intervals(turns, label, duration)
        overlap = sum(max(0.0, min(end, other_end) - max(start, other_start))
                      for start, end in intervals for other_start, other_end in candidate)
        scores.append((overlap / seconds, label, sum(end - start for start, end in candidate)))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < .8 or (len(scores) > 1 and scores[0][0] - scores[1][0] < .2):
        raise EnrollmentError('source_changed')
    if scores[0][2] < 10:
        raise EnrollmentError('insufficient_speech')
    return _vector(result, scores[0][1])
