import sqlite3
import tempfile
import unittest
from pathlib import Path
from app.db.session import initialize_meeting_store, open_meeting_db
from app.services.meetings.repository import MeetingRepository
from app.schemas.transcript import AudioProcessResponse, TranscriptSegment


def complete_meeting(repository, meeting_id='m', text='Kế hoạch phát hành'):
    repository.create_meeting(meeting_id, 'Planning', 'm.wav', meeting_id+'key', 'checksum', None)
    repository.claim_next()
    repository.complete(meeting_id, AudioProcessResponse(filename="m.wav", duration_seconds=5, sample_rate=16000, transcript=text,
        vad_segments=[], diarization_segments=[], transcript_segments=[TranscriptSegment(start=1, end=4, speaker='A', text=text)] if text else []))


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        initialize_meeting_store(Path(self.temp.name))
        self.connection = open_meeting_db(Path(self.temp.name))
        self.addCleanup(self.connection.close)
        self.repository = MeetingRepository(self.connection)

    def test_complete_and_cancel_enqueue_without_postgres(self):
        complete_meeting(self.repository)
        self.assertEqual(self.connection.execute('SELECT kind FROM rag_outbox').fetchone()[0], 'index')
        self.repository.cancel('m')
        self.assertEqual([r[0] for r in self.connection.execute('SELECT kind FROM rag_outbox ORDER BY id')], ['index', 'purge'])
        self.assertEqual(self.repository.get_meeting('m').status, 'cancelled')

    def test_event_failure_rolls_back_completion(self):
        self.repository.create_meeting('m', None, 'm.wav', 'key', 'hash', None)
        self.repository.claim_next()
        self.connection.execute("CREATE TRIGGER reject_rag BEFORE INSERT ON rag_outbox BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.complete('m', AudioProcessResponse(filename="m.wav", duration_seconds=0, sample_rate=16000, transcript='', vad_segments=[], diarization_segments=[], transcript_segments=[]))
        self.assertEqual(self.repository.get_meeting('m').status, 'processing')
