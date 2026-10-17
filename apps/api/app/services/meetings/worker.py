"""Single-process local worker for queued meetings."""

from __future__ import annotations

import fcntl
import logging
import multiprocessing
import os
import signal
import time
from contextlib import contextmanager
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Iterator

from app.core.audio_models import ModelLoadError, ModelRegistry
from app.core.config import Settings, get_settings
from app.db.session import initialize_meeting_store, open_meeting_db
from app.pipelines.meeting_pipeline import process_audio
from app.services.audio.preprocess import AudioTooLongError, InvalidAudioError
from app.services.meetings.repository import MeetingAudioFile, MeetingRepository
from app.services.meetings.storage import audio_path, cleanup_orphaned_audio
from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment


logger = logging.getLogger(__name__)


def process_start_ticks(pid: int) -> int | None:
    """Read the Linux process start ticks used to distinguish reused PIDs."""
    try:
        _, fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", maxsplit=1)
    except (FileNotFoundError, ValueError):
        return None
    return int(fields.split()[19])


def terminate_process(pid: int, start_ticks: int) -> bool:
    """Kill a child only when its PID still identifies the recorded process."""
    if process_start_ticks(pid) != start_ticks:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


@contextmanager
def exclusive_worker_lock(data_dir: Path) -> Iterator[None]:
    """Prevent concurrent workers for one local data directory."""
    descriptor = os.open(data_dir / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def run_claimed_meeting(meeting_id: str, data_dir: Path, settings: Settings, start_event: Event) -> None:
    """Process a claimed meeting in an interruptible child process."""
    start_event.wait()
    connection = open_meeting_db(data_dir)
    try:
        repository = MeetingRepository(connection)
        meeting = repository.get_meeting(meeting_id)
        if meeting is None or meeting.status != "processing":
            return
        try:
            parts = repository.get_audio_files(meeting_id)
            result, durations = process_meeting_parts(parts, data_dir, ModelRegistry(settings), meeting.source)
            repository.complete(meeting.id, result, durations)
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
    finally:
        connection.close()


def process_meeting_parts(
    parts: list[MeetingAudioFile], data_dir: Path, models: ModelRegistry,
    source: str = "upload",
) -> tuple[AudioProcessResponse, list[float]]:
    """Process ordered recordings and shift segments to meeting time."""
    if not parts:
        raise InvalidAudioError("Meeting has no audio files")
    offset = 0.0
    total_end = 0.0
    durations: list[float] = []
    transcripts: list[str] = []
    vad_segments: list[SpeechSegment] = []
    diarization_segments: list[SpeakerTurn] = []
    transcript_segments: list[TranscriptSegment] = []
    for part in parts:
        data = audio_path(data_dir, part.storage_key).read_bytes()
        result = process_audio(data, part.original_filename, models)
        if result.duration_seconds > 30 * 60:
            raise AudioTooLongError("Audio part must be at most 30 minutes long")
        part_start = part.start_seconds if source == "google_meet_bot" else offset
        if part_start is None or part_start < 0:
            raise InvalidAudioError("Meeting part has no valid start time")
        if source == "upload" and offset + result.duration_seconds > 30 * 60:
            raise AudioTooLongError("Meeting audio must be at most 30 minutes long")
        speaker_prefix = f"PART_{part.ordinal + 1}_" if part.ordinal else ""
        vad_segments.extend(segment.model_copy(update={
            "start": segment.start + part_start, "end": segment.end + part_start,
        }) for segment in result.vad_segments)
        diarization_segments.extend(segment.model_copy(update={
            "start": segment.start + part_start, "end": segment.end + part_start,
            "speaker": speaker_prefix + segment.speaker,
        }) for segment in result.diarization_segments)
        transcript_segments.extend(segment.model_copy(update={
            "start": segment.start + part_start, "end": segment.end + part_start,
            "speaker": speaker_prefix + segment.speaker,
        }) for segment in result.transcript_segments)
        if result.transcript:
            transcripts.append(result.transcript)
        durations.append(result.duration_seconds)
        offset += result.duration_seconds
        total_end = max(total_end, part_start + result.duration_seconds)
    return AudioProcessResponse(
        filename=parts[0].original_filename, duration_seconds=total_end,
        transcript=" ".join(transcripts), vad_segments=vad_segments,
        diarization_segments=diarization_segments, transcript_segments=transcript_segments,
    ), durations


def run_once(repository: MeetingRepository, data_dir: Path, settings: Settings) -> bool:
    """Claim and supervise one interruptible meeting job."""
    meeting = repository.claim_next()
    if meeting is None:
        return False
    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    process = context.Process(
        target=run_claimed_meeting, args=(meeting.id, data_dir, settings, start_event), daemon=True
    )
    process.start()
    start_ticks = process_start_ticks(process.pid)
    if start_ticks is None or not repository.set_pipeline_process(meeting.id, process.pid, start_ticks):
        process.kill()
    else:
        start_event.set()
    process.join()
    return True


def main() -> None:
    """Run one reusable model registry until interrupted."""
    settings = get_settings()
    data_dir = settings.meeting_data_dir
    initialize_meeting_store(data_dir)
    with exclusive_worker_lock(data_dir):
        cleanup_orphaned_audio(data_dir)
        connection = open_meeting_db(data_dir)
        try:
            repository = MeetingRepository(connection)
            repository.recover_interrupted()
            while True:
                if not run_once(repository, data_dir, settings):
                    time.sleep(1)
        except KeyboardInterrupt:
            return
        finally:
            connection.close()


if __name__ == "__main__":
    main()
