"""Durable bot-session and published-part contracts."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from app.db.session import initialize_meeting_store, open_meeting_db
from app.services.meetings.bot_repository import BotSessionRepository, BotStateConflict
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path, cleanup_orphaned_audio, publish_audio_file


class BotRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        initialize_meeting_store(self.data_dir)
        self.connection = open_meeting_db(self.data_dir)
        self.addCleanup(self.connection.close)
        self.repository = BotSessionRepository(self.connection)

    def test_concurrent_create_allows_one_active_session(self) -> None:
        outcomes: list[str] = []

        def create(suffix: str) -> None:
            connection = open_meeting_db(self.data_dir)
            try:
                try:
                    BotSessionRepository(connection).create(f"https://meet.google.com/{suffix}", None, None)
                    outcomes.append("created")
                except BotStateConflict:
                    outcomes.append("conflict")
            finally:
                connection.close()

        threads = [threading.Thread(target=create, args=(suffix,)) for suffix in ("abc-defg-hij", "xyz-abcd-efg")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["created", "conflict"])
        self.assertIsNotNone(self.repository.get_active())

    def test_reused_key_with_different_link_conflicts(self) -> None:
        first = self.repository.create("https://meet.google.com/abc-defg-hij", None, "key")
        self.assertEqual(self.repository.create(first.url, None, "key").id, first.id)
        with self.assertRaises(BotStateConflict):
            self.repository.create("https://meet.google.com/xyz-abcd-efg", None, "key")

    def test_reused_key_after_finalization_still_rejects_different_link(self) -> None:
        first = self.repository.create("https://meet.google.com/abc-defg-hij", None, "key")
        self.repository.claim_next()
        self.repository.set_status(first.id, "recording")
        self.repository.add_part(first.id, self._part(b"audio"), 0, 1)
        self.repository.finalize(first.id)
        with self.assertRaises(BotStateConflict):
            self.repository.create("https://meet.google.com/xyz-abcd-efg", None, "key")

    def test_finalization_is_idempotent_and_moves_references_atomically(self) -> None:
        session = self.repository.create("https://meet.google.com/abc-defg-hij", "Planning", None)
        self.repository.claim_next()
        self.repository.set_status(session.id, "recording")
        first = self._part(b"first")
        second = self._part(b"second")
        self.repository.add_part(session.id, first, 0, 1)
        self.repository.add_part(session.id, second, 3, 2)
        meeting = self.repository.finalize(session.id)
        self.assertEqual(self.repository.finalize(session.id).id, meeting.id)
        self.assertEqual(MeetingRepository(self.connection).count_meetings(), 1)
        self.assertEqual(meeting.source, "google_meet_bot")
        self.assertEqual([(part.storage_key, part.start_seconds) for part in MeetingRepository(self.connection).get_audio_files(meeting.id)], [(first, 0), (second, 3)])
        self.assertIsNone(self.repository.get(session.id).url)
        self.assertEqual(self.repository.parts(session.id), [])

    def test_partial_finalization_marks_capture_incomplete(self) -> None:
        session = self.repository.create("https://meet.google.com/abc-defg-hij", None, None)
        self.repository.claim_next()
        self.repository.set_status(session.id, "recording")
        self.repository.add_part(session.id, self._part(b"audio"), 0, 1)
        self.repository.set_status(session.id, "interrupted", "browser_closed")
        self.repository.finalize(session.id, partial=True)
        self.assertTrue(self.repository.get(session.id).capture_incomplete)

    def test_cleanup_keeps_registered_session_part_and_removes_unregistered_file(self) -> None:
        session = self.repository.create("https://meet.google.com/abc-defg-hij", None, None)
        self.repository.claim_next()
        self.repository.set_status(session.id, "recording")
        kept = self._part(b"kept")
        orphan = self._part(b"orphan")
        self.repository.add_part(session.id, kept, 0, 1)
        cleanup_orphaned_audio(self.data_dir)
        self.assertTrue(audio_path(self.data_dir, kept).exists())
        self.assertFalse(audio_path(self.data_dir, orphan).exists())
        self.assertEqual(stat.S_IMODE(audio_path(self.data_dir, kept).stat().st_mode), 0o600)

    def test_stop_discard_and_stale_recovery(self) -> None:
        session = self.repository.create("https://meet.google.com/abc-defg-hij", None, None)
        self.repository.claim_next()
        self.assertTrue(self.repository.request_stop(session.id).stop_requested)
        self.repository.set_status(session.id, "recording")
        key = self._part(b"recorded")
        self.repository.add_part(session.id, key, 0, 1)
        self.assertEqual(self.repository.recover_stale(), 1)
        self.assertEqual(self.repository.get(session.id).status, "interrupted")
        self.assertIsNone(self.repository.get(session.id).url)
        self.assertEqual(self.repository.discard(session.id), (key,))
        self.assertEqual(self.repository.get(session.id).status, "cancelled")
        self.assertIsNone(self.repository.get(session.id).url)

    def _part(self, content: bytes) -> str:
        temporary = self.data_dir / "tmp" / f"part-{len(list((self.data_dir / 'audio').iterdir()))}.wav"
        temporary.write_bytes(content)
        return publish_audio_file(self.data_dir, temporary)
