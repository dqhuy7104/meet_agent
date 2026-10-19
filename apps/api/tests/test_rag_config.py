import json
import unittest
from pydantic import ValidationError
from app.core.rag_config import load_rag_settings, ProviderRegistry, RagError
from app.schemas.chat import RagQuery


def environment(kind='openai_compatible'):
    return {'RAG_ENABLED': 'true', 'RAG_DATABASE_URL': 'postgresql://secret@localhost/rag',
            'KEY': 'private-token', 'RAG_PROVIDERS_JSON': json.dumps([{
                'id': 'local', 'name': 'Local', 'kind': kind, 'base_url': 'http://127.0.0.1:11434',
                'api_key_env': 'KEY', 'chat_models': [{'id': 'chat', 'context_tokens': 4096, 'output_tokens': 512}],
                'embedding_models': ['embed@sha256:abc']}]),
            'RAG_EMBEDDING_PROFILES_JSON': json.dumps([{'id': 'p', 'provider_id': 'local',
                'model': 'embed@sha256:abc', 'revision': 'sha256:abc', 'dimensions': 2}])}


class ConfigTest(unittest.TestCase):
    def test_query_boundaries(self):
        values = dict(question='  Xin chào  ', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p')
        self.assertEqual(RagQuery(**values).question, 'Xin chào')
        for change in ({'k': True}, {'k': 0}, {'question': ''}, {'question': '\0'}, {'question': 'a'*2001}, {'endpoint': 'evil'}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RagQuery(**(values | change))

    def test_allowlist_and_redaction(self):
        settings = load_rag_settings(environment())
        registry = ProviderRegistry(settings)
        self.assertEqual(registry.resolve_profile('p').dimensions, 2)
        registry.resolve_chat('local', 'chat')
        with self.assertRaises(RagError):
            registry.resolve_chat('local', 'unknown')
        self.assertNotIn('private-token', repr(settings))
        self.assertNotIn('secret@', repr(settings))
        self.assertNotIn('base_url', registry.catalog(['p']).model_dump_json())

    def test_disabled_and_external_consent(self):
        self.assertFalse(load_rag_settings({}).enabled)
        env = environment()
        providers = json.loads(env['RAG_PROVIDERS_JSON'])
        providers[0]['base_url'] = 'https://external.example/v1'
        env['RAG_PROVIDERS_JSON'] = json.dumps(providers)
        with self.assertRaisesRegex(RagError, 'invalid_configuration'):
            load_rag_settings(env)
