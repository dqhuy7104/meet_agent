from unittest.mock import patch
import psycopg

from app.core.config import Settings
from app.services.meetings import worker
from apps.api.tests.postgres_support import PostgresTestCase


class WorkerStartupTest(PostgresTestCase):
    def test_transient_startup_outage_reconnects_before_claiming(self):
        connect = worker.open_database
        calls = []
        def open_after_outage(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise psycopg.OperationalError('startup outage')
            return connect(*args, **kwargs)
        with patch.object(worker, 'get_settings', return_value=Settings(meeting_data_dir=self.data_dir)), patch.object(worker, 'open_database', side_effect=open_after_outage), patch.object(worker, 'run_once', side_effect=KeyboardInterrupt), patch.object(worker.time, 'sleep'):
            worker.main()
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue((self.data_dir / '.database-identity').exists())
