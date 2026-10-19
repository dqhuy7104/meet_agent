import json
import unittest
import httpx
from test_rag_config import environment
from app.core.rag_config import load_rag_settings, ProviderRegistry, RagError
from app.services.rag.providers import ModelClient


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_payloads(self):
        for kind in ('ollama', 'openai_compatible'):
            registry = ProviderRegistry(load_rag_settings(environment(kind)))
            requests = []
            def respond(request):
                body = json.loads(request.content)
                requests.append((request.url.path, body))
                if 'embed' in request.url.path:
                    return httpx.Response(200, json={'embeddings': [[1., 0.]], 'data': [{'index': 0, 'embedding': [1., 0.]}]})
                return httpx.Response(200, json={'message': {'content': '{}'}, 'choices': [{'message': {'content': '{}'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = ModelClient(registry, http)
                self.assertEqual(await client.embed(registry.resolve_profile('p'), ['hello']), [[1., 0.]])
                self.assertEqual(await client.generate(registry.resolve_chat('local', 'chat'), [{'role': 'user', 'content': 'hi'}]), '{}')
            self.assertEqual(requests[0][0], '/api/embed' if kind == 'ollama' else '/embeddings')
            self.assertEqual(requests[1][0], '/api/chat' if kind == 'ollama' else '/chat/completions')
            self.assertFalse(requests[1][1]['stream'])

    async def test_bad_embeddings_and_safe_errors(self):
        registry = ProviderRegistry(load_rag_settings(environment()))
        for data in ([], [{'index': 0, 'embedding': [1.]}], [{'index': 0, 'embedding': [float('nan'), 0.]}]):
            def respond(request):
                return httpx.Response(200, content=json.dumps({'data': data}))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                with self.assertRaisesRegex(RagError, 'invalid_provider_response'):
                    await ModelClient(registry, http).embed(registry.resolve_profile('p'), ['hi'])
        def fail(request):
            raise httpx.ReadTimeout('private-token transcript', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            with self.assertRaisesRegex(RagError, '^provider_timeout$'):
                await ModelClient(registry, http).embed(registry.resolve_profile('p'), ['private'])
