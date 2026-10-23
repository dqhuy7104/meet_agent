"""Enrollment queue lifecycle with isolated PostgreSQL and fake inference."""
import threading
from unittest.mock import patch

from psycopg.pq import TransactionStatus

from app.core.config import Settings
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path, cleanup_orphaned_audio, initialize_meeting_storage
from app.services.voice.repository import VoiceRepository
from apps.api.tests.postgres_support import PostgresTestCase
from apps.api.tests.test_meeting_worker import _result


class VoiceJobsTest(PostgresTestCase):
    def setUp(self):
        super().setUp()
        initialize_meeting_storage(self.data_dir, self.connection)
        self.voices = VoiceRepository(self.connection)
        self.profile = self.voices.create_profile('Participant')
        self.meetings = MeetingRepository(self.connection)

    def upload(self):
        from app.services.voice.storage import ingest_enrollment_upload
        return ingest_enrollment_upload(self.database, self.data_dir, self.profile.id, b'private audio')

    def test_claim_and_recover_twice(self):
        job = self.upload()
        self.assertEqual(self.voices.claim_next_job().attempt_count, 1)
        self.voices.recover_interrupted_jobs()
        self.assertEqual(self.voices.get_job(job.id).status, 'queued')
        self.assertEqual(self.voices.claim_next_job().attempt_count, 2)
        self.voices.recover_interrupted_jobs()
        job = self.voices.get_job(job.id)
        self.assertEqual((job.status, job.failure_code), ('failed', 'interrupted'))
        cleanup_orphaned_audio(self.data_dir, self.meetings)
        self.assertFalse(audio_path(self.data_dir, job.storage_key).exists())

    def test_failed_job_keeps_five_samples(self):
        for _ in range(5):
            self.voices.add_sample(self.profile.id, [1., 0.], 'fingerprint', 'upload')
        before = self.voices.list_samples(self.profile.id)
        job = self.upload()
        self.voices.claim_next_job()
        self.voices.fail_job(job.id, 'private/path: secret error')
        self.assertEqual(self.voices.list_samples(self.profile.id), before)
        self.assertEqual(self.voices.get_job(job.id).failure_code, 'processing_error')

    def test_pending_upload_is_referenced_during_orphan_cleanup(self):
        job = self.upload()
        cleanup_orphaned_audio(self.data_dir, self.meetings)
        self.assertEqual(audio_path(self.data_dir, job.storage_key).read_bytes(), b'private audio')
        self.assertEqual(audio_path(self.data_dir, job.storage_key).stat().st_mode & 0o777, 0o600)

    def test_profile_deletion_during_processing_prevents_publish(self):
        job = self.upload()
        self.voices.claim_next_job()
        self.voices.delete_profile(self.profile.id)
        self.assertFalse(self.voices.complete_job(job.id, [1., 0.], 'fingerprint'))
        self.assertEqual(self.connection.execute('SELECT count(*) FROM voice_samples').fetchone()[0], 0)

    def test_source_cancelled_during_processing_prevents_publish(self):
        self.meetings.create_meeting('source', None, 'a.wav', 'a'*32, 'hash', None)
        self.meetings.claim_next()
        self.meetings.complete('source', _result())
        job = self.voices.enqueue_from_meeting(self.profile.id, 'source', 'SPEAKER_00')
        self.voices.claim_next_job()
        self.meetings.cancel('source')
        self.assertFalse(self.voices.complete_job(job.id, [1., 0.], 'fingerprint'))
        self.assertEqual(self.voices.list_samples(self.profile.id), [])

    def test_child_removes_upload_after_success_and_safe_failure(self):
        from app.services.meetings import worker
        for should_fail in (False, True):
            with self.subTest(failure=should_fail):
                job = self.upload()
                self.voices.claim_next_job()
                started, owned = threading.Event(), threading.Event()
                started.set()
                owned.set()
                real_open = worker.open_database
                connections = []
                def connect(database):
                    connection = real_open(database)
                    connections.append(connection)
                    return connection
                def extract(data, models):
                    self.assertEqual(connections[0].info.transaction_status, TransactionStatus.IDLE)
                    self.assertEqual(data, b'private audio')
                    if should_fail:
                        raise RuntimeError('/private/path secret')
                    return [1., 0.]
                with patch.object(worker, 'open_database', side_effect=connect), patch.object(worker, 'extract_upload_embedding', side_effect=extract), patch.object(worker, 'pipeline_fingerprint', return_value='fingerprint'):
                    worker.run_claimed_enrollment(job.id, self.data_dir, Settings(), self.database, started, owned)
                stored = self.voices.get_job(job.id)
                self.assertEqual(stored.status, 'failed' if should_fail else 'completed')
                self.assertEqual(stored.failure_code, 'processing_error' if should_fail else None)
                self.assertFalse(audio_path(self.data_dir, job.storage_key).exists())

    def test_profile_and_source_deletion_preserve_failed_job_status(self):
        job = self.upload()
        self.voices.delete_profile(self.profile.id)
        stored = self.voices.get_job(job.id)
        self.assertIsNotNone(stored)
        self.assertEqual((stored.profile_id, stored.status, stored.failure_code), (None, 'failed', 'source_changed'))
        profile = self.voices.create_profile('Another')
        self.meetings.create_meeting('source', None, 'a.wav', 'a'*32, 'hash', None)
        self.meetings.claim_next()
        self.meetings.complete('source', _result())
        job = self.voices.enqueue_from_meeting(profile.id, 'source', 'SPEAKER_00')
        self.meetings.delete('source')
        stored = self.voices.get_job(job.id)
        self.assertEqual((stored.source_meeting_id, stored.status, stored.failure_code), (None, 'failed', 'source_changed'))

    def test_lost_commit_acknowledgement_preserves_queued_upload(self):
        import psycopg
        original = VoiceRepository.enqueue_upload
        def commit_then_disconnect(repository, *args):
            original(repository, *args)
            raise psycopg.OperationalError('acknowledgement lost')
        with patch.object(VoiceRepository, 'enqueue_upload', commit_then_disconnect):
            job = self.upload()
        self.assertEqual(job.status, 'queued')
        self.assertTrue(audio_path(self.data_dir, job.storage_key).exists())

    def test_cancellation_removes_existing_source_samples(self):
        self.meetings.create_meeting('source', None, 'a.wav', 'a'*32, 'hash', None)
        self.meetings.claim_next()
        self.meetings.complete('source', _result())
        job = self.voices.enqueue_from_meeting(self.profile.id, 'source', 'SPEAKER_00')
        self.voices.claim_next_job()
        self.assertTrue(self.voices.complete_job(job.id, [1., 0.], 'fingerprint'))
        self.assertFalse(self.voices.complete_job(job.id, [1., 0.], 'fingerprint'))
        self.assertEqual(len(self.voices.list_samples(self.profile.id)), 1)
        self.meetings.cancel('source')
        self.assertEqual(self.voices.list_samples(self.profile.id), [])

    def test_source_inference_has_no_transaction_and_lost_ownership_does_not_publish(self):
        from app.services.meetings import worker
        self.meetings.create_meeting('source', None, 'a.wav', 'a'*32, 'hash', None)
        self.meetings.claim_next()
        self.meetings.complete('source', _result())
        job = self.voices.enqueue_from_meeting(self.profile.id, 'source', 'SPEAKER_00')
        self.voices.claim_next_job()
        started, owned = threading.Event(), threading.Event()
        started.set()
        owned.set()
        connections = []
        real_open = worker.open_database
        def connect(database):
            connection = real_open(database)
            connections.append(connection)
            return connection
        def extract(detail, speaker, data_dir, models):
            self.assertEqual(connections[0].info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual((detail.meeting.id, speaker), ('source', 'SPEAKER_00'))
            owned.clear()
            return [1., 0.]
        with patch.object(worker, 'open_database', side_effect=connect), patch.object(worker, 'extract_meeting_embedding', side_effect=extract), patch.object(worker, 'pipeline_fingerprint', return_value='fingerprint'):
            worker.run_claimed_enrollment(job.id, self.data_dir, Settings(), self.database, started, owned)
        self.assertEqual(self.voices.get_job(job.id).status, 'processing')
        self.assertEqual(self.voices.list_samples(self.profile.id), [])
