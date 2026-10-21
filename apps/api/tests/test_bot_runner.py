"""Bot runner stop, rollover, interruption, and process ownership."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.db.session import open_database
from app.services.meetings.storage import initialize_meeting_storage
from apps.api.tests.postgres_support import PostgresTestCase, PostgresAsyncTestCase
from app.services.meetings.bot_capture import RecordedPart
from app.services.meetings.bot_repository import BotSessionRepository
from app.services.meetings.bot_runner import exclusive_bot_lock, run_session
from app.services.meetings.meet_browser import JoinFailed
from app.services.meetings.repository import MeetingRepository


class FakeBrowser:
    def __init__(self, settings: Settings, admitted: bool = True, crashes: bool = False) -> None:
        self.admitted = admitted
        self.crashes = crashes
        self.closed = False
        self.ended = asyncio.Event()
        self.started = False

    async def start(self, url: str) -> None:
        self.started = True

    async def wait_for_admission(self, timeout_seconds: int, on_waiting) -> None:
        if not self.admitted:
            on_waiting()
            await asyncio.Event().wait()

    async def wait_for_end(self) -> None:
        if self.crashes:
            await asyncio.sleep(0.06)
            raise JoinFailed("browser_closed")
        await self.ended.wait()

    async def close(self) -> None:
        self.closed = True


class FakeRecorder:
    def __init__(self, settings: Settings) -> None:
        self.calls = 0
        self.was_stopped = False

    async def record_part(self, temporary_path: Path, stop: asyncio.Event, max_seconds: int = 1790) -> RecordedPart:
        self.calls += 1
        started = time.monotonic()
        if self.calls > 1:
            await stop.wait()
            self.was_stopped = True
        temporary_path.write_bytes(f"part-{self.calls}".encode())
        return RecordedPart(1.0, started, time.monotonic())


class BotRunnerTest(PostgresAsyncTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        initialize_meeting_storage(self.data_dir, self.connection)
        self.connection = open_database(self.database)
        self.addCleanup(self.connection.close)
        self.repository = BotSessionRepository(self.connection, self.data_dir)
        self.settings = Settings(meeting_data_dir=self.data_dir, bot_audio_sink="meet_bot")
        self.session = self.repository.create("https://meet.google.com/abc-defg-hij", None, None)
        self.repository.claim_next()

    async def test_waiting_stop_creates_no_meeting_or_audio(self) -> None:
        browser = FakeBrowser(self.settings, admitted=False)
        recorder = FakeRecorder(self.settings)
        with patch("app.services.meetings.bot_runner.check_bot_runtime"):
            task = asyncio.create_task(run_session(self.session.id, self.data_dir, self.settings, lambda _: browser, lambda _: recorder))
            await asyncio.sleep(0.05)
            self.repository.request_stop(self.session.id)
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.repository.get(self.session.id).status, "cancelled")
        self.assertEqual(MeetingRepository(self.connection).count_meetings(), 0)
        self.assertEqual(recorder.calls, 0)
        self.assertTrue(browser.closed)

    async def test_recorded_parts_finalize_to_one_meeting_and_stop_recorder(self) -> None:
        browser = FakeBrowser(self.settings)
        recorder = FakeRecorder(self.settings)
        with patch("app.services.meetings.bot_runner.check_bot_runtime"):
            task = asyncio.create_task(run_session(self.session.id, self.data_dir, self.settings, lambda _: browser, lambda _: recorder))
            await asyncio.sleep(0.1)
            self.repository.request_stop(self.session.id)
            await asyncio.wait_for(task, 2)
        session = self.repository.get(self.session.id)
        self.assertEqual(session.status, "completed")
        self.assertTrue(recorder.was_stopped)
        self.assertEqual(MeetingRepository(self.connection).count_meetings(), 1)
        self.assertEqual(len(MeetingRepository(self.connection).get_audio_files(session.meeting_id)), 2)

    async def test_browser_exit_keeps_completed_parts_and_marks_interrupted(self) -> None:
        browser = FakeBrowser(self.settings, crashes=True)
        recorder = FakeRecorder(self.settings)
        with patch("app.services.meetings.bot_runner.check_bot_runtime"):
            await asyncio.wait_for(run_session(
                self.session.id, self.data_dir, self.settings, lambda _: browser, lambda _: recorder,
            ), 2)
        self.assertEqual(self.repository.get(self.session.id).status, "interrupted")
        self.assertGreaterEqual(len(self.repository.parts(self.session.id)), 1)
        self.assertEqual(MeetingRepository(self.connection).count_meetings(), 0)

    async def test_restart_recovery_does_not_rejoin(self) -> None:
        self.assertEqual(self.repository.recover_stale(), 1)
        self.assertIsNone(self.repository.claim_next())
        self.assertEqual(self.repository.get(self.session.id).status, "interrupted")

    async def test_second_runner_cannot_hold_file_lock(self) -> None:
        with exclusive_bot_lock(self.data_dir):
            with self.assertRaises(BlockingIOError):
                with exclusive_bot_lock(self.data_dir):
                    pass

    async def test_cancelling_runner_settles_active_recorder(self) -> None:
        class WaitingRecorder:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.finished = asyncio.Event()

            async def record_part(self, temporary_path: Path, stop: asyncio.Event, max_seconds: int = 1790):
                self.entered.set()
                try:
                    await stop.wait()
                finally:
                    self.finished.set()

        recorder = WaitingRecorder()
        browser = FakeBrowser(self.settings)
        with patch("app.services.meetings.bot_runner.check_bot_runtime"):
            task = asyncio.create_task(run_session(
                self.session.id, self.data_dir, self.settings, lambda _: browser, lambda _: recorder,
            ))
            await asyncio.wait_for(recorder.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(recorder.finished.is_set())
