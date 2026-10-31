import json
from unittest.mock import patch
from uuid import uuid4

import httpx

from apps.api.tests.postgres_support import PostgresAsyncTestCase, seed_snapshot
from app.api.deps import get_conversation_service
from app.core.rag_config import load_rag_settings
from app.main import app
from app.services.access.repository import AccessRepository
from app.services.access.sessions import COOKIE_NAME, SessionRepository
from app.services.meetings.repository import MeetingRepository
from app.services.rag.chunking import make_snapshot, chunk_transcript
from app.services.rag.repository import RagRepository
from app.services.rag.request import RagRequestService
from app.services.rag.types import SourceSegment
from test_rag_config import environment


class ConversationRoutesTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        self.access = AccessRepository(self.connection)
        self.owner = self.access.create_account('owner@example.test','unusable')
        self.other = self.access.create_account('other@example.test','unusable')
        self.admin = self.access.create_account('admin@example.test','unusable')
        self.connection.execute('UPDATE accounts SET is_system_admin=true WHERE id=%s',(self.admin.id,))
        self.settings = load_rag_settings(environment())
        app.dependency_overrides[get_conversation_service] = lambda: RagRequestService(self.settings,self.database)
        self.snapshot = seed_snapshot(self.connection,make_snapshot('meeting','Private meeting',(
            SourceSegment(0,'Lào hợp tác giáo dục. Nhật Bản hợp tác về nguồn vốn và nhân lực.','A',0,20),)))
        self.project_id = MeetingRepository(self.connection).get_meeting('meeting').project_id
        self.access.set_membership(self.project_id,self.owner.id,'viewer')
        RagRepository(self.connection).publish(self.snapshot,self.settings.profiles[0],chunk_transcript(self.snapshot),[[1.,0.]])
        self.calls = []
        self.embedded = []
        owner = self
        async def embed(client, profile, texts):
            owner.embedded.extend(texts)
            return [[1.,0.] for text in texts]
        async def generate(client, selection, messages):
            owner.calls.append(messages)
            data = json.loads(messages[1]['content'])
            if 'chat_routing_v1' in messages[0]['content']:
                return json.dumps({"intent":"retrieval","question":data["question"] if not data["history"] else "Nhật Bản đề xuất hợp tác gì?"})
            if 'source_ids' in messages[0]['content']:
                return json.dumps({'source_ids':[s['source_id'] for s in data['sources']]})
            if not data['sources']:
                return json.dumps({'parts':[{'kind':'conversation','text':'Chưa đủ bằng chứng.','citation_ids':[]}]})
            return json.dumps({'parts':[{'kind':'meeting','text':'Hợp tác về giáo dục và nhân lực.','citation_ids':[data['sources'][0]['source_id']]}]})
        for target, replacement in [('embed',embed),('generate',generate)]:
            context = patch('app.services.rag.providers.ModelClient.'+target,replacement)
            context.start(); self.addCleanup(context.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',1)),base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.login(self.owner)

    def login(self, account):
        self.client.cookies.clear()
        token, csrf = SessionRepository(self.connection).create(account.id)
        self.client.cookies.set(COOKIE_NAME,token)
        self.client.headers['X-CSRF-Token'] = csrf

    async def create(self):
        response = await self.client.post('/api/chat/conversations',json={'project_id':self.project_id})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()['id']

    def turn(self, question='Lào hợp tác gì?', ordinal=0):
        return {'question':question,'request_id':str(uuid4()),'expected_last_ordinal':ordinal,
                'config':{'k':1,'chat_provider_id':'local','chat_model_id':'chat','embedding_profile_id':'p','context_weight':.8}}

    async def test_persistence_followup_and_replay_do_not_regenerate(self):
        conversation = await self.create()
        request = self.turn()
        first = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['status'],'completed')
        self.assertEqual(len(self.calls),3)
        replay = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(replay.json()['id'],first.json()['id'])
        self.assertEqual(len(self.calls),3)
        second = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Còn Nhật Bản?',1))
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(len(self.calls),6)
        self.assertEqual(self.embedded[-1],'Nhật Bản đề xuất hợp tác gì?')
        detail = await self.client.get(f'/api/chat/conversations/{conversation}')
        self.assertEqual(len(detail.json()['turns']),2)
        self.assertEqual(detail.json()['turns'][1]['question'],'Còn Nhật Bản?')
        self.assertEqual(detail.headers['cache-control'],'no-store')
        self.assertEqual(len(self.calls),6)

    async def test_creator_admin_other_and_csrf_boundaries(self):
        conversation = await self.create()
        self.login(self.other)
        self.assertEqual((await self.client.get(f'/api/chat/conversations/{conversation}')).status_code,404)
        self.assertEqual((await self.client.delete(f'/api/chat/conversations/{conversation}')).status_code,404)
        self.assertEqual((await self.client.get('/api/chat/conversations?view=others')).status_code,403)
        self.login(self.admin)
        detail = await self.client.get(f'/api/chat/conversations/{conversation}')
        self.assertTrue(detail.json()['conversation']['read_only'])
        self.assertEqual((await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())).status_code,403)
        self.assertEqual((await self.client.delete(f'/api/chat/conversations/{conversation}')).status_code,403)
        self.assertEqual(len((await self.client.get('/api/chat/conversations?view=others')).json()['items']),1)
        self.login(self.owner)
        self.assertEqual((await self.client.delete(f'/api/chat/conversations/{conversation}',headers={'X-CSRF-Token':'bad'})).status_code,403)
        self.assertEqual((await self.client.delete(f'/api/chat/conversations/{conversation}')).status_code,204)
        self.assertEqual((await self.client.get(f'/api/chat/conversations/{conversation}')).status_code,404)

    async def test_revoked_history_is_redacted_and_not_sent_to_resolver(self):
        conversation = await self.create()
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.access.remove_membership(self.project_id,self.owner.id)
        detail = (await self.client.get(f'/api/chat/conversations/{conversation}')).json()
        self.assertEqual(detail['turns'][0]['status'],'redacted')
        self.assertIsNone(detail['turns'][0]['question'])
        self.assertIsNone(detail['turns'][0]['answer'])
        self.assertEqual(detail['conversation']['title'],'Hội thoại')
        calls = len(self.calls)
        self.assertEqual((await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Còn Nhật?',1))).status_code,404)
        self.assertEqual(len(self.calls),calls)
        self.login(self.admin)
        self.assertEqual((await self.client.get(f'/api/chat/conversations/{conversation}')).json()['turns'][0]['status'],'completed')

    async def test_reprocessed_source_is_historical_and_has_no_context_reuse(self):
        conversation = await self.create()
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.connection.execute("UPDATE meetings SET title='New title' WHERE id='meeting'")
        detail = (await self.client.get(f'/api/chat/conversations/{conversation}')).json()
        self.assertEqual(detail['turns'][0]['status'],'stale')
        self.assertIsNotNone(detail['turns'][0]['answer'])

    async def test_history_and_completed_replay_work_with_rag_disabled(self):
        from app.core.rag_config import RagSettings
        conversation = await self.create()
        request = self.turn()
        first = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.settings = RagSettings()
        self.assertEqual((await self.client.get(f'/api/chat/conversations/{conversation}')).status_code,200)
        replay = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(replay.status_code,200,replay.text)
        self.assertEqual(replay.json()['id'],first.json()['id'])
        self.assertEqual((await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('New',1))).status_code,503)
        self.assertEqual(len(self.calls),3)

    async def test_revocation_during_generation_does_not_persist_sensitive_output(self):
        conversation = await self.create()
        original = self.access
        async def generate(client, selection, messages):
            data = json.loads(messages[1]['content'])
            if 'chat_routing_v1' in messages[0]['content']:
                return json.dumps({'intent':'retrieval','question':data['question']})
            if 'source_ids' in messages[0]['content']:
                return json.dumps({'source_ids':[s['source_id'] for s in data['sources']]})
            original.remove_membership(self.project_id,self.owner.id)
            return json.dumps({'parts':[{'kind':'meeting','text':'Sensitive output','citation_ids':[data['sources'][0]['source_id']]}]})
        with patch('app.services.rag.providers.ModelClient.generate',generate):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.assertEqual(response.status_code,404,response.text)
        row = self.connection.execute('SELECT answer,status FROM rag_conversation_turns WHERE conversation_id=%s',(conversation,)).fetchone()
        self.assertEqual(row,(None,'failed'))

    async def test_deleted_sources_redact_admin_view_and_keep_dependency_records(self):
        conversation = await self.create()
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.connection.execute("DELETE FROM meetings WHERE id='meeting'")
        self.login(self.admin)
        detail = (await self.client.get(f'/api/chat/conversations/{conversation}')).json()
        self.assertEqual(detail['turns'][0]['status'],'redacted')
        self.assertIsNone(detail['turns'][0]['answer'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_turn_sources').fetchone()[0],1)

    async def test_history_dependencies_are_transitive_and_hidden_context_is_not_reused(self):
        from app.schemas.chat import RagAnswer
        conversation = (await self.client.post('/api/chat/conversations',json={'project_id':None})).json()['id']
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        async def no_new_sources(service, request, record_sources=None, token_budget=None):
            return [], []
        with patch('app.services.rag.query.QueryService.retrieve',no_new_sources):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Còn Nhật?',1))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_turn_sources').fetchone()[0],2)
        self.access.remove_membership(self.project_id,self.owner.id)
        detail = (await self.client.get(f'/api/chat/conversations/{conversation}')).json()
        self.assertEqual([turn['status'] for turn in detail['turns']],['redacted','redacted'])
        async def clarify(client, selection, messages):
            self.assertEqual(json.loads(messages[1]['content'])['history'],[])
            return '{"intent":"clarification","question":null}'
        with patch('app.services.rag.providers.ModelClient.generate',clarify):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Còn việc đó?',2))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['answer']['citations'],[])

    async def test_provider_failure_is_recorded_and_replay_has_no_model_calls(self):
        from app.core.rag_config import RagError
        conversation = await self.create()
        request = self.turn()
        async def fail(*args): raise RagError('provider_timeout',504)
        with patch('app.services.rag.providers.ModelClient.generate',fail):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(response.status_code,504)
        replay = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(replay.json()['status'],'failed')
        self.assertEqual(replay.json()['failure_code'],'provider_timeout')
        self.assertEqual(self.calls,[])

    async def test_pagination_and_empty_scope_update(self):
        conversation = await self.create()
        response = await self.client.patch(f'/api/chat/conversations/{conversation}',json={'project_id':None})
        self.assertIsNone(response.json()['project_id'])
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.assertEqual((await self.client.patch(f'/api/chat/conversations/{conversation}',json={'project_id':None})).status_code,409)
        for _ in range(21):
            await self.client.post('/api/chat/conversations',json={'project_id':None})
        first = (await self.client.get('/api/chat/conversations')).json()
        self.assertEqual(len(first['items']),20)
        second = (await self.client.get('/api/chat/conversations',params={'cursor':first['next_cursor']})).json()
        self.assertEqual(len(second['items']),2)
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        self.assertEqual((await self.client.get('/api/chat/conversations?cursor=invalid')).status_code,422)

    async def test_scope_changed_before_claim_is_used_for_retrieval(self):
        from app.services.conversations.repository import ConversationRepository
        from app.services.rag.query import QueryService
        conversation = await self.create()
        original_claim = ConversationRepository.claim
        original_query = QueryService.retrieve
        scopes = []
        def changed_scope(repository, conversation_id, request):
            repository.set_scope(conversation_id,None)
            return original_claim(repository,conversation_id,request)
        async def query(service, request, record_sources=None, token_budget=None):
            scopes.append(request.project_id)
            return await original_query(service,request,record_sources=record_sources,token_budget=token_budget)
        with patch.object(ConversationRepository,'claim',changed_scope),patch.object(QueryService,'retrieve',query):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(scopes,[None])

    async def test_deleted_conversation_is_not_resurrected_after_generation(self):
        conversation = await self.create()
        async def generate(client, selection, messages):
            self.connection.execute('DELETE FROM rag_conversations WHERE id=%s',(conversation,))
            return '{"source_ids":[]}'
        with patch('app.services.rag.providers.ModelClient.generate',generate):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_conversation_turns').fetchone()[0],0)

    async def test_history_pages_are_chronological_without_duplicates(self):
        from app.schemas.conversations import TurnRequest
        from app.services.conversations.repository import ConversationRepository
        conversation = await self.create()
        repository = ConversationRepository(self.connection)
        for ordinal in range(51):
            turn, _ = repository.claim(conversation,TurnRequest.model_validate(self.turn(ordinal=ordinal)))
            repository.fail(turn['id'],turn['claim_id'],'provider_timeout')
        first = (await self.client.get(f'/api/chat/conversations/{conversation}')).json()
        self.assertEqual([turn['ordinal'] for turn in first['turns']],list(range(2,52)))
        second = (await self.client.get(f'/api/chat/conversations/{conversation}',params={'before_ordinal':first['next_before_ordinal']})).json()
        self.assertEqual([turn['ordinal'] for turn in second['turns']],[1])
        self.assertIsNone(second['next_before_ordinal'])

    async def test_evaluator_reports_the_selected_similarity_weight(self):
        from app.api.deps import get_rag_service
        app.dependency_overrides[get_rag_service] = lambda: RagRequestService(self.settings,self.database)
        params = {'embedding_profile_id':'p','chat_provider_id':'local','chat_model_id':'chat','context_weight':.25}
        response = await self.client.get('/api/chat/evaluation-state',params=params)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['ranking_weights'],{'cosine':.25,'keyword':.75})
        params['context_weight'] = 'nan'
        self.assertEqual((await self.client.get('/api/chat/evaluation-state',params=params)).status_code,422)

    async def test_greeting_does_not_embed_or_retrieve(self):
        conversation = await self.create()
        async def generate(client, selection, messages):
            if 'chat_routing_v1' in messages[0]['content']:
                return '{"intent":"conversation","question":null}'
            return '{"parts":[{"kind":"conversation","text":"Xin chào!","citation_ids":[]}]}'
        with patch('app.services.rag.providers.ModelClient.generate',generate),patch('app.services.rag.query.QueryService.retrieve',side_effect=AssertionError('unexpected retrieval')):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Xin chào'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['answer']['answer'],'Xin chào!')
        self.assertEqual(self.embedded,[])

    async def test_model_opt_in_still_retrieves_first_and_mode_off_removes_model_history(self):
        conversation = await self.create()
        async def generate(client, selection, messages):
            data = json.loads(messages[1]['content'])
            if 'chat_routing_v1' in messages[0]['content']:
                return json.dumps({'intent':'retrieval','question':data['question']})
            if 'source_ids' in messages[0]['content']:
                return json.dumps({'source_ids':[source['source_id'] for source in data['sources']]})
            self.assertTrue(data['sources'])
            parts = [{'kind':'meeting','text':'Giáo dục.','citation_ids':[data['sources'][0]['source_id']]}]
            if data['allow_model_knowledge']:
                parts.append({'kind':'model','text':'General model advice','citation_ids':[]})
            else:
                self.assertNotIn('General model advice',json.dumps(data['history']))
            return json.dumps({'parts':parts})
        request = self.turn()
        request['config']['allow_model_knowledge'] = True
        with patch('app.services.rag.providers.ModelClient.generate',generate):
            first = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
            self.assertEqual(first.status_code,200,first.text)
            second = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Tiếp tục',1))
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(len(self.embedded),2)
        self.assertEqual(len(first.json()['answer']['citations']),1)

    async def test_transform_reloads_original_evidence_without_retrieval(self):
        conversation = await self.create()
        first = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        async def generate(client, selection, messages):
            data = json.loads(messages[1]['content'])
            if 'chat_routing_v1' in messages[0]['content']:
                return json.dumps({'intent':'transform','question':'Viết ngắn lại câu trả lời trước','target_turn':1})
            self.assertEqual(data['sources'][0]['text'],chunk_transcript(self.snapshot)[0].text)
            return json.dumps({'parts':[{'kind':'meeting','text':'Giáo dục và nhân lực.','citation_ids':[data['sources'][0]['source_id']]}]})
        with patch('app.services.rag.providers.ModelClient.generate',generate),patch('app.services.rag.query.QueryService.retrieve',side_effect=AssertionError('unexpected retrieval')):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Ngắn lại',1))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['answer']['citations'],first.json()['answer']['citations'])

    async def test_enabled_model_does_not_hide_index_failure(self):
        conversation = await self.create()
        request = self.turn()
        request['config']['allow_model_knowledge'] = True
        self.connection.execute('DELETE FROM rag_chunks')
        self.connection.execute('DELETE FROM rag_publications')
        response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(len(self.calls),1)

    async def test_transform_can_target_an_earlier_answer(self):
        conversation = await self.create()
        await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn())
        async def greeting(client, selection, messages):
            if 'chat_routing_v1' in messages[0]['content']:
                return '{"intent":"conversation","question":null}'
            return '{"parts":[{"kind":"conversation","text":"Rất vui được hỗ trợ!","citation_ids":[]}]}'
        with patch('app.services.rag.providers.ModelClient.generate',greeting):
            await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Cảm ơn',1))
        async def transform(client, selection, messages):
            if 'chat_routing_v1' in messages[0]['content']:
                return '{"intent":"transform","question":"Viết lại câu trả lời đầu tiên","target_turn":1}'
            sources = json.loads(messages[1]['content'])['sources']
            self.assertEqual(len(sources),1)
            return json.dumps({'parts':[{'kind':'meeting','text':'Giáo dục.','citation_ids':[sources[0]['source_id']]}]})
        with patch('app.services.rag.providers.ModelClient.generate',transform):
            response = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=self.turn('Viết lại câu trả lời đầu',2))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['answer']['citations']),1)

    async def test_legacy_false_config_replay_is_idempotent(self):
        conversation = await self.create()
        request = self.turn()
        first = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.connection.execute("UPDATE rag_conversation_turns SET config=config-'allow_model_knowledge'")
        replay = await self.client.post(f'/api/chat/conversations/{conversation}/turns',json=request)
        self.assertEqual(replay.status_code,200,replay.text)
        self.assertEqual(first.json()['id'],replay.json()['id'])
