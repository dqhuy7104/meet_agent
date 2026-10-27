from pathlib import Path
from functools import partial
from unittest.mock import patch
import psycopg

from app.core.database_config import DatabaseUnavailableError
from app.services.meetings.repository import MeetingRepository, IdempotencyConflict
from app.services.meetings.storage import initialize_meeting_storage, cleanup_orphaned_audio, publish_audio, audio_path
from app.services.meetings.ingestion import ingest_meeting
from apps.api.tests.postgres_support import PostgresTestCase, seed_project


class MeetingStorageTest(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.account, self.project = seed_project(self.connection)
        self.ingest = partial(ingest_meeting, project_id=self.project.id, uploaded_by_account_id=self.account.id)

    def test_identity_and_cleanup_preserve_referenced_audio(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        initialize_meeting_storage(self.data_dir, self.connection)
        record = self.ingest(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key')
        orphan = publish_audio(self.data_dir, b'orphan')
        cleanup_orphaned_audio(self.data_dir, MeetingRepository(self.connection))
        self.assertEqual(audio_path(self.data_dir, record.storage_key).read_bytes(), b'kept')
        self.assertFalse(audio_path(self.data_dir, orphan).exists())
        self.assertEqual(self.ingest(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key').id, record.id)

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
            record = self.ingest(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key')
        self.assertEqual(record.status, 'queued')
        self.assertEqual(audio_path(self.data_dir, record.storage_key).read_bytes(), b'kept')

    def test_racing_idempotency_conflict_never_replays_another_project(self):
        record = self.ingest(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key')
        account, project = seed_project(self.connection)
        with patch.object(MeetingRepository, 'find_by_idempotency_key', side_effect=[None, record]), \
             patch.object(MeetingRepository, 'create_meeting', side_effect=IdempotencyConflict('key')):
            with self.assertRaises(IdempotencyConflict):
                ingest_meeting(self.database, self.data_dir, [('a.wav', b'kept')], None, 'key',
                               project_id=project.id, uploaded_by_account_id=account.id)
        self.assertEqual([path.name for path in (self.data_dir / 'audio').iterdir()], [record.storage_key])
