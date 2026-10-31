import unittest

from apps.api.tests.postgres_support import PostgresAsyncTestCase, seed_snapshot
from app.core.rag_config import load_rag_settings
from app.services.rag.chunking import make_snapshot, chunk_transcript
from app.services.rag.types import SourceSegment
from test_rag_config import environment


class IndexStatusAccessTest(PostgresAsyncTestCase):
    async def test_real_request_scope_revocation_and_project_readiness(self):
        from app.services.access.repository import AccessRepository
        from app.services.meetings.repository import MeetingRepository
        from app.services.rag.repository import RagRepository
        from app.services.rag.request import RagRequestService
        from app.core.rag_config import RagError
        access = AccessRepository(self.connection)
        reader = access.create_account('reader@example.test', 'unusable-hash')
        snapshots = [seed_snapshot(self.connection, make_snapshot(key, title,
            (SourceSegment(0, 'Kế hoạch', 'A', 0, 10),)))
            for key, title in [('ready','Ready meeting'),('pending','Pending meeting'),('hidden','Private title')]]
        meetings = MeetingRepository(self.connection)
        project_ids = {s.meeting_id:meetings.get_meeting(s.meeting_id).project_id for s in snapshots}
        for key in ['ready','pending']:
            access.set_membership(project_ids[key], reader.id, 'viewer')
        settings = load_rag_settings(environment())
        rag = RagRepository(self.connection)
        rag.publish(snapshots[0], settings.profiles[0], chunk_transcript(snapshots[0]), [[1.,0.]])
        service = RagRequestService(settings, self.database)
        result = await service.index_status(reader.id)
        self.assertEqual([m.meeting_id for m in result.profiles[0].meetings], ['pending','ready'])
        self.assertNotIn('Private title', result.model_dump_json())
        self.assertEqual((await service.catalog(reader.id)).embedding_profiles, [])
        self.assertEqual(len((await service.catalog(reader.id, project_ids['ready'])).embedding_profiles), 1)
        with self.assertRaisesRegex(RagError, 'project_not_found'):
            await service.index_status(reader.id, project_ids['hidden'])
        self.connection.execute('DELETE FROM project_memberships WHERE account_id=%s', (reader.id,))
        self.assertEqual((await service.index_status(reader.id)).profiles[0].meetings, [])


if __name__ == '__main__':
    unittest.main()
