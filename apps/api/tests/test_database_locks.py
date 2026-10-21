from app.core.database_config import DatabaseUnavailableError
from app.db.worker_locks import worker_lease
from apps.api.tests.postgres_support import PostgresTestCase


class DatabaseLocksTest(PostgresTestCase):
    def test_roles_are_exclusive_and_released(self):
        with worker_lease(self.database, 'meeting') as first:
            first.ensure_owned()
            with self.assertRaises(DatabaseUnavailableError):
                with worker_lease(self.database, 'meeting'):
                    pass
            with worker_lease(self.database, 'rag') as other:
                other.ensure_owned()
        with worker_lease(self.database, 'meeting') as next_owner:
            next_owner.ensure_owned()

    def test_lost_backend_invalidates_ownership(self):
        with worker_lease(self.database, 'meeting') as lease:
            self.connection.execute('SELECT pg_terminate_backend(%s)', (lease.connection.info.backend_pid,))
            with self.assertRaises(DatabaseUnavailableError):
                lease.ensure_owned()
