import json
import unittest

from app.core.rag_config import RagError, ProviderRegistry, load_rag_settings
from app.schemas.chat import ConversationConfig
from app.services.conversations.chatbot import route_message, generate_chat_answer
from test_rag_config import environment


class ChatbotTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local','chat')

    async def test_routing_validates_intent_and_question(self):
        class Client:
            async def generate(self, selection, messages):
                return '{"intent":"conversation","question":null}'
        decision = await route_message('Xin chào',[],self.selection,Client())
        self.assertEqual(decision.intent,'conversation')

    async def test_model_knowledge_requires_opt_in(self):
        class Client:
            async def generate(self, selection, messages):
                return json.dumps({'parts':[{'kind':'model','text':'General advice','citation_ids':[]}]})
        with self.assertRaisesRegex(RagError,'invalid_chat_response'):
            await generate_chat_answer('Advice',[],[],False,self.selection,Client())
        answer = await generate_chat_answer('Advice',[],[],True,self.selection,Client())
        self.assertEqual(answer.answer,'General advice')
        self.assertEqual(answer.parts[0].kind,'model')
        self.assertEqual(answer.citations,[])

    def test_knowledge_defaults_off(self):
        config = ConversationConfig(k=1,chat_provider_id='p',chat_model_id='m',embedding_profile_id='e')
        self.assertFalse(config.allow_model_knowledge)

    async def test_forged_or_missing_citations_are_rejected(self):
        class Client:
            async def generate(self, selection, messages):
                return json.dumps({'parts':[{'kind':'meeting','text':'Invented','citation_ids':['forged']}]})
        with self.assertRaisesRegex(RagError,'invalid_chat_response'):
            await generate_chat_answer('Question',[],[],True,self.selection,Client())

    async def test_malformed_or_missing_route_question_fails(self):
        class Client:
            raw = ''
            async def generate(self, selection, messages): return self.raw
        client = Client()
        for raw in ['{}','not json','{"intent":"retrieval","question":null}']:
            client.raw = raw
            with self.assertRaisesRegex(RagError,'invalid_chat_route'):
                await route_message('Question',[],self.selection,client)

    async def test_transform_requires_valid_history_target(self):
        class Client:
            async def generate(self, selection, messages):
                return '{"intent":"transform","question":"Rewrite first","target_turn":2}'
        with self.assertRaisesRegex(RagError,'invalid_chat_route'):
            await route_message('Rewrite first',[{'question':'Q','answer':'A'}],self.selection,Client())

    def test_chat_budget_accounts_for_history(self):
        from app.services.conversations.chatbot import source_budget
        empty = source_budget('Question',[],False,self.selection)
        with_history = source_budget('Question',[{'question':'Q','answer':'A'*100}],False,self.selection)
        self.assertLess(with_history,empty)
