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
