"""Real sessions, storage, indexing and request authorization; fake model providers only."""
import json
from unittest.mock import patch

import httpx

from apps.api.tests.postgres_support import PostgresAsyncTestCase
from test_rag_config import environment
from app.api.deps import get_rag_service
from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.core.rag_config import load_rag_settings
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, TranscriptSegment
from app.services.access.repository import AccessRepository
from app.services.access.sessions import COOKIE_NAME, SessionRepository
from app.services.meetings.repository import MeetingRepository
from app.services.rag.chunking import snapshot_meeting, chunk_transcript
from app.services.rag.repository import RagRepository
from app.services.rag.request import RagRequestService


class ProjectAccessIntegrationTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        self.access = AccessRepository(self.connection)
        self.accounts = {role: self.access.create_account(role + '@example.test', 'hash')
                         for role in ('admin', 'manager', 'member', 'viewer', 'unassigned')}
        self.connection.execute('UPDATE accounts SET is_system_admin=true WHERE id=%s', (self.accounts['admin'].id,))
        self.projects = [self.access.create_project(name) for name in ('Visible', 'Confidential')]
        for role, membership in [('manager', 'project_manager'), ('member', 'member'), ('viewer', 'viewer')]:
            self.access.set_membership(self.projects[0].id, self.accounts[role].id, membership)
        self.settings = load_rag_settings(environment())
        app.dependency_overrides[get_meeting_settings] = lambda: Settings(meeting_data_dir=self.data_dir)
        app.dependency_overrides[get_rag_service] = lambda: RagRequestService(self.settings, self.database)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.calls = []

        async def embed(*args):
            return [[1., 0.]]

        async def generate(client, selection, messages):
            self.calls.append(messages)
            if 'sources' in json.loads(messages[1]['content']):
                sources = json.loads(messages[1]['content'])['sources']
                return json.dumps({'status':'answered', 'claims':[{'text':'Visible plan', 'citation_ids':[sources[0]['source_id']]}]})
            return '{"summary":"Visible summary"}'

        for target, replacement in [('embed', embed), ('generate', generate)]:
            provider_patch = patch('app.services.rag.providers.ModelClient.' + target, replacement)
            provider_patch.start()
            self.addCleanup(provider_patch.stop)

    def sign_in(self, role):
        self.client.cookies.clear()
        token, csrf = SessionRepository(self.connection).create(self.accounts[role].id)
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers['X-CSRF-Token'] = csrf

    async def upload_completed(self, project, title):
        response = await self.client.post('/api/meetings', data={'project_id':project.id, 'title':title}, files={'file':('private.wav', title.encode(), 'audio/wav')})
        self.assertEqual(response.status_code, 202, response.text)
        meeting_id = response.json()['meeting_id']
        self.connection.execute("UPDATE meetings SET status='processing' WHERE id=%s", (meeting_id,))
        repository = MeetingRepository(self.connection)
        repository.complete(meeting_id, AudioProcessResponse(filename='private.wav', duration_seconds=1, transcript=title, vad_segments=[], diarization_segments=[SpeakerTurn(start=0, end=1, speaker='S1')], transcript_segments=[TranscriptSegment(start=0, end=1, speaker='S1', text=title)]))
        snapshot = snapshot_meeting(repository.get_detail(meeting_id))
        rag = RagRepository(self.connection)
        rag.register_profiles(self.settings.profiles)
        rag.publish(snapshot, self.settings.profiles[0], chunk_transcript(snapshot), [[1., 0.]])
        return meeting_id

    def query(self, **changes):
        return dict(question='plan', k=1, chat_provider_id='local', chat_model_id='chat', embedding_profile_id='p') | changes

    async def test_upload_to_rag_role_revocation_and_deletion(self):
        self.sign_in('admin')
        hidden = await self.upload_completed(self.projects[1], 'CONFIDENTIAL secret plan')
        self.sign_in('member')
        visible = await self.upload_completed(self.projects[0], 'Visible plan')
        for role in ('manager', 'member', 'viewer', 'unassigned', 'admin'):
            self.sign_in(role)
            self.calls.clear()
            allowed = role != 'unassigned'
            listing = await self.client.get('/api/meetings')
            expected = {visible, hidden} if role == 'admin' else ({visible} if allowed else set())
            self.assertEqual({item['id'] for item in listing.json()['items']}, expected)
            for suffix in ('', '/audio', '/audio/0'):
                response = await self.client.get('/api/meetings/' + visible + suffix)
                self.assertEqual(response.status_code, 200 if allowed else 404)
            answer = await self.client.post('/api/chat/queries', json=self.query(meeting_ids=[visible, hidden]))
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertTrue({c['meeting_id'] for c in answer.json()['citations']} <= expected)
            if role != 'admin':
                for path in (f'/api/meetings/{hidden}', f'/api/meetings/{hidden}/audio'):
                    response = await self.client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn('CONFIDENTIAL', response.text)
                response = await self.client.post(f'/api/chat/meetings/{hidden}/summary')
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn('CONFIDENTIAL', response.text)
                self.assertNotIn('CONFIDENTIAL', json.dumps(self.calls))
        self.sign_in('viewer')
        summary = await self.client.post(f'/api/chat/meetings/{visible}/summary')
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual((await self.client.delete(f'/api/meetings/{visible}')).status_code, 403)
        self.access.remove_membership(self.projects[0].id, self.accounts['viewer'].id)
        self.assertEqual((await self.client.get(f'/api/meetings/{visible}')).status_code, 404)
        self.assertEqual((await self.client.post(f'/api/chat/meetings/{visible}/summary')).status_code, 404)
        self.assertEqual((await self.client.post('/api/chat/queries', json=self.query())).json()['citations'], [])
        self.sign_in('member')
        self.assertEqual((await self.client.delete(f'/api/meetings/{visible}')).status_code, 204)
        self.assertEqual((await self.client.post('/api/chat/queries', json=self.query())).json()['citations'], [])

    async def test_anonymous_route_inventory_and_stateless_exception(self):
        paths = ['/api/projects', '/api/admin/accounts', '/api/meetings', '/api/meetings/search?q=plan', '/api/bot-sessions/active', '/api/bot-sessions/recent', '/api/bot-sessions/missing', '/api/voice-profiles', '/api/voice-profiles/missing/samples', '/api/voice-enrollment-jobs/missing', '/api/chat/providers', '/api/chat/evaluation-state?embedding_profile_id=p&chat_provider_id=local&chat_model_id=chat']
        for path in paths:
            self.assertEqual((await self.client.get(path)).status_code, 401, path)
        for path, body in [('/api/bot-sessions', {'project_id':self.projects[0].id, 'url':'https://meet.google.com/abc-defg-hij'}), ('/api/chat/queries', self.query()), ('/api/chat/meetings/missing/summary', None), ('/api/voice-profiles', {'display_name':'Name'})]:
            self.assertEqual((await self.client.post(path, json=body)).status_code, 401, path)
        # Stateless processing remains unauthenticated and validates the upload normally.
        response = await self.client.post('/api/audio/process', files={'file':('empty.wav', b'', 'audio/wav')})
        self.assertEqual(response.status_code, 400, response.text)

    async def test_bot_and_voice_resources_follow_project_revocation(self):
        self.sign_in('admin')
        hidden = await self.upload_completed(self.projects[1], 'CONFIDENTIAL speaker plan')
        response = await self.client.post('/api/voice-profiles', json={'display_name':'Shared name'})
        self.assertEqual(response.status_code, 201, response.text)
        profile = response.json()['id']
        response = await self.client.post('/api/bot-sessions', json={'project_id':self.projects[1].id, 'url':'https://meet.google.com/abc-defg-hij'})
        self.assertEqual(response.status_code, 202, response.text)
        bot = response.json()['id']
        response = await self.client.post(f'/api/voice-profiles/{profile}/samples/from-meeting', json={'meeting_id':hidden, 'speaker':'S1'})
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()['id']
        self.sign_in('manager')
        for path in (f'/api/bot-sessions/{bot}', f'/api/voice-enrollment-jobs/{job}'):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn(hidden, response.text)
        active = await self.client.get('/api/bot-sessions/active')
        self.assertEqual(active.status_code, 204)
        self.assertEqual(active.content, b'')
        self.assertEqual((await self.client.post(f'/api/bot-sessions/{bot}/stop')).status_code, 404)
        self.assertEqual((await self.client.post(f'/api/voice-profiles/{profile}/samples/from-meeting', json={'meeting_id':hidden, 'speaker':'S1'})).status_code, 404)
        self.assertEqual((await self.client.put(f'/api/meetings/{hidden}/speaker-identity', json={'speaker':'S1', 'profile_id':profile})).status_code, 404)
        self.access.set_membership(self.projects[1].id, self.accounts['manager'].id, 'project_manager')
        self.assertEqual((await self.client.get(f'/api/bot-sessions/{bot}')).status_code, 200)
        self.assertEqual((await self.client.get(f'/api/voice-enrollment-jobs/{job}')).status_code, 200)
        self.access.set_membership(self.projects[1].id, self.accounts['admin'].id, 'project_manager')
        self.access.remove_membership(self.projects[1].id, self.accounts['manager'].id)
        self.assertEqual((await self.client.get(f'/api/bot-sessions/{bot}')).status_code, 404)
        self.assertEqual((await self.client.get(f'/api/voice-enrollment-jobs/{job}')).status_code, 404)
