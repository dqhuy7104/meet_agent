"""HTTP and pipeline regression tests with inference models replaced by fakes."""

from __future__ import annotations

import io
import unittest
import wave
from unittest.mock import patch

import httpx
from pyannote.core import Annotation, Segment

from app.api.deps import get_models
from app.core.audio_models import ModelLoadError
from app.main import app
from app.pipelines.meeting_pipeline import process_audio
from app.services.audio.preprocess import SAMPLE_RATE


def _wav_bytes(seconds: float = 1.0) -> bytes:
    """Create a valid, silent PCM WAV used only as an upload fixture."""
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(b"\x00\x00" * int(seconds * SAMPLE_RATE))
        return buffer.getvalue()


class FakeRegistry:
    """Record which lazy models are requested by the pipeline."""

    def __init__(self) -> None:
        """Create one reusable fake model for each stage."""
        self.calls: list[str] = []
        annotation = Annotation(uri="upload")
        annotation[Segment(0.0, 1.0), "turn-1"] = "SPEAKER_00"
        self.annotation = annotation

    def get_vad(self) -> object:
        """Return a dummy VAD model."""
        self.calls.append("vad")
        return object()

    def get_diarization(self) -> "FakeRegistry":
        """Return this fake as the pyannote callable."""
        self.calls.append("diarization")
        return self

    def __call__(self, audio: object) -> Annotation:
        """Return a known speaker turn."""
        return self.annotation

    def get_asr(self) -> "FakeRegistry":
        """Return this fake as the ASR callable."""
        self.calls.append("asr")
        return self


class AudioApiTest(unittest.IsolatedAsyncioTestCase):
    """Verify upload validation and response alignment without real inference."""

    def setUp(self) -> None:
        """Override the shared registry dependency with a fake."""
        self.models = FakeRegistry()
        async def fake_models() -> FakeRegistry:
            return self.models

        app.dependency_overrides[get_models] = fake_models
        async def run_without_worker(data: bytes, filename: str, models: FakeRegistry) -> object:
            return process_audio(data, filename, models)  # type: ignore[arg-type]

        self.pipeline_patch = patch("app.api.routes.audio._run_pipeline", side_effect=run_without_worker)
        self.pipeline_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        """Restore the original dependency map."""
        app.dependency_overrides.clear()
        await self.client.aclose()
        self.pipeline_patch.stop()

    async def test_process_audio_returns_transcript_and_speaker(self) -> None:
        """VAD timings and pyannote speaker labels reach the HTTP response."""
        with patch(
            "app.pipelines.meeting_pipeline.get_speech_timestamps",
            return_value=[{"start": 0, "end": SAMPLE_RATE}],
        ), patch.object(self.models, "get_asr", return_value=lambda audio: {"text": "Xin chào"}) as asr:
            response = await self.client.post(
                "/api/audio/process",
                files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["transcript"], "Xin chào")
        self.assertEqual(body["vad_segments"], [{"start": 0.0, "end": 1.0}])
        self.assertEqual(body["transcript_segments"][0]["speaker"], "SPEAKER_00")
        self.assertEqual(body["diarization_segments"][0]["speaker"], "SPEAKER_00")
        self.assertEqual(self.models.calls, ["vad", "diarization"])
        asr.assert_called_once()

    async def test_silence_skips_asr_and_diarization(self) -> None:
        """A no-speech VAD result should avoid loading the larger models."""
        with patch("app.pipelines.meeting_pipeline.get_speech_timestamps", return_value=[]):
            response = await self.client.post(
                "/api/audio/process",
                files={"file": ("silent.wav", _wav_bytes(), "audio/wav")},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "")
        self.assertEqual(self.models.calls, ["vad"])

    async def test_invalid_audio_returns_400_without_loading_models(self) -> None:
        """Invalid bytes fail during preprocessing before any model is used."""
        response = await self.client.post(
            "/api/audio/process",
            files={"file": ("invalid.wav", b"not audio", "audio/wav")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.models.calls, [])

    async def test_model_load_failure_returns_503(self) -> None:
        """A failed lazy model load produces an actionable service status."""
        with patch.object(
            self.models,
            "get_vad",
            side_effect=ModelLoadError("vad", "silero-vad", RuntimeError("unavailable")),
        ):
            response = await self.client.post(
                "/api/audio/process",
                files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "vad model unavailable")


if __name__ == "__main__":
    unittest.main()
