import asyncio
import os
import threading
import time
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from app.api.routes.chat import router
from app.core.rag_config import RagError
from test_rag_config import environment


class DeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_returns_while_database_dependency_is_blocked(self):
        release=threading.Event()
        def blocked(settings, **kwargs):
            release.wait(1)
            raise RagError('index_unavailable')
        app=FastAPI();app.include_router(router,prefix='/api')
        try:
            with patch.dict(os.environ,environment()),patch('app.db.session.open_database',side_effect=blocked),patch('app.api.routes.chat.QUERY_TIMEOUT_SECONDS',.05,create=True):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',1)),base_url='http://localhost') as client:
                    start=time.monotonic()
                    response=await asyncio.wait_for(client.get('/api/chat/providers'),timeout=.4)
                    self.assertEqual(response.status_code,504)
                    self.assertLess(time.monotonic()-start,.3)
        finally:release.set()

    async def test_deadline_also_bounds_authoritative_source_reads(self):
        from apps.api.tests.postgres_support import enter_postgres
        from app.services.meetings.repository import MeetingRepository
        from test_rag_outbox import complete_meeting
        release = threading.Event()
        fixture = enter_postgres(self)
        complete_meeting(MeetingRepository(fixture.connection))
        env = environment()
        env['DATABASE_URL'] = fixture.database.database_url.get_secret_value()
        def blocked_source(*args, **kwargs):
            release.wait(1)
            return None
        app = FastAPI()
        app.include_router(router, prefix='/api')
        try:
            with patch.dict(os.environ, env), patch('app.services.rag.repository.RagRepository.validate_profile'), patch('app.services.rag.repository.RagRepository.is_profile_ready', return_value=True), patch.object(MeetingRepository, 'get_detail', side_effect=blocked_source), patch('app.api.routes.chat.QUERY_TIMEOUT_SECONDS', .05):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost') as client:
                    response = await asyncio.wait_for(client.get('/api/chat/providers'), .4)
                    self.assertEqual(response.status_code, 504)
        finally:
            release.set()
