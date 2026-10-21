from pathlib import Path
from unittest.mock import patch
import psycopg

from app.core.database_config import DatabaseUnavailableError
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import initialize_meeting_storage, cleanup_orphaned_audio, publish_audio, audio_path
from app.services.meetings.ingestion import ingest_meeting
from apps.api.tests.postgres_support import PostgresTestCase


class MeetingStorageTest(PostgresTestCase):
    def test_identity_and_cleanup_preserve_referenced_audio(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        initialize_meeting_storage(self.data_dir, self.connection)
        record = ingest_meeting(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key')
        orphan = publish_audio(self.data_dir, b'orphan')
        cleanup_orphaned_audio(self.data_dir, MeetingRepository(self.connection))
        self.assertEqual(audio_path(self.data_dir, record.storage_key).read_bytes(), b'kept')
        self.assertFalse(audio_path(self.data_dir, orphan).exists())
        self.assertEqual(ingest_meeting(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key').id, record.id)

    def test_unmarked_nonempty_or_mismatched_root_is_never_cleaned(self):
        (self.data_dir / 'audio').mkdir(parents=True)
        retained = self.data_dir / 'audio' / 'old'
        retained.write_bytes(b'private')
        with self.assertRaises(DatabaseUnavailableError):
            initialize_meeting_storage(self.data_dir, self.connection)
        self.assertEqual(retained.read_bytes(), b'private')
        retained.unlink()
        initialize_meeting_storage(self.data_dir, self.connection)
        retained.write_bytes(b'private')
        self.connection.execute("UPDATE storage_identity SET instance_id=gen_random_uuid()")
        with self.assertRaises(DatabaseUnavailableError):
            cleanup_orphaned_audio(self.data_dir, MeetingRepository(self.connection))
        self.assertEqual(retained.read_bytes(), b'private')

    def test_database_outage_deletes_nothing(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        key = publish_audio(self.data_dir, b'retained')
        temporary = self.data_dir / 'tmp' / 'retained'
        temporary.write_bytes(b'partial')
        with patch.object(MeetingRepository, 'referenced_audio_keys', side_effect=psycopg.OperationalError('offline')):
            with self.assertRaises(psycopg.OperationalError):
                cleanup_orphaned_audio(self.data_dir, MeetingRepository(self.connection))
        self.assertEqual(audio_path(self.data_dir, key).read_bytes(), b'retained')
        self.assertTrue(temporary.exists())

    def test_lost_commit_acknowledgement_does_not_delete_referenced_audio(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        original = MeetingRepository.create_meeting
        def commit_then_disconnect(repository, *args, **kwargs):
            original(repository, *args, **kwargs)
            raise psycopg.OperationalError('acknowledgement lost')
        with patch.object(MeetingRepository, 'create_meeting', commit_then_disconnect):
            record = ingest_meeting(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key')
        self.assertEqual(record.status, 'queued')
        self.assertEqual(audio_path(self.data_dir, record.storage_key).read_bytes(), b'kept')
