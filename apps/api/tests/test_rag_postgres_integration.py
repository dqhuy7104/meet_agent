"""Explicit opt-in tests; each run owns a random schema, never a user's tables."""
import os
import unittest
import uuid
from dataclasses import replace


@unittest.skipUnless(os.environ.get('RAG_TEST_DATABASE_URL'), 'RAG_TEST_DATABASE_URL unavailable: PostgreSQL not verified')
class PostgresTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from app.db.rag_session import migrate_rag_db
        from app.services.rag.repository import RagRepository
        from app.core.rag_config import EmbeddingProfile
        from app.services.rag.chunking import make_snapshot, chunk_transcript
        from app.services.rag.types import SourceSegment
        self.connection = psycopg.connect(os.environ['RAG_TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'rag_test_' + uuid.uuid4().hex
        self.connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
        self.connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.connection.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(self.schema)))
        migrate_rag_db(self.connection)
        self.repository = RagRepository(self.connection)
        self.profile = EmbeddingProfile(id='p', provider_id='local', model='immutable', revision='v1', dimensions=2)
        self.snapshot = make_snapshot("m'; DROP TABLE rag_chunks;--", 'Vietnamese', (SourceSegment(0, 'Kế hoạch phát hành Việt Nam', 'A', 1, 4),))
        self.chunks = chunk_transcript(self.snapshot)
        self.repository.register_profiles([self.profile])

    def tearDown(self):
        from psycopg import sql
        self.connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.connection.close()

    def test_publish_rollback_idempotence_search_and_purge(self):
        from app.core.rag_config import RagError
        self.repository.publish(self.snapshot, self.profile, self.chunks, [[1., 0.]])
        self.repository.publish(self.snapshot, self.profile, self.chunks, [[1., 0.]])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunk_embeddings').fetchone()[0], 1)
        with self.assertRaises(RagError):
            self.repository.publish(self.snapshot, self.profile, self.chunks, [[1.]])
        self.assertEqual(self.repository.manifest('p', self.snapshot.meeting_id), self.snapshot.revision)
        vectors, texts = self.repository.search('p', 'Việt', [1., 0.], None)
        self.assertEqual(texts[0].chunk.text, 'Kế hoạch phát hành Việt Nam')
        self.assertEqual(vectors[0].chunk.source_id, self.chunks[0].source_id)
        self.assertEqual(self.repository.search('p', 'Việt', [1., 0.], []), ([], []))
        self.repository.mark_ready('p')
        self.assertTrue(self.repository.is_profile_ready('p'))
        self.repository.purge(self.snapshot.meeting_id)
        for table in ('rag_chunks', 'rag_chunk_embeddings', 'rag_publications', 'rag_index_jobs'):
            self.assertEqual(self.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_profile_immutability_and_empty_publication(self):
        from app.core.rag_config import RagError
        with self.assertRaises(RagError):
            self.repository.register_profiles([self.profile.model_copy(update={'dimensions': 3})])
        self.repository.publish(replace(self.snapshot, segments=()), self.profile, [], [])
        self.assertEqual(self.repository.manifest('p', self.snapshot.meeting_id), self.snapshot.revision)
        self.assertEqual(self.repository.search('p', 'Việt', [1., 0.], None), ([], []))

    def test_failed_sql_batch_keeps_old_revision(self):
        self.repository.publish(self.snapshot, self.profile, self.chunks, [[1., 0.]])
        bad = replace(self.chunks[0], ordinal=-1, revision='new')
        with self.assertRaises(Exception):
            self.repository.publish(replace(self.snapshot, revision='new'), self.profile, [bad], [[1., 0.]])
        self.assertEqual(self.repository.manifest('p', self.snapshot.meeting_id), self.snapshot.revision)

    def test_profile_dimensions_and_candidate_caps(self):
        from app.services.rag.chunking import make_snapshot, chunk_transcript
        from app.services.rag.types import SourceSegment
        for i in range(55):
            snapshot = make_snapshot(str(i), 'Title', (SourceSegment(0, 'Việt Nam', 'A', 0, 1),))
            self.repository.publish(snapshot, self.profile, chunk_transcript(snapshot), [[1., 0.]])
        a, b = self.repository.search('p', 'Việt', [1., 0.], None)
        self.assertEqual((len(a), len(b)), (50, 50))
        other = self.profile.model_copy(update={'id': 'other', 'dimensions': 3})
        self.repository.register_profiles([other])
        self.assertEqual(self.repository.search('other', 'Việt', [1., 0., 0.], None), ([], []))
