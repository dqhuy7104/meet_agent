"""Meeting persistence and private audio storage contracts."""

from __future__ import annotations

import hashlib
from functools import partial
import psycopg
import stat
import tempfile
import unittest
from pathlib import Path

from app.core.config import ConfigurationError, Settings
from apps.api.tests.postgres_support import PostgresTestCase, seed_project
from app.services.meetings.storage import initialize_meeting_storage
from app.services.meetings.repository import IdempotencyConflict, MeetingRepository
from app.services.meetings.storage import audio_path, cleanup_orphaned_audio, publish_audio
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment


class MeetingStoreTest(PostgresTestCase):
    def setUp(self) -> None:
        super().setUp()
        initialize_meeting_storage(self.data_dir, self.connection)
        self.repository = MeetingRepository(self.connection)
        self.account, self.project = seed_project(self.connection)
        self.create_meeting = partial(self.repository.create_meeting, project_id=self.project.id, uploaded_by_account_id=self.account.id)

    def test_initialization_is_idempotent_and_private(self) -> None:
        initialize_meeting_storage(self.data_dir, self.connection)
        self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.data_dir / "audio").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.data_dir / "tmp").stat().st_mode), 0o700)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)


    def test_publish_uses_opaque_private_name(self) -> None:
        key = publish_audio(self.data_dir, b"private recording")
        self.assertNotIn("meeting", key)
        self.assertEqual(len(key), 32)
        self.assertEqual(audio_path(self.data_dir, key).read_bytes(), b"private recording")
        self.assertEqual(stat.S_IMODE(audio_path(self.data_dir, key).stat().st_mode), 0o600)
        self.assertEqual(list((self.data_dir / "tmp").iterdir()), [])

    def test_cleanup_removes_orphans_but_keeps_referenced_audio(self) -> None:
        kept = publish_audio(self.data_dir, b"kept")
        orphan = publish_audio(self.data_dir, b"orphan")
        self.create_meeting("m1", None, "user/meeting.wav", kept, hashlib.sha256(b"kept").hexdigest(), None)
        (self.data_dir / "tmp" / "abandoned").write_bytes(b"partial")
        cleanup_orphaned_audio(self.data_dir, self.repository)
        self.assertTrue(audio_path(self.data_dir, kept).exists())
        self.assertFalse(audio_path(self.data_dir, orphan).exists())
        self.assertEqual(list((self.data_dir / "tmp").iterdir()), [])

    def test_failed_insert_can_remove_published_audio(self) -> None:
        key = publish_audio(self.data_dir, b"unlinked")
        self.connection.execute("DROP TABLE meetings CASCADE")
        with self.assertRaises(psycopg.errors.UndefinedTable):
            self.create_meeting("m1", None, "original.wav", key, "checksum", None)
        audio_path(self.data_dir, key).unlink()
        self.assertFalse(audio_path(self.data_dir, key).exists())

    def test_duplicate_idempotency_key_is_distinct_conflict(self) -> None:
        first = publish_audio(self.data_dir, b"first")
        second = publish_audio(self.data_dir, b"second")
        self.create_meeting("m1", "first", "a.wav", first, "checksum1", "retry-key")
        with self.assertRaises(IdempotencyConflict):
            self.create_meeting("m2", "second", "b.wav", second, "checksum2", "retry-key")
        self.assertEqual(self.repository.get_meeting("m1").title, "first")
        self.assertIsNone(self.repository.get_meeting("m2"))

    def test_config_rejects_empty_data_dir(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_environment({"MEETING_DATA_DIR": " "})

    def test_cancelled_meeting_retains_metadata_and_clears_results(self) -> None:
        key = publish_audio(self.data_dir, b"audio")
        self.create_meeting("m1", "Planning", "meeting.wav", key, "checksum", None)
        self.repository.claim_next()
        self.repository.complete("m1", AudioProcessResponse(
            filename="meeting.wav", duration_seconds=1.0, transcript="Xin chào",
            vad_segments=[SpeechSegment(start=0.0, end=1.0)],
            diarization_segments=[SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00")],
            transcript_segments=[TranscriptSegment(start=0.0, end=1.0, speaker="SPEAKER_00", text="Xin chào")],
        ))
        self.connection.execute("UPDATE meetings SET status='processing' WHERE id='m1'")
        self.connection.commit()
        self.assertTrue(self.repository.set_pipeline_process("m1", 123, 456))

        cancelled = self.repository.cancel("m1")

        record = self.repository.get_meeting("m1")
        self.assertEqual(cancelled.storage_key, key)
        self.assertEqual(cancelled.pipeline_pid, 123)
        self.assertEqual(cancelled.pipeline_start_ticks, 456)
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(record.pipeline_pid, 123)
        self.assertIsNotNone(record.cancelled_at)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)



if __name__ == "__main__":
    unittest.main()
