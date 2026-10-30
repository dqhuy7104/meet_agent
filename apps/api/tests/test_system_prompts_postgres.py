import json
from unittest.mock import patch

import httpx

from apps.api.tests.postgres_support import PostgresAsyncTestCase, seed_snapshot
from app.core.rag_config import ProviderRegistry, load_rag_settings
from app.schemas.chat import RagQuery
from app.services.access.repository import AccessRepository
from app.services.access.sessions import COOKIE_NAME, SessionRepository
from app.services.llm.prompt_settings import PromptRepository
from app.services.rag.chunking import make_snapshot
from app.services.rag.repository import RagRepository
from app.services.rag.request import RagRequestService
from app.services.rag.types import SourceSegment
from app.services.rag.worker import synchronize_once
from app.services.meetings.repository import MeetingRepository
from test_rag_config import environment


class SystemPromptsPostgresTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        from app.main import app
        repository = AccessRepository(self.connection)
        self.admin = repository.create_account('admin@example.test', 'hash')
        self.connection.execute('UPDATE accounts SET is_system_admin=true WHERE id=%s', (self.admin.id,))
        token, csrf = SessionRepository(self.connection).create(self.admin.id)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://localhost')
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers['X-CSRF-Token'] = csrf
        self.addAsyncCleanup(self.client.aclose)

    async def test_persistence_and_real_session_permissions(self):
        url = '/api/admin/system-prompts'
        payload = {'summary_system_prompt': 'Use bullet points.', 'rag_system_prompt': 'Be concise.'}
        self.assertEqual((await self.client.put(url, json=payload)).status_code, 200)
        self.assertEqual((await self.client.get(url)).json(), payload)
        from app.db.session import open_database
        with open_database(self.database) as connection:
            self.assertEqual(PromptRepository(connection).get().model_dump(), payload)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.put(url, json=payload)).status_code, 403)
        member = AccessRepository(self.connection).create_account('member@example.test', 'hash')
        token, csrf = SessionRepository(self.connection).create(member.id)
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers['X-CSRF-Token'] = csrf
        self.assertEqual((await self.client.get(url)).status_code, 403)
        self.assertEqual((await self.client.put(url, json=payload)).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual((await self.client.get(url)).status_code, 401)

    async def test_saved_prompts_reach_summary_and_rag_without_provider_calls(self):
        snapshot = seed_snapshot(self.connection, make_snapshot('one', 'Synthetic meeting', (
            SourceSegment(0, 'Kế hoạch dự án', 'Speaker', 0, 1),)))
        settings = load_rag_settings(environment())
        payload = {'summary_system_prompt': 'Summarize decisions only.', 'rag_system_prompt': 'Respond briefly.'}
        self.assertEqual((await self.client.put('/api/admin/system-prompts', json=payload)).status_code, 200)
        seen = []
        class FakeClient:
            async def embed(self, profile, texts):
                return [[1., 0.] for _ in texts]
            async def generate(self, selection, messages):
                seen.append(messages[0]['content'])
                data = json.loads(messages[1]['content'])
                if 'transcript' in data:
                    return '{"summary":"Synthetic summary"}'
                return json.dumps({'status': 'answered', 'claims': [
                    {'text': 'Kế hoạch dự án', 'citation_ids': [data['sources'][0]['source_id']]}]})
        client = FakeClient()
        await synchronize_once(MeetingRepository(self.connection), RagRepository(self.connection), client, settings)
        service = RagRequestService(settings, self.database)
        async def generate(_client, selection, messages):
            return await client.generate(selection, messages)
        async def embed(_client, profile, texts):
            return await client.embed(profile, texts)
        with patch('app.services.rag.providers.ModelClient.generate', generate), patch('app.services.rag.providers.ModelClient.embed', embed):
            summary = await service.summarize(snapshot.meeting_id, self.admin.id)
            self.assertTrue(summary['prompt_version'].startswith('meeting_summary_v1:'))
            self.assertIn(payload['summary_system_prompt'], seen[-1])
            answer = await service.query(RagQuery(question='kế hoạch', k=1, chat_provider_id='local',
                chat_model_id='chat', embedding_profile_id='p'), self.admin.id)
            self.assertEqual(answer.retrieval_status, 'answered')
            self.assertIn(payload['rag_system_prompt'], seen[-1])
            self.assertEqual(len(seen), 2)
            self.assertEqual((await service.summarize(snapshot.meeting_id, self.admin.id))['summary'], 'Synthetic summary')
            self.assertEqual(len(seen), 2)
