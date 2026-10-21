import asyncio
from unittest.mock import patch
import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from app.core.config import Settings
from app.core.database_config import DatabaseSettings, DatabaseUnavailableError
from app.db.migrate import migrate_database
from app.db.session import open_database, validate_database
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import initialize_meeting_storage
from apps.api.tests.postgres_support import PostgresTestCase, PostgresAsyncTestCase, isolated_postgres


class ReviewDatabaseTest(PostgresTestCase):
    def test_fixture_does_not_require_python_311_enter_context(self):
        import unittest
        probe = PostgresTestCase()
        self.addCleanup(probe.doCleanups)
        with patch.object(unittest.TestCase, 'enterContext', None):
            probe.setUp()
            self.assertEqual(probe.connection.execute('SELECT current_schema()').fetchone()[0], probe.schema)

    def test_meeting_cleanup_preserves_bot_capture(self):
        from app.services.meetings.storage import cleanup_orphaned_audio
        initialize_meeting_storage(self.data_dir, self.connection)
        active = self.data_dir / 'bot-tmp' / 'active.wav'
        active.write_bytes(b'active recording')
        cleanup_orphaned_audio(self.data_dir, MeetingRepository(self.connection))
        self.assertEqual(active.read_bytes(), b'active recording')

    def test_valid_dsn_with_connect_timeout_connects(self):
        settings = DatabaseSettings(SecretStr(make_conninfo(self.database.database_url.get_secret_value(), connect_timeout=1)))
        with open_database(settings) as connection:
            self.assertEqual(connection.execute('SELECT current_schema()').fetchone()[0], self.schema)

    def test_process_start_ticks_preserve_64_bit_identity(self):
        repository = MeetingRepository(self.connection)
        repository.create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
        repository.claim_next()
        self.assertTrue(repository.set_pipeline_process('m', 123, 2**33))
        self.assertEqual(repository.get_meeting('m').pipeline_start_ticks, 2**33)

    def test_validation_does_not_accept_fallback_schema_ledger(self):
        with isolated_postgres(migrate=False) as fresh:
            settings = DatabaseSettings(SecretStr(make_conninfo(fresh.database.database_url.get_secret_value(),
                options=f'-c search_path={fresh.schema},{self.schema},public')))
            with open_database(settings) as connection:
                with self.assertRaises(DatabaseUnavailableError):
                    validate_database(connection)

    def test_migration_creates_local_tables_without_writing_fallback(self):
        with isolated_postgres(migrate=False) as fresh:
            settings = DatabaseSettings(SecretStr(make_conninfo(fresh.database.database_url.get_secret_value(),
                options=f'-c search_path={fresh.schema},{self.schema},public')))
            with open_database(settings) as connection:
                migrate_database(connection)
                local_count = connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='meetings'", (fresh.schema,)).fetchone()[0]
                self.assertEqual(local_count, 1)
                MeetingRepository(connection).create_meeting('local', None, 'a.wav', 'b'*32, 'hash', None)
                self.assertEqual(MeetingRepository(self.connection).count_meetings(), 0)


class StopAfterStartup(Exception):
    pass


class ReviewBotStartupTest(PostgresAsyncTestCase):
    async def test_runner_reaches_claim_with_validated_storage(self):
        from app.services.meetings import bot_runner
        with patch('app.services.meetings.bot_repository.BotSessionRepository.claim_next', side_effect=StopAfterStartup):
            with self.assertRaises(StopAfterStartup):
                await bot_runner._run_loop(Settings(meeting_data_dir=self.data_dir))

    async def test_runner_removes_abandoned_bot_tmp_after_validation(self):
        from app.services.meetings import bot_runner
        initialize_meeting_storage(self.data_dir, self.connection)
        abandoned = self.data_dir / 'bot-tmp' / 'interrupted.wav'
        abandoned.write_bytes(b'private interrupted recording')
        with patch('app.services.meetings.bot_repository.BotSessionRepository.claim_next', side_effect=StopAfterStartup):
            with self.assertRaises(StopAfterStartup):
                await bot_runner._run_loop(Settings(meeting_data_dir=self.data_dir))
        self.assertFalse(abandoned.exists())

    async def test_runner_outage_does_not_remove_bot_tmp(self):
        from app.services.meetings import bot_runner
        initialize_meeting_storage(self.data_dir, self.connection)
        abandoned = self.data_dir / 'bot-tmp' / 'retained.wav'
        abandoned.write_bytes(b'private interrupted recording')
        self.connection.execute('UPDATE storage_identity SET instance_id=gen_random_uuid()')
        with self.assertRaises(DatabaseUnavailableError):
            await bot_runner._run_loop(Settings(meeting_data_dir=self.data_dir))
        self.assertEqual(abandoned.read_bytes(), b'private interrupted recording')
