"""Dedicated local audio capture behavior."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.services.meetings.bot_capture import CaptureUnavailable, PulseWavRecorder, check_bot_runtime


class BotCaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_rejects_missing_dedicated_sink_without_default_fallback(self) -> None:
        with patch("app.services.meetings.bot_capture.shutil.which", return_value="/usr/bin/google-chrome"):
            with self.assertRaises(CaptureUnavailable):
                check_bot_runtime(Settings())

    async def test_recorder_invokes_ffmpeg_for_pulse_monitor_and_pcm_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "part.wav"
            command: list[str] = []
            actual_spawn = asyncio.create_subprocess_exec

            async def spawn(*args: str, **kwargs):
                command.extend(args)
                return await actual_spawn(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-t", "30", "-y", str(output),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )

            stop = asyncio.Event()
            async def stop_soon() -> None:
                await asyncio.sleep(0.4)
                stop.set()

            with patch("app.services.meetings.bot_capture.asyncio.create_subprocess_exec", side_effect=spawn):
                stopper = asyncio.create_task(stop_soon())
                result = await PulseWavRecorder(Settings(bot_audio_sink="meet_bot")).record_part(output, stop)
                await stopper
            self.assertIn("meet_bot.monitor", command)
            self.assertEqual(command[command.index("-ar") + 1], "16000")
            self.assertEqual(command[command.index("-ac") + 1], "1")
            self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
            self.assertEqual(command[command.index("-t") + 1], "1790")
            self.assertGreater(result.duration_seconds, 0)
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getframerate(), 16000)
                self.assertGreater(recording.getnframes(), 0)

    async def test_forced_kill_discards_unfinished_file(self) -> None:
        class Input:
            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

        class HungProcess:
            def __init__(self) -> None:
                self.stdin = Input()
                self.returncode = None
                self.finished = asyncio.Event()

            async def wait(self) -> int:
                await self.finished.wait()
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9
                self.finished.set()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "part.wav"
            output.write_bytes(b"unfinished")
            stop = asyncio.Event()
            stop.set()
            with patch("app.services.meetings.bot_capture.asyncio.create_subprocess_exec", return_value=HungProcess()):
                with patch("app.services.meetings.bot_capture.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                    with self.assertRaises(CaptureUnavailable):
                        await PulseWavRecorder(Settings(bot_audio_sink="meet_bot")).record_part(output, stop)
            self.assertFalse(output.exists())
