"""Database-wide supervisor ownership, separate from work transactions."""
from contextlib import contextmanager
from typing import Iterator

import psycopg
from app.core.database_config import DatabaseSettings, DatabaseUnavailableError
from app.db.session import open_database, validate_database

ROLE_KEYS = {'meeting': 1, 'bot': 2, 'rag': 3}
LOCK_NAMESPACE = 0x4D454554


class WorkerLease:
    def __init__(self, connection: psycopg.Connection, role: str) -> None:
        self.connection = connection
        self.role = role

    def ensure_owned(self) -> None:
        try:
            self.connection.execute('SELECT 1')
        except psycopg.Error:
            raise DatabaseUnavailableError('worker_ownership_lost') from None

    def close(self) -> None:
        self.connection.close()


@contextmanager
def worker_lease(settings: DatabaseSettings, role: str) -> Iterator[WorkerLease]:
    with open_database(settings) as connection:
        validate_database(connection)
        acquired = connection.execute('SELECT pg_try_advisory_lock(%s,%s)', (LOCK_NAMESPACE, ROLE_KEYS[role])).fetchone()[0]
        if not acquired:
            raise DatabaseUnavailableError('worker_already_running')
        yield WorkerLease(connection, role)
