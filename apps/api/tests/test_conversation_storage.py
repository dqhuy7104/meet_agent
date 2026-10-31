from uuid import uuid4
import unittest

from apps.api.tests.postgres_support import PostgresTestCase
from app.schemas.conversations import ConversationConfig, TurnRequest
from app.services.conversations.repository import ConversationRepository
from app.core.rag_config import RagError


class ConversationStorageTest(PostgresTestCase):
    def setUp(self):
        super().setUp()
        from app.services.access.repository import AccessRepository
        self.owner = AccessRepository(self.connection).create_account('owner@example.test', 'unusable')
        self.repository = ConversationRepository(self.connection)
        self.conversation = self.repository.create(self.owner.id, None)
        self.config = ConversationConfig(k=3, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')

    def request(self, question='Question', ordinal=0, request_id=None):
        return TurnRequest(question=question, request_id=request_id or uuid4(), expected_last_ordinal=ordinal, config=self.config)

    def test_claim_replay_conflicts_and_delete_cascade(self):
        request = self.request()
        turn, claimed = self.repository.claim(self.conversation['id'], request)
        self.assertTrue(claimed)
        replay, claimed = self.repository.claim(self.conversation['id'], request)
        self.assertFalse(claimed)
        self.assertEqual(replay['id'], turn['id'])
        with self.assertRaises(RagError):
            self.repository.claim(self.conversation['id'], self.request(ordinal=1))
        self.repository.add_dependencies(turn['id'], [('source','project','revision')])
        self.assertTrue(self.repository.finish(turn['id'], turn['claim_id'], {'answer':'Saved'}, 'Standalone', {'version':'v1'}))
        replay, claimed = self.repository.claim(self.conversation['id'], request)
        self.assertFalse(claimed)
        self.assertEqual(replay['answer']['answer'], 'Saved')
        with self.assertRaisesRegex(RagError, 'idempotency_conflict'):
            self.repository.claim(self.conversation['id'], self.request('Different', request_id=request.request_id))
        self.repository.delete(self.conversation['id'])
        for table in ['rag_conversation_turns','rag_turn_sources']:
            self.assertEqual(self.connection.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)

    def test_expired_claim_cannot_overwrite_new_turn(self):
        turn, _ = self.repository.claim(self.conversation['id'], self.request())
        self.connection.execute("UPDATE rag_conversation_turns SET lease_until=now()-interval '1 second'")
        next_turn, claimed = self.repository.claim(self.conversation['id'], self.request(ordinal=1))
        self.assertTrue(claimed)
        self.assertEqual(next_turn['ordinal'], 2)
        self.assertFalse(self.repository.finish(turn['id'], turn['claim_id'], {'answer':'Late'}, 'Late', {}))
        self.assertEqual(self.repository.get_turn(turn['id'])['failure_code'], 'turn_interrupted')

    def test_empty_scope_can_change_but_locks_after_claim(self):
        self.repository.set_scope(self.conversation['id'], 'project')
        self.assertEqual(self.repository.get(self.conversation['id'])['project_id'], 'project')
        self.repository.claim(self.conversation['id'], self.request())
        with self.assertRaisesRegex(RagError, 'scope_locked'):
            self.repository.set_scope(self.conversation['id'], None)

    def test_final_check_failure_rolls_back_answer(self):
        turn, _ = self.repository.claim(self.conversation['id'],self.request())
        def expired(): raise RagError('query_timeout',504)
        with self.assertRaisesRegex(RagError,'query_timeout'):
            self.repository.finish(turn['id'],turn['claim_id'],{'answer':'Too late'},'Question',{},before_commit=expired)
        saved = self.repository.get_turn(turn['id'])
        self.assertIsNone(saved['answer'])
        self.assertEqual(saved['status'],'running')

    def test_simultaneous_claims_use_one_active_turn(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import psycopg
        barrier = threading.Barrier(2)
        def claim():
            with psycopg.connect(self.database.database_url.get_secret_value(),autocommit=True) as connection:
                barrier.wait()
                try:
                    return ConversationRepository(connection).claim(self.conversation['id'],self.request())[0]['status']
                except RagError as error:
                    return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _:claim(),range(2)))
        self.assertEqual(sorted(results),['running','turn_generating'])


class ConversationInputTest(unittest.TestCase):
    def test_weight_is_finite_numeric_and_bounded(self):
        from pydantic import ValidationError
        data = dict(k=1,chat_provider_id='p',chat_model_id='m',embedding_profile_id='e')
        for weight in [0,.5,1]:
            self.assertEqual(ConversationConfig(**data,context_weight=weight).context_weight, weight)
        for weight in [-.1,1.1,True,float('nan'),float('inf'),'0.8']:
            with self.subTest(weight=weight), self.assertRaises(ValidationError):
                ConversationConfig(**data,context_weight=weight)
