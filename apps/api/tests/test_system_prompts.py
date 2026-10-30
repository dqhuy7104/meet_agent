import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from pydantic import ValidationError

from app.api.auth import require_account
from app.api.routes.prompt_settings import get_prompt_repository, router
from app.core.rag_config import ProviderRegistry, RagError, load_rag_settings
from app.schemas.system_prompts import SystemPrompts
from app.services.llm.summarizer import summarize_transcript
from app.services.rag.prompt import build_messages
from test_rag_config import environment


class SystemPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_custom_prompts_keep_built_in_defaults(self):
        from app.services.llm.summarizer import SYSTEM_PROMPT, summary_prompt
        from app.services.rag.prompt import PROMPT_TEXT, rag_prompt
        self.assertEqual(summary_prompt(''), SYSTEM_PROMPT)
        self.assertEqual(rag_prompt(''), PROMPT_TEXT)

    async def test_oversized_effective_summary_prompt_does_not_call_provider(self):
        class Client:
            async def generate(self, selection, messages):
                raise AssertionError('Prompt must fit before provider dispatch')
        selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local', 'chat')
        with self.assertRaisesRegex(RagError, '^prompt_too_long$'):
            await summarize_transcript([SimpleNamespace(start=0, end=1, speaker='speaker', text='Text')],
                                       selection, Client(), 'x' * 8000)

    async def test_custom_prompts_preserve_contracts(self):
        messages = build_messages('Question', [], 'meeting_qa_v1', 'Answer briefly in Vietnamese.')
        self.assertIn('Answer briefly in Vietnamese.', messages[0]['content'])
        self.assertIn('citation_ids', messages[0]['content'])
        self.assertIn('JSON', messages[0]['content'])
        self.assertNotIn('Question', messages[0]['content'])
        calls = []
        class Client:
            async def generate(self, selection, messages):
                calls.append(messages)
                return '{"summary":"Summary"}'
        selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local', 'chat')
        result = await summarize_transcript(
            [SimpleNamespace(start=0, end=1, speaker='speaker', text='Private transcript')],
            selection, Client(), 'Write three key points.')
        self.assertEqual(result, 'Summary')
        self.assertIn('Write three key points.', calls[0][0]['content'])
        self.assertIn('JSON', calls[0][0]['content'])
        self.assertNotIn('Private transcript', calls[0][0]['content'])

    async def test_prompt_validation(self):
        self.assertEqual(SystemPrompts(summary_system_prompt='  Text  ').summary_system_prompt, 'Text')
        for value in ('x' * 8001, '\0', None):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                SystemPrompts(summary_system_prompt=value)

    async def test_admin_permissions_csrf_and_roundtrip(self):
        state = SystemPrompts()
        class Repository:
            def get(self):
                return state
            def save(self, prompts):
                nonlocal state
                state = prompts
                return state
        account = SimpleNamespace(is_system_admin=True, is_disabled=False)
        app = FastAPI()
        app.include_router(router, prefix='/api')
        async def authenticated(request: Request):
            request.state.csrf_token = 'csrf'
            return account
        app.dependency_overrides[require_account] = authenticated
        app.dependency_overrides[get_prompt_repository] = lambda: Repository()
        url = '/api/admin/system-prompts'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            self.assertEqual((await client.get(url)).json(), state.model_dump())
            payload = {'summary_system_prompt': 'Summarize briefly', 'rag_system_prompt': 'Use Vietnamese'}
            self.assertEqual((await client.put(url, json=payload)).status_code, 403)
            response = await client.put(url, json=payload, headers={'X-CSRF-Token': 'csrf'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await client.get(url)).json(), payload)
            self.assertEqual((await client.put(url, json={'summary_system_prompt': 'x'*8001}, headers={'X-CSRF-Token': 'csrf'})).status_code, 422)
            account.is_system_admin = False
            self.assertEqual((await client.get(url)).status_code, 403)
            self.assertEqual((await client.put(url, json=payload, headers={'X-CSRF-Token': 'csrf'})).status_code, 403)
