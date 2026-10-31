"""Local upload-to-search flow with real FFmpeg and fake inference models."""

from __future__ import annotations
from apps.api.tests.postgres_support import PostgresAsyncTestCase

import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.deps import get_models
from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.db.session import open_database
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.audio.preprocess import SAMPLE_RATE
from app.services.meetings.repository import MeetingRepository
from apps.api.tests.worker_support import run_once_inline as run_once
from apps.api.tests.test_audio_pipeline import FakeRegistry


def _wav_bytes() -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(b"\x00\x00" * SAMPLE_RATE)
        return buffer.getvalue()


class MeetingFlowTest(PostgresAsyncTestCase):
    async def test_upload_worker_search_audio_delete_and_legacy_response(self) -> None:
        from apps.api.tests.postgres_support import seed_project
        from app.services.access.sessions import COOKIE_NAME, SessionRepository
        account, project = seed_project(self.connection)
        token, csrf = SessionRepository(self.connection).create(account.id)
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "meetings"
            audio = _wav_bytes()
            models = FakeRegistry()

            async def settings() -> Settings:
                return Settings(meeting_data_dir=data_dir)

            async def fake_models() -> FakeRegistry:
                return models

            app.dependency_overrides[get_meeting_settings] = settings
            app.dependency_overrides[get_models] = fake_models
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)),
                    base_url="http://localhost",
                ) as client:
                    client.cookies.set(COOKIE_NAME, token)
                    client.headers['X-CSRF-Token'] = csrf
                    uploaded = await client.post(
                        "/api/meetings", files={"file": ("meeting.wav", audio, "audio/wav")}, data={'project_id':project.id}
                    )
                    self.assertEqual(uploaded.status_code, 202)
                    meeting_id = uploaded.json()["meeting_id"]
                    self.assertEqual((await client.get(f"/api/meetings/{meeting_id}")).json()["status"], "queued")

                    connection = open_database(self.database)
                    try:
                        repository = MeetingRepository(connection)
                        result = AudioProcessResponse(
                            filename="meeting.wav", duration_seconds=1.0, transcript="Xin chào",
                            vad_segments=[SpeechSegment(start=0.0, end=1.0)],
                            diarization_segments=[SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00")],
                            transcript_segments=[
                                TranscriptSegment(start=0.0, end=1.0, speaker="SPEAKER_00", text="Xin chào")
                            ],
                        )
                        def transcribe(clip, *, return_timestamps):
                            self.assertTrue(return_timestamps)
                            return {"text": "Xin chào"}

                        with patch("app.services.meetings.worker.process_audio", return_value=result), patch(
                            "app.pipelines.meeting_pipeline.get_speech_timestamps",
                            return_value=[{"start": 0, "end": SAMPLE_RATE}],
                        ), patch.object(models, "get_asr", return_value=transcribe):
                            self.assertTrue(run_once(repository, data_dir, Settings(meeting_data_dir=data_dir), self.database))
                            legacy = await client.post(
                                "/api/audio/process", files={"file": ("meeting.wav", audio, "audio/wav")}
                            )
                    finally:
                        connection.close()

                    detail = await client.get(f"/api/meetings/{meeting_id}")
                    self.assertEqual(detail.json()["status"], "completed")
                    self.assertEqual(detail.json()["transcript"], "Xin chào")
                    self.assertEqual(detail.json()["transcript_segments"][0]["start"], 0.0)
                    self.assertEqual(detail.json()["transcript_segments"][0]["end"], 1.0)
                    found = await client.get("/api/meetings/search", params={"q": "Xin chào"})
                    self.assertEqual(found.json()["items"][0]["meeting_id"], meeting_id)
                    self.assertEqual(found.json()["items"][0]["speaker"], "SPEAKER_00")
                    original = await client.get(f"/api/meetings/{meeting_id}/audio")
                    self.assertEqual(original.content, audio)
                    self.assertEqual(legacy.status_code, 200)
                    self.assertEqual(
                        set(legacy.json()),
                        {"filename", "duration_seconds", "transcript", "vad_segments", "diarization_segments", "transcript_segments"},
                    )
                    self.assertEqual((await client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
                    self.assertEqual((await client.get(f"/api/meetings/{meeting_id}")).json()["status"], "cancelled")
            finally:
                app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
