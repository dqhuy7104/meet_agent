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
                body = json.loads(request.content) if request.content else {}
                if request.url.path == '/api/tags':
                    return httpx.Response(200, json={'models': [{'name': 'embed:fixed', 'digest': 'a'*64}]})
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

    async def test_ollama_digest_change_is_rejected_before_embedding(self):
        env = environment('ollama')
        providers=json.loads(env['RAG_PROVIDERS_JSON']); providers[0]['embedding_models']=['embed:fixed']
        profiles=json.loads(env['RAG_EMBEDDING_PROFILES_JSON']); profiles[0].update(model='embed:fixed',revision='sha256:'+'a'*64)
        env.update(RAG_PROVIDERS_JSON=json.dumps(providers),RAG_EMBEDDING_PROFILES_JSON=json.dumps(profiles))
        registry=ProviderRegistry(load_rag_settings(env))
        def respond(request):
            if request.url.path=='/api/tags':
                return httpx.Response(200,json={'models':[{'name':'embed:fixed','digest':'b'*64}]})
            self.fail('must not embed changed model')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with self.assertRaisesRegex(RagError,'embedding_revision_changed'):
                await ModelClient(registry,http).embed(registry.resolve_profile('p'),['hello'])

    async def test_digest_change_during_embedding_preserves_safe_code(self):
        registry=ProviderRegistry(load_rag_settings(environment('ollama')))
        tag_calls=0
        def respond(request):
            nonlocal tag_calls
            if request.url.path=='/api/tags':
                tag_calls+=1
                return httpx.Response(200,json={'models':[{'name':'embed:fixed','digest':('a' if tag_calls==1 else 'b')*64}]})
            return httpx.Response(200,json={'embeddings':[[1.,0.]]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with self.assertRaisesRegex(RagError,'embedding_revision_changed'):
                await ModelClient(registry,http).embed(registry.resolve_profile('p'),['hello'])
