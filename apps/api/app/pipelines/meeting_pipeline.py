"""Synchronous VAD, language-routed ASR, and diarization orchestration."""

from __future__ import annotations

import numpy as np
import torch
from silero_vad import get_speech_timestamps

from app.core.audio_models import ModelRegistry
from app.schemas.transcript import AudioProcessResponse, SpeechSegment, TranscriptSegment
from app.services.alignment.align import speaker_for_region
from app.services.audio.preprocess import SAMPLE_RATE, decode_audio
from app.services.diarization.pyannote import diarization_turns


def process_audio(data: bytes, filename: str, models: ModelRegistry) -> AudioProcessResponse:
    """Transcribe each speech region and retain original-audio timestamps.

    Language detection selects PhoWhisper or multilingual Whisper for each VAD
    region. A region with multiple
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
    turns = diarization_turns(diarization({"waveform": audio.unsqueeze(0), "sample_rate": SAMPLE_RATE}))
    language_detector = models.get_language()
    transcripts: list[TranscriptSegment] = []
    for part in timestamps:
        start_sample, end_sample = part["start"], part["end"]
        clip: np.ndarray = waveform[start_sample:end_sample]
        if clip.size == 0:
            continue
        language = language_detector(torch.from_numpy(clip))
        asr_input = {"array": clip, "sampling_rate": SAMPLE_RATE}
        if language == "vi":
            prediction = models.get_asr()(asr_input)
        else:
            prediction = models.get_whisper()(asr_input, generate_kwargs={"task": "transcribe"})
        text = str(prediction["text"]).strip()
        if not text:
            continue
        start, end = start_sample / SAMPLE_RATE, end_sample / SAMPLE_RATE
        transcripts.append(
            TranscriptSegment(start=start, end=end, speaker=speaker_for_region(start, end, turns), text=text)
        )
    return AudioProcessResponse(
        filename=filename,
        duration_seconds=len(waveform) / SAMPLE_RATE,
        transcript=" ".join(segment.text for segment in transcripts),
        vad_segments=vad_segments,
        diarization_segments=turns,
        transcript_segments=transcripts,
    )
