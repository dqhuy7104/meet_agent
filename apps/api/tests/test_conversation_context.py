import json
import unittest

from app.core.rag_config import ProviderRegistry, RagError, load_rag_settings
from app.services.conversations.context import resolve_question, select_history
from test_rag_config import environment


class ConversationContextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.selection = ProviderRegistry(load_rag_settings(environment())).resolve_chat('local','chat')

    async def test_resolution_and_clarification_validate_output(self):
        class Client:
            raw = ''
            async def generate(self, selection, messages):
                self.messages = messages
                return self.raw
        client = Client()
        history = [{'question':'Lào hợp tác gì?', 'answer':'Giáo dục.'}]
        client.raw = '{"status":"resolved","question":"Nhật Bản đề xuất hợp tác gì?"}'
        self.assertEqual(await resolve_question('Còn Nhật Bản?', history, self.selection, client), 'Nhật Bản đề xuất hợp tác gì?')
        self.assertEqual(json.loads(client.messages[1]['content'])['history'], history)
        self.assertNotIn('Giáo dục.', client.messages[0]['content'])
        client.raw = '{"status":"clarification","question":null}'
        self.assertIsNone(await resolve_question('Còn việc đó?', [], self.selection, client))
        for raw in ['not json','{}','{"status":"resolved","question":null}','{"status":"resolved","question":"  "}']:
            client.raw = raw
            with self.assertRaisesRegex(RagError, 'invalid_context_response'):
                await resolve_question('Question', history, self.selection, client)

    def test_history_is_recent_contiguous_chronological_and_bounded(self):
        rows = [{'id':str(i),'question':str(i),'answer':'text'} for i in range(10,0,-1)]
        selected = select_history('Question', rows, self.selection)
        self.assertEqual([r['question'] for r in selected], ['5','6','7','8','9','10'])
        rows[1]['answer'] = 'Vietnamese ' * 4000
        self.assertEqual([r['question'] for r in select_history('Question',rows,self.selection)], ['10'])
