"""DeepSeek configuration and HTTP contract without external requests."""
import json
import unittest
from pathlib import Path

import httpx

from app.core.rag_config import ProviderRegistry, RagError, load_rag_settings
from app.services.rag.providers import ModelClient


def deepseek_environment() -> dict[str, str]:
    template = Path(__file__).resolve().parents[3] / '.env.example'
    provider = next(line[2:] for line in template.read_text().splitlines()
                    if line.startswith('# {"id":"deepseek"'))
    return {'RAG_ENABLED': 'true', 'DEEPSEEK_API_KEY': 'test-deepseek-secret',
            'RAG_PROVIDERS_JSON': '[' + provider + ']'}


class DeepSeekProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_template_supports_chat_and_redacts_credentials(self) -> None:
        settings = load_rag_settings(deepseek_environment())
        registry = ProviderRegistry(settings)
        selection = registry.resolve_chat('deepseek', 'deepseek-flash')
        catalog = registry.catalog([]).model_dump_json()
        self.assertIn('DeepSeek', catalog)
        self.assertTrue(selection.provider.is_external)
        self.assertEqual(selection.provider.embedding_models, [])
        for private_value in ('test-deepseek-secret', 'api.deepseek.com', 'api_key'):
            self.assertNotIn(private_value, catalog)
        self.assertNotIn('test-deepseek-secret', repr(settings))
        messages = [{'role': 'user', 'content': 'Return a JSON answer.'}]

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, 'POST')
            self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-deepseek-secret')
            self.assertEqual(json.loads(request.content), {
                'model': 'deepseek-flash', 'messages': messages, 'stream': False,
                'max_tokens': 100000, 'response_format': {'type': 'json_object'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"answer":"ok"}'}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            answer = await ModelClient(registry, http).generate(selection, messages)
        self.assertEqual(json.loads(answer), {'answer': 'ok'})

    async def test_missing_key_and_external_consent_are_rejected(self) -> None:
        environment = deepseek_environment()
        environment['DEEPSEEK_API_KEY'] = ''
        with self.assertRaisesRegex(RagError, '^invalid_configuration$'):
            load_rag_settings(environment)
        environment = deepseek_environment()
        providers = json.loads(environment['RAG_PROVIDERS_JSON'])
        providers[0]['external_data_enabled'] = False
        environment['RAG_PROVIDERS_JSON'] = json.dumps(providers)
        with self.assertRaisesRegex(RagError, '^invalid_configuration$'):
            load_rag_settings(environment)

    async def test_provider_failures_return_safe_errors(self) -> None:
        registry = ProviderRegistry(load_rag_settings(deepseek_environment()))
        selection = registry.resolve_chat('deepseek', 'deepseek-flash')
        for failure, code, status in (
            ('timeout', 'provider_timeout', 504),
            ('unauthorized', 'provider_unavailable', 503),
            ('malformed', 'invalid_provider_response', 503),
        ):
            with self.subTest(failure=failure):
                def respond(request: httpx.Request) -> httpx.Response:
                    if failure == 'timeout':
                        raise httpx.ReadTimeout('test-deepseek-secret', request=request)
                    if failure == 'unauthorized':
                        return httpx.Response(401, text='test-deepseek-secret')
                    return httpx.Response(200, json={'choices': []})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                    with self.assertRaises(RagError) as caught:
                        await ModelClient(registry, http).generate(selection, [])
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(caught.exception.status, status)
