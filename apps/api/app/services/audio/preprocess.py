"""Decode uploaded audio to the shared 16 kHz mono inference format."""

from __future__ import annotations

import subprocess

import numpy as np


SAMPLE_RATE = 16_000
MAX_DURATION_SECONDS = 30 * 60


class InvalidAudioError(ValueError):
    """The uploaded bytes could not be decoded as supported audio."""


def decode_audio(data: bytes) -> np.ndarray:
    """Decode an audio upload with FFmpeg into mono float32 samples.

    Args:
        data: Uploaded audio bytes in a format supported by FFmpeg.

    Returns:
        A one-dimensional, 16 kHz float32 waveform.

    Raises:
        InvalidAudioError: If the upload is empty, invalid, or too long.
        RuntimeError: If FFmpeg is missing or decoding times out.
    """
    if not data:
        raise InvalidAudioError("Audio file is empty")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0", "-t", str(MAX_DURATION_SECONDS + 1),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ]
    try:
        completed = subprocess.run(command, input=data, capture_output=True, timeout=180, check=False)
    except FileNotFoundError as error:
        raise RuntimeError("FFmpeg is required to decode uploaded audio") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Audio decoding timed out") from error
    if completed.returncode != 0:
        raise InvalidAudioError("Audio file could not be decoded")
    waveform = np.frombuffer(completed.stdout, dtype="<f4")
    if waveform.size == 0:
        raise InvalidAudioError("Audio contains no samples")
    if waveform.size > MAX_DURATION_SECONDS * SAMPLE_RATE:
        raise InvalidAudioError("Audio must be at most 30 minutes long")
    return waveform.copy()
