"""Plain-text transcript search contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from app.api.routes.meetings import get_meeting_settings
from app.core.config import Settings
from app.db.session import open_meeting_db
from app.main import app
from app.schemas.transcript import AudioProcessResponse, TranscriptSegment
from app.services.meetings.repository import MeetingRepository


class MeetingSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "meetings"

        async def settings() -> Settings:
            return Settings(meeting_data_dir=self.data_dir)

        app.dependency_overrides[get_meeting_settings] = settings
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4000)), base_url="http://localhost"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        app.dependency_overrides.clear()

    async def _upload(self, title: str = "Planning") -> str:
        response = await self.client.post(
            "/api/meetings", files={"file": ("meeting.wav", b"audio", "audio/wav")}, data={"title": title}
        )
        self.assertEqual(response.status_code, 202)
        return response.json()["meeting_id"]

    def _complete(self, meeting_id: str, texts: list[str]) -> None:
        connection = open_meeting_db(self.data_dir)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            segments = [
                TranscriptSegment(start=float(index + 2), end=float(index + 3), speaker="SPEAKER_01", text=text)
                for index, text in enumerate(texts)
            ]
            repository.complete(meeting_id, AudioProcessResponse(
                filename="meeting.wav", duration_seconds=20.0, transcript=" ".join(texts),
                vad_segments=[], diarization_segments=[], transcript_segments=segments,
            ))
        finally:
            connection.close()

    async def test_vietnamese_result_has_meeting_speaker_and_original_seconds(self) -> None:
        meeting_id = await self._upload("Buổi họp")
        self._complete(meeting_id, ["Xin chào", "Kế hoạch quý mới"])
        response = await self.client.get("/api/meetings/search", params={"q": "Kế hoạch"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"], [{
            "meeting_id": meeting_id, "title": "Buổi họp", "speaker": "SPEAKER_01",
            "start": 3.0, "end": 4.0, "text": "Kế hoạch quý mới",
        }])

    async def test_queued_and_failed_meetings_do_not_appear(self) -> None:
        completed = await self._upload()
        self._complete(completed, ["Nội dung cuộc họp"])
        failed = await self._upload()
        queued = await self._upload()
        connection = open_meeting_db(self.data_dir)
        try:
            repository = MeetingRepository(connection)
            repository.claim_next()
            repository.fail(failed, "invalid_audio")
            connection.execute(
                """INSERT INTO transcript_fts(meeting_id, segment_id, text)
                VALUES (?, 999, 'Nội dung không hợp lệ')""", (failed,)
            )
            connection.commit()
        finally:
            connection.close()
        response = await self.client.get("/api/meetings/search", params={"q": "Nội dung"})
        self.assertEqual([item["meeting_id"] for item in response.json()["items"]], [completed])

    async def test_empty_and_malformed_fts_like_input_is_plain_text(self) -> None:
        meeting_id = await self._upload()
        self._complete(meeting_id, ["Xin chào"])
        for query in ("không-tìm-thấy", '" OR *', '"', "*"):
            with self.subTest(query=query):
                response = await self.client.get("/api/meetings/search", params={"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"], [])
        self.assertEqual((await self.client.get("/api/meetings/search", params={"q": " "})).status_code, 422)
        self.assertEqual((await self.client.get("/api/meetings/search", params={"q": "x" * 201})).status_code, 422)

    async def test_pagination_is_stable(self) -> None:
        meeting_id = await self._upload()
        self._complete(meeting_id, ["kế hoạch đầu", "kế hoạch sau"])
        first = await self.client.get("/api/meetings/search", params={"q": "kế hoạch", "limit": 1})
        second = await self.client.get("/api/meetings/search", params={"q": "kế hoạch", "limit": 1, "offset": 1})
        self.assertEqual(first.json()["total"], 2)
        self.assertEqual(first.json()["items"][0]["start"], 2.0)
        self.assertEqual(second.json()["items"][0]["start"], 3.0)
        self.assertEqual((await self.client.get("/api/meetings/search", params={"q": "kế", "limit": 101})).status_code, 422)

    async def test_delete_and_reprocessing_remove_stale_hits(self) -> None:
        meeting_id = await self._upload()
        self._complete(meeting_id, ["Nội dung cũ"])
        connection = open_meeting_db(self.data_dir)
        try:
            connection.execute("UPDATE meetings SET status='failed' WHERE id=?", (meeting_id,))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual((await self.client.post(f"/api/meetings/{meeting_id}/retry")).status_code, 202)
        self._complete(meeting_id, ["Nội dung mới"])
        old = await self.client.get("/api/meetings/search", params={"q": "cũ"})
        new = await self.client.get("/api/meetings/search", params={"q": "mới"})
        self.assertEqual(old.json()["items"], [])
        self.assertEqual(new.json()["total"], 1)
        self.assertEqual((await self.client.delete(f"/api/meetings/{meeting_id}")).status_code, 204)
        gone = await self.client.get("/api/meetings/search", params={"q": "mới"})
        self.assertEqual(gone.json()["items"], [])

    async def test_non_loopback_client_cannot_search(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4000)), base_url="http://localhost"
        ) as remote:
            response = await remote.get("/api/meetings/search", params={"q": "meeting"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
