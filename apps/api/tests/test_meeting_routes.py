"""HTTP upload and local-access contracts for stored meetings."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.db.session import open_meeting_db
from app.main import app
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.meetings.repository import MeetingRepository


class MeetingUploadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "meetings"
        async def test_settings() -> Settings:
            return Settings(meeting_data_dir=self.data_dir)

        app.dependency_overrides[get_meeting_settings] = test_settings
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000), raise_app_exceptions=False),
            base_url="http://localhost",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        app.dependency_overrides.clear()

    async def _upload(self, data: bytes, *, title: str | None = None, key: str | None = None) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key is not None else {}
        form = {"title": title} if title else {}
        return await self.client.post(
            "/api/meetings", files={"file": ("private.wav", data, "audio/wav")}, data=form, headers=headers
        )

    async def test_upload_returns_queued_metadata_without_decoding(self) -> None:
        response = await self._upload(b"not decoded in the route", title="Planning")
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["title"], "Planning")
        self.assertEqual(body["original_filename"], "private.wav")
        self.assertTrue(body["meeting_id"])
        self.assertTrue(body["created_at"])
        self.assertEqual(len(list((self.data_dir / "audio").iterdir())), 1)

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
        self.assertEqual(len(list((self.data_dir / "audio").iterdir())), 1)

    async def test_same_key_with_different_audio_is_409(self) -> None:
        await self._upload(b"first", key="request-1")
        second = await self._upload(b"different", key="request-1")
        self.assertEqual(second.status_code, 409)

    async def test_replay_reports_current_completed_status(self) -> None:
        first = await self._upload(b"audio", key="request-1")
        meeting_id = first.json()["meeting_id"]
        connection = open_meeting_db(self.data_dir)
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

        def create_with_concurrent_cleanup(repository: MeetingRepository, *args: object) -> object:
            from app.services.meetings.storage import cleanup_orphaned_audio

            def clean() -> None:
                cleanup_started.set()
                cleanup_orphaned_audio(self.data_dir)
                cleanup_done.set()

            cleaner = threading.Thread(target=clean, daemon=True)
            cleaners.append(cleaner)
            cleaner.start()
            self.assertTrue(cleanup_started.wait(1))
            self.assertFalse(cleanup_done.wait(0.1))
            return original_create(repository, *args)

        try:
            with patch.object(MeetingRepository, "create_meeting", create_with_concurrent_cleanup):
                response = await self._upload(b"audio")
        finally:
            for cleaner in cleaners:
                cleaner.join(timeout=2)
        self.assertEqual(response.status_code, 202)
        connection = open_meeting_db(self.data_dir)
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
        with patch("app.services.meetings.repository.MeetingRepository.create_meeting", side_effect=sqlite3.OperationalError):
            response = await self._upload(b"private")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list((self.data_dir / "audio").iterdir()), [])

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
        connection = open_meeting_db(self.data_dir)
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
            "/api/meetings", files={"file": ("../../client.wav", b"abcdefghij", "audio/wav")}
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
        connection = open_meeting_db(self.data_dir)
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
            self.assertIsNone(repository.get_meeting(meeting_id))
            self.assertEqual(connection.execute("SELECT count(*) FROM transcript_fts").fetchone()[0], 0)
            self.assertFalse((self.data_dir / "audio" / key).exists())
            self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 404)
        finally:
            connection.close()

    async def test_processing_meeting_cannot_be_deleted(self) -> None:
        meeting_id = (await self._upload(b"audio")).json()["meeting_id"]
        connection = open_meeting_db(self.data_dir)
        try:
            MeetingRepository(connection).claim_next()
            self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 409)
        finally:
            connection.close()

    async def test_queued_and_failed_meetings_can_be_deleted(self) -> None:
        failed_id = (await self._upload(b"failed")).json()["meeting_id"]
        queued_id = (await self._upload(b"queued")).json()["meeting_id"]
        connection = open_meeting_db(self.data_dir)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.fail(failed_id, "invalid_audio")
            failed_detail = await self.client.get(f"/api/meetings/{failed_id}")
            self.assertIsNone(failed_detail.json()["transcript"])
            for meeting_id in (queued_id, failed_id):
                self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
                self.assertIsNone(repository.get_meeting(meeting_id))
            self.assertEqual(list((self.data_dir / "audio").iterdir()), [])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
