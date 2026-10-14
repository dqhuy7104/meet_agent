"""Single-process local worker for queued meetings."""

from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.audio_models import ModelLoadError, ModelRegistry
from app.core.config import get_settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.pipelines.meeting_pipeline import process_audio
from app.services.audio.preprocess import AudioTooLongError, InvalidAudioError
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import audio_path, cleanup_orphaned_audio


logger = logging.getLogger(__name__)


@contextmanager
def exclusive_worker_lock(data_dir: Path) -> Iterator[None]:
    """Prevent concurrent workers for one local data directory."""
    descriptor = os.open(data_dir / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def run_once(repository: MeetingRepository, data_dir: Path, models: ModelRegistry) -> bool:
    """Claim and process one queued meeting, returning false when none remain."""
    meeting = repository.claim_next()
    if meeting is None:
        return False
    try:
        data = audio_path(data_dir, meeting.storage_key).read_bytes()
        result = process_audio(data, meeting.original_filename, models)
        repository.complete(meeting.id, result)
    except AudioTooLongError:
        repository.fail(meeting.id, "audio_too_long")
        logger.warning("Meeting %s failed: audio_too_long", meeting.id)
    except InvalidAudioError:
        repository.fail(meeting.id, "invalid_audio")
        logger.warning("Meeting %s failed: invalid_audio", meeting.id)
    except ModelLoadError:
        repository.fail(meeting.id, "model_unavailable")
        logger.warning("Meeting %s failed: model_unavailable", meeting.id)
    except Exception:
        repository.fail(meeting.id, "processing_error")
        logger.warning("Meeting %s failed: processing_error", meeting.id)
    return True


def main() -> None:
    """Run one reusable model registry until interrupted."""
    settings = get_settings()
    data_dir = settings.meeting_data_dir
    initialize_meeting_store(data_dir)
    models = ModelRegistry(settings)
    with exclusive_worker_lock(data_dir):
        cleanup_orphaned_audio(data_dir)
        connection = open_meeting_db(data_dir)
        try:
            repository = MeetingRepository(connection)
            repository.recover_interrupted()
            while True:
                if not run_once(repository, data_dir, models):
                    time.sleep(1)
        except KeyboardInterrupt:
            return
        finally:
            connection.close()


if __name__ == "__main__":
    main()
