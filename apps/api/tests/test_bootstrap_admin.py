from apps.api.tests.postgres_support import PostgresTestCase


class BootstrapAdminTest(PostgresTestCase):
    def test_only_first_admin_can_be_bootstrapped(self):
        from app.commands.bootstrap_admin import bootstrap_admin
        account = bootstrap_admin(self.connection, 'admin@example.com', 'private-password')
        self.assertTrue(account.is_system_admin)
        with self.assertRaises(ValueError):
            bootstrap_admin(self.connection, 'other@example.com', 'private-password')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM accounts').fetchone()[0], 1)

    def test_command_prompts_without_echo_and_never_prints_password(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from app.commands.bootstrap_admin import main
        output = StringIO()
        with patch('builtins.input', return_value='admin@example.com'), patch('getpass.getpass', return_value='private-password') as prompt, redirect_stdout(output):
            main()
        prompt.assert_called_once()
        self.assertNotIn('private-password', output.getvalue())
        self.assertIn('Administrator created', output.getvalue())
