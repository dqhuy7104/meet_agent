"""One local process supervises Chrome and publishes closed recording parts."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import Settings, get_settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.services.meetings.bot_capture import CaptureUnavailable, PulseWavRecorder, RecordedPart, check_bot_runtime
from app.services.meetings.bot_repository import BotSessionRepository, BotStateConflict
from app.services.meetings.meet_browser import ChromeMeetBrowser, JoinFailed
from app.services.meetings.storage import (
    audio_path, audio_publication_lock, cleanup_orphaned_audio, publish_audio_file,
)


@contextmanager
def exclusive_bot_lock(data_dir: Path) -> Iterator[None]:
    descriptor = os.open(data_dir / "bot-runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


async def _wait_for_stop(repository: BotSessionRepository, session_id: str) -> None:
    while True:
        session = repository.get(session_id)
        if session is None or session.stop_requested:
            return
        await asyncio.sleep(0.2)


def _register_part(
    repository: BotSessionRepository, data_dir: Path, session_id: str,
    temporary_path: Path, part: RecordedPart, first_start: float | None,
) -> float:
    origin = part.started_monotonic if first_start is None else first_start
    start_seconds = max(0.0, part.started_monotonic - origin)
    with audio_publication_lock(data_dir):
        key = publish_audio_file(data_dir, temporary_path)
        try:
            repository.add_part(session_id, key, start_seconds, part.duration_seconds)
        except BaseException:
            audio_path(data_dir, key).unlink(missing_ok=True)
            raise
    return origin


async def run_session(
    session_id: str,
    data_dir: Path,
    settings: Settings,
    browser_factory: Callable[..., ChromeMeetBrowser] = ChromeMeetBrowser,
    recorder_factory: Callable[..., PulseWavRecorder] = PulseWavRecorder,
) -> None:
    """Run a claimed session until stop, end, or a safe interruption."""
    connection = open_meeting_db(data_dir)
    repository = BotSessionRepository(connection)
    browser: ChromeMeetBrowser | None = None
    end_task: asyncio.Task[None] | None = None
    admission_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    record_task: asyncio.Task[RecordedPart] | None = None
    stop_recording: asyncio.Event | None = None
    temporary_path: Path | None = None
    try:
        session = repository.get(session_id)
        if session is None or session.status != "joining":
            return
        if session.stop_requested:
            repository.set_status(session_id, "cancelled")
            return
        check_bot_runtime(settings)
        browser = browser_factory(settings)
        await browser.start(session.url or "")
        admission_task = asyncio.create_task(browser.wait_for_admission(
            settings.bot_admission_timeout_seconds,
            lambda: repository.set_status(session_id, "waiting_admission"),
        ))
        stop_task = asyncio.create_task(_wait_for_stop(repository, session_id))
        done, pending = await asyncio.wait({admission_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
            repository.set_status(session_id, "cancelled")
            return
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        admission_task.result()
        if repository.get(session_id).stop_requested:
            repository.set_status(session_id, "cancelled")
            return
        repository.set_status(session_id, "recording")
        recorder = recorder_factory(settings)
        first_start: float | None = None
        end_task = asyncio.create_task(browser.wait_for_end())
        while True:
            temporary_path = data_dir / "bot-tmp" / f"{uuid.uuid4().hex}.wav"
            stop_recording = asyncio.Event()
            record_task = asyncio.create_task(recorder.record_part(temporary_path, stop_recording))
            stop_task = asyncio.create_task(_wait_for_stop(repository, session_id))
            done, pending = await asyncio.wait(
                {record_task, stop_task, end_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            should_end = stop_task in done or end_task in done
            if should_end:
                stop_recording.set()
            try:
                part = await record_task
                first_start = _register_part(repository, data_dir, session_id, temporary_path, part, first_start)
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                temporary_path.unlink(missing_ok=True)
            if end_task in done:
                end_task.result()
            if should_end or repository.get(session_id).stop_requested:
                repository.set_status(session_id, "finalizing")
                with audio_publication_lock(data_dir):
                    repository.finalize(session_id)
                return
    except (CaptureUnavailable, JoinFailed) as error:
        current = repository.get(session_id)
        if current and current.status not in {"completed", "cancelled", "interrupted"}:
            repository.set_status(session_id, "interrupted", str(error))
    except Exception:
        current = repository.get(session_id)
        if current and current.status not in {"completed", "cancelled", "interrupted"}:
            repository.set_status(session_id, "interrupted", "runner_error")
    finally:
        if stop_recording is not None:
            stop_recording.set()
        if record_task is not None and not record_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(record_task), timeout=6)
            except asyncio.TimeoutError:
                record_task.cancel()
        for task in (record_task, admission_task, stop_task, end_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if browser is not None:
            await browser.close()
        if asyncio.current_task() and asyncio.current_task().cancelling():
            current = repository.get(session_id)
            if current and current.status not in {"completed", "cancelled", "interrupted"}:
                repository.set_status(session_id, "interrupted", "runner_interrupted")
        connection.close()


async def _run_loop(settings: Settings) -> None:
    data_dir = settings.meeting_data_dir
    initialize_meeting_store(data_dir)
    with exclusive_bot_lock(data_dir):
        connection = open_meeting_db(data_dir)
        try:
            repository = BotSessionRepository(connection)
            repository.recover_stale()
            for temporary in (data_dir / "bot-tmp").iterdir():
                if temporary.is_file() or temporary.is_symlink():
                    temporary.unlink(missing_ok=True)
            cleanup_orphaned_audio(data_dir)
            while True:
                session = repository.claim_next()
                if session is None:
                    await asyncio.sleep(1)
                    continue
                await run_session(session.id, data_dir, settings)
        finally:
            connection.close()


def main() -> None:
    try:
        asyncio.run(_run_loop(get_settings()))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
