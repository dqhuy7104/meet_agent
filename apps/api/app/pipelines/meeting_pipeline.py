"""Synchronous VAD, PhoWhisper, and diarization inference orchestration."""

from __future__ import annotations

from typing import Any

import numpy as np
import torch
from silero_vad import get_speech_timestamps

from app.core.audio_models import ModelRegistry
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.audio.preprocess import SAMPLE_RATE, decode_audio


def _overlap(start: float, end: float, turn: SpeakerTurn) -> float:
    """Return the number of seconds shared with a speaker turn."""
    return max(0.0, min(end, turn.end) - max(start, turn.start))


def _speaker_for(start: float, end: float, turns: list[SpeakerTurn]) -> str:
    """Assign the speaker with greatest temporal overlap, if any."""
    if not turns:
        return "unknown"
    best = max(turns, key=lambda turn: _overlap(start, end, turn))
    return best.speaker if _overlap(start, end, best) > 0 else "unknown"


def _diarization_turns(result: Any) -> list[SpeakerTurn]:
    """Convert either pyannote 4 output or a legacy annotation to API turns."""
    annotation = getattr(result, "speaker_diarization", result)
    return sorted(
        [
            SpeakerTurn(start=float(segment.start), end=float(segment.end), speaker=str(speaker))
            for segment, _, speaker in annotation.itertracks(yield_label=True)
        ],
        key=lambda turn: (turn.start, turn.end, turn.speaker),
    )


def process_audio(data: bytes, filename: str, models: ModelRegistry) -> AudioProcessResponse:
    """Run the three inference stages and retain original-audio timestamps.

    PhoWhisper transcribes each VAD region independently. A region with multiple
    diarized speakers receives the speaker with the most overlap; the complete
    diarization turns remain available in the response.
    """
    waveform = decode_audio(data)
    audio = torch.from_numpy(waveform)
    timestamps = get_speech_timestamps(audio, models.get_vad(), sampling_rate=SAMPLE_RATE)
    vad_segments = [
        SpeechSegment(start=part["start"] / SAMPLE_RATE, end=part["end"] / SAMPLE_RATE)
        for part in timestamps
    ]
    if not vad_segments:
        return AudioProcessResponse(
            filename=filename,
            duration_seconds=len(waveform) / SAMPLE_RATE,
            transcript="",
            vad_segments=[],
            diarization_segments=[],
            transcript_segments=[],
        )

    diarization = models.get_diarization()
    turns = _diarization_turns(diarization({"waveform": audio.unsqueeze(0), "sample_rate": SAMPLE_RATE}))
    asr = models.get_asr()
    transcripts: list[TranscriptSegment] = []
    for part in timestamps:
        start_sample, end_sample = part["start"], part["end"]
        clip: np.ndarray = waveform[start_sample:end_sample]
        if clip.size == 0:
            continue
        prediction = asr({"array": clip, "sampling_rate": SAMPLE_RATE})
        text = str(prediction["text"]).strip()
        if not text:
            continue
        start, end = start_sample / SAMPLE_RATE, end_sample / SAMPLE_RATE
        transcripts.append(
            TranscriptSegment(start=start, end=end, speaker=_speaker_for(start, end, turns), text=text)
        )
    return AudioProcessResponse(
        filename=filename,
        duration_seconds=len(waveform) / SAMPLE_RATE,
        transcript=" ".join(segment.text for segment in transcripts),
        vad_segments=vad_segments,
        diarization_segments=turns,
        transcript_segments=transcripts,
    )
