import psycopg
import tempfile
import unittest
from pathlib import Path
from apps.api.tests.postgres_support import PostgresTestCase, seed_project
from app.services.meetings.repository import MeetingRepository
from app.schemas.transcript import AudioProcessResponse, TranscriptSegment


def complete_meeting(repository, meeting_id='m', text='Kế hoạch phát hành'):
    account, project = seed_project(repository.connection)
    repository.create_meeting(meeting_id, 'Planning', 'm.wav', meeting_id+'key', 'checksum', None,
                              project_id=project.id, uploaded_by_account_id=account.id)
    repository.claim_next()
    repository.complete(meeting_id, AudioProcessResponse(filename="m.wav", duration_seconds=5, sample_rate=16000, transcript=text,
        vad_segments=[], diarization_segments=[], transcript_segments=[TranscriptSegment(start=1, end=4, speaker='A', text=text)] if text else []))


class OutboxTest(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.repository = MeetingRepository(self.connection)

    def test_complete_and_cancel_enqueue_without_provider(self):
        complete_meeting(self.repository)
        self.assertEqual(self.connection.execute('SELECT kind FROM rag_outbox').fetchone()[0], 'index')
        self.repository.cancel('m')
        self.assertEqual([r[0] for r in self.connection.execute('SELECT kind FROM rag_outbox ORDER BY id')], ['index', 'purge'])
        self.assertEqual(self.repository.get_meeting('m').status, 'cancelled')

    def test_event_failure_rolls_back_completion(self):
        account, project = seed_project(self.connection)
        self.repository.create_meeting('m', None, 'm.wav', 'key', 'hash', None,
                                       project_id=project.id, uploaded_by_account_id=account.id)
        self.repository.claim_next()
        self.connection.execute("ALTER TABLE rag_outbox ADD CHECK (kind <> 'index')")
        with self.assertRaises(psycopg.IntegrityError):
            self.repository.complete('m', AudioProcessResponse(filename="m.wav", duration_seconds=0, sample_rate=16000, transcript='', vad_segments=[], diarization_segments=[], transcript_segments=[]))
        self.assertEqual(self.repository.get_meeting('m').status, 'processing')
