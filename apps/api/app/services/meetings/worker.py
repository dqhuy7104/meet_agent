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
from app.db.session import open_database, validate_database
from app.core.database_config import DatabaseSettings, DatabaseUnavailableError, get_database_settings
from app.db.worker_locks import WorkerLease, worker_lease
import psycopg
from app.services.voice.repository import VoiceRepository
from app.services.voice.enrollment import EnrollmentError, extract_upload_embedding, extract_meeting_embedding
from app.services.voice.matching import pipeline_fingerprint, match_speakers
from app.services.meetings.storage import initialize_meeting_storage
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


def run_claimed_meeting(meeting_id: str, data_dir: Path, settings: Settings, database: DatabaseSettings, start_event: Event, ownership_event: Event) -> None:
    """Process a claimed meeting in an interruptible child process."""
    start_event.wait()
    connection = open_database(database)
    try:
        repository = MeetingRepository(connection)
        meeting = repository.get_meeting(meeting_id)
        if meeting is None or meeting.status != "processing":
            return
        try:
            parts = repository.get_audio_files(meeting_id)
            embeddings = {} if settings.voice_identification_enabled else None
            embedding_errors: list[str] = []
            result, durations = process_meeting_parts(
                parts, data_dir, ModelRegistry(settings), meeting.source,
                speaker_embeddings=embeddings, embedding_errors=embedding_errors)
            identities, fingerprint = None, None
            voice_status = "disabled"
            if embeddings is not None:
                try:
                    if embedding_errors:
                        raise ValueError("embedding_extraction_failed")
                    fingerprint = pipeline_fingerprint(settings)
                    profiles = VoiceRepository(connection).profile_embeddings(fingerprint)
                    identities = match_speakers(embeddings, profiles,
                        settings.voice_match_min_cosine, settings.voice_match_min_margin)
                    for segment in result.diarization_segments:
                        identities.setdefault(segment.speaker, None)
                    voice_status = "completed"
                except Exception:
                    identities, fingerprint = None, None
                    voice_status = "failed"
                    logger.warning("Meeting %s voice identification failed", meeting.id)
            if ownership_event.is_set():
                repository.complete(meeting.id, result, durations, identities=identities,
                                    voice_status=voice_status, voice_fingerprint=fingerprint)
        except psycopg.Error:
            logger.warning("Meeting database unavailable")
            raise SystemExit(1) from None
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


def run_claimed_enrollment(job_id: str, data_dir: Path, settings: Settings, database: DatabaseSettings,
                           start_event: Event, ownership_event: Event) -> None:
    """Extract outside transactions and publish only while the worker owns its lease."""
    start_event.wait()
    connection = open_database(database)
    repository = VoiceRepository(connection)
    job = None
    try:
        job = repository.get_job(job_id)
        if job is None or job.status != 'processing':
            return
        try:
            fingerprint = pipeline_fingerprint(settings)
            models = ModelRegistry(settings)
            if job.source == 'upload':
                embedding = extract_upload_embedding(audio_path(data_dir, job.storage_key).read_bytes(), models)
            else:
                detail = MeetingRepository(connection).get_detail(job.source_meeting_id)
                if detail is None:
                    raise EnrollmentError('source_changed')
                embedding = extract_meeting_embedding(detail, job.source_speaker, data_dir, models)
            if ownership_event.is_set():
                repository.complete_job(job_id, embedding, fingerprint)
        except psycopg.Error:
            logger.warning('Enrollment database unavailable')
            raise SystemExit(1) from None
        except EnrollmentError as error:
            if ownership_event.is_set():
                repository.fail_job(job_id, error.code)
        except (ModelLoadError, ValueError):
            if ownership_event.is_set():
                repository.fail_job(job_id, 'model_unavailable')
        except Exception:
            if ownership_event.is_set():
                repository.fail_job(job_id, 'processing_error')
        stored = repository.get_job(job_id)
        if job.storage_key and (stored is None or stored.status in ('completed', 'failed')):
            audio_path(data_dir, job.storage_key).unlink(missing_ok=True)
    finally:
        connection.close()


def process_meeting_parts(
    parts: list[MeetingAudioFile], data_dir: Path, models: ModelRegistry,
    source: str = "upload", speaker_embeddings: dict[str, list[float]] | None = None,
    embedding_errors: list[str] | None = None,
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
        part_embeddings = {} if speaker_embeddings is not None else None
        if part_embeddings is None:
            result = process_audio(data, part.original_filename, models)
        else:
            result = process_audio(data, part.original_filename, models,
                                   speaker_embeddings=part_embeddings, embedding_errors=embedding_errors)
        if result.duration_seconds > 30 * 60:
            raise AudioTooLongError("Audio part must be at most 30 minutes long")
        part_start = part.start_seconds if source == "google_meet_bot" else offset
        if part_start is None or part_start < 0:
            raise InvalidAudioError("Meeting part has no valid start time")
        if source == "upload" and offset + result.duration_seconds > 30 * 60:
            raise AudioTooLongError("Meeting audio must be at most 30 minutes long")
        speaker_prefix = f"PART_{part.ordinal + 1}_" if part.ordinal else ""
        if speaker_embeddings is not None:
            speaker_embeddings.update({speaker_prefix + label: vector for label, vector in part_embeddings.items()})
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


def run_once(repository: MeetingRepository, data_dir: Path, settings: Settings,
             database: DatabaseSettings, lease: WorkerLease, prefer_enrollment: bool = False) -> bool:
    """Supervise either queue with the same spawned-child ownership checks."""
    lease.ensure_owned()
    voices = VoiceRepository(repository.connection)
    claims = ((voices.claim_next_job, True), (repository.claim_next, False))
    if not prefer_enrollment:
        claims = tuple(reversed(claims))
    for claim, is_enrollment in claims:
        job = claim()
        if job is not None:
            break
    else:
        return False
    context = multiprocessing.get_context("spawn")
    start_event, ownership_event = context.Event(), context.Event()
    ownership_event.set()
    target = run_claimed_enrollment if is_enrollment else run_claimed_meeting
    process = context.Process(target=target,
        args=(job.id, data_dir, settings, database, start_event, ownership_event), daemon=True)
    process.start()
    try:
        start_ticks = process_start_ticks(process.pid)
        set_process = voices.set_job_process if is_enrollment else repository.set_pipeline_process
        if start_ticks is None or not set_process(job.id, process.pid, start_ticks):
            return True
        start_event.set()
        while process.is_alive():
            process.join(timeout=0.2)
            lease.ensure_owned()
        if process.exitcode != 0:
            if is_enrollment:
                voices.fail_job(job.id, 'processing_error')
            else:
                repository.fail(job.id, 'processing_error')
        return True
    finally:
        ownership_event.clear()
        if process.is_alive():
            process.kill()
        process.join()
        if is_enrollment and job.storage_key:
            stored = voices.get_job(job.id)
            if stored is None or stored.status in ('completed', 'failed'):
                audio_path(data_dir, job.storage_key).unlink(missing_ok=True)


def main() -> None:
    settings, database = get_settings(), get_database_settings()
    data_dir = settings.meeting_data_dir
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_worker_lock(data_dir):
        try:
            while True:
                try:
                    with worker_lease(database, 'meeting') as lease, open_database(database) as connection:
                        initialize_meeting_storage(data_dir, connection)
                        repository = MeetingRepository(connection)
                        repository.recover_interrupted()
                        VoiceRepository(connection).recover_interrupted_jobs()
                        cleanup_orphaned_audio(data_dir, repository)
                        prefer_enrollment = False
                        while True:
                            if run_once(repository, data_dir, settings, database, lease, prefer_enrollment):
                                prefer_enrollment = not prefer_enrollment
                            else:
                                time.sleep(1)
                except (psycopg.OperationalError, DatabaseUnavailableError) as error:
                    if getattr(error, 'code', '') not in ('', 'worker_ownership_lost'):
                        raise
                    logger.warning('Meeting worker database unavailable; reconnecting')
                    time.sleep(1)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
