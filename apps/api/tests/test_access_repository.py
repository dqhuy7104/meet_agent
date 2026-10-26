import unittest
from unittest.mock import patch

import psycopg

from app.db.migrate import migrate_database, migration_definitions
from apps.api.tests.postgres_support import PostgresTestCase, isolated_postgres


class AccessRepositoryTest(PostgresTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.services.access.repository import AccessRepository
        self.repository = AccessRepository(self.connection)

    def test_account_email_is_normalized_and_unique(self) -> None:
        account = self.repository.create_account('  Ada@Example.com ', 'hash')
        self.assertEqual(account.email, 'ada@example.com')
        self.assertEqual(self.repository.account_by_email(' ADA@example.COM '), account)
        self.assertEqual(self.repository.account_by_id(account.id), account)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.repository.create_account('ada@example.com', 'different-hash')

    def test_new_account_sees_no_projects_and_membership_is_per_project(self) -> None:
        account = self.repository.create_account('ada@example.com', 'hash')
        first = self.repository.create_project('First')
        second = self.repository.create_project('Second')
        self.assertEqual(self.repository.visible_projects(account), [])
        self.assertIsNone(self.repository.role(account, first.id))
        self.repository.set_membership(first.id, account.id, 'project_manager')
        self.repository.set_membership(second.id, account.id, 'viewer')
        self.assertEqual([project.id for project in self.repository.visible_projects(account)], [first.id, second.id])
        self.assertEqual(self.repository.role(account, first.id), 'project_manager')
        self.assertEqual(self.repository.role(account, second.id), 'viewer')
        self.repository.set_membership(second.id, account.id, 'member')
        self.assertEqual(self.repository.role(account, second.id), 'member')

    def test_cannot_remove_or_demote_last_manager_of_active_project(self) -> None:
        account = self.repository.create_account('ada@example.com', 'hash')
        project = self.repository.create_project('First')
        self.repository.set_membership(project.id, account.id, 'project_manager')
        with self.assertRaises(ValueError):
            self.repository.remove_membership(project.id, account.id)
        with self.assertRaises(ValueError):
            self.repository.set_membership(project.id, account.id, 'viewer')
        self.assertEqual(self.repository.role(account, project.id), 'project_manager')
        other = self.repository.create_account('bea@example.com', 'hash')
        self.repository.set_membership(project.id, other.id, 'project_manager')
        self.repository.remove_membership(project.id, account.id)
        self.assertIsNone(self.repository.role(account, project.id))


class ProjectMigrationUpgradeTest(unittest.TestCase):
    def test_new_project_rows_cannot_become_legacy_unassigned_on_update(self) -> None:
        with isolated_postgres() as fixture:
            from app.services.access.repository import AccessRepository
            project = AccessRepository(fixture.connection).create_project('First')
            fixture.connection.execute(
                "INSERT INTO meetings (id,original_filename,storage_key,checksum,created_at,updated_at,status,project_id) "
                "VALUES ('new','new.wav',%s,'hash',now(),now(),'queued',%s)", ('b'*32, project.id)
            )
            fixture.connection.execute(
                "INSERT INTO bot_sessions (id,url_hash,status,created_at,project_id) "
                "VALUES ('new-bot','hash','queued',now(),%s)", (project.id,)
            )
            for table, row_id in (('meetings', 'new'), ('bot_sessions', 'new-bot')):
                with self.subTest(table=table), self.assertRaises(psycopg.errors.CheckViolation):
                    fixture.connection.execute(
                        f"UPDATE {table} SET legacy_unassigned=true, project_id=NULL WHERE id=%s", (row_id,)
                    )

    def test_old_rows_remain_unassigned_and_mutable_but_new_rows_require_projects(self) -> None:
        definitions = migration_definitions()
        with isolated_postgres(migrate=False) as fixture:
            with patch('app.db.migrate.migration_definitions', return_value=definitions[:3]):
                migrate_database(fixture.connection)
            fixture.connection.execute("INSERT INTO meetings (id,original_filename,storage_key,checksum,created_at,updated_at,status) VALUES ('old','old.wav',%s,'hash',now(),now(),'queued')", ('a'*32,))
            fixture.connection.execute("INSERT INTO bot_sessions (id,url_hash,status,created_at) VALUES ('old-bot','hash','queued',now())")
            migrate_database(fixture.connection)
            self.assertEqual(fixture.connection.execute("SELECT project_id,legacy_unassigned FROM meetings WHERE id='old'").fetchone(), (None, True))
            self.assertEqual(fixture.connection.execute("SELECT project_id,legacy_unassigned FROM bot_sessions WHERE id='old-bot'").fetchone(), (None, True))
            fixture.connection.execute("UPDATE meetings SET status='processing' WHERE id='old'")
            fixture.connection.execute("UPDATE bot_sessions SET status='cancelled' WHERE id='old-bot'")
            self.assertEqual(fixture.connection.execute("SELECT status FROM meetings WHERE id='old'").fetchone()[0], 'processing')
            with self.assertRaises(psycopg.errors.CheckViolation):
                fixture.connection.execute("INSERT INTO meetings (id,original_filename,storage_key,checksum,created_at,updated_at,status) VALUES ('new','new.wav',%s,'hash',now(),now(),'queued')", ('b'*32,))
            with self.assertRaises(psycopg.errors.CheckViolation):
                fixture.connection.execute("INSERT INTO bot_sessions (id,url_hash,status,created_at) VALUES ('new-bot','hash','queued',now())")
            from app.services.access.repository import AccessRepository
            account = AccessRepository(fixture.connection).create_account('ada@example.com', 'hash')
            self.assertEqual(AccessRepository(fixture.connection).visible_projects(account), [])
