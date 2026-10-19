import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_rag_postgres_integration import PostgresTest
from test_rag_config import environment
from test_rag_outbox import complete_meeting
from app.core.rag_config import load_rag_settings, RagError
from app.db.session import initialize_meeting_store, open_meeting_db
from app.services.meetings.repository import MeetingRepository
from app.services.rag.worker import synchronize_once


@unittest.skipUnless(os.environ.get('RAG_TEST_DATABASE_URL'), 'PostgreSQL unavailable')
class WorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        PostgresTest.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        initialize_meeting_store(Path(self.temp.name))
        self.sqlite = open_meeting_db(Path(self.temp.name))
        self.meetings = MeetingRepository(self.sqlite)
        self.settings = load_rag_settings(environment())
        # Same profile as settings; fixture profile is only used by PG tests.
        self.connection.execute('DELETE FROM rag_embedding_profiles')
        class Embedder:
            async def embed(inner, profile, texts):
                return [[1., 0.] for _ in texts]
        self.client = Embedder()

    def tearDown(self):
        self.sqlite.close()
        self.temp.cleanup()
        PostgresTest.tearDown(self)

    async def test_backfill_empty_and_repeat_after_publish_ack_crash(self):
        complete_meeting(self.meetings)
        with patch('app.services.rag.worker.acknowledge', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunk_embeddings').fetchone()[0], 1)
        self.assertEqual(self.sqlite.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)
        complete_meeting(self.meetings, 'empty', '')
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertIsNotNone(self.repository.manifest('p', 'empty'))
        self.assertTrue(self.repository.is_profile_ready('p'))

    async def test_cancel_during_embedding_and_postgres_outage(self):
        complete_meeting(self.meetings)
        async def cancel(profile, texts):
            self.meetings.cancel('m')
            return [[1., 0.] for _ in texts]
        self.client.embed = cancel
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertIsNone(self.repository.manifest('p', 'm'))
        with patch.object(self.repository, 'purge', side_effect=RagError('database_unavailable')):
            with self.assertRaises(RagError):
                await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertGreater(self.sqlite.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertEqual(self.sqlite.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)

    async def test_reconciliation_and_retired_profile(self):
        complete_meeting(self.meetings)
        self.sqlite.execute('DELETE FROM rag_outbox')
        self.sqlite.commit()
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertIsNotNone(self.repository.manifest('p', 'm'))
        from dataclasses import replace
        await synchronize_once(self.meetings, self.repository, self.client, replace(self.settings, profiles=()))
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunks').fetchone()[0], 0)
