"""Owned PostgreSQL schemas; TEST_DATABASE_URL never falls back to application data."""
from contextlib import ExitStack, contextmanager
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


def enter_postgres(test_case: unittest.TestCase) -> PostgresFixture:
    stack = ExitStack()
    test_case.addCleanup(stack.close)
    return stack.enter_context(isolated_postgres())


class PostgresTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fixture = enter_postgres(self)
        self.database, self.connection = fixture.database, fixture.connection
        self.data_dir, self.schema = fixture.data_dir, fixture.schema

    def open_connection(self) -> psycopg.Connection:
        return psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True)


class PostgresAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fixture = enter_postgres(self)
        self.database, self.connection = fixture.database, fixture.connection
        self.data_dir, self.schema = fixture.data_dir, fixture.schema
        from app.api.routes.meetings import get_meeting_database_settings
        from app.main import app
        app.dependency_overrides[get_meeting_database_settings] = lambda: self.database
        self.addCleanup(app.dependency_overrides.clear)


def seed_snapshot(connection, snapshot):
    """Publish test source data through the real meeting repository."""
    import uuid
    from app.services.meetings.repository import MeetingRepository
    from app.schemas.transcript import AudioProcessResponse, TranscriptSegment
    from app.services.rag.chunking import snapshot_meeting
    repository = MeetingRepository(connection)
    if repository.get_meeting(snapshot.meeting_id) is None:
        repository.create_meeting(snapshot.meeting_id, snapshot.title, 'source.wav', uuid.uuid4().hex, 'hash', None)
    connection.execute("UPDATE meetings SET status='processing',title=%s WHERE id=%s", (snapshot.title, snapshot.meeting_id))
    repository.complete(snapshot.meeting_id, AudioProcessResponse(filename='source.wav', duration_seconds=max((s.end_seconds for s in snapshot.segments), default=0),
        transcript=' '.join(s.text for s in snapshot.segments), vad_segments=[], diarization_segments=[],
        transcript_segments=[TranscriptSegment(start=s.start_seconds,end=s.end_seconds,speaker=s.speaker,text=s.text) for s in snapshot.segments]))
    return snapshot_meeting(repository.get_detail(snapshot.meeting_id))
