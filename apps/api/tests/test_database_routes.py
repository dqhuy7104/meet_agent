import asyncio
import threading
from unittest.mock import patch

import httpx

from app.api.routes.meetings import get_meeting_database_settings, get_meeting_settings
from app.core.config import Settings
from app.core.database_config import DatabaseUnavailableError
from app.main import app
from apps.api.tests.postgres_support import PostgresAsyncTestCase


class DatabaseRoutesTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        from apps.api.tests.postgres_support import seed_project
        from app.services.access.sessions import COOKIE_NAME, SessionRepository
        account, _ = seed_project(self.connection)
        app.dependency_overrides[get_meeting_settings] = lambda: Settings(meeting_data_dir=self.data_dir)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        token, _ = SessionRepository(self.connection).create(account.id)
        self.client.cookies.set(COOKIE_NAME, token)

    async def test_database_failure_is_safe_503(self):
        def missing():
            raise DatabaseUnavailableError('database_not_configured')
        app.dependency_overrides[get_meeting_database_settings] = missing
        response = await self.client.get('/api/meetings')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': 'database_not_configured'})
        self.assertEqual((await self.client.get('/openapi.json')).status_code, 200)

    async def test_slow_storage_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        from app.api.routes.meetings import open_database
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return open_database(*args, **kwargs)
        with patch('app.api.routes.meetings.open_database', delayed):
            request = asyncio.create_task(self.client.get('/api/meetings'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                response = await asyncio.wait_for(self.client.get('/openapi.json'), timeout=1)
                self.assertEqual(response.status_code, 200)
            finally:
                release.set()
                self.assertEqual((await request).status_code, 200)
