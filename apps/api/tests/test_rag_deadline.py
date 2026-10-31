import asyncio
import os
import threading
import time
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from app.api.routes.chat import router
from app.api.auth import require_account
from app.services.access.repository import Account, AccessRepository
from app.core.rag_config import RagError
from test_rag_config import environment


class DeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_filter_and_answer_share_the_original_deadline(self):
        import json
        from apps.api.tests.postgres_support import enter_postgres
        from app.services.meetings.repository import MeetingRepository
        from app.services.rag.repository import RagRepository
        from app.services.rag.chunking import snapshot_meeting, chunk_transcript
        from app.services.rag.request import RagRequestService, RequestDeadline
        from app.core.rag_config import load_rag_settings
        from app.schemas.chat import RagQuery
        from test_rag_outbox import complete_meeting
        fixture = enter_postgres(self)
        meetings = MeetingRepository(fixture.connection)
        complete_meeting(meetings)
        settings = load_rag_settings(environment())
        snapshot = snapshot_meeting(meetings.get_detail('m'))
        chunks = chunk_transcript(snapshot)
        RagRepository(fixture.connection).publish(snapshot, settings.profiles[0], chunks, [[1.,0.] for c in chunks])
        account_id = meetings.get_meeting('m').uploaded_by_account_id
        deadline = RequestDeadline()
        calls = []
        async def embed(*args): return [[1.,0.]]
        async def generate(client, selection, messages):
            calls.append(messages[0]['content'])
            deadline.expires_at = time.monotonic()-1
            sources = json.loads(messages[1]['content'])['sources']
            return json.dumps({'source_ids':[s['source_id'] for s in sources]})
        service = RagRequestService(settings, fixture.database)
        request = RagQuery(question='kế hoạch', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')
        with patch('app.services.rag.providers.ModelClient.embed', embed), patch('app.services.rag.providers.ModelClient.generate', generate):
            with self.assertRaisesRegex(RagError, '^query_timeout$'):
                await asyncio.to_thread(service._serve, deadline, 'query', (request, account_id))
        self.assertEqual(len(calls), 1)
        self.assertIn('relevance_filter_v1', calls[0])

    async def test_deadline_returns_while_database_dependency_is_blocked(self):
        release=threading.Event()
        def blocked(settings, **kwargs):
            release.wait(1)
            raise RagError('index_unavailable')
        app=FastAPI();app.include_router(router,prefix='/api')
        async def account():
            return Account('reader', 'reader@example.test', False, False)
        app.dependency_overrides[require_account] = account
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
        account_id = fixture.connection.execute('SELECT uploaded_by_account_id FROM meetings LIMIT 1').fetchone()[0]
        async def account():
            return AccessRepository(fixture.connection).account_by_id(account_id)
        app.dependency_overrides[require_account] = account
        try:
            with patch.dict(os.environ, env), patch('app.services.rag.repository.RagRepository.validate_profile'), patch('app.services.rag.repository.RagRepository.is_profile_ready', return_value=True), patch.object(MeetingRepository, 'get_detail', side_effect=blocked_source), patch('app.api.routes.chat.QUERY_TIMEOUT_SECONDS', .05):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost') as client:
                    response = await asyncio.wait_for(client.get('/api/chat/providers'), .4)
                    self.assertEqual(response.status_code, 504)
        finally:
            release.set()
