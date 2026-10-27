import unittest
import httpx
from apps.api.tests.postgres_support import PostgresAsyncTestCase


class AuthRoutesTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        from app.main import app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def register(self):
        response = await self.client.post('/api/auth/register', json={'email': 'Ada@example.com', 'password': 'correct-password'})
        self.assertEqual(response.status_code, 201, response.text)
        return response

    async def test_registration_cookie_duplicate_and_csrf(self):
        response = await self.register()
        self.assertFalse(response.json()['account']['is_system_admin'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM project_memberships').fetchone()[0], 0)
        stored = self.connection.execute('SELECT password_hash FROM accounts').fetchone()[0]
        self.assertNotEqual(stored, 'correct-password')
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=strict', response.headers['set-cookie'])
        duplicate = await self.client.post('/api/auth/register', json={'email':'ADA@example.com','password':'correct-password'})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual((await self.client.post('/api/auth/logout')).status_code, 403)
        session = (await self.client.get('/api/auth/session')).json()
        self.assertEqual((await self.client.post('/api/auth/logout', headers={'X-CSRF-Token':session['csrf_token']})).status_code, 204)
        self.assertEqual((await self.client.get('/api/auth/session')).status_code, 401)

    async def test_login_generic_disabled_expired_and_password_change(self):
        await self.register()
        for email in ('missing@example.com', 'ada@example.com'):
            response = await self.client.post('/api/auth/login', json={'email':email,'password':'wrong'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['detail'], 'Invalid email or password')
        csrf = (await self.client.get('/api/auth/session')).json()['csrf_token']
        changed = await self.client.post('/api/auth/change-password', headers={'X-CSRF-Token':csrf}, json={'current_password':'correct-password','new_password':'new-password-123'})
        self.assertEqual(changed.status_code, 204, changed.text)
        self.assertEqual((await self.client.get('/api/auth/session')).status_code, 401)
        login = await self.client.post('/api/auth/login', json={'email':'ada@example.com','password':'new-password-123'})
        self.assertEqual(login.status_code, 200)
        self.connection.execute("UPDATE auth_sessions SET last_seen_at=now()-interval '2 hours'")
        self.assertEqual((await self.client.get('/api/auth/session')).status_code, 401)
        await self.client.post('/api/auth/login', json={'email':'ada@example.com','password':'new-password-123'})
        self.connection.execute('UPDATE accounts SET is_disabled=true')
        self.assertEqual((await self.client.get('/api/auth/session')).status_code, 401)

    async def test_validation_never_echoes_password_and_rate_limits(self):
        response = await self.client.post('/api/auth/register', json={'email':'ada@example.com','password':'secret'})
        self.assertNotIn('secret', response.text)
        for _ in range(20):
            self.connection.execute("INSERT INTO auth_attempts(peer) VALUES ('127.0.0.1')")
        response = await self.client.post('/api/auth/login', json={'email':'ada@example.com','password':'correct-password'})
        self.assertEqual(response.status_code, 429)

    async def test_absolute_expiry_and_token_digest_only(self):
        response = await self.register()
        token = self.client.cookies['meeting_session']
        digest, csrf = self.connection.execute('SELECT token_digest,csrf_token FROM auth_sessions').fetchone()
        self.assertNotEqual(token, digest)
        self.assertEqual(len(digest), 64)
        self.connection.execute("UPDATE auth_sessions SET created_at=now()-interval '8 days'")
        self.assertEqual((await self.client.get('/api/auth/session')).status_code, 401)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM auth_sessions').fetchone()[0], 0)

    async def test_secure_mode_accepts_only_https_local_origin(self):
        from unittest.mock import patch
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',1234)), base_url='https://localhost') as client:
            with patch.dict('os.environ', {'AUTH_COOKIE_SECURE':'true'}):
                response = await client.post('/api/auth/register', headers={'Origin':'https://localhost'}, json={'email':'secure@example.com','password':'correct-password'})
                self.assertEqual(response.status_code, 201, response.text)
                self.assertIn('Secure', response.headers['set-cookie'])
                insecure = await self.client.post('/api/auth/login', json={'email':'secure@example.com','password':'correct-password'})
                self.assertEqual(insecure.status_code, 403)

    async def test_concurrent_password_change_revokes_old_password_login(self):
        from concurrent.futures import ThreadPoolExecutor
        import psycopg
        from threading import Event, current_thread
        import time
        from unittest.mock import patch
        from fastapi import Request, Response
        from app.api.routes import auth
        from app.schemas.auth import LoginCredentials, PasswordChange
        from app.services.access.repository import AccessRepository
        from app.services.access.sessions import SessionRepository

        await self.register()
        account = AccessRepository(self.connection).account_by_email('ada@example.com')
        password_read = Event()
        continue_login = Event()
        original_verify = auth.verify_password

        def pause_login(password, encoded):
            if current_thread().name.startswith('paused-login'):
                password_read.set()
                if not continue_login.wait(10):
                    raise AssertionError('Login was never released')
            return original_verify(password, encoded)

        request = Request({'type':'http', 'scheme':'http', 'headers':[], 'server':('localhost',80), 'path':'/api/auth/login'})
        with psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True) as login_connection, psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True) as change_connection:
            change_pid = change_connection.info.backend_pid
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='paused-login') as login_executor, ThreadPoolExecutor(max_workers=1) as change_executor:
                with patch.object(auth, 'verify_password', side_effect=pause_login):
                    login = login_executor.submit(auth.login, LoginCredentials(email=account.email, password='correct-password'), request, Response(), SessionRepository(login_connection))
                    try:
                        self.assertTrue(password_read.wait(5), 'Login did not reach verification')
                        change = change_executor.submit(auth.change_password, PasswordChange(current_password='correct-password', new_password='new-password-123'), account, SessionRepository(change_connection))
                        deadline = time.monotonic() + 5
                        while not change.done():
                            waiting = self.connection.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (change_pid,)).fetchone()
                            if waiting and waiting[0] == 'Lock':
                                break
                            self.assertLess(time.monotonic(), deadline, 'Password change neither completed nor waited on the login lock')
                            time.sleep(0.01)
                    finally:
                        continue_login.set()
                    login.result(timeout=5)
                    self.assertEqual(change.result(timeout=5).status_code, 204)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM auth_sessions WHERE account_id=%s', (account.id,)).fetchone()[0], 0,
                         'Old-password login created a session that survived password-change revocation')


class AuthDependencyTest(unittest.TestCase):
    def test_revoked_between_identity_and_csrf_is_unauthorized(self):
        from unittest.mock import Mock
        from fastapi import HTTPException, Request
        from app.api.auth import require_account
        from app.services.access.repository import Account
        repository = Mock()
        repository.resolve.return_value = Account('id', 'email@example.test', False, False)
        repository.csrf.return_value = None
        request = Request({'type':'http', 'headers':[(b'cookie', b'meeting_session=token')]})
        with self.assertRaises(HTTPException) as raised:
            require_account(request, repository, None)
        self.assertEqual(raised.exception.status_code, 401)

    def test_non_ascii_csrf_is_forbidden(self):
        from fastapi import HTTPException, Request
        from app.api.auth import require_csrf
        request = Request({'type':'http', 'headers':[(b'x-csrf-token', b'\xff')]})
        request.state.csrf_token = 'expected'
        with self.assertRaises(HTTPException) as raised:
            require_csrf(request, None)
        self.assertEqual(raised.exception.status_code, 403)
