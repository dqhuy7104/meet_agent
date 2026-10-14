"""SQLite operations for the local meeting library."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas.transcript import AudioProcessResponse, SpeakerTurn, SpeechSegment, TranscriptSegment


class IdempotencyConflict(ValueError):
    """An upload key already belongs to a different meeting."""


class MeetingStateConflict(ValueError):
    """A meeting cannot make the requested state transition."""


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


@dataclass(frozen=True)
class MeetingDetail:
    meeting: MeetingRecord
    vad_segments: list[SpeechSegment]
    diarization_segments: list[SpeakerTurn]
    transcript_segments: list[TranscriptSegment]


def _record(row: sqlite3.Row) -> MeetingRecord:
    return MeetingRecord(**dict(row))


class MeetingRepository:
    """Own metadata and state transactions on one SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_meeting(
        self,
        meeting_id: str,
        title: str | None,
        filename: str,
        storage_key: str,
        checksum: str,
        idempotency_key: str | None,
    ) -> MeetingRecord:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO meetings
                    (id, title, original_filename, storage_key, checksum, idempotency_key,
                     created_at, updated_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
                    (meeting_id, title, filename, storage_key, checksum, idempotency_key, now, now),
                )
        except sqlite3.IntegrityError as error:
            if idempotency_key and self.find_by_idempotency_key(idempotency_key):
                raise IdempotencyConflict(idempotency_key) from error
            raise
        return self.get_meeting(meeting_id)  # type: ignore[return-value]

    def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        row = self.connection.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return _record(row) if row else None

    def find_by_idempotency_key(self, key: str) -> MeetingRecord | None:
        row = self.connection.execute("SELECT * FROM meetings WHERE idempotency_key = ?", (key,)).fetchone()
        return _record(row) if row else None

    def list_meetings(self, limit: int, offset: int) -> list[MeetingRecord]:
        rows = self.connection.execute(
            "SELECT * FROM meetings ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [_record(row) for row in rows]

    def claim_next(self) -> MeetingRecord | None:
        """Claim the oldest queued meeting in one write transaction."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT id FROM meetings WHERE status='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            meeting_id = row["id"]
            self.connection.execute(
                """UPDATE meetings SET status='processing', attempt_count=attempt_count+1,
                   updated_at=? WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), meeting_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.get_meeting(meeting_id)

    def recover_interrupted(self) -> None:
        """Recover only jobs interrupted before the two-claim limit."""
        with self.connection:
            self.connection.execute(
                """UPDATE meetings SET status=CASE WHEN attempt_count<2 THEN 'queued' ELSE 'failed' END,
                   failure_code=CASE WHEN attempt_count<2 THEN NULL ELSE 'interrupted' END,
                   updated_at=? WHERE status='processing'""",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def _clear_results(self, meeting_id: str) -> None:
        self.connection.execute("DELETE FROM transcript_fts WHERE meeting_id=?", (meeting_id,))
        for table in ("transcript_segments", "diarization_segments", "vad_segments"):
            self.connection.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))

    def complete(self, meeting_id: str, result: AudioProcessResponse) -> None:
        """Replace all derived rows and publish completion atomically."""
        with self.connection:
            self._clear_results(meeting_id)
            for ordinal, segment in enumerate(result.vad_segments):
                self.connection.execute(
                    "INSERT INTO vad_segments VALUES (?, ?, ?, ?)",
                    (meeting_id, ordinal, segment.start, segment.end),
                )
            for ordinal, segment in enumerate(result.diarization_segments):
                self.connection.execute(
                    "INSERT INTO diarization_segments VALUES (?, ?, ?, ?, ?)",
                    (meeting_id, ordinal, segment.start, segment.end, segment.speaker),
                )
            for ordinal, segment in enumerate(result.transcript_segments):
                cursor = self.connection.execute(
                    """INSERT INTO transcript_segments
                    (meeting_id, ordinal, start_seconds, end_seconds, speaker, text)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (meeting_id, ordinal, segment.start, segment.end, segment.speaker, segment.text),
                )
                self.connection.execute(
                    "INSERT INTO transcript_fts (meeting_id, segment_id, text) VALUES (?, ?, ?)",
                    (meeting_id, cursor.lastrowid, segment.text),
                )
            cursor = self.connection.execute(
                """UPDATE meetings SET status='completed', duration_seconds=?, transcript=?,
                   failure_code=NULL, updated_at=? WHERE id=? AND status='processing'""",
                (result.duration_seconds, result.transcript, datetime.now(timezone.utc).isoformat(), meeting_id),
            )
            if cursor.rowcount != 1:
                raise MeetingStateConflict(meeting_id)

    def fail(self, meeting_id: str, failure_code: str) -> None:
        """Mark processing failure without exposing partial results."""
        with self.connection:
            self._clear_results(meeting_id)
            cursor = self.connection.execute(
                """UPDATE meetings SET status='failed', failure_code=?, duration_seconds=NULL,
                   transcript=NULL, updated_at=? WHERE id=? AND status='processing'""",
                (failure_code, datetime.now(timezone.utc).isoformat(), meeting_id),
            )
            if cursor.rowcount != 1:
                raise MeetingStateConflict(meeting_id)

    def retry(self, meeting_id: str) -> MeetingRecord | None:
        """Requeue one failed meeting while preserving its ID and audio."""
        with self.connection:
            record = self.get_meeting(meeting_id)
            if record is None:
                return None
            if record.status != "failed":
                raise MeetingStateConflict(meeting_id)
            self.connection.execute(
                """UPDATE meetings SET status='queued', attempt_count=0, failure_code=NULL,
                   transcript=NULL, duration_seconds=NULL, updated_at=? WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), meeting_id),
            )
        return self.get_meeting(meeting_id)

    def count_meetings(self) -> int:
        return self.connection.execute("SELECT count(*) FROM meetings").fetchone()[0]

    def get_detail(self, meeting_id: str) -> MeetingDetail | None:
        """Read completed results, keeping incomplete states transcript-free."""
        meeting = self.get_meeting(meeting_id)
        if meeting is None:
            return None
        if meeting.status != "completed":
            return MeetingDetail(meeting, [], [], [])
        vad = [
            SpeechSegment(start=row["start_seconds"], end=row["end_seconds"])
            for row in self.connection.execute(
                "SELECT start_seconds, end_seconds FROM vad_segments WHERE meeting_id=? ORDER BY ordinal",
                (meeting_id,),
            )
        ]
        turns = [
            SpeakerTurn(start=row["start_seconds"], end=row["end_seconds"], speaker=row["speaker"])
            for row in self.connection.execute(
                """SELECT start_seconds, end_seconds, speaker FROM diarization_segments
                   WHERE meeting_id=? ORDER BY ordinal""",
                (meeting_id,),
            )
        ]
        transcripts = [
            TranscriptSegment(
                start=row["start_seconds"], end=row["end_seconds"],
                speaker=row["speaker"], text=row["text"],
            )
            for row in self.connection.execute(
                """SELECT start_seconds, end_seconds, speaker, text FROM transcript_segments
                   WHERE meeting_id=? ORDER BY ordinal""",
                (meeting_id,),
            )
        ]
        return MeetingDetail(meeting, vad, turns, transcripts)

    def delete(self, meeting_id: str) -> str | None:
        """Remove database rows atomically and return the audio storage key."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            meeting = self.get_meeting(meeting_id)
            if meeting is None:
                self.connection.commit()
                return None
            if meeting.status == "processing":
                raise MeetingStateConflict(meeting_id)
            self._clear_results(meeting_id)
            self.connection.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            self.connection.commit()
            return meeting.storage_key
        except BaseException:
            self.connection.rollback()
            raise
