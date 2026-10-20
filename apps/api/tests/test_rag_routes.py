import unittest
import httpx
from fastapi import FastAPI
from app.api.routes.chat import router
from app.api.deps import get_rag_service
from app.core.rag_config import load_rag_settings, ProviderRegistry, RagError
from test_rag_config import environment


class RoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_boundary_validation_and_safe_failures(self):
        settings = load_rag_settings(environment())
        class Service:
            error = None
            registry = ProviderRegistry(settings)
            async def catalog(inner):
                return inner.registry.catalog(['p'])
            async def query(inner, request):
                inner.registry.resolve_chat(request.chat_provider_id, request.chat_model_id)
                if request.k > settings.max_k:
                    raise RagError('invalid_k', 422)
                raise inner.error or RagError('profile_not_ready', 409)
        service = Service()
        app = FastAPI()
        app.include_router(router, prefix='/api')
        app.dependency_overrides[get_rag_service] = lambda: service
        body = dict(question='private-question', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',123)), base_url='http://localhost') as client:
            self.assertEqual((await client.get('/api/chat/providers')).status_code, 200)
            for change in ({'k':True}, {'k':11}, {'chat_model_id':'unknown'}, {'question':None}, {'question':23}):
                response = await client.post('/api/chat/queries', json=body | change)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn('private-question', response.text)
            self.assertEqual((await client.post('/api/chat/queries', json=body)).status_code, 409)
            for error in (RagError('provider_timeout',504), RagError('provider_unavailable',503)):
                service.error=error
                self.assertEqual((await client.post('/api/chat/queries', json=body)).status_code, error.status)
            self.assertEqual((await client.post('/api/chat/queries', json=body, headers={'Origin':'https://evil.example'})).status_code, 403)
            self.assertEqual((await client.get('/api/chat/providers', headers={'Host':'evil.example'})).status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('10.0.0.1',123)), base_url='http://localhost') as client:
            self.assertEqual((await client.get('/api/chat/providers')).status_code, 403)
