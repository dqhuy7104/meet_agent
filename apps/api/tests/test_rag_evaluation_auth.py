"""Evaluator session flow against real auth dependencies and isolated PostgreSQL."""
import asyncio
import unittest

import httpx

from apps.api.tests.postgres_support import PostgresTestCase
from ai_training.evaluation.evaluate_meeting_rag import authenticated_session


class EvaluationAuthTest(PostgresTestCase):
    def test_login_csrf_query_logout_and_scoped_status(self):
        from app.main import app
        from app.api.auth import get_session_repository
        from app.api.deps import get_rag_service
        from app.services.access.repository import AccessRepository
        from app.services.access.passwords import hash_password
        from app.services.access.sessions import SessionRepository
        from app.schemas.chat import RagAnswer, IndexStatus
        repository = AccessRepository(self.connection)
        account = repository.create_account('evaluation@example.test', hash_password('evaluation-password'))
        class Service:
            async def query(inner, request, account_id):
                self.assertEqual(account_id, account.id)
                return RagAnswer(answer='No evidence', retrieval_status='insufficient_evidence', prompt_version='meeting_qa_v1')
            async def index_status(inner, account_id, project_id):
                self.assertEqual(account_id, account.id)
                return IndexStatus(profiles=[])
        app.dependency_overrides[get_session_repository] = lambda: SessionRepository(self.connection)
        app.dependency_overrides[get_rag_service] = lambda: Service()
        self.addCleanup(app.dependency_overrides.clear)

        async def dispatch(request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',123)),
                                         base_url='http://localhost') as api:
                result = await api.request(request.method, str(request.url), headers=request.headers,
                                           content=request.content)
                return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(
                lambda request: asyncio.run(dispatch(request)))) as client:
            self.assertEqual(client.get('/api/chat/index-status').status_code, 401)
            with authenticated_session(client, account.email, 'evaluation-password'):
                self.assertEqual(client.get('/api/chat/index-status').json(), {'profiles':[]})
                body = dict(question='Question', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')
                self.assertEqual(client.post('/api/chat/queries', json=body, headers={'X-CSRF-Token':'wrong'}).status_code, 403)
                self.assertEqual(client.post('/api/chat/queries', json=body).status_code, 200)
            self.assertEqual(client.get('/api/chat/index-status').status_code, 401)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM auth_sessions').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
