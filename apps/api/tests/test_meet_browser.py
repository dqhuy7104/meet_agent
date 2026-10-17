"""Meet browser state detection with a fake Playwright surface."""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.services.meetings.meet_browser import ChromeMeetBrowser, JoinFailed


class FakeLocator:
    def __init__(self, page: "FakePage", name: re.Pattern[str]) -> None:
        self.page = page
        self.name = name

    async def is_visible(self) -> bool:
        return any(self.name.search(label) for label in self.page.labels())

    async def click(self) -> None:
        if self.name.search("Turn off microphone"):
            self.page.mic_on = False
        elif self.name.search("Turn off camera"):
            self.page.camera_on = False
        elif self.name.search("Join now"):
            self.page.joined = True

    async def fill(self, value: str) -> None:
        self.page.display_name = value


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.mic_on = True
        self.camera_on = True
        self.joined = False
        self.admitted = False
        self.display_name = ""
        self.closed = False

    async def goto(self, url: str, **kwargs) -> None:
        self.url = url

    def labels(self) -> list[str]:
        labels = ["Join now", "Your name"] if not self.joined else []
        if self.mic_on:
            labels.append("Turn off microphone")
        else:
            labels.append("Turn on microphone")
        if self.camera_on:
            labels.append("Turn off camera")
        else:
            labels.append("Turn on camera")
        if self.admitted and self.joined:
            labels.append("Leave call")
        if self.joined and not self.admitted:
            labels.append("You left the meeting")
        return labels

    def get_by_role(self, role: str, name: re.Pattern[str]) -> FakeLocator:
        return FakeLocator(self, name)

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.pages = [self.page]
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True
        self.page.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.launch_kwargs = {}
        self.stopped = False
        self.chromium = self

    async def launch_persistent_context(self, user_data_dir: str, **kwargs) -> FakeContext:
        self.launch_kwargs = kwargs
        self.user_data_dir = user_data_dir
        return self.context

    async def stop(self) -> None:
        self.stopped = True


class MeetBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fake = FakePlaywright()
        self.browser = ChromeMeetBrowser(
            Settings(meeting_data_dir=Path(self.temporary.name), bot_audio_sink="meet_bot",
                     bot_chrome_path=Path("/usr/bin/true")),
            playwright_factory=lambda: self.fake,
        )
        self.browser._poll_seconds = 0.01

    async def test_join_disables_mic_camera_and_waits_for_admission(self) -> None:
        await self.browser.start("https://meet.google.com/abc-defg-hij")
        page = self.fake.context.page
        self.assertFalse(page.mic_on)
        self.assertFalse(page.camera_on)
        self.assertTrue(page.joined)
        self.assertFalse(page.admitted)
        self.assertFalse(self.fake.launch_kwargs["headless"])
        self.assertEqual(self.fake.launch_kwargs["env"]["PULSE_SINK"], "meet_bot")
        waiting: list[bool] = []

        async def admit() -> None:
            await asyncio.sleep(0.04)
            page.admitted = True

        admit_task = asyncio.create_task(admit())
        await self.browser.wait_for_admission(1, lambda: waiting.append(True))
        await admit_task
        self.assertTrue(waiting)

    async def test_account_challenge_or_timeout_reports_join_failure(self) -> None:
        await self.browser.start("https://meet.google.com/abc-defg-hij")
        with self.assertRaises(JoinFailed):
            await self.browser.wait_for_admission(0.03, lambda: None)
        self.fake.context.page.url = "https://accounts.google.com/signin"
        with self.assertRaises(JoinFailed):
            await self.browser.wait_for_admission(1, lambda: None)

    async def test_browser_end_and_close_release_context(self) -> None:
        await self.browser.start("https://meet.google.com/abc-defg-hij")
        page = self.fake.context.page
        page.admitted = True
        await self.browser.wait_for_admission(1, lambda: None)
        page.admitted = False
        await self.browser.wait_for_end()
        await self.browser.close()
        self.assertTrue(self.fake.context.closed)
        self.assertTrue(self.fake.stopped)

    async def test_unexpected_browser_close_is_interruption(self) -> None:
        await self.browser.start("https://meet.google.com/abc-defg-hij")
        self.fake.context.page.closed = True
        with self.assertRaises(JoinFailed):
            await self.browser.wait_for_end()
