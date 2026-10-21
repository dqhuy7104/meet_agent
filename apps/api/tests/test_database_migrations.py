import unittest
from concurrent.futures import ThreadPoolExecutor
import psycopg

from app.core.database_config import DatabaseUnavailableError
from app.db.migrate import migrate_database
from app.db.session import open_database, validate_database
from apps.api.tests.postgres_support import isolated_postgres


class DatabaseMigrationTest(unittest.TestCase):
    def test_initialization_repeat_checksums_and_storage_identity(self) -> None:
        with isolated_postgres(migrate=False) as fixture:
            migrate_database(fixture.connection)
            identity = fixture.connection.execute("SELECT instance_id FROM storage_identity").fetchone()[0]
            migrate_database(fixture.connection)
            self.assertEqual(fixture.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)
            self.assertEqual(fixture.connection.execute("SELECT instance_id FROM storage_identity").fetchone()[0], identity)
            with open_database(fixture.database) as connection:
                validate_database(connection)
                self.assertEqual(connection.execute("SHOW statement_timeout").fetchone()[0], "10s")
                self.assertEqual(connection.execute("SHOW lock_timeout").fetchone()[0], "5s")
                self.assertEqual(connection.execute("SELECT current_schema()").fetchone()[0], fixture.schema)
            fixture.connection.execute("UPDATE schema_migrations SET checksum='changed'")
            with self.assertRaises(DatabaseUnavailableError):
                migrate_database(fixture.connection)

    def test_concurrent_initialization(self) -> None:
        with isolated_postgres(migrate=False) as fixture:
            def migrate() -> None:
                with psycopg.connect(fixture.database.database_url.get_secret_value(), autocommit=True) as connection:
                    migrate_database(connection)
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: migrate(), range(2)))
            self.assertEqual(fixture.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)

    def test_unversioned_application_tables_are_not_adopted(self) -> None:
        with isolated_postgres(migrate=False) as fixture:
            fixture.connection.execute("CREATE TABLE meetings(id text)")
            fixture.connection.execute("INSERT INTO meetings VALUES ('retained')")
            with self.assertRaises(DatabaseUnavailableError):
                migrate_database(fixture.connection)
            self.assertEqual(fixture.connection.execute("SELECT id FROM meetings").fetchone()[0], "retained")

    def test_unknown_version_is_rejected(self) -> None:
        with isolated_postgres() as fixture:
            fixture.connection.execute("INSERT INTO schema_migrations(version,checksum) VALUES (999,'future')")
            with self.assertRaises(DatabaseUnavailableError):
                validate_database(fixture.connection)

    def test_invalid_audio_intervals_and_ordinals_are_rejected(self) -> None:
        from app.services.meetings.repository import MeetingRepository
        with isolated_postgres() as fixture:
            MeetingRepository(fixture.connection).create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
            for values in ((-1, 0, 1), (0, -1, 1), (0, 2, 1)):
                with self.subTest(values=values), self.assertRaises(psycopg.errors.CheckViolation):
                    fixture.connection.execute('INSERT INTO vad_segments VALUES (%s,%s,%s,%s)', ('m', *values))
