"""Local recovery updates credentials and invalidates sessions atomically."""
from unittest import TestCase
from unittest.mock import patch

from apps.api.tests.postgres_support import PostgresTestCase
from app.commands.recover_password import recover_password
from app.services.access.passwords import hash_password, verify_password
from app.services.access.repository import AccessRepository
from app.services.access.sessions import SessionRepository


class PasswordRecoveryTest(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.account = AccessRepository(self.connection).create_account('ada@example.test', hash_password('old-password-123'))
        self.sessions = SessionRepository(self.connection)
        self.tokens = [self.sessions.create(self.account.id)[0] for _ in range(2)]

    def password_hash(self):
        return self.connection.execute('SELECT password_hash FROM accounts WHERE id=%s', (self.account.id,)).fetchone()[0]

    def test_recovery_updates_password_and_revokes_every_session(self):
        recover_password(self.connection, ' ADA@example.test ', 'new-password-123')
        self.assertTrue(verify_password('new-password-123', self.password_hash()))
        self.assertFalse(verify_password('old-password-123', self.password_hash()))
        for token in self.tokens:
            self.assertIsNone(self.sessions.resolve(token))
        self.assertFalse(AccessRepository(self.connection).account_by_id(self.account.id).is_system_admin)

    def test_session_failure_rolls_back_password_update(self):
        before = self.password_hash()
        with patch.object(SessionRepository, 'revoke_account', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                recover_password(self.connection, self.account.email, 'new-password-123')
        self.assertEqual(self.password_hash(), before)
        self.assertIsNotNone(self.sessions.resolve(self.tokens[0]))

    def test_missing_account_and_invalid_password_leave_state_unchanged(self):
        before = self.password_hash()
        for email, password in [('missing@example.test', 'new-password-123'), (self.account.email, 'short')]:
            with self.assertRaises(ValueError):
                recover_password(self.connection, email, password)
        self.assertEqual(self.password_hash(), before)
        self.assertIsNotNone(self.sessions.resolve(self.tokens[0]))


class PasswordRecoveryPromptTest(TestCase):
    def test_secure_prompt_confirmation_and_no_password_output(self):
        from app.commands.recover_password import main
        with patch('builtins.input', return_value='ada@example.test'), patch(
            'app.commands.recover_password.getpass.getpass', side_effect=['new-password-123', 'different-password']
        ) as prompt, patch('app.commands.recover_password.open_database') as database:
            with self.assertRaisesRegex(SystemExit, 'Passwords do not match'):
                main()
        self.assertEqual(prompt.call_count, 2)
        database.assert_not_called()
