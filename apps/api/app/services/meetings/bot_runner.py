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
from app.db.session import open_database, validate_database
from app.core.database_config import DatabaseSettings, DatabaseUnavailableError, get_database_settings
from app.db.worker_locks import worker_lease
import psycopg
from app.services.meetings.bot_capture import CaptureUnavailable, PulseWavRecorder, RecordedPart, check_bot_runtime
from app.services.meetings.bot_repository import BotSessionRepository, BotStateConflict
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.meet_browser import ChromeMeetBrowser, JoinFailed
from app.services.meetings.storage import (
    audio_path, audio_publication_lock, cleanup_orphaned_audio, cleanup_bot_temporary_audio,
    publish_audio_file, initialize_meeting_storage,
)

@contextmanager
def exclusive_bot_lock(data_dir: Path) -> Iterator[None]:
    descriptor = os.open(data_dir / 'bot-runner.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)

async def _wait_for_stop(repository: BotSessionRepository, session_id: str) -> None:
    while True:
        session = await asyncio.to_thread(repository.get, session_id)
        if session is None or session.stop_requested:
            return
        await asyncio.sleep(0.2)

def _register_part(repository: BotSessionRepository, data_dir: Path, session_id: str, temporary_path: Path, part: RecordedPart, first_start: float | None) -> float:
    origin = part.started_monotonic if first_start is None else first_start
    start_seconds = max(0.0, part.started_monotonic - origin)
    with audio_publication_lock(data_dir):
        key = publish_audio_file(data_dir, temporary_path)
        try:
            repository.add_part(session_id, key, start_seconds, part.duration_seconds)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except BaseException:
            audio_path(data_dir, key).unlink(missing_ok=True)
            raise
    return origin

async def run_session(session_id: str, data_dir: Path, settings: Settings, browser_factory: Callable[..., ChromeMeetBrowser]=ChromeMeetBrowser, recorder_factory: Callable[..., PulseWavRecorder]=PulseWavRecorder, *, database: DatabaseSettings | None=None) -> None:
    opening = asyncio.create_task(asyncio.to_thread(open_database, database or get_database_settings()))
    try:
        connection = await asyncio.shield(opening)
    except asyncio.CancelledError:
        connection = await opening
        await asyncio.to_thread(connection.close)
        raise
    try:
        await asyncio.to_thread(validate_database, connection)
        await _run_session(connection, session_id, data_dir, settings, browser_factory, recorder_factory)
    finally:
        await asyncio.to_thread(connection.close)


async def _run_session(connection: psycopg.Connection, session_id: str, data_dir: Path,
                       settings: Settings, browser_factory: Callable, recorder_factory: Callable) -> None:

    repository = BotSessionRepository(connection, data_dir)
    browser: ChromeMeetBrowser | None = None
    end_task: asyncio.Task[None] | None = None
    admission_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    record_task: asyncio.Task[RecordedPart] | None = None
    stop_recording: asyncio.Event | None = None
    temporary_path: Path | None = None
    try:
        session = await asyncio.to_thread(repository.get, session_id)
        if session is None or session.status != 'joining':
            return
        if session.stop_requested:
            await asyncio.to_thread(repository.set_status, session_id, 'cancelled')
            return
        check_bot_runtime(settings)
        browser = browser_factory(settings)
        await browser.start(session.url or '')
        admission_task = asyncio.create_task(browser.wait_for_admission(settings.bot_admission_timeout_seconds, lambda: repository.set_status(session_id, 'waiting_admission')))
        stop_task = asyncio.create_task(_wait_for_stop(repository, session_id))
        done, pending = await asyncio.wait({admission_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
            await asyncio.to_thread(repository.set_status, session_id, 'cancelled')
            return
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        admission_task.result()
        if (await asyncio.to_thread(repository.get, session_id)).stop_requested:
            await asyncio.to_thread(repository.set_status, session_id, 'cancelled')
            return
        await asyncio.to_thread(repository.set_status, session_id, 'recording')
        recorder = recorder_factory(settings)
        first_start: float | None = None
        end_task = asyncio.create_task(browser.wait_for_end())
        while True:
            temporary_path = data_dir / 'bot-tmp' / f'{uuid.uuid4().hex}.wav'
            stop_recording = asyncio.Event()
            record_task = asyncio.create_task(recorder.record_part(temporary_path, stop_recording))
            stop_task = asyncio.create_task(_wait_for_stop(repository, session_id))
            done, pending = await asyncio.wait({record_task, stop_task, end_task}, return_when=asyncio.FIRST_COMPLETED)
            should_end = stop_task in done or end_task in done
            if should_end:
                stop_recording.set()
            try:
                part = await record_task
                first_start = await asyncio.to_thread(_register_part, repository, data_dir, session_id, temporary_path, part, first_start)
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                temporary_path.unlink(missing_ok=True)
            if end_task in done:
                end_task.result()
            if should_end or (await asyncio.to_thread(repository.get, session_id)).stop_requested:
                await asyncio.to_thread(repository.set_status, session_id, 'finalizing')
                with audio_publication_lock(data_dir):
                    await asyncio.to_thread(repository.finalize, session_id)
                return
    except (CaptureUnavailable, JoinFailed) as error:
        current = await asyncio.to_thread(repository.get, session_id)
        if current and current.status not in {'completed', 'cancelled', 'interrupted'}:
            await asyncio.to_thread(repository.set_status, session_id, 'interrupted', str(error))
    except psycopg.Error:
        raise
    except Exception:
        current = await asyncio.to_thread(repository.get, session_id)
        if current and current.status not in {'completed', 'cancelled', 'interrupted'}:
            await asyncio.to_thread(repository.set_status, session_id, 'interrupted', 'runner_error')
    finally:
        if stop_recording is not None:
            stop_recording.set()
        if record_task is not None and (not record_task.done()):
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
            try:
                current = await asyncio.to_thread(repository.get, session_id)
                if current and current.status not in {'completed', 'cancelled', 'interrupted'}:
                    await asyncio.to_thread(repository.set_status, session_id, 'interrupted', 'runner_interrupted')
            except psycopg.Error:
                pass  # Recovery marks the session interrupted after reconnection.

async def _run_loop(settings: Settings) -> None:
    data_dir, database = (settings.meeting_data_dir, get_database_settings())
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_bot_lock(data_dir):
        while True:
            try:
                with worker_lease(database, 'bot') as lease, open_database(database) as connection:
                    await asyncio.to_thread(initialize_meeting_storage, data_dir, connection)
                    repository = BotSessionRepository(connection, data_dir)
                    await asyncio.to_thread(repository.recover_stale)
                    await asyncio.to_thread(cleanup_orphaned_audio, data_dir, MeetingRepository(connection))
                    await asyncio.to_thread(cleanup_bot_temporary_audio, data_dir, connection)
                    while True:
                        await asyncio.to_thread(lease.ensure_owned)
                        session = await asyncio.to_thread(repository.claim_next)
                        if session is None:
                            await asyncio.sleep(1)
                            continue
                        running = asyncio.create_task(run_session(session.id, data_dir, settings, database=database))
                        try:
                            while not running.done():
                                await asyncio.wait({running}, timeout=0.2)
                                await asyncio.to_thread(lease.ensure_owned)
                            await running
                        finally:
                            if not running.done():
                                running.cancel()
                            await asyncio.gather(running, return_exceptions=True)
            except (psycopg.OperationalError, DatabaseUnavailableError) as error:
                if getattr(error, 'code', '') not in ('', 'worker_ownership_lost'):
                    raise
                await asyncio.sleep(1)

def main() -> None:
    try:
        asyncio.run(_run_loop(get_settings()))
    except KeyboardInterrupt:
        return
if __name__ == '__main__':
    main()
