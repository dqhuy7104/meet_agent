"""HTTP upload and local-access contracts for stored meetings."""

from __future__ import annotations
from apps.api.tests.postgres_support import PostgresAsyncTestCase, seed_project
from app.services.access.repository import AccessRepository, Account
from app.services.access.sessions import SessionRepository, COOKIE_NAME

import psycopg
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.db.session import open_database
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.meetings.repository import MeetingRepository


class MeetingUploadTest(PostgresAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "meetings"
        async def test_settings() -> Settings:
            return Settings(meeting_data_dir=self.data_dir)

        app.dependency_overrides[get_meeting_settings] = test_settings
        self.account, self.project = seed_project(self.connection)
        self.access = AccessRepository(self.connection)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000), raise_app_exceptions=False),
            base_url="http://localhost",
        )

        self.as_account(self.account)

    def as_account(self, account: Account) -> None:
        token, csrf = SessionRepository(self.connection).create(account.id)
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers["X-CSRF-Token"] = csrf

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        app.dependency_overrides.clear()

    async def _upload(self, data: bytes, *, title: str | None = None, key: str | None = None) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key is not None else {}
        form = {"project_id": self.project.id}
        if title:
            form["title"] = title
        return await self.client.post(
            "/api/meetings", files={"file": ("private.wav", data, "audio/wav")}, data=form, headers=headers
        )

    async def test_anonymous_and_new_account_are_scoped(self) -> None:
        meeting = (await self._upload(b"private")).json()["meeting_id"]
        self.client.cookies.clear()
        self.assertEqual((await self.client.get("/api/meetings")).status_code, 401)
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting}")).status_code, 401)
        outsider = self.access.create_account("new@example.com", "hash")
        self.as_account(outsider)
        self.assertEqual((await self.client.get("/api/meetings")).json()["total"], 0)
        for path in (meeting, f"{meeting}/audio", f"{meeting}/audio/0"):
            self.assertEqual((await self.client.get(f"/api/meetings/{path}")).status_code, 404)
        self.assertEqual((await self.client.get("/api/meetings/search", params={"q":"private"})).json()["total"], 0)
        self.assertEqual((await self._upload(b"forbidden")).status_code, 404)

    async def test_project_filter_idempotency_and_archived_upload(self) -> None:
        first = (await self._upload(b"audio", key="scoped")).json()["meeting_id"]
        second_project = self.access.create_project("Second")
        self.access.set_membership(second_project.id, self.account.id, "member")
        original_project = self.project
        self.project = second_project
        conflict = await self._upload(b"audio", key="scoped")
        self.assertEqual(conflict.status_code, 409)
        second = (await self._upload(b"other")).json()["meeting_id"]
        response = await self.client.get("/api/meetings", params={"project_id":second_project.id})
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual([item["id"] for item in response.json()["items"]], [second])
        self.assertEqual(response.json()["items"][0]["uploaded_by_account_id"], self.account.id)
        self.access.update_project(second_project.id, is_archived=True)
        self.assertEqual((await self._upload(b"blocked")).status_code, 403)
        self.assertEqual((await self.client.get(f"/api/meetings/{second}")).status_code, 200)
        self.project = original_project
        other = self.access.create_account("other@example.com", "hash")
        self.access.set_membership(self.project.id, other.id, "member")
        self.as_account(other)
        self.assertEqual((await self._upload(b"audio", key="scoped")).status_code, 409)

    async def test_member_ownership_and_manager_authorization(self) -> None:
        own = (await self._upload(b"own")).json()["meeting_id"]
        self.assertEqual((await self.client.delete(f"/api/meetings/{own}")).status_code, 204)
        other = self.access.create_account("other@example.com", "hash")
        self.access.set_membership(self.project.id, other.id, "member")
        self.as_account(other)
        theirs = (await self._upload(b"theirs")).json()["meeting_id"]
        self.connection.execute("UPDATE meetings SET status='failed' WHERE id=%s", (theirs,))
        self.as_account(self.account)
        self.assertEqual((await self.client.delete(f"/api/meetings/{theirs}")).status_code, 403)
        self.assertEqual((await self.client.post(f"/api/meetings/{theirs}/retry")).status_code, 403)
        bot = (await self._upload(b"bot")).json()["meeting_id"]
        self.connection.execute("UPDATE meetings SET source='bot',status='failed' WHERE id=%s", (bot,))
        self.assertEqual((await self.client.delete(f"/api/meetings/{bot}")).status_code, 403)
        self.assertEqual((await self.client.post(f"/api/meetings/{bot}/retry")).status_code, 403)
        self.access.set_membership(self.project.id, self.account.id, "project_manager")
        self.assertEqual((await self.client.post(f"/api/meetings/{theirs}/retry")).status_code, 202)
        self.assertEqual((await self.client.delete(f"/api/meetings/{bot}")).status_code, 204)

    async def test_search_and_direct_ids_do_not_cross_projects(self) -> None:
        own = (await self._upload(b"own")).json()["meeting_id"]
        account, project = seed_project(self.connection)
        self.as_account(account)
        self.project = project
        hidden = (await self._upload(b"hidden")).json()["meeting_id"]
        repository = MeetingRepository(self.connection)
        for meeting_id in (own, hidden):
            self.connection.execute("UPDATE meetings SET status='processing' WHERE id=%s", (meeting_id,))
            repository.complete(meeting_id, AudioProcessResponse(
                filename="a.wav", duration_seconds=1, transcript="needle",
                vad_segments=[], diarization_segments=[],
                transcript_segments=[TranscriptSegment(start=0, end=1, speaker="S0", text="needle")]))
        self.as_account(self.account)
        response = await self.client.get("/api/meetings/search", params={"q":"needle", "limit":1})
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual([item["meeting_id"] for item in response.json()["items"]], [own])
        for path in (hidden, f"{hidden}/audio", f"{hidden}/audio/0"):
            self.assertEqual((await self.client.get(f"/api/meetings/{path}")).status_code, 404)
        self.assertEqual((await self.client.delete(f"/api/meetings/{hidden}")).status_code, 404)
        self.assertEqual((await self.client.post(f"/api/meetings/{hidden}/retry")).status_code, 404)
        for path in ("/api/meetings", "/api/meetings/search?q=needle"):
            self.assertEqual((await self.client.get(path, params={"project_id":project.id, "q":"needle"})).status_code, 404)

    async def test_historical_meeting_hidden_from_admin_and_csrf_required(self) -> None:
        meeting = (await self._upload(b"old")).json()["meeting_id"]
        # Emulate a row retained by the access migration in this disposable schema.
        self.connection.execute("ALTER TABLE meetings DISABLE TRIGGER meetings_reject_new_legacy")
        self.connection.execute("UPDATE meetings SET project_id=NULL,legacy_unassigned=true WHERE id=%s", (meeting,))
        self.connection.execute("ALTER TABLE meetings ENABLE TRIGGER meetings_reject_new_legacy")
        self.connection.execute("UPDATE accounts SET is_system_admin=true WHERE id=%s", (self.account.id,))
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting}")).status_code, 404)
        self.assertEqual((await self.client.get("/api/meetings")).json()["total"], 0)
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self._upload(b"csrf")).status_code, 403)

    async def test_viewer_reads_but_cannot_upload_delete_or_retry(self) -> None:
        meeting = (await self._upload(b"read only")).json()["meeting_id"]
        viewer = self.access.create_account("viewer@example.com", "hash")
        self.access.set_membership(self.project.id, viewer.id, "viewer")
        self.as_account(viewer)
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting}")).status_code, 200)
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting}/audio")).status_code, 200)
        self.assertEqual((await self._upload(b"forbidden")).status_code, 403)
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting}")).status_code, 403)
        self.assertEqual((await self.client.post(f"/api/meetings/{meeting}/retry")).status_code, 403)

    async def test_upload_requires_project_and_mutations_require_csrf(self) -> None:
        missing = await self.client.post("/api/meetings", files={"file":("a.wav", b"audio", "audio/wav")})
        self.assertEqual(missing.status_code, 422)
        meeting = (await self._upload(b"own")).json()["meeting_id"]
        self.connection.execute("UPDATE meetings SET status='failed' WHERE id=%s", (meeting,))
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting}")).status_code, 403)
        self.assertEqual((await self.client.post(f"/api/meetings/{meeting}/retry")).status_code, 403)
        self.as_account(self.account)
        self.assertEqual((await self.client.post(f"/api/meetings/{meeting}/retry")).status_code, 202)

    async def test_upload_returns_queued_metadata_without_decoding(self) -> None:
        response = await self._upload(b"not decoded in the route", title="Planning")
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["title"], "Planning")
        self.assertEqual(body["original_filename"], "private.wav")
        self.assertTrue(body["meeting_id"])
        self.assertTrue(body["created_at"])
        self.assertEqual(len(list((self.data_dir / "audio").glob("*"))), 1)

    async def test_multiple_files_create_one_meeting_and_preserve_order(self) -> None:
        response = await self.client.post(
            "/api/meetings",
            files=[
                ("files", ("first.wav", b"first audio", "audio/wav")),
                ("files", ("second.wav", b"second audio", "audio/wav")),
            ],
            data={"title": "Planning", "project_id": self.project.id},
        )
        self.assertEqual(response.status_code, 202)
        meeting_id = response.json()["meeting_id"]
        self.assertEqual((await self.client.get("/api/meetings")).json()["total"], 1)
        detail = (await self.client.get(f"/api/meetings/{meeting_id}")).json()
        self.assertEqual([part["original_filename"] for part in detail["audio_files"]], ["first.wav", "second.wav"])
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting_id}/audio/0")).content, b"first audio")
        self.assertEqual((await self.client.get(f"/api/meetings/{meeting_id}/audio/1")).content, b"second audio")
        self.assertEqual(
            (await self.client.get(f"/api/meetings/{meeting_id}/audio/1", headers={"Range": "bytes=0-5"})).content,
            b"second",
        )
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
        self.assertEqual(list((self.data_dir / "audio").glob("*")), [])

    async def test_multiple_files_reject_empty_part_without_persisting(self) -> None:
        response = await self.client.post(
            "/api/meetings",
            files=[("files", ("a.wav", b"valid", "audio/wav")), ("files", ("empty.wav", b"", "audio/wav"))], data={"project_id": self.project.id},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list((self.data_dir / "audio").glob("*")), [])

    async def test_multi_file_idempotency_uses_ordered_content(self) -> None:
        first = [("files", ("a.wav", b"a", "audio/wav")), ("files", ("b.wav", b"b", "audio/wav"))]
        reverse = list(reversed(first))
        original = await self.client.post("/api/meetings", files=first, data={"project_id": self.project.id}, headers={"Idempotency-Key": "multi"})
        repeated = await self.client.post("/api/meetings", files=first, data={"project_id": self.project.id}, headers={"Idempotency-Key": "multi"})
        conflict = await self.client.post("/api/meetings", files=reverse, data={"project_id": self.project.id}, headers={"Idempotency-Key": "multi"})
        self.assertEqual(original.status_code, 202)
        self.assertEqual(repeated.json()["meeting_id"], original.json()["meeting_id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(list((self.data_dir / "audio").glob("*"))), 2)

    async def test_empty_upload_is_400(self) -> None:
        response = await self._upload(b"")
        self.assertEqual(response.status_code, 400)

    async def test_too_large_upload_is_413(self) -> None:
        response = await self._upload(b"x" * (25 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    async def test_exact_size_limit_is_accepted(self) -> None:
        response = await self._upload(b"x" * (25 * 1024 * 1024))
        self.assertEqual(response.status_code, 202)

    async def test_same_key_and_audio_returns_original_metadata(self) -> None:
        first = await self._upload(b"first", title="Original", key="request-1")
        second = await self._upload(b"first", title="Changed", key="request-1")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(list((self.data_dir / "audio").glob("*"))), 1)

    async def test_same_key_with_different_audio_is_409(self) -> None:
        await self._upload(b"first", key="request-1")
        second = await self._upload(b"different", key="request-1")
        self.assertEqual(second.status_code, 409)

    async def test_replay_reports_current_completed_status(self) -> None:
        first = await self._upload(b"audio", key="request-1")
        meeting_id = first.json()["meeting_id"]
        connection = open_database(self.database)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.complete(meeting_id, AudioProcessResponse(
                filename="private.wav", duration_seconds=1.0, transcript="done",
                vad_segments=[], diarization_segments=[], transcript_segments=[],
            ))
        finally:
            connection.close()
        replay = await self._upload(b"audio", key="request-1")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["meeting_id"], meeting_id)
        self.assertEqual(replay.json()["status"], "completed")

    async def test_empty_idempotency_key_is_rejected(self) -> None:
        self.assertEqual((await self._upload(b"audio", key="")).status_code, 422)

    async def test_cleanup_waits_for_upload_database_commit(self) -> None:
        original_create = MeetingRepository.create_meeting
        cleanup_started = threading.Event()
        cleanup_done = threading.Event()
        cleaners: list[threading.Thread] = []

        def create_with_concurrent_cleanup(repository: MeetingRepository, *args: object, **kwargs: object) -> object:
            from app.services.meetings.storage import cleanup_orphaned_audio

            def clean() -> None:
                cleanup_started.set()
                with open_database(self.database) as connection:
                    cleanup_orphaned_audio(self.data_dir, MeetingRepository(connection))
                cleanup_done.set()

            cleaner = threading.Thread(target=clean, daemon=True)
            cleaners.append(cleaner)
            cleaner.start()
            self.assertTrue(cleanup_started.wait(1))
            self.assertFalse(cleanup_done.wait(0.1))
            return original_create(repository, *args, **kwargs)

        try:
            with patch.object(MeetingRepository, "create_meeting", create_with_concurrent_cleanup):
                response = await self._upload(b"audio")
        finally:
            for cleaner in cleaners:
                cleaner.join(timeout=2)
        self.assertEqual(response.status_code, 202)
        connection = open_database(self.database)
        try:
            record = MeetingRepository(connection).get_meeting(response.json()["meeting_id"])
            self.assertTrue((self.data_dir / "audio" / record.storage_key).exists())
        finally:
            connection.close()

    async def test_without_key_uploads_make_distinct_meetings(self) -> None:
        first = await self._upload(b"same")
        second = await self._upload(b"same")
        self.assertNotEqual(first.json()["meeting_id"], second.json()["meeting_id"])

    async def test_failed_insert_removes_published_audio(self) -> None:
        with patch("app.services.meetings.repository.MeetingRepository.create_meeting", side_effect=psycopg.errors.CheckViolation):
            response = await self._upload(b"private")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(list((self.data_dir / "audio").glob("*")), [])

    async def test_non_loopback_peer_cannot_forge_local_headers(self) -> None:
        remote = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4000)),
            base_url="http://localhost",
        )
        try:
            response = await remote.post(
                "/api/meetings",
                files={"file": ("private.wav", b"private", "audio/wav")},
                headers={"Host": "localhost", "X-Forwarded-For": "127.0.0.1"},
            )
        finally:
            await remote.aclose()
        self.assertEqual(response.status_code, 403)

    async def test_nonlocal_host_and_foreign_origin_are_rejected(self) -> None:
        hostile_host = await self.client.get("/api/meetings", headers={"Host": "attacker.example"})
        hostile_origin = await self.client.post(
            "/api/meetings", files={"file": ("private.wav", b"audio", "audio/wav")},
            headers={"Origin": "http://attacker.example"},
        )
        self.assertEqual(hostile_host.status_code, 403)
        self.assertEqual(hostile_origin.status_code, 403)

    async def test_local_vite_proxy_origin_can_upload(self) -> None:
        proxied = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)),
            base_url="http://127.0.0.1:8000",
            cookies=self.client.cookies,
        )
        try:
            response = await proxied.post(
                "/api/meetings",
                files={"file": ("private.wav", b"audio", "audio/wav")},
                data={"project_id": self.project.id},
                headers={"Origin": "http://localhost:5173", "X-CSRF-Token": self.client.headers["X-CSRF-Token"]},
            )
        finally:
            await proxied.aclose()
        self.assertEqual(response.status_code, 202)

    async def test_title_and_idempotency_key_lengths_are_bounded(self) -> None:
        self.assertEqual((await self._upload(b"x", title="t" * 201)).status_code, 422)
        self.assertEqual((await self._upload(b"x", key="k" * 201)).status_code, 422)

    async def test_list_is_newest_first_and_paginated(self) -> None:
        first = (await self._upload(b"first")).json()["meeting_id"]
        second = (await self._upload(b"second")).json()["meeting_id"]
        response = await self.client.get("/api/meetings", params={"limit": 1, "offset": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual([item["id"] for item in response.json()["items"]], [second])
        next_page = await self.client.get("/api/meetings", params={"limit": 1, "offset": 1})
        self.assertEqual([item["id"] for item in next_page.json()["items"]], [first])
        self.assertEqual((await self.client.get("/api/meetings", params={"limit": 101})).status_code, 422)

    async def test_detail_shows_only_completed_results(self) -> None:
        meeting_id = (await self._upload(b"audio")).json()["meeting_id"]
        queued = await self.client.get(f"/api/meetings/{meeting_id}")
        self.assertEqual(queued.status_code, 200)
        self.assertIsNone(queued.json()["transcript"])
        self.assertEqual(queued.json()["transcript_segments"], [])
        connection = open_database(self.database)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.complete(meeting_id, AudioProcessResponse(
                filename="private.wav", duration_seconds=1.0, transcript="Xin chào",
                vad_segments=[SpeechSegment(start=0.0, end=1.0)],
                diarization_segments=[SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00")],
                transcript_segments=[TranscriptSegment(start=0.0, end=1.0, speaker="SPEAKER_00", text="Xin chào")],
            ))
        finally:
            connection.close()
        completed = await self.client.get(f"/api/meetings/{meeting_id}")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["transcript"], "Xin chào")
        self.assertEqual(completed.json()["vad_segments"], [{"start": 0.0, "end": 1.0}])
        self.assertEqual(completed.json()["diarization_segments"][0]["speaker"], "SPEAKER_00")
        self.assertEqual(completed.json()["transcript_segments"][0]["text"], "Xin chào")
        self.assertEqual((await self.client.get("/api/meetings/missing")).status_code, 404)

    async def test_original_audio_and_ranges_use_stored_key(self) -> None:
        response = await self.client.post(
            "/api/meetings", files={"file": ("../../client.wav", b"abcdefghij", "audio/wav")}, data={"project_id": self.project.id}
        )
        meeting_id = response.json()["meeting_id"]
        full = await self.client.get(f"/api/meetings/{meeting_id}/audio")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"abcdefghij")
        self.assertIn("client.wav", full.headers["content-disposition"])
        self.assertNotIn("../", full.headers["content-disposition"])
        partial = await self.client.get(
            f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"cdef")
        self.assertEqual(partial.headers["content-range"], "bytes 2-5/10")
        unsatisfiable = await self.client.get(
            f"/api/meetings/{meeting_id}/audio", headers={"Range": "bytes=20-30"}
        )
        self.assertEqual(unsatisfiable.status_code, 416)
        self.assertEqual((await self.client.get("/api/meetings/missing/audio")).status_code, 404)

    async def test_delete_removes_audio_rows_and_search_entries(self) -> None:
        meeting_id = (await self._upload(b"audio")).json()["meeting_id"]
        connection = open_database(self.database)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.complete(meeting_id, AudioProcessResponse(
                filename="private.wav", duration_seconds=1.0, transcript="Xin chào",
                vad_segments=[], diarization_segments=[],
                transcript_segments=[TranscriptSegment(start=0.0, end=1.0, speaker="SPEAKER_00", text="Xin chào")],
            ))
            key = repository.get_meeting(meeting_id).storage_key
            deleted = await self.client.delete(f"/api/meetings/{meeting_id}")
            self.assertEqual(deleted.status_code, 204)
            self.assertEqual(repository.get_meeting(meeting_id).status, "cancelled")
            self.assertEqual(connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)
            self.assertFalse((self.data_dir / "audio" / key).exists())
            self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
        finally:
            connection.close()

    async def test_processing_meeting_is_cancelled_and_audio_is_removed(self) -> None:
        meeting_id = (await self._upload(b"audio")).json()["meeting_id"]
        connection = open_database(self.database)
        try:
            repository = MeetingRepository(connection)
            record = repository.claim_next()
            self.assertTrue(repository.set_pipeline_process(meeting_id, 123, 456))

            self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
            detail = await self.client.get(f"/api/meetings/{meeting_id}")
            self.assertEqual(detail.json()["status"], "cancelled")
            self.assertEqual(detail.json()["pipeline_pid"], 123)
            self.assertIsNone(detail.json()["transcript"])
            self.assertFalse((self.data_dir / "audio" / record.storage_key).exists())
            self.assertEqual((await self.client.get(f"/api/meetings/{meeting_id}/audio")).status_code, 404)
            self.assertEqual((await self.client.post(f"/api/meetings/{meeting_id}/retry")).status_code, 409)
            self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
        finally:
            connection.close()

    async def test_queued_and_failed_meetings_can_be_deleted(self) -> None:
        failed_id = (await self._upload(b"failed")).json()["meeting_id"]
        queued_id = (await self._upload(b"queued")).json()["meeting_id"]
        connection = open_database(self.database)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.fail(failed_id, "invalid_audio")
            failed_detail = await self.client.get(f"/api/meetings/{failed_id}")
            self.assertIsNone(failed_detail.json()["transcript"])
            for meeting_id in (queued_id, failed_id):
                self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
                self.assertEqual(repository.get_meeting(meeting_id).status, "cancelled")
            self.assertEqual(list((self.data_dir / "audio").glob("*")), [])
        finally:
            connection.close()


class MeetingScopeContractTest(unittest.TestCase):
    def test_collection_passes_only_visible_projects_to_repository(self) -> None:
        from unittest.mock import Mock
        from app.api.routes.meetings import list_meetings
        from app.services.access.repository import Account, Project
        account = Account('account', 'member@example.com', False, False)
        access = Mock()
        access.visible_projects.return_value = [Project('visible', 'Visible', False)]
        repository = Mock()
        repository.list_meetings.return_value = []
        repository.count_meetings.return_value = 0
        response = list_meetings(account=account, access=access, repository=repository,
                                 limit=20, offset=0, project_id=None)
        self.assertEqual(response.total, 0)
        repository.list_meetings.assert_called_once_with(20, 0, ['visible'])
        repository.count_meetings.assert_called_once_with(['visible'])


if __name__ == "__main__":
    unittest.main()
