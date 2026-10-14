"""SQLite operations for the local meeting library."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


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
