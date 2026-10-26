"""Local bot control API contracts."""

from __future__ import annotations
from apps.api.tests.postgres_support import PostgresAsyncTestCase

import tempfile
import unittest
from pathlib import Path

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.db.session import open_database
from app.services.meetings.storage import initialize_meeting_storage
from app.main import app
from apps.api.tests.postgres_support import seed_project
from app.services.access.repository import AccessRepository
from app.services.access.sessions import SessionRepository, COOKIE_NAME
from app.services.meetings.bot_repository import BotSessionRepository
from app.services.meetings.storage import publish_audio


class BotSessionRouteTest(PostgresAsyncTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        initialize_meeting_storage(self.data_dir, self.connection)
        self.connection = open_database(self.database)
        self.addCleanup(self.connection.close)
        self.repository = BotSessionRepository(self.connection, self.data_dir)
        self.account, self.project = seed_project(self.connection)

        async def settings() -> Settings:
            return Settings(meeting_data_dir=self.data_dir)

        app.dependency_overrides[get_meeting_settings] = settings
        self.addCleanup(app.dependency_overrides.clear)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.as_account(self.account)

    async def test_create_accepts_canonical_meet_url_and_strips_query(self) -> None:
        response = await self.client.post("/api/bot-sessions", json={
            "project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij?authuser=1#frag", "title": "Planning"
        })
        self.assertEqual(response.status_code, 202)
        session = self.repository.get(response.json()["id"])
        self.assertEqual(session.url, "https://meet.google.com/abc-defg-hij")
        self.assertNotIn("url", response.json())
        self.assertEqual(response.json()["finalized_part_count"], 0)
        active = await self.client.get("/api/bot-sessions/active")
        self.assertEqual(active.json()["id"], session.id)

    async def test_create_rejects_lookalike_host_and_noncanonical_path(self) -> None:
        for url in (
            "https://meet.google.com.evil/abc-defg-hij",
            "http://meet.google.com/abc-defg-hij",
            "https://meet.google.com/lookup/abc-defg-hij",
            "https://meet.google.com/ABC-defg-hij",
        ):
            with self.subTest(url=url):
                response = await self.client.post("/api/bot-sessions", json={"project_id": self.project.id, "url": url})
                self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.repository.get_active())

    async def test_stop_partial_finalize_and_discard_controls(self) -> None:
        created = await self.client.post("/api/bot-sessions", json={"project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij"})
        session_id = created.json()["id"]
        self.assertEqual((await self.client.post(f"/api/bot-sessions/{session_id}/process-parts")).status_code, 409)
        self.repository.claim_next()
        self.repository.set_status(session_id, "recording")
        self.repository.add_part(session_id, publish_audio(self.data_dir, b"audio"), 0, 1)
        stopped = await self.client.post(f"/api/bot-sessions/{session_id}/stop")
        self.assertTrue(self.repository.get(session_id).stop_requested)
        self.assertEqual(stopped.status_code, 200)
        self.repository.set_status(session_id, "interrupted", "browser_closed")
        final = await self.client.post(f"/api/bot-sessions/{session_id}/process-parts")
        self.assertEqual(final.status_code, 200)
        self.assertTrue(final.json()["capture_incomplete"])
        self.assertEqual(final.json()["finalized_part_count"], 1)
        self.assertIsNotNone(final.json()["meeting_id"])
        self.assertEqual((await self.client.delete(f"/api/bot-sessions/{session_id}")).status_code, 403)
        meeting_id = final.json()["meeting_id"]
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 403)
        AccessRepository(self.connection).set_membership(self.project.id, self.account.id, "project_manager")
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)

    async def test_idempotency_second_active_and_discard(self) -> None:
        body = {"project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij"}
        first = await self.client.post("/api/bot-sessions", json=body, headers={"Idempotency-Key": "abc"})
        replay = await self.client.post("/api/bot-sessions", json=body, headers={"Idempotency-Key": "abc"})
        self.assertEqual(first.json()["id"], replay.json()["id"])
        conflict = await self.client.post("/api/bot-sessions", json={"project_id": self.project.id, "url": "https://meet.google.com/xyz-abcd-efg"})
        self.assertEqual(conflict.status_code, 409)
        session_id = first.json()["id"]
        self.assertEqual((await self.client.delete(f"/api/bot-sessions/{session_id}")).status_code, 204)
        self.assertEqual(self.repository.get(session_id).status, "cancelled")

    async def test_stopping_unclaimed_session_frees_slot_without_runner(self) -> None:
        first = await self.client.post("/api/bot-sessions", json={
            "project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij"
        })
        session_id = first.json()["id"]

        stopped = await self.client.post(f"/api/bot-sessions/{session_id}/stop")

        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["status"], "cancelled")
        self.assertIsNone(self.repository.get_active())
        next_session = await self.client.post("/api/bot-sessions", json={
            "project_id": self.project.id, "url": "https://meet.google.com/xyz-abcd-efg"
        })
        self.assertEqual(next_session.status_code, 202)

    async def test_remote_peer_and_foreign_origin_are_forbidden(self) -> None:
        remote = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 4000)), base_url="http://localhost"
        )
        try:
            self.assertEqual((await remote.get("/api/bot-sessions/active")).status_code, 403)
        finally:
            await remote.aclose()
        response = await self.client.post("/api/bot-sessions", json={
            "project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij"
        }, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    async def test_recent_recovers_interrupted_session_after_page_reload(self) -> None:
        created = await self.client.post("/api/bot-sessions", json={"project_id": self.project.id, "url": "https://meet.google.com/abc-defg-hij"})
        session_id = created.json()["id"]
        self.repository.claim_next()
        self.repository.set_status(session_id, "interrupted", "runner_interrupted")
        response = await self.client.get("/api/bot-sessions/recent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], session_id)

    def as_account(self, account):
        token, csrf = SessionRepository(self.connection).create(account.id)
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers['X-CSRF-Token'] = csrf

    async def test_project_visibility_roles_archive_and_csrf(self):
        access = AccessRepository(self.connection)
        body = {'project_id': self.project.id, 'url': 'https://meet.google.com/abc-defg-hij'}
        session = (await self.client.post('/api/bot-sessions', json=body)).json()['id']
        outsider, other = seed_project(self.connection)
        self.as_account(outsider)
        for suffix in ('active', 'recent'):
            self.assertEqual((await self.client.get('/api/bot-sessions/'+suffix)).status_code, 204)
        self.assertEqual((await self.client.get('/api/bot-sessions/'+session)).status_code,404)
        self.assertEqual((await self.client.post('/api/bot-sessions/'+session+'/stop')).status_code,404)
        self.assertEqual((await self.client.delete('/api/bot-sessions/'+session)).status_code,404)
        access.set_membership(self.project.id, outsider.id, 'viewer')
        self.assertEqual((await self.client.get('/api/bot-sessions/'+session)).status_code,200)
        self.assertEqual((await self.client.post('/api/bot-sessions/'+session+'/stop')).status_code,403)
        access.set_membership(self.project.id, outsider.id, 'member')
        self.assertEqual((await self.client.delete('/api/bot-sessions/'+session)).status_code,403)
        access.set_membership(self.project.id, outsider.id, 'project_manager')
        self.assertEqual((await self.client.delete('/api/bot-sessions/'+session)).status_code,204)
        access.update_project(self.project.id, is_archived=True)
        self.as_account(self.account)
        self.assertEqual((await self.client.post('/api/bot-sessions', json=body)).status_code,403)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.post('/api/bot-sessions', json=body)).status_code,403)


class BotSessionContractTest(unittest.TestCase):
    def test_project_is_required(self):
        from app.schemas.bot_session import BotSessionCreate
        with self.assertRaises(ValueError):
            BotSessionCreate(url='https://meet.google.com/abc-defg-hij')
