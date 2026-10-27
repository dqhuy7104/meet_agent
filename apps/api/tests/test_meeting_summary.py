import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.api.deps import get_rag_service
from app.api.routes.chat import router
from app.api.auth import require_account, require_csrf
from app.services.access.repository import Account
from app.core.rag_config import ProviderRegistry, RagError, load_rag_settings
from app.services.llm.summarizer import summarize_transcript
from app.services.rag.query import QueryService
from test_rag_config import environment


class SummaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_transcript_never_calls_model(self):
        class Client:
            async def generate(self, *args):
                self.fail('Model should not be called')

        selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local', 'chat')
        self.assertEqual(await summarize_transcript([], selection, Client()), '')

    async def test_long_transcript_is_chunked_and_combined(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def generate(self, selection, messages):
                self.calls.append(messages)
                return '{"summary":"Tóm tắt hợp lệ"}'

        client = Client()
        selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local', 'chat')
        segments = [SimpleNamespace(start=0, end=10, speaker='SPEAKER_00', text='Nội dung ' * 5000)]
        result = await summarize_transcript(segments, selection, client)
        self.assertEqual(result, 'Tóm tắt hợp lệ')
        self.assertGreater(len(client.calls), 1)
        for messages in client.calls:
            self.assertLess(sum(len(message['content'].encode()) // 3 for message in messages), 4096)

    async def test_invalid_model_json_is_safe_error(self):
        class Client:
            async def generate(self, *args):
                return 'private transcript'

        selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local', 'chat')
        with self.assertRaisesRegex(RagError, 'invalid_provider_response'):
            await summarize_transcript([SimpleNamespace(start=0, end=1, speaker='SPEAKER_00', text='Hello')], selection, Client())

    async def test_route_contract_and_safe_failures(self):
        class Service:
            error = None

            async def summarize(self, meeting_id, account_id):
                if self.error:
                    raise self.error
                return dict(summary='Nội dung chính', provider_id='local', model_id='chat', prompt_version='meeting_summary_v1')

        service = Service()
        app = FastAPI()
        app.include_router(router, prefix='/api')
        async def account():
            return Account('reader', 'reader@example.test', False, False)
        async def csrf():
            pass
        app.dependency_overrides[require_account] = account
        app.dependency_overrides[require_csrf] = csrf
        async def override_service():
            return service
        app.dependency_overrides[get_rag_service] = override_service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost') as client:
            response = await client.post('/api/chat/meetings/meeting-1/summary')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['summary'], 'Nội dung chính')
            self.assertEqual((await client.post('/api/chat/meetings/' + 'a' * 201 + '/summary')).status_code, 422)
            for error in (RagError('meeting_not_found', 404), RagError('meeting_not_completed', 409), RagError('provider_unavailable')):
                service.error = error
                response = await client.post('/api/chat/meetings/meeting-1/summary')
                self.assertEqual(response.status_code, error.status)
                self.assertEqual(response.json(), {'detail': error.code})
            self.assertEqual((await client.post('/api/chat/meetings/meeting-1/summary', headers={'Origin': 'https://evil.example'})).status_code, 403)

    async def test_query_service_checks_meeting_state_and_provider(self):
        settings = load_rag_settings(environment())
        registry = ProviderRegistry(settings)
        class Meetings:
            detail = None
            def get_detail(self, meeting_id):
                return self.detail
        class Client:
            async def generate(self, selection, messages):
                return '{"summary":"Đã tóm tắt"}'
        meetings = Meetings()
        service = QueryService(settings, registry, meetings, None, Client())
        with self.assertRaisesRegex(RagError, 'meeting_not_found'):
            await service.summarize('missing')
        meetings.detail = SimpleNamespace(meeting=SimpleNamespace(id='one', status='processing', title='Họp', original_filename='one.wav'), transcript_segments=[])
        with self.assertRaisesRegex(RagError, 'meeting_not_completed'):
            await service.summarize('one')
        meetings.detail.meeting.status = 'completed'
        meetings.detail.transcript_segments = [SimpleNamespace(start=0, end=1, speaker='SPEAKER_00', text='Nội dung')]
        result = await service.summarize('one')
        self.assertEqual(result['summary'], 'Đã tóm tắt')
        self.assertEqual(result['provider_id'], 'local')
