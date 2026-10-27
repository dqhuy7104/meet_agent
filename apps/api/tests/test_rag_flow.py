import json
import os
import unittest
from test_rag_worker import WorkerTest
from test_rag_outbox import complete_meeting
from app.core.rag_config import ProviderRegistry
from app.schemas.chat import RagQuery
from app.services.rag.query import QueryService
from app.services.rag.worker import synchronize_once


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

    async def test_same_id_new_model_cannot_query_old_vectors(self):
        from dataclasses import replace
        from app.core.rag_config import RagError
        complete_meeting(self.meetings,'a')
        await synchronize_once(self.meetings,self.repository,self.client,self.settings)
        changed=self.settings.profiles[0].model_copy(update={'model':'changed','revision':'v2'})
        settings=replace(self.settings,profiles=(changed,))
        class Database:
            async def call(inner,method,*args):return getattr(self.repository,method)(*args)
        async def generate(*args):return '{"status":"insufficient","claims":[]}'
        self.client.generate=generate
        service=QueryService(settings,ProviderRegistry(settings),self.meetings,Database(),self.client)
        request=RagQuery(question='kế hoạch',k=1,chat_provider_id='local',chat_model_id='chat',embedding_profile_id='p')
        with self.assertRaisesRegex(RagError,'immutable_profile_changed'):
            await service.query(request)
        self.assertEqual((await service.catalog()).embedding_profiles,[])

class ScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_scope_skips_embedding_and_storage(self):
        from test_rag_config import environment
        from app.core.rag_config import load_rag_settings
        settings = load_rag_settings(environment())
        class Scope:
            def meeting_ids(self, project_id=None): return []
            def can_read(self, meeting_id): return False
        service = QueryService(settings, ProviderRegistry(settings), None, None, None, Scope())
        request = RagQuery(question='private', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')
        self.assertEqual((await service.query(request)).retrieval_status, 'insufficient_evidence')

    async def test_hidden_summary_rejected_before_read(self):
        from test_rag_config import environment
        from app.core.rag_config import load_rag_settings, RagError
        settings = load_rag_settings(environment())
        class Scope:
            def can_read(self, meeting_id): return False
        service = QueryService(settings, ProviderRegistry(settings), None, None, None, Scope())
        with self.assertRaisesRegex(RagError, 'meeting_not_found'):
            await service.summarize('hidden')

class ScopedQueryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from types import SimpleNamespace
        from test_rag_config import environment
        from app.core.rag_config import load_rag_settings
        from app.services.rag.chunking import snapshot_meeting, chunk_transcript
        from app.services.rag.ranking import Candidate
        self.settings = load_rag_settings(environment())
        self.details = {key: SimpleNamespace(meeting=SimpleNamespace(id=key, status='completed', title=key),
            transcript_segments=[SimpleNamespace(text='kế hoạch', speaker='S1', start=0, end=1)]) for key in ['a', 'b']}
        self.chunks = [chunk_transcript(snapshot_meeting(detail))[0] for detail in self.details.values()]
        self.allowed = ['a', 'b']
        self.searched = None
        self.revoke_at = None
        owner = self
        class Scope:
            def meeting_ids(inner, project_id=None):
                if project_id == 'hidden':
                    from app.core.rag_config import RagError
                    raise RagError('project_not_found', 404)
                return [key for key in owner.allowed if project_id is None or key == project_id]
            def can_read(inner, meeting_id): return meeting_id in owner.allowed
        class Meetings:
            def get_detail(inner, meeting_id): return owner.details.get(meeting_id)
        class Database:
            async def call(inner, method, *args):
                if method == 'manifest':
                    return snapshot_meeting(owner.details[args[1]]).revision if args[1] == 'a' else None
                if method == 'search':
                    owner.searched = args[-1]
                    if owner.revoke_at == 'search': owner.allowed = []
                    return [Candidate(chunk, 1.) for chunk in owner.chunks], []
                if method == 'is_profile_ready':
                    raise AssertionError('Global readiness must not affect a scoped query')
        class Client:
            async def embed(inner, *args): return [[1., 0.]]
            async def generate(inner, selection, messages):
                sources = json.loads(messages[1]['content'])['sources']
                if owner.revoke_at == 'generate': owner.allowed = []
                return json.dumps({'status':'answered', 'claims':[{'text':'plan', 'citation_ids':[sources[0]['source_id']]}]})
        self.service = QueryService(self.settings, ProviderRegistry(self.settings), Meetings(), Database(), Client(), Scope())
        self.request = RagQuery(question='kế hoạch', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p', project_id='a')

    async def test_selected_project_ignores_unready_other_project(self):
        answer = await self.service.query(self.request)
        self.assertEqual(self.searched, ['a'])
        self.assertEqual([citation.meeting_id for citation in answer.citations], ['a'])

    async def test_client_meeting_filter_only_narrows_access(self):
        answer = await self.service.query(self.request.model_copy(update={'meeting_ids':['b', 'hidden']}))
        self.assertEqual(answer.retrieval_status, 'insufficient_evidence')
        self.assertIsNone(self.searched)

    async def test_invisible_project_fails_safely(self):
        from app.core.rag_config import RagError
        with self.assertRaisesRegex(RagError, 'project_not_found'):
            await self.service.query(self.request.model_copy(update={'project_id':'hidden'}))
        self.assertIsNone(self.searched)

    async def test_revocation_before_provider_and_after_generation(self):
        for stage in ['search', 'generate']:
            self.allowed = ['a']
            self.revoke_at = stage
            answer = await self.service.query(self.request)
            self.assertEqual(answer.retrieval_status, 'insufficient_evidence')
            self.assertEqual(answer.citations, [])
            self.assertEqual(answer.selected_meetings, [])

    async def test_evaluation_revisions_are_scoped(self):
        self.allowed = ['a']
        state = await self.service.evaluation_state('p', 'local', 'chat')
        self.assertEqual(list(state['meeting_revisions']), ['a'])

    async def test_default_query_uses_only_visible_meetings(self):
        self.allowed = ['a']
        answer = await self.service.query(self.request.model_copy(update={'project_id':None}))
        self.assertEqual(self.searched, ['a'])
        self.assertEqual([citation.meeting_id for citation in answer.citations], ['a'])

    async def test_summary_revocation_after_generation_drops_result(self):
        from app.core.rag_config import RagError
        async def generate(*args):
            self.allowed = []
            return '{"summary":"private result"}'
        self.service.client.generate = generate
        with self.assertRaisesRegex(RagError, 'meeting_not_found'):
            await self.service.summarize('a')

    async def test_summary_stops_before_second_dispatch_when_source_changes(self):
        from app.core.rag_config import RagError
        for change in ['cancelled', 'revision']:
            with self.subTest(change=change):
                self.details['a'].meeting.status = 'completed'
                self.details['a'].transcript_segments[0].text = 'Nội dung ' * 5000
                calls = []
                async def generate(*args):
                    calls.append(1)
                    if change == 'cancelled':
                        self.details['a'].meeting.status = 'cancelled'
                    else:
                        self.details['a'].transcript_segments[0].text = 'changed transcript'
                    return '{"summary":"partial summary"}'
                self.service.client.generate = generate
                with self.assertRaisesRegex(RagError, 'meeting_changed'):
                    await self.service.summarize('a')
                self.assertEqual(len(calls), 1)
