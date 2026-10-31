import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException

from app.api.routes import audio
from app.core.config import ConfigurationError, Settings
from app.main import app
from app.schemas.transcript import AudioProcessResponse


class AudioLimitSettingsTest(unittest.TestCase):
    def test_default_and_configured_limit(self) -> None:
        self.assertEqual(Settings.from_environment({}).audio_max_concurrent_inferences, 1)
        self.assertEqual(Settings.from_environment({'AUDIO_MAX_CONCURRENT_INFERENCES': '2'}).audio_max_concurrent_inferences, 2)

    def test_invalid_limits(self) -> None:
        for value in ('0', '-1', 'bad', '1.5', ''):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Settings.from_environment({'AUDIO_MAX_CONCURRENT_INFERENCES': value})


class AudioInferenceLimitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.slots = threading.BoundedSemaphore(1)
        self.slot_patch = patch.object(audio, 'get_inference_slots', return_value=self.slots)
        self.slot_provider = self.slot_patch.start()
        self.addCleanup(self.slot_patch.stop)
        self.models = Mock()
        self.response = AudioProcessResponse(filename='audio.wav', duration_seconds=1, transcript='',
            vad_segments=[], diarization_segments=[], transcript_segments=[])

    async def start_inference(self) -> tuple[asyncio.Task[AudioProcessResponse], threading.Event, asyncio.Event]:
        started = asyncio.Event()
        released = threading.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def process(*args):
            loop.call_soon_threadsafe(started.set)
            try:
                if not released.wait(5):
                    raise TimeoutError('Test did not release inference')
                return self.response
            finally:
                loop.call_soon_threadsafe(finished.set)

        processor = patch.object(audio, 'process_audio', side_effect=process)
        processor.start()
        self.addCleanup(processor.stop)
        self.addCleanup(released.set)
        task = asyncio.create_task(audio._run_pipeline(b'audio', 'audio.wav', self.models))
        await asyncio.wait_for(started.wait(), 2)
        return task, released, finished

    async def assert_busy(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await asyncio.wait_for(audio._run_pipeline(b'audio', 'audio.wav', self.models), .2)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn('Retry-After', caught.exception.headers)

    async def test_overload_is_rejected_and_success_releases_slot(self) -> None:
        task, released, _ = await self.start_inference()
        try:
            await self.assert_busy()
        finally:
            released.set()
            await task
        self.assertEqual(await audio._run_pipeline(b'audio', 'audio.wav', self.models), self.response)

    async def test_cancellation_keeps_slot_until_worker_finishes(self) -> None:
        task, released, finished = await self.start_inference()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        try:
            await self.assert_busy()
        finally:
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(await audio._run_pipeline(b'audio', 'audio.wav', self.models), self.response)

    async def test_pipeline_failure_releases_slot(self) -> None:
        with patch.object(audio, 'process_audio', side_effect=RuntimeError('failed')):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                await audio._run_pipeline(b'audio', 'audio.wav', self.models)
        with patch.object(audio, 'process_audio', return_value=self.response):
            self.assertEqual(await audio._run_pipeline(b'audio', 'audio.wav', self.models), self.response)

    async def test_configured_capacity_allows_two_workers(self) -> None:
        self.slot_provider.return_value = threading.BoundedSemaphore(2)
        first, release_first, _ = await self.start_inference()
        second, release_second, _ = await self.start_inference()
        try:
            await self.assert_busy()
        finally:
            release_first.set()
            release_second.set()
            await asyncio.gather(first, second)

    async def test_executor_submission_failure_releases_slot(self) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'run_in_executor', side_effect=RuntimeError('executor stopped')):
            with self.assertRaisesRegex(RuntimeError, 'executor stopped'):
                await audio._run_pipeline(b'audio', 'audio.wav', self.models)
        with patch.object(audio, 'process_audio', return_value=self.response):
            self.assertEqual(await audio._run_pipeline(b'audio', 'audio.wav', self.models), self.response)

    async def test_overload_http_response(self) -> None:
        from app.api.deps import get_models
        app.dependency_overrides[get_models] = lambda: self.models
        self.addCleanup(app.dependency_overrides.pop, get_models)
        self.slots.acquire()
        try:
            with patch.object(audio, 'process_audio', return_value=self.response) as process:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                    response = await client.post('/api/audio/process', files={'file': ('audio.wav', b'audio')})
                self.assertEqual(response.status_code, 429)
                self.assertIn('Retry-After', response.headers)
                process.assert_not_called()
        finally:
            self.slots.release()
