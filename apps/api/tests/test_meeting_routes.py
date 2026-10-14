"""HTTP upload and local-access contracts for stored meetings."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.main import app


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
        headers = {"Idempotency-Key": key} if key else {}
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

    async def test_title_and_idempotency_key_lengths_are_bounded(self) -> None:
        self.assertEqual((await self._upload(b"x", title="t" * 201)).status_code, 422)
        self.assertEqual((await self._upload(b"x", key="k" * 201)).status_code, 422)


if __name__ == "__main__":
    unittest.main()
