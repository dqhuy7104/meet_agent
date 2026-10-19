import json
import os
import unittest
from test_rag_worker import WorkerTest
from test_rag_outbox import complete_meeting
from app.core.rag_config import ProviderRegistry
from app.schemas.chat import RagQuery
from app.services.rag.query import QueryService
from app.services.rag.worker import synchronize_once


@unittest.skipUnless(os.environ.get('RAG_TEST_DATABASE_URL'), 'PostgreSQL unavailable')
class FlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        WorkerTest.setUp(self)
    def tearDown(self):
        WorkerTest.tearDown(self)

    async def test_index_query_and_cancel_before_purge(self):
        complete_meeting(self.meetings, 'a')
        complete_meeting(self.meetings, 'b')
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        class Database:
            async def call(inner, method, *args):
                return getattr(self.repository, method)(*args)
        async def generate(selection, messages):
            sources=json.loads(messages[1]['content'])['sources']
            return json.dumps({'status':'answered','claims':[{'text':'Kế hoạch', 'citation_ids':[sources[0]['source_id']]}]})
        self.client.generate=generate
        service=QueryService(self.settings, ProviderRegistry(self.settings), self.meetings, Database(), self.client)
        request=RagQuery(question='kế hoạch', k=1, chat_provider_id='local',chat_model_id='chat',embedding_profile_id='p')
        answer=await service.query(request)
        self.assertEqual(answer.retrieval_status, 'answered')
        self.assertEqual(len(answer.selected_meetings), 1)
        deleted=answer.citations[0].meeting_id
        self.meetings.cancel(deleted)
        answer=await service.query(request)
        self.assertTrue(all(c.meeting_id != deleted for c in answer.citations))
        empty=await service.query(request.model_copy(update={'meeting_ids':[]}))
        self.assertEqual(empty.retrieval_status, 'insufficient_evidence')
