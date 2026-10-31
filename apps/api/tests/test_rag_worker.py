import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_rag_postgres_integration import PostgresTest
from test_rag_config import environment
from test_rag_outbox import complete_meeting
from app.core.rag_config import load_rag_settings, RagError
from app.db.session import open_database
from app.services.meetings.repository import MeetingRepository
from app.services.rag.worker import synchronize_once


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        PostgresTest.setUp(self)
        self.source_db = self.connection
        self.connection.execute('DELETE FROM meetings')
        self.connection.execute('DELETE FROM rag_outbox')
        self.meetings = MeetingRepository(self.source_db)
        self.settings = load_rag_settings(environment())
        # Same profile as settings; fixture profile is only used by PG tests.
        self.connection.execute('DELETE FROM rag_embedding_profiles')
        class Embedder:
            async def embed(inner, profile, texts):
                return [[1., 0.] for _ in texts]
        self.client = Embedder()

    def tearDown(self):
        PostgresTest.tearDown(self)

    async def test_worker_uses_configured_chunk_revision(self):
        from dataclasses import replace
        complete_meeting(self.meetings)
        profile = self.settings.profiles[0].model_copy(update={'id':'turn-profile','chunk_revision':'turns-v2'})
        settings = replace(self.settings, profiles=(profile,))
        await synchronize_once(self.meetings, self.repository, self.client, settings)
        payload = self.connection.execute('SELECT payload FROM rag_chunks WHERE profile_id=%s', (profile.id,)).fetchone()[0]
        self.assertEqual(payload['chunk_revision'], 'turns-v2')
        self.assertIn(':turns-v2:', payload['source_id'])

    async def test_backfill_empty_and_repeat_after_publish_ack_crash(self):
        complete_meeting(self.meetings)
        with patch('app.services.rag.worker.acknowledge', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunk_embeddings').fetchone()[0], 1)
        self.assertEqual(self.source_db.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)
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
        self.assertGreater(self.source_db.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertEqual(self.source_db.execute('SELECT count(*) FROM rag_outbox').fetchone()[0], 0)

    async def test_reconciliation_and_retired_profile(self):
        complete_meeting(self.meetings)
        self.source_db.execute('DELETE FROM rag_outbox')
        self.source_db.commit()
        await synchronize_once(self.meetings, self.repository, self.client, self.settings)
        self.assertIsNotNone(self.repository.manifest('p', 'm'))
        from dataclasses import replace
        await synchronize_once(self.meetings, self.repository, self.client, replace(self.settings, profiles=()))
        self.assertEqual(self.connection.execute('SELECT count(*) FROM rag_chunks').fetchone()[0], 0)

    async def test_batches_fit_bounded_provider_responses(self):
        from app.schemas.transcript import AudioProcessResponse,TranscriptSegment
        from app.core.rag_config import RagError
        from dataclasses import replace
        profile=self.settings.profiles[0].model_copy(update={'dimensions':768})
        settings=replace(self.settings,profiles=(profile,))
        from apps.api.tests.postgres_support import seed_project
        account, project = seed_project(self.connection)
        self.meetings.create_meeting('long','Long','m.wav','key','hash',None,
                                    project_id=project.id, uploaded_by_account_id=account.id)
        self.meetings.claim_next()
        self.meetings.complete('long',AudioProcessResponse(filename='m.wav',duration_seconds=60,sample_rate=16000,transcript='text',vad_segments=[],diarization_segments=[],transcript_segments=[TranscriptSegment(start=i,end=i+1,speaker='A',text='word '*180) for i in range(30)]))
        batches=[]
        async def embed(profile,texts):
            import json
            vectors=[[0.12345678901234567]*768 for _ in texts]
            if len(json.dumps({'data':[{'index':i,'embedding':v} for i,v in enumerate(vectors)]}).encode())>131072:
                raise RagError('invalid_provider_response')
            batches.append(len(texts));return vectors
        self.client.embed=embed
        await synchronize_once(self.meetings,self.repository,self.client,settings)
        self.assertIsNotNone(self.repository.manifest('p','long'))
        self.assertTrue(batches)

    async def test_worker_reconnects_after_startup_and_runtime_outage(self):
        import argparse
        import psycopg
        from app.services.rag.worker import _run
        from app.core.config import Settings
        class Finished(Exception):pass
        calls=[]
        def connect(settings):
            calls.append('connect')
            if len(calls)==1:raise psycopg.OperationalError('startup outage')
            return psycopg.connect(self.database.database_url.get_secret_value(),autocommit=True)
        synchronization=[]
        async def synchronize(*args):
            synchronization.append(1)
            if len(synchronization)==1:raise psycopg.OperationalError('runtime restart')
            raise Finished()
        async def pause(*args):pass
        args=argparse.Namespace(once=False,migrate=False,backfill=False)
        with patch('app.services.rag.worker.load_rag_settings',return_value=self.settings),patch('app.services.rag.worker.get_settings',return_value=Settings(meeting_data_dir=self.data_dir)),patch('app.services.rag.worker.open_database',side_effect=connect),patch('app.services.rag.worker.synchronize_once',side_effect=synchronize),patch('app.services.rag.worker.asyncio.sleep',side_effect=pause):
            with self.assertRaises(Finished):await _run(args)
        self.assertGreaterEqual(len(calls),3)
