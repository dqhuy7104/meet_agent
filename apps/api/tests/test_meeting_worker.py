"""Worker state machine and failure contracts."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.audio_models import ModelLoadError
from app.core.config import Settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.audio.preprocess import AudioTooLongError, InvalidAudioError
from app.services.meetings.repository import MeetingRepository, MeetingStateConflict
from app.services.meetings.storage import publish_audio
from app.services.meetings.worker import exclusive_worker_lock, process_start_ticks, run_once, terminate_process


def _result(text: str = "Xin chào") -> AudioProcessResponse:
    return AudioProcessResponse(
        filename="meeting.wav", duration_seconds=1.0, transcript=text,
        vad_segments=[SpeechSegment(start=0.0, end=1.0)],
        diarization_segments=[SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00")],
        transcript_segments=[TranscriptSegment(start=0.0, end=1.0, speaker="SPEAKER_00", text=text)],
    )


class MeetingWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "meetings"
        initialize_meeting_store(self.data_dir)
        self.connection = open_meeting_db(self.data_dir)
        self.addCleanup(self.connection.close)
        self.repository = MeetingRepository(self.connection)
        self.settings = Settings(meeting_data_dir=self.data_dir)
        self.add_meeting()

    def add_meeting(self, meeting_id: str = "m1") -> None:
        data = b"audio bytes"
        key = publish_audio(self.data_dir, data)
        self.repository.create_meeting(
            meeting_id, "Planning", "meeting.wav", key, hashlib.sha256(data).hexdigest(), None
        )

    def test_claim_is_transactional_across_connections(self) -> None:
        second = open_meeting_db(self.data_dir)
        self.addCleanup(second.close)
        first_claim = self.repository.claim_next()
        self.assertEqual(first_claim.id, "m1")
        self.assertEqual(first_claim.attempt_count, 1)
        self.assertEqual(first_claim.status, "processing")
        self.assertIsNone(MeetingRepository(second).claim_next())

    def test_recovery_requeues_once_then_fails_after_second_claim(self) -> None:
        self.repository.claim_next()
        self.repository.recover_interrupted()
        self.assertEqual(self.repository.get_meeting("m1").status, "queued")
        self.repository.claim_next()
        self.repository.recover_interrupted()
        record = self.repository.get_meeting("m1")
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.failure_code, "interrupted")
        self.assertIsNone(self.repository.claim_next())

    def test_worker_keeps_one_exclusive_lock(self) -> None:
        with exclusive_worker_lock(self.data_dir):
            with self.assertRaises(BlockingIOError):
                with exclusive_worker_lock(self.data_dir):
                    pass

    def test_termination_requires_matching_process_identity(self) -> None:
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        start_ticks = process_start_ticks(child.pid)
        self.assertIsNotNone(start_ticks)

        self.assertFalse(terminate_process(child.pid, start_ticks + 1))
        self.assertIsNone(child.poll())
        self.assertTrue(terminate_process(child.pid, start_ticks))
        child.wait(timeout=2)

    def test_worker_completes_all_result_rows(self) -> None:
        with patch("app.services.meetings.worker.process_audio", return_value=_result()):
            self.assertTrue(run_once(self.repository, self.data_dir, self.settings))
        record = self.repository.get_meeting("m1")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.duration_seconds, 1.0)
        self.assertEqual(record.transcript, "Xin chào")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM vad_segments").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM diarization_segments").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_fts").fetchone()[0], 1)
        self.assertIsNone(self.repository.claim_next())
        self.assertFalse(run_once(self.repository, self.data_dir, self.settings))

    def test_worker_maps_known_and_unknown_failures_to_safe_codes(self) -> None:
        failures = [
            (InvalidAudioError("bad"), "invalid_audio"),
            (AudioTooLongError("Audio must be at most 30 minutes long"), "audio_too_long"),
            (ModelLoadError("vad", "private-model", RuntimeError("secret")), "model_unavailable"),
            (RuntimeError("private path and token"), "processing_error"),
        ]
        for error, code in failures:
            with self.subTest(code=code):
                self.connection.execute("UPDATE meetings SET status='queued', attempt_count=0, failure_code=NULL")
                self.connection.commit()
                with patch("app.services.meetings.worker.process_audio", side_effect=error):
                    self.assertTrue(run_once(self.repository, self.data_dir, self.settings))
                record = self.repository.get_meeting("m1")
                self.assertEqual(record.status, "failed")
                self.assertEqual(record.failure_code, code)
                self.assertNotIn("secret", record.failure_code)
                self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)

    def test_retry_resets_attempts_and_replaces_old_results(self) -> None:
        self.repository.claim_next()
        self.repository.complete("m1", _result("Old words"))
        self.connection.execute("UPDATE meetings SET status='failed', failure_code='processing_error' WHERE id='m1'")
        self.connection.commit()
        previous = self.repository.get_meeting("m1")
        retry = self.repository.retry("m1")
        self.assertEqual(retry.id, previous.id)
        self.assertEqual(retry.storage_key, previous.storage_key)
        self.assertEqual(retry.attempt_count, 0)
        self.assertIsNone(retry.failure_code)
        with patch("app.services.meetings.worker.process_audio", return_value=_result("New words")):
            self.assertTrue(run_once(self.repository, self.data_dir, self.settings))
        texts = [row[0] for row in self.connection.execute("SELECT text FROM transcript_segments")]
        indexed = [row[0] for row in self.connection.execute("SELECT text FROM transcript_fts")]
        self.assertEqual(texts, ["New words"])
        self.assertEqual(indexed, ["New words"])
        with self.assertRaises(MeetingStateConflict):
            self.repository.retry("m1")
        self.assertIsNone(self.repository.retry("missing"))

    def test_retry_serializes_with_another_retry_and_claim(self) -> None:
        self.repository.claim_next()
        self.repository.fail("m1", "processing_error")
        first_read = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        outcomes: list[str] = []

        def first_retry() -> None:
            connection = open_meeting_db(self.data_dir)
            try:
                repository = MeetingRepository(connection)
                original_get = repository.get_meeting

                def paused_get(meeting_id: str):
                    record = original_get(meeting_id)
                    first_read.set()
                    release_first.wait(2)
                    return record

                with patch.object(repository, "get_meeting", paused_get):
                    repository.retry("m1")
            finally:
                connection.close()

        def second_retry_and_claim() -> None:
            connection = open_meeting_db(self.data_dir)
            try:
                repository = MeetingRepository(connection)
                try:
                    repository.retry("m1")
                    repository.claim_next()
                    outcomes.append("claimed")
                except MeetingStateConflict:
                    outcomes.append("conflict")
            finally:
                second_done.set()
                connection.close()

        first = threading.Thread(target=first_retry, daemon=True)
        second = threading.Thread(target=second_retry_and_claim, daemon=True)
        first.start()
        try:
            self.assertTrue(first_read.wait(1))
            second.start()
            self.assertFalse(second_done.wait(0.1))
        finally:
            release_first.set()
            first.join(timeout=2)
            if second.ident is not None:
                second.join(timeout=2)
        self.assertEqual(outcomes, ["conflict"])


class RetryRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_route_returns_404_409_or_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "meetings"
            initialize_meeting_store(data_dir)
            connection = open_meeting_db(data_dir)
            repository = MeetingRepository(connection)
            key = publish_audio(data_dir, b"audio")
            repository.create_meeting("m1", None, "meeting.wav", key, "checksum", None)

            async def settings() -> Settings:
                return Settings(meeting_data_dir=data_dir)

            app.dependency_overrides[get_meeting_settings] = settings
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)), base_url="http://localhost"
                ) as client:
                    self.assertEqual((await client.post("/api/meetings/missing/retry")).status_code, 404)
                    self.assertEqual((await client.post("/api/meetings/m1/retry")).status_code, 409)
                    repository.claim_next()
                    repository.fail("m1", "invalid_audio")
                    response = await client.post("/api/meetings/m1/retry")
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json()["status"], "queued")
            finally:
                app.dependency_overrides.clear()
                connection.close()


if __name__ == "__main__":
    unittest.main()
