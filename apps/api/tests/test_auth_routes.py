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
