"""Explicit opt-in tests; each run owns a random schema, never a user's tables."""
import os
import unittest
import uuid
from dataclasses import replace


class PostgresTest(unittest.TestCase):
    def setUp(self):
        from apps.api.tests.postgres_support import enter_postgres
        from app.services.rag.repository import RagRepository
        from app.core.rag_config import EmbeddingProfile
        from app.services.rag.chunking import make_snapshot, chunk_transcript
        from app.services.rag.types import SourceSegment
        from apps.api.tests.postgres_support import seed_snapshot
        fixture = enter_postgres(self)
        self.database, self.connection = fixture.database, fixture.connection
        self.data_dir, self.schema = fixture.data_dir, fixture.schema
        self.seed = lambda snapshot: seed_snapshot(self.connection, snapshot)
        self.repository = RagRepository(self.connection)
        self.profile = EmbeddingProfile(id='p',provider_id='local',model='immutable',revision='v1',dimensions=2)
        self.snapshot = self.seed(make_snapshot("m'; DROP TABLE rag_chunks;--", 'Vietnamese', (SourceSegment(0, 'Kế hoạch phát hành Việt Nam', 'A', 1, 4),)))
        self.chunks = chunk_transcript(self.snapshot)
        self.repository.register_profiles([self.profile])

    def tearDown(self):
        pass

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
        self.snapshot = self.seed(replace(self.snapshot, segments=()))
        self.repository.publish(self.snapshot, self.profile, [], [])
        self.assertEqual(self.repository.manifest('p', self.snapshot.meeting_id), self.snapshot.revision)
        self.assertEqual(self.repository.search('p', 'Việt', [1., 0.], None), ([], []))

    def test_failed_sql_batch_keeps_old_revision(self):
        self.repository.publish(self.snapshot, self.profile, self.chunks, [[1., 0.]])
        bad = replace(self.chunks[0], ordinal=-1)
        with self.assertRaises(Exception):
            self.repository.publish(self.snapshot, self.profile, [bad], [[1., 0.]])
        self.assertEqual(self.repository.manifest('p', self.snapshot.meeting_id), self.snapshot.revision)

    def test_profile_dimensions_and_candidate_caps(self):
        from app.services.rag.chunking import make_snapshot, chunk_transcript
        from app.services.rag.types import SourceSegment
        for i in range(55):
            snapshot = make_snapshot(str(i), 'Title', (SourceSegment(0, 'Việt Nam', 'A', 0, 1),))
            snapshot = self.seed(snapshot)
            self.repository.publish(snapshot, self.profile, chunk_transcript(snapshot), [[1., 0.]])
        a, b = self.repository.search('p', 'Việt', [1., 0.], None)
        self.assertEqual((len(a), len(b)), (50, 50))
        other = self.profile.model_copy(update={'id': 'other', 'dimensions': 3})
        self.repository.register_profiles([other])
        self.assertEqual(self.repository.search('other', 'Việt', [1., 0., 0.], None), ([], []))

    def test_sql_diversifies_before_global_limit(self):
        from app.services.rag.chunking import make_snapshot, chunk_transcript
        from app.services.rag.types import SourceSegment
        for meeting_id, count, vector in [('A',60,[1.,0.]), ('B',1,[.9,.1])]:
            snapshot=make_snapshot(meeting_id,meeting_id,tuple(SourceSegment(i,'Việt Nam '+str(i),'A',i,i+1) for i in range(count)))
            snapshot = self.seed(snapshot)
            chunks=[replace(chunk_transcript(replace(snapshot,segments=(segment,)))[0],ordinal=i,source_id=meeting_id+':'+str(i)) for i,segment in enumerate(snapshot.segments)]
            snapshot = self.seed(snapshot)
            self.repository.publish(snapshot,self.profile,chunks,[vector for _ in chunks])
        vector_hits,text_hits=self.repository.search('p','Việt',[1.,0.],None)
        self.assertIn('B',{h.chunk.meeting_id for h in vector_hits})
        self.assertIn('B',{h.chunk.meeting_id for h in text_hits})

    def test_expired_leases_stop_at_three_attempts(self):
        self.repository.queue(self.profile,self.snapshot)
        for attempt in range(3):
            self.assertIsNotNone(self.repository.claim())
            self.connection.execute("UPDATE rag_index_jobs SET lease_until=now()-interval '1 second'")
        self.assertIsNone(self.repository.claim())
        row=self.connection.execute('SELECT state,attempts FROM rag_index_jobs').fetchone()
        self.assertEqual(row,('failed',3))

    def test_cancelled_source_cannot_publish(self):
        from app.core.rag_config import RagError
        from app.services.meetings.repository import MeetingRepository
        MeetingRepository(self.connection).cancel(self.snapshot.meeting_id)
        with self.assertRaisesRegex(RagError, 'stale_source'):
            self.repository.publish(self.snapshot, self.profile, self.chunks, [[1., 0.]])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunks').fetchone()[0], 0)
