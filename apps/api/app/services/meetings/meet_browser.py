"""Visible Google Meet participant controlled through a private Chrome profile."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.services.meetings.bot_capture import chrome_executable


class JoinFailed(RuntimeError):
    """Meet entry could not be confirmed; message is a safe failure code."""


_MIC_ON = re.compile(r"turn off microphone|tắt micrô|tắt micro", re.I)
_MIC_OFF = re.compile(r"turn on microphone|bật micrô|bật micro", re.I)
_CAM_ON = re.compile(r"turn off camera|tắt máy ảnh|tắt camera", re.I)
_CAM_OFF = re.compile(r"turn on camera|bật máy ảnh|bật camera", re.I)
_JOIN = re.compile(r"join now|ask to join|tham gia ngay|yêu cầu tham gia", re.I)
_LEAVE = re.compile(r"leave call|leave meeting|rời khỏi cuộc gọi|rời cuộc họp", re.I)
_ENDED = re.compile(r"you left the meeting|meeting has ended|bạn đã rời khỏi cuộc họp|cuộc họp đã kết thúc", re.I)
_DENIED = re.compile(r"you can.t join this video call|you cannot join this video call", re.I)
_NAME = re.compile(r"your name|tên của bạn", re.I)


class ChromeMeetBrowser:
    def __init__(self, settings: Settings, playwright_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self._playwright_factory = playwright_factory
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._poll_seconds = 0.5

    async def start(self, url: str) -> None:
        """Open the Meet prejoin page with input devices disabled."""
        if not self.settings.bot_audio_sink:
            raise JoinFailed("audio_sink_unconfigured")
        profile = self.settings.bot_profile_dir
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        stage = "launch"
        try:
            if self._playwright_factory:
                self._playwright = self._playwright_factory()
            else:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile), executable_path=chrome_executable(self.settings), headless=False,
                env={**os.environ, "PULSE_SINK": self.settings.bot_audio_sink},
                ignore_default_args=["--mute-audio"],
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            stage = "navigation"
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if "accounts.google.com" in self._page.url:
                raise JoinFailed("account_challenge")
            await self._check_page_ready()
            stage = "device_control"
            await self._disable_device(_MIC_ON, _MIC_OFF)
            await self._disable_device(_CAM_ON, _CAM_OFF)
            stage = "name_control"
            name_field = self._page.get_by_role("textbox", name=_NAME)
            if await name_field.is_visible():
                await name_field.fill("Meeting AI Recorder")
            stage = "join_control"
            join_button = self._page.get_by_role("button", name=_JOIN)
            await self._wait_visible(join_button, 30, "join_control_unavailable")
            stage = "join_click"
            await join_button.click()
        except JoinFailed:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            raise JoinFailed(f"browser_{stage}_failed") from error

    async def _disable_device(self, on_pattern: re.Pattern[str], off_pattern: re.Pattern[str]) -> None:
        on_button = self._page.get_by_role("button", name=on_pattern)
        off_button = self._page.get_by_role("button", name=off_pattern)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if await off_button.is_visible():
                return
            if await on_button.is_visible():
                await on_button.click()
                await self._wait_visible(off_button, 5, "device_state_unconfirmed")
                return
            await self._check_page_ready()
            await asyncio.sleep(self._poll_seconds)
        raise JoinFailed("device_state_unconfirmed")

    async def _wait_visible(self, locator: Any, timeout_seconds: int, failure_code: str) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await locator.is_visible():
                return
            await self._check_page_ready()
            await asyncio.sleep(self._poll_seconds)
        raise JoinFailed(failure_code)

    async def _check_page_ready(self) -> None:
        if self._page.is_closed() or "accounts.google.com" in self._page.url:
            raise JoinFailed("account_challenge_or_browser_closed")
        if await self._page.get_by_role("heading", name=_DENIED).is_visible():
            raise JoinFailed("meeting_access_denied")

    async def wait_for_admission(self, timeout_seconds: int, on_waiting: Callable[[], None]) -> None:
        """Confirm the in-call leave control before any recorder starts."""
        if self._page is None:
            raise JoinFailed("browser_not_started")
        deadline = time.monotonic() + timeout_seconds
        waiting_reported = False
        while time.monotonic() < deadline:
            await self._check_page_ready()
            if await self._page.get_by_role("button", name=_LEAVE).is_visible():
                return
            if not waiting_reported:
                on_waiting()
                waiting_reported = True
            await asyncio.sleep(self._poll_seconds)
        raise JoinFailed("admission_timeout")

    async def wait_for_end(self) -> None:
        if self._page is None:
            return
        missing_control_since: float | None = None
        while True:
            if self._page.is_closed():
                raise JoinFailed("browser_closed")
            if await self._page.get_by_role("heading", name=_ENDED).is_visible():
                return
            if await self._page.get_by_role("button", name=_LEAVE).is_visible():
                missing_control_since = None
            elif missing_control_since is None:
                missing_control_since = time.monotonic()
            elif time.monotonic() - missing_control_since > 10:
                raise JoinFailed("meeting_state_lost")
            await asyncio.sleep(self._poll_seconds)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
