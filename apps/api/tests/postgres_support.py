"""Owned PostgreSQL schemas; TEST_DATABASE_URL never falls back to application data."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Iterator
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from app.core.database_config import DatabaseSettings


@dataclass
class PostgresFixture:
    database: DatabaseSettings
    connection: psycopg.Connection
    data_dir: Path
    schema: str


@contextmanager
def isolated_postgres(*, migrate: bool = True) -> Iterator[PostgresFixture]:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise unittest.SkipTest("TEST_DATABASE_URL unavailable: PostgreSQL not verified")
    schema = "meeting_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-c search_path={schema},public")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with psycopg.connect(scoped, autocommit=True) as connection:
                    if migrate:
                        from app.db.migrate import migrate_database
                        migrate_database(connection)
                    from app.core.database_config import get_database_settings
                    get_database_settings.cache_clear()
                    try:
                        with patch.dict(os.environ, {"DATABASE_URL": scoped}):
                            yield PostgresFixture(DatabaseSettings(SecretStr(scoped)), connection,
                                                  Path(temporary) / "data", schema)
                    finally:
                        get_database_settings.cache_clear()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class PostgresTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fixture = self.enterContext(isolated_postgres())
        self.database, self.connection = fixture.database, fixture.connection
        self.data_dir, self.schema = fixture.data_dir, fixture.schema

    def open_connection(self) -> psycopg.Connection:
        return psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True)


class PostgresAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fixture = self.enterContext(isolated_postgres())
        self.database, self.connection = fixture.database, fixture.connection
        self.data_dir, self.schema = fixture.data_dir, fixture.schema
        from app.api.routes.meetings import get_meeting_database_settings
        from app.main import app
        app.dependency_overrides[get_meeting_database_settings] = lambda: self.database
        self.addCleanup(app.dependency_overrides.clear)
