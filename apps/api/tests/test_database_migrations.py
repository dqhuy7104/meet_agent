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
            self.assertEqual([row[0] for row in fixture.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version").fetchall()], [1, 2, 3, 4, 5])
            from app.services.access.repository import AccessRepository
            project = AccessRepository(fixture.connection).create_project('Migration test')
            fixture.connection.execute("INSERT INTO meetings (id,original_filename,storage_key,checksum,created_at,updated_at,status,project_id) VALUES ('m','a.wav',%s,'hash',now(),now(),'queued',%s)", ('a'*32, project.id))
            self.assertEqual(fixture.connection.execute(
                "SELECT voice_identification_status FROM meetings WHERE id='m'").fetchone()[0], "not_run")
            identity = fixture.connection.execute("SELECT instance_id FROM storage_identity").fetchone()[0]
            migrate_database(fixture.connection)
            self.assertEqual(fixture.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 5)
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
            self.assertEqual(fixture.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 5)

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
        from app.services.access.repository import AccessRepository
        with isolated_postgres() as fixture:
            project = AccessRepository(fixture.connection).create_project('Migration test')
            fixture.connection.execute("INSERT INTO meetings (id,original_filename,storage_key,checksum,created_at,updated_at,status,project_id) VALUES ('m','a.wav',%s,'hash',now(),now(),'queued',%s)", ('a'*32, project.id))
            for values in ((-1, 0, 1), (0, -1, 1), (0, 2, 1)):
                with self.subTest(values=values), self.assertRaises(psycopg.errors.CheckViolation):
                    fixture.connection.execute('INSERT INTO vad_segments VALUES (%s,%s,%s,%s)', ('m', *values))

    def test_upgrade_keeps_existing_jobs_and_fails_them_after_profile_deletion(self) -> None:
        from unittest.mock import patch
        from app.db.migrate import migration_definitions
        definitions = migration_definitions()
        with isolated_postgres(migrate=False) as fixture:
            with patch('app.db.migrate.migration_definitions', return_value=definitions[:2]):
                migrate_database(fixture.connection)
            fixture.connection.execute("INSERT INTO voice_profiles(id,display_name) VALUES ('p','Participant')")
            fixture.connection.execute("INSERT INTO voice_enrollment_jobs(id,profile_id,source,upload_key) VALUES ('j','p','upload',%s)", ('a'*32,))
            migrate_database(fixture.connection)
            fixture.connection.execute("DELETE FROM voice_profiles WHERE id='p'")
            self.assertEqual(fixture.connection.execute("SELECT profile_id,state,failure_code FROM voice_enrollment_jobs WHERE id='j'").fetchone(),
                             (None, 'failed', 'source_changed'))
