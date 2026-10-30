"""Local voice enrollment and identity API contracts."""
import httpx
import unittest

from apps.api.tests import test_meeting_search
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, TranscriptSegment
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path
from app.services.voice.repository import VoiceRepository


class VoiceRoutesTest(test_meeting_search.MeetingSearchTest):
    def setUp(self):
        super().setUp()
        from app.api.auth import require_account, require_csrf
        from app.services.access.repository import AccessRepository
        from apps.api.tests.postgres_support import seed_project
        account, self.project = seed_project(self.connection)
        self.account = AccessRepository(self.connection).update_account(account.id, is_system_admin=True)
        app.dependency_overrides[require_account] = lambda: self.account
        app.dependency_overrides[require_csrf] = lambda: None

    async def _upload(self, title="Planning"):
        response = await self.client.post('/api/meetings', files={'file': ('meeting.wav', b'audio', 'audio/wav')},
                                          data={'title': title, 'project_id': self.project.id})
        self.assertEqual(response.status_code, 202)
        return response.json()['meeting_id']

    async def profile(self):
        response = await self.client.post('/api/voice-profiles', json={'display_name': ' Alice '})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['display_name'], 'Alice')
        self.assertEqual(set(response.json()), {'id', 'display_name', 'sample_count'})
        return response.json()['id']

    async def test_profile_samples_upload_and_cleanup(self):
        profile = await self.profile()
        self.assertEqual(len((await self.client.get('/api/voice-profiles')).json()), 1)
        for name in (' ', 'x' * 101):
            self.assertEqual((await self.client.patch(f'/api/voice-profiles/{profile}', json={'display_name': name})).status_code, 422)
        for data, code in ((b'', 400), (b'x' * (100 * 1024 * 1024 + 1), 413), (b'audio', 202)):
            response = await self.client.post(f'/api/voice-profiles/{profile}/samples/upload', files={'file': ('a.wav', data)})
            self.assertEqual(response.status_code, code)
        job = response.json()
        self.assertEqual(set(job), {'id', 'status', 'failure_code'})
        self.assertEqual((await self.client.get(f"/api/voice-enrollment-jobs/{job['id']}")).json(), job)
        repository = VoiceRepository(self.connection)
        key = repository.get_job(job['id']).storage_key
        self.assertTrue(audio_path(self.data_dir, key).is_file())
        sample = repository.add_sample(profile, [1., 0.], 'test-fingerprint', 'upload')
        samples = (await self.client.get(f'/api/voice-profiles/{profile}/samples')).json()
        self.assertEqual(samples[0]['fifo_position'], 1)
        self.assertEqual(set(samples[0]), {'id','source','source_meeting_id','source_speaker','fingerprint','created_at','fifo_position'})
        self.assertEqual((await self.client.delete(f'/api/voice-profiles/{profile}/samples/{sample.id}')).status_code, 204)
        self.assertEqual((await self.client.delete(f'/api/voice-profiles/{profile}')).status_code, 204)
        self.assertFalse(audio_path(self.data_dir, key).exists())
        self.assertEqual((await self.client.get(f'/api/voice-profiles/{profile}/samples')).status_code, 404)

    async def test_meeting_enrollment_correction_rename_clear_and_search(self):
        profile = await self.profile()
        meeting = await self._upload()
        body = {'meeting_id': meeting, 'speaker': 'SPEAKER_01'}
        endpoint = f'/api/voice-profiles/{profile}/samples/from-meeting'
        self.assertEqual((await self.client.post(endpoint, json=body)).status_code, 409)
        repository = MeetingRepository(self.connection)
        repository.claim_next()
        repository.complete(meeting, AudioProcessResponse(filename='a.wav', duration_seconds=12, transcript='hello', vad_segments=[], diarization_segments=[SpeakerTurn(start=0,end=12,speaker='SPEAKER_01')], transcript_segments=[TranscriptSegment(start=0,end=12,speaker='SPEAKER_01',text='hello')]))
        self.assertEqual((await self.client.post(endpoint, json=body)).status_code, 202)
        self.assertEqual((await self.client.post(endpoint, json={**body, 'speaker':'absent'})).status_code, 409)
        self.assertEqual((await self.client.post(endpoint, json={**body, 'meeting_id':'absent'})).status_code, 404)
        correction = f'/api/meetings/{meeting}/speaker-identity'
        response = await self.client.put(correction, json={'speaker':'SPEAKER_01','profile_id':profile})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['profile_id'], profile)
        await self.client.patch(f'/api/voice-profiles/{profile}', json={'display_name':'Renamed'})
        detail = (await self.client.get(f'/api/meetings/{meeting}')).json()
        self.assertEqual(detail['voice_identification_status'], 'not_run')
        self.assertEqual(detail['speaker_identities'][0]['display_name'], 'Renamed')
        self.assertEqual(detail['transcript_segments'][0]['speaker'], 'SPEAKER_01')
        hit = (await self.client.get('/api/meetings/search', params={'q':'hello'})).json()['items'][0]
        self.assertEqual((hit['speaker'], hit['speaker_name']), ('SPEAKER_01','Renamed'))
        response = await self.client.put(correction, json={'speaker':'SPEAKER_01','profile_id':None})
        self.assertIsNone(response.json()['profile_id'])
        self.assertEqual(response.json()['speaker'], 'SPEAKER_01')
        self.assertEqual((await self.client.put(correction, json={'speaker':'absent','profile_id':None})).status_code, 409)
        self.assertEqual((await self.client.put(correction, json={'speaker':'SPEAKER_01','profile_id':'absent'})).status_code, 404)

    async def test_loopback_and_missing_resources(self):
        for client, base in ((('203.0.113.1',1),'http://localhost'), (('127.0.0.1',1),'http://evil.example')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=client),base_url=base) as remote:
                self.assertEqual((await remote.get('/api/voice-profiles')).status_code,403)
        self.assertEqual((await self.client.post('/api/voice-profiles',json={'display_name':'x'},headers={'Origin':'https://evil.example'})).status_code,403)
        for path in ('/api/voice-enrollment-jobs/absent','/api/voice-profiles/absent/samples'):
            self.assertEqual((await self.client.get(path)).status_code,404)

    async def test_project_manager_cannot_enroll_or_correct_other_project(self):
        from app.services.access.repository import AccessRepository
        access = AccessRepository(self.connection)
        profile = await self.profile()
        own_meeting = await self._upload()
        other_project = access.create_project('Other')
        other_meeting = await self._upload()
        self.connection.execute('UPDATE meetings SET project_id=%s WHERE id=%s', (other_project.id, other_meeting))
        access.set_membership(self.project.id, self.account.id, 'project_manager')
        # A second administrator allows demoting the current account.
        administrator = access.create_account('admin@example.com', 'hash')
        access.update_account(administrator.id, is_system_admin=True)
        self.account = access.update_account(self.account.id, is_system_admin=False)
        endpoint = f'/api/voice-profiles/{profile}/samples/from-meeting'
        self.assertEqual((await self.client.post(endpoint, json={'meeting_id': own_meeting, 'speaker': 'S0'})).status_code, 409)
        self.assertEqual((await self.client.post(endpoint, json={'meeting_id': other_meeting, 'speaker': 'S0'})).status_code, 404)
        self.assertEqual((await self.client.put(f'/api/meetings/{other_meeting}/speaker-identity', json={'speaker': 'S0', 'profile_id': profile})).status_code, 404)
        job = VoiceRepository(self.connection).enqueue_upload(profile, 'b' * 32, administrator.id)
        self.assertEqual((await self.client.get(f'/api/voice-enrollment-jobs/{job.id}')).status_code, 404)


class VoicePermissionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from unittest.mock import Mock, patch
        from app.api.auth import require_account, require_csrf
        from app.api.routes.voice import get_voice_repository
        from app.services.access.repository import Account, Project
        self.account = Account('manager', 'manager@example.com', False, False)
        self.project = Project('project', 'Project', False)
        self.repository = Mock()
        self.access = Mock()
        self.access.visible_projects.return_value = [self.project]
        self.access.role.return_value = 'project_manager'
        self.access.project.return_value = self.project
        self.repository.create_profile.return_value = self.repository.rename_profile.return_value = {
            'id': 'profile', 'display_name': 'Alice', 'sample_count': 0}
        app.dependency_overrides[require_account] = lambda: self.account
        app.dependency_overrides[require_csrf] = lambda: None
        app.dependency_overrides[get_voice_repository] = lambda: self.repository
        from app.api.routes.meetings import get_meeting_database_settings
        app.dependency_overrides[get_meeting_database_settings] = lambda: None
        self.patcher = patch('app.api.routes.voice.AccessRepository', return_value=self.access)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(app.dependency_overrides.clear)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_manager_edits_but_only_admin_deletes(self):
        for method, path in (('post', '/api/voice-profiles'), ('patch', '/api/voice-profiles/profile')):
            response = await getattr(self.client, method)(path, json={'display_name': 'Alice'})
            self.assertIn(response.status_code, (200, 201))
        self.assertEqual((await self.client.delete('/api/voice-profiles/profile')).status_code, 403)
        self.assertEqual((await self.client.delete('/api/voice-profiles/profile/samples/1')).status_code, 403)
        self.repository.delete_profile.assert_not_called()
        self.repository.delete_sample.assert_not_called()

    async def test_member_and_viewer_cannot_edit_or_upload(self):
        for role in ('member', 'viewer'):
            self.access.role.return_value = role
            for method, path in (('post', '/api/voice-profiles'), ('patch', '/api/voice-profiles/profile')):
                self.assertEqual((await getattr(self.client, method)(path, json={'display_name': 'Alice'})).status_code, 403)
            response = await self.client.post('/api/voice-profiles/profile/samples/upload', files={'file': ('a.wav', b'audio')})
            self.assertEqual(response.status_code, 403)

    async def test_unrelated_job_is_hidden(self):
        from app.services.voice.repository import EnrollmentJob
        self.repository.get_job.return_value = EnrollmentJob('job', 'profile', 'upload', None, None, None, 'queued', 0, None, 'other', None)
        self.assertEqual((await self.client.get('/api/voice-enrollment-jobs/job')).status_code, 404)

    async def test_enrollment_and_correction_require_managed_meeting(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        endpoint = '/api/voice-profiles/profile/samples/from-meeting'
        body = {'meeting_id': 'meeting', 'speaker': 'S0'}
        with patch('app.api.routes.voice.require_meeting', side_effect=HTTPException(404, 'Meeting not found')) as authorize:
            self.assertEqual((await self.client.post(endpoint, json=body)).status_code, 404)
            self.assertEqual((await self.client.put('/api/meetings/meeting/speaker-identity', json={'speaker': 'S0', 'profile_id': 'profile'})).status_code, 404)
            self.assertEqual(authorize.call_args.args[2], 'identify')
        self.repository.enqueue_from_meeting.assert_not_called()
        self.repository.set_meeting_identity.assert_not_called()

    async def test_meeting_enrollment_records_creator_and_project(self):
        from unittest.mock import Mock, patch
        from app.services.voice.repository import EnrollmentJob
        self.repository.enqueue_from_meeting.return_value = EnrollmentJob('job', 'profile', 'meeting', None, 'meeting', 'S0', 'queued', 0, None)
        with patch('app.api.routes.voice.require_meeting', return_value=Mock(project_id='project')):
            response = await self.client.post('/api/voice-profiles/profile/samples/from-meeting', json={'meeting_id': 'meeting', 'speaker': 'S0'})
        self.assertEqual(response.status_code, 202)
        self.repository.enqueue_from_meeting.assert_called_once_with('profile', 'meeting', 'S0', 'manager', 'project')

    async def test_shared_reads_need_project_and_hide_inaccessible_samples(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        from app.services.voice.repository import VoiceSample
        from datetime import datetime, timezone
        self.repository.list_samples.return_value = [VoiceSample(1, 'profile', 2, 'v1', 'meeting', 'other', 'S0', datetime.now(timezone.utc))]
        with patch('app.api.routes.voice.require_meeting', side_effect=HTTPException(404, 'Meeting not found')):
            response = await self.client.get('/api/voice-profiles/profile/samples')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.access.visible_projects.return_value = []
        self.assertEqual((await self.client.get('/api/voice-profiles')).status_code, 403)
