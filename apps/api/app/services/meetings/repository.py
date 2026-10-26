"""PostgreSQL operations for the local meeting library."""

from __future__ import annotations

from typing import Any, Mapping

from app.services.voice.matching import VoiceMatch
from app.services.voice.repository import VoiceRepository

import psycopg
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus
from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment
from app.services.rag.outbox import enqueue_rag_event


class IdempotencyConflict(ValueError):
    """An upload key already belongs to a different meeting."""


class MeetingStateConflict(ValueError):
    """A meeting cannot make the requested state transition."""


@dataclass(frozen=True)
class CancelledMeeting:
    """Audio and process metadata retained after a cancellation."""

    storage_key: str
    pipeline_pid: int | None
    pipeline_start_ticks: int | None
    other_storage_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MeetingAudioFile:
    ordinal: int
    original_filename: str
    storage_key: str
    duration_seconds: float | None
    start_seconds: float | None


@dataclass(frozen=True)
class MeetingRecord:
    id: str
    title: str | None
    original_filename: str
    storage_key: str
    checksum: str
    idempotency_key: str | None
    created_at: str
    updated_at: str
    status: str
    attempt_count: int
    duration_seconds: float | None
    failure_code: str | None
    transcript: str | None
    pipeline_pid: int | None
    pipeline_start_ticks: int | None
    cancelled_at: str | None
    source: str
    voice_identification_status: str = "not_run"
    project_id: str | None = None
    uploaded_by_account_id: str | None = None
    legacy_unassigned: bool = False


@dataclass(frozen=True)
class MeetingDetail:
    meeting: MeetingRecord
    vad_segments: list[SpeechSegment]
    diarization_segments: list[SpeakerTurn]
    transcript_segments: list[TranscriptSegment]
    audio_files: list[MeetingAudioFile]


@dataclass(frozen=True)
class SearchHit:
    speaker_name: str | None
    meeting_id: str
    title: str | None
    speaker: str
    start: float
    end: float
    text: str


def _record(row: dict[str, Any]) -> MeetingRecord:
    values = dict(row)
    for name in ("created_at", "updated_at", "cancelled_at"):
        if values[name] is not None:
            values[name] = values[name].astimezone(timezone.utc).isoformat()
    return MeetingRecord(**values)


class MeetingRepository:
    """Own PostgreSQL meeting transactions without changing HTTP contracts."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def _execute(self, query: str, parameters: tuple = ()) -> psycopg.Cursor:
        cursor = self.connection.cursor(row_factory=dict_row)
        cursor.execute(query, parameters)
        return cursor

    def create_meeting(self, meeting_id: str, title: str | None, filename: str,
                       storage_key: str, checksum: str, idempotency_key: str | None,
                       audio_files: list[tuple[str, str]] | None = None,
                       source: str = "upload", *, project_id: str,
                       uploaded_by_account_id: str | None = None) -> MeetingRecord:
        now = datetime.now(timezone.utc)
        try:
            with self.connection.transaction():
                self.connection.execute("""INSERT INTO meetings
                    (id,title,original_filename,storage_key,checksum,idempotency_key,created_at,updated_at,status,source,project_id,uploaded_by_account_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s)""",
                    (meeting_id, title, filename, storage_key, checksum, idempotency_key, now, now, source, project_id, uploaded_by_account_id))
                for ordinal, (part_filename, part_key) in enumerate(audio_files or [(filename, storage_key)]):
                    self.connection.execute("""INSERT INTO meeting_audio_files
                        (meeting_id,ordinal,original_filename,storage_key) VALUES (%s,%s,%s,%s)""",
                        (meeting_id, ordinal, part_filename, part_key))
        except psycopg.errors.UniqueViolation:
            if idempotency_key and self.find_by_idempotency_key(idempotency_key):
                raise IdempotencyConflict(idempotency_key) from None
            raise
        return self.get_meeting(meeting_id)

    def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        row = self._execute("SELECT * FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
        return _record(row) if row else None

    def _locked_meeting(self, meeting_id: str) -> MeetingRecord | None:
        row = self._execute("SELECT * FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
        return _record(row) if row else None

    def get_audio_files(self, meeting_id: str) -> list[MeetingAudioFile]:
        return [MeetingAudioFile(**row) for row in self._execute("""SELECT ordinal,original_filename,
            storage_key,duration_seconds,start_seconds FROM meeting_audio_files WHERE meeting_id=%s ORDER BY ordinal""",
            (meeting_id,)).fetchall()]

    def find_by_idempotency_key(self, key: str) -> MeetingRecord | None:
        row = self._execute("SELECT * FROM meetings WHERE idempotency_key=%s", (key,)).fetchone()
        return _record(row) if row else None

    def list_meetings(self, limit: int, offset: int, project_ids: list[str] | None = None) -> list[MeetingRecord]:
        scope = " WHERE project_id = ANY(%s)" if project_ids is not None else ""
        parameters = (project_ids, limit, offset) if project_ids is not None else (limit, offset)
        return [_record(row) for row in self._execute(
            "SELECT * FROM meetings" + scope + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s", parameters).fetchall()]

    def claim_next(self) -> MeetingRecord | None:
        with self.connection.transaction():
            row = self._execute("""SELECT id FROM meetings WHERE status='queued'
                ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row is None:
                return None
            self.connection.execute("""UPDATE meetings SET status='processing', attempt_count=attempt_count+1,
                updated_at=now() WHERE id=%s""", (row['id'],))
            return self.get_meeting(row['id'])

    def recover_interrupted(self) -> None:
        with self.connection.transaction():
            self.connection.execute("""UPDATE meetings SET status=CASE WHEN attempt_count<2 THEN 'queued' ELSE 'failed' END,
                failure_code=CASE WHEN attempt_count<2 THEN NULL ELSE 'interrupted' END,
                updated_at=now() WHERE status='processing'""")

    def _clear_results(self, meeting_id: str) -> None:
        self.connection.execute("UPDATE meetings SET voice_identification_status='not_run' WHERE id=%s", (meeting_id,))
        for table in ("meeting_speaker_identities", "rag_publications", "rag_index_jobs", "transcript_segments", "diarization_segments", "vad_segments"):
            self.connection.execute(f"DELETE FROM {table} WHERE meeting_id=%s", (meeting_id,))

    def complete(self, meeting_id: str, result: AudioProcessResponse, durations: list[float] | None = None,
                 *, identities: Mapping[str, VoiceMatch | None] | None = None,
                 voice_status: str = "not_run", voice_fingerprint: str | None = None) -> None:
        with self.connection.transaction():
            record = self._locked_meeting(meeting_id)
            if record is None or record.status != 'processing':
                return
            if durations is not None:
                offset = 0.0
                for ordinal, duration in enumerate(durations):
                    self.connection.execute("""UPDATE meeting_audio_files SET duration_seconds=%s,
                        start_seconds=COALESCE(start_seconds,%s) WHERE meeting_id=%s AND ordinal=%s""",
                        (duration, offset, meeting_id, ordinal))
                    offset += duration
            self._clear_results(meeting_id)
            for ordinal, segment in enumerate(result.vad_segments):
                self.connection.execute("INSERT INTO vad_segments VALUES (%s,%s,%s,%s)",
                                        (meeting_id, ordinal, segment.start, segment.end))
            for ordinal, segment in enumerate(result.diarization_segments):
                self.connection.execute("INSERT INTO diarization_segments VALUES (%s,%s,%s,%s,%s)",
                                        (meeting_id, ordinal, segment.start, segment.end, segment.speaker))
            for ordinal, segment in enumerate(result.transcript_segments):
                self.connection.execute("""INSERT INTO transcript_segments
                    (meeting_id,ordinal,start_seconds,end_seconds,speaker,text) VALUES (%s,%s,%s,%s,%s,%s)""",
                    (meeting_id, ordinal, segment.start, segment.end, segment.speaker, segment.text))
            for speaker, match in sorted((identities or {}).items()):
                profile_id = None
                if match is not None:
                    profile = self.connection.execute(
                        "SELECT id FROM voice_profiles WHERE id=%s FOR KEY SHARE", (match.profile_id,)).fetchone()
                    if profile is not None:
                        profile_id = profile[0]
                self.connection.execute("""INSERT INTO meeting_speaker_identities
                    (meeting_id,speaker,profile_id,score,origin,fingerprint)
                    VALUES (%s,%s,%s,%s,'automatic',%s)""",
                    (meeting_id, speaker, profile_id, match.similarity if profile_id else None, voice_fingerprint))
            self.connection.execute("""UPDATE meetings SET status='completed',duration_seconds=%s,transcript=%s,
                failure_code=NULL,voice_identification_status=%s,updated_at=now() WHERE id=%s""",
                (result.duration_seconds, result.transcript, voice_status, meeting_id))
            enqueue_rag_event(self.connection, meeting_id, "index")

    def fail(self, meeting_id: str, failure_code: str) -> None:
        with self.connection.transaction():
            record = self._locked_meeting(meeting_id)
            if record is None or record.status != 'processing':
                return
            self._clear_results(meeting_id)
            self.connection.execute("""UPDATE meetings SET status='failed',failure_code=%s,duration_seconds=NULL,
                transcript=NULL,updated_at=now() WHERE id=%s""", (failure_code, meeting_id))
            enqueue_rag_event(self.connection, meeting_id, "purge")

    def set_pipeline_process(self, meeting_id: str, pid: int, start_ticks: int) -> bool:
        return self.connection.execute("""UPDATE meetings SET pipeline_pid=%s,pipeline_start_ticks=%s,
            updated_at=now() WHERE id=%s AND status='processing'""", (pid, start_ticks, meeting_id)).rowcount == 1

    def cancel(self, meeting_id: str) -> CancelledMeeting | None:
        with self.connection.transaction():
            record = self._locked_meeting(meeting_id)
            if record is None:
                return None
            cancelled = CancelledMeeting(record.storage_key, record.pipeline_pid, record.pipeline_start_ticks,
                tuple(part.storage_key for part in self.get_audio_files(meeting_id) if part.ordinal > 0))
            if record.status != 'cancelled':
                self._clear_results(meeting_id)
                VoiceRepository(self.connection).purge_meeting_source(meeting_id)
                self.connection.execute("DELETE FROM meeting_audio_files WHERE meeting_id=%s", (meeting_id,))
                self.connection.execute("""UPDATE meetings SET status='cancelled',failure_code=NULL,duration_seconds=NULL,
                    transcript=NULL,cancelled_at=now(),updated_at=now() WHERE id=%s""", (meeting_id,))
                enqueue_rag_event(self.connection, meeting_id, "purge")
            return cancelled

    def retry(self, meeting_id: str) -> MeetingRecord | None:
        with self.connection.transaction():
            record = self._locked_meeting(meeting_id)
            if record is None:
                return None
            if record.status != 'failed':
                raise MeetingStateConflict(meeting_id)
            self._clear_results(meeting_id)
            self.connection.execute("""UPDATE meetings SET status='queued',attempt_count=0,failure_code=NULL,
                transcript=NULL,duration_seconds=NULL,updated_at=now() WHERE id=%s""", (meeting_id,))
            enqueue_rag_event(self.connection, meeting_id, "purge")
            return self.get_meeting(meeting_id)

    def count_meetings(self, project_ids: list[str] | None = None) -> int:
        if project_ids is not None:
            return self.connection.execute("SELECT count(*) FROM meetings WHERE project_id = ANY(%s)", (project_ids,)).fetchone()[0]
        return self.connection.execute("SELECT count(*) FROM meetings").fetchone()[0]

    def completed_meeting_ids(self) -> list[str]:
        return [row[0] for row in self.connection.execute("SELECT id FROM meetings WHERE status='completed' ORDER BY id")]

    def referenced_audio_keys(self) -> set[str]:
        return {row[0] for row in self.connection.execute(
            """SELECT storage_key FROM meeting_audio_files UNION SELECT storage_key FROM bot_session_parts
            UNION SELECT upload_key FROM voice_enrollment_jobs
            WHERE upload_key IS NOT NULL AND state IN ('queued','processing')""")}

    def get_detail(self, meeting_id: str) -> MeetingDetail | None:
        if self.connection.info.transaction_status == TransactionStatus.IDLE:
            with self.connection.transaction():
                self.connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                return self._detail(meeting_id)
        return self._detail(meeting_id)

    def _detail(self, meeting_id: str) -> MeetingDetail | None:
        record = self.get_meeting(meeting_id)
        if record is None:
            return None
        parts = self.get_audio_files(meeting_id)
        if record.status != 'completed':
            return MeetingDetail(record, [], [], [], parts)
        vad = [SpeechSegment(**row) for row in self._execute(
            "SELECT start_seconds AS start,end_seconds AS end FROM vad_segments WHERE meeting_id=%s ORDER BY ordinal", (meeting_id,))]
        turns = [SpeakerTurn(**row) for row in self._execute("""SELECT start_seconds AS start,end_seconds AS end,speaker
            FROM diarization_segments WHERE meeting_id=%s ORDER BY ordinal""", (meeting_id,))]
        transcripts = [TranscriptSegment(**row) for row in self._execute("""SELECT start_seconds AS start,end_seconds AS end,speaker,text
            FROM transcript_segments WHERE meeting_id=%s ORDER BY ordinal""", (meeting_id,))]
        return MeetingDetail(record, vad, turns, transcripts, parts)

    def delete(self, meeting_id: str) -> str | None:
        with self.connection.transaction():
            record = self._locked_meeting(meeting_id)
            if record is None:
                return None
            if record.status == 'processing':
                raise MeetingStateConflict(meeting_id)
            VoiceRepository(self.connection).purge_meeting_source(meeting_id)
            self.connection.execute("DELETE FROM meetings WHERE id=%s", (meeting_id,))
            enqueue_rag_event(self.connection, meeting_id, "purge")
            return record.storage_key

    def search_segments(self, query: str, limit: int, offset: int, project_ids: list[str] | None = None) -> tuple[list[SearchHit], int]:
        joined = """FROM transcript_segments AS segment JOIN meetings AS meeting ON meeting.id=segment.meeting_id
            LEFT JOIN meeting_speaker_identities AS identity ON identity.meeting_id=meeting.id AND identity.speaker=segment.speaker
            LEFT JOIN voice_profiles AS profile ON profile.id=identity.profile_id
            WHERE segment.document @@ plainto_tsquery('simple',%s) AND meeting.status='completed'"""
        parameters = (query,)
        if project_ids is not None:
            joined += " AND meeting.project_id = ANY(%s)"
            parameters = (query, project_ids)
        with self.connection.transaction():
            self.connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = self.connection.execute("SELECT count(*) " + joined, parameters).fetchone()[0]
            rows = self._execute("""SELECT meeting.id AS meeting_id,meeting.title,segment.speaker,
                segment.start_seconds AS start,segment.end_seconds AS end,segment.text,profile.display_name AS speaker_name """ + joined +
                " ORDER BY meeting.created_at DESC,meeting.id DESC,segment.start_seconds,segment.id LIMIT %s OFFSET %s",
                (*parameters, limit, offset)).fetchall()
            return [SearchHit(**row) for row in rows], total
