"""Local voice enrollment and identity API contracts."""
import httpx

from apps.api.tests import test_meeting_search
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, TranscriptSegment
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path
from app.services.voice.repository import VoiceRepository


class VoiceRoutesTest(test_meeting_search.MeetingSearchTest):
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
        for data, code in ((b'', 400), (b'x' * (25 * 1024 * 1024 + 1), 413), (b'audio', 202)):
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
