import multiprocessing
import threading
import time
from unittest.mock import patch

from app.core.config import Settings
from app.core.database_config import DatabaseUnavailableError
from app.db.worker_locks import worker_lease
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import initialize_meeting_storage
from app.services.meetings.worker import run_once, process_start_ticks
from apps.api.tests.postgres_support import PostgresTestCase
from apps.api.tests.test_meeting_concurrency import result


def complete_without_models(meeting_id, data_dir, settings, database, start_event, ownership_event):
    from app.db.session import open_database
    start_event.wait()
    with open_database(database) as connection:
        if ownership_event.is_set():
            MeetingRepository(connection).complete(meeting_id, result())


class WorkerSupervisionTest(PostgresTestCase):
    def test_spawned_child_uses_its_own_schema_connection(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        repository = MeetingRepository(self.connection)
        repository.create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
        context = multiprocessing.get_context('spawn')
        make_process = context.Process
        def process(**kwargs):
            kwargs['target'] = complete_without_models
            return make_process(**kwargs)
        with worker_lease(self.database, 'meeting') as lease, patch.object(context, 'Process', process):
            self.assertTrue(run_once(repository, self.data_dir, Settings(), self.database, lease))
        self.assertEqual(repository.get_meeting('m').status, 'completed')

    def test_lost_lease_reaps_child_before_new_ownership(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        repository = MeetingRepository(self.connection)
        repository.create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
        with worker_lease(self.database, 'meeting') as lease:
            def disconnect():
                with self.open_connection() as connection:
                    for _ in range(100):
                        pid = connection.execute("SELECT pipeline_pid FROM meetings WHERE id='m'").fetchone()[0]
                        if pid:
                            connection.execute('SELECT pg_terminate_backend(%s)', (lease.connection.info.backend_pid,))
                            return
                        time.sleep(.02)
            killer = threading.Thread(target=disconnect)
            killer.start()
            try:
                with self.assertRaises(DatabaseUnavailableError):
                    run_once(repository, self.data_dir, Settings(), self.database, lease)
            finally:
                killer.join(3)
        pid = repository.get_meeting('m').pipeline_pid
        self.assertIsNone(process_start_ticks(pid))
        with worker_lease(self.database, 'meeting') as new_owner:
            new_owner.ensure_owned()


def completion_connection_failure(meeting_id, data_dir, settings, database, start_event, ownership_event):
    from app.services.meetings import worker
    real_connect = worker.open_database
    connections = []
    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection
    def inference(*args, **kwargs):
        with real_connect(database) as admin:
            admin.execute('SELECT pg_terminate_backend(%s)', (connections[0].info.backend_pid,))
        return result(), [2.0]
    with patch.object(worker, 'open_database', side_effect=connect), patch.object(worker, 'process_meeting_parts', side_effect=inference):
        worker.run_claimed_meeting(meeting_id, data_dir, settings, database, start_event, ownership_event)


class ChildConnectionFailureTest(PostgresTestCase):
    def test_failed_child_connection_does_not_strand_processing(self):
        initialize_meeting_storage(self.data_dir, self.connection)
        repository = MeetingRepository(self.connection)
        repository.create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
        context = multiprocessing.get_context('spawn')
        make_process = context.Process
        def process(**kwargs):
            kwargs['target'] = completion_connection_failure
            return make_process(**kwargs)
        with worker_lease(self.database, 'meeting') as lease, patch.object(context, 'Process', process):
            run_once(repository, self.data_dir, Settings(), self.database, lease)
        self.assertEqual(repository.get_meeting('m').status, 'failed')
        self.assertEqual(repository.retry('m').status, 'queued')


def complete_either_queue(job_id, data_dir, settings, database, start_event, ownership_event):
    from app.db.session import open_database
    from app.services.voice.repository import VoiceRepository
    start_event.wait()
    with open_database(database) as connection:
        if ownership_event.is_set():
            voices = VoiceRepository(connection)
            if voices.get_job(job_id):
                voices.complete_job(job_id, [1., 0.], 'fingerprint')
            else:
                MeetingRepository(connection).complete(job_id, result())


class QueueSelectionTest(PostgresTestCase):
    def test_alternating_selector_runs_enrollment_between_meetings(self):
        from app.services.voice.repository import VoiceRepository
        from app.services.voice.storage import ingest_enrollment_upload
        initialize_meeting_storage(self.data_dir, self.connection)
        meetings = MeetingRepository(self.connection)
        voices = VoiceRepository(self.connection)
        profile = voices.create_profile('Participant')
        job = ingest_enrollment_upload(self.database, self.data_dir, profile.id, b'audio')
        for name in ('first', 'second'):
            meetings.create_meeting(name, None, 'a.wav', name[0]*32, 'hash', None)
        context = multiprocessing.get_context('spawn')
        make_process = context.Process
        def process(**kwargs):
            kwargs['target'] = complete_either_queue
            return make_process(**kwargs)
        with worker_lease(self.database, 'meeting') as lease, patch.object(context, 'Process', process):
            for preference, expected in ((False, ('completed', 'queued', 'queued')), (True, ('completed', 'completed', 'queued')), (False, ('completed', 'completed', 'completed'))):
                self.assertTrue(run_once(meetings, self.data_dir, Settings(), self.database, lease, prefer_enrollment=preference))
                self.assertEqual((meetings.get_meeting('first').status, voices.get_job(job.id).status, meetings.get_meeting('second').status), expected)

    def test_enrollment_lost_lease_reaps_child_and_allows_recovery(self):
        from app.services.voice.repository import VoiceRepository
        from app.services.voice.storage import ingest_enrollment_upload
        voices = VoiceRepository(self.connection)
        profile = voices.create_profile('Participant')
        job = ingest_enrollment_upload(self.database, self.data_dir, profile.id, b'audio')
        meetings = MeetingRepository(self.connection)
        context = multiprocessing.get_context('spawn')
        make_process = context.Process
        def process(**kwargs):
            kwargs['target'] = wait_for_termination
            return make_process(**kwargs)
        with worker_lease(self.database, 'meeting') as lease, patch.object(context, 'Process', process):
            def disconnect():
                with self.open_connection() as connection:
                    for _ in range(100):
                        pid = connection.execute('SELECT pipeline_pid FROM voice_enrollment_jobs WHERE id=%s', (job.id,)).fetchone()[0]
                        if pid:
                            connection.execute('SELECT pg_terminate_backend(%s)', (lease.connection.info.backend_pid,))
                            return
                        time.sleep(.02)
            killer = threading.Thread(target=disconnect)
            killer.start()
            try:
                with self.assertRaises(DatabaseUnavailableError):
                    run_once(meetings, self.data_dir, Settings(), self.database, lease)
            finally:
                killer.join(3)
        pid = self.connection.execute('SELECT pipeline_pid FROM voice_enrollment_jobs WHERE id=%s', (job.id,)).fetchone()[0]
        self.assertIsNone(process_start_ticks(pid))
        voices.recover_interrupted_jobs()
        self.assertEqual(voices.get_job(job.id).status, 'queued')


def wait_for_termination(job_id, data_dir, settings, database, start_event, ownership_event):
    start_event.wait()
    time.sleep(20)
