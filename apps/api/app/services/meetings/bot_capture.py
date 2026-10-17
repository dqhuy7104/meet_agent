"""Capture Chrome's dedicated PulseAudio output as bounded PCM WAV parts."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


class CaptureUnavailable(RuntimeError):
    """Local capture prerequisites or the recorder failed."""


@dataclass(frozen=True)
class RecordedPart:
    duration_seconds: float
    started_monotonic: float
    ended_monotonic: float


def chrome_executable(settings: Settings) -> str:
    if settings.bot_chrome_path:
        path = settings.bot_chrome_path
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise CaptureUnavailable("chrome_unavailable")
    binary = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if binary is None:
        raise CaptureUnavailable("chrome_unavailable")
    return binary


def check_bot_runtime(settings: Settings) -> None:
    """Require an explicit sink and its monitor; never capture default audio."""
    chrome_executable(settings)
    if not shutil.which("ffmpeg"):
        raise CaptureUnavailable("ffmpeg_unavailable")
    sink = settings.bot_audio_sink
    if not sink:
        raise CaptureUnavailable("audio_sink_unconfigured")
    if not shutil.which("pactl"):
        raise CaptureUnavailable("audio_server_unavailable")
    try:
        sinks = subprocess.run(
            ["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        sources = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise CaptureUnavailable("audio_server_unavailable") from error
    sink_names = {line.split("\t")[1] for line in sinks.splitlines() if "\t" in line}
    source_names = {line.split("\t")[1] for line in sources.splitlines() if "\t" in line}
    if sink not in sink_names or f"{sink}.monitor" not in source_names:
        raise CaptureUnavailable("audio_sink_unavailable")


class PulseWavRecorder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def record_part(
        self, temporary_path: Path, stop: asyncio.Event, max_seconds: int = 1790,
    ) -> RecordedPart:
        sink = self.settings.bot_audio_sink
        if not sink or not 0 < max_seconds < 1800:
            raise CaptureUnavailable("audio_sink_unavailable")
        started = time.monotonic()
        temporary_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse",
            "-i", f"{sink}.monitor", "-ar", "16000", "-ac", "1",
            "-c:a", "pcm_s16le", "-t", str(max_seconds), "-y", str(temporary_path),
        ]
        process: asyncio.subprocess.Process | None = None
        wait_process: asyncio.Task[int] | None = None
        wait_stop: asyncio.Task[bool] | None = None
        completed = False
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            wait_process = asyncio.create_task(process.wait())
            wait_stop = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({wait_process, wait_stop}, return_when=asyncio.FIRST_COMPLETED)
            if wait_stop in done and process.returncode is None:
                if process.stdin:
                    process.stdin.write(b"q\n")
                    await process.stdin.drain()
                try:
                    await asyncio.wait_for(wait_process, timeout=5)
                except asyncio.TimeoutError as error:
                    process.kill()
                    await process.wait()
                    raise CaptureUnavailable("recorder_unresponsive") from error
            if process.returncode != 0:
                raise CaptureUnavailable("recorder_failed")
            with wave.open(str(temporary_path), "rb") as recording:
                if recording.getnchannels() != 1 or recording.getframerate() != 16000:
                    raise CaptureUnavailable("invalid_recording")
                duration = recording.getnframes() / recording.getframerate()
            if not 0 < duration <= 1800:
                raise CaptureUnavailable("invalid_recording")
            completed = True
            return RecordedPart(duration, started, time.monotonic())
        except (OSError, wave.Error) as error:
            raise CaptureUnavailable("recorder_failed") from error
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(wait_process or process.wait()), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if wait_stop is not None:
                wait_stop.cancel()
                await asyncio.gather(wait_stop, return_exceptions=True)
            if wait_process is not None:
                await asyncio.gather(wait_process, return_exceptions=True)
            if not completed:
                temporary_path.unlink(missing_ok=True)
