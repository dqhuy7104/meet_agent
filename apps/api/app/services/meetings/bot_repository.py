"""Durable local Google Meet bot sessions and finalized audio parts."""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg.rows import dict_row
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.services.meetings.repository import MeetingRecord, MeetingRepository
from app.services.meetings.storage import audio_path


class BotStateConflict(ValueError):
    """The bot session cannot make the requested state transition."""


@dataclass(frozen=True)
class BotSession:
    id: str
    url: str | None
    url_hash: str
    title: str | None
    idempotency_key: str | None
    status: str
    failure_code: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    stop_requested: bool
    meeting_id: str | None
    capture_incomplete: bool
    project_id: str | None
    started_by_account_id: str | None
    legacy_unassigned: bool


@dataclass(frozen=True)
class BotPart:
    session_id: str
    ordinal: int
    storage_key: str
    start_seconds: float
    duration_seconds: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(row: dict[str, Any]) -> BotSession:
    values = dict(row)
    for name in ('created_at', 'started_at', 'ended_at'):
        if values[name] is not None:
            values[name] = values[name].astimezone(timezone.utc).isoformat()
    return BotSession(**values)


class BotSessionRepository:
    """Serialize all bot state changes through PostgreSQL row transactions."""

    def __init__(self, connection: psycopg.Connection, data_dir: Path) -> None:
        self.connection = connection
        self.data_dir = data_dir

    def _execute(self, query: str, parameters: tuple = ()) -> psycopg.Cursor:
        cursor = self.connection.cursor(row_factory=dict_row)
        cursor.execute(query, parameters)
        return cursor

    def _locked(self, session_id: str) -> BotSession | None:
        row = self._execute("SELECT * FROM bot_sessions WHERE id=%s FOR UPDATE", (session_id,)).fetchone()
        return _session(row) if row else None

    def get(self, session_id: str) -> BotSession | None:
        row = self._execute("SELECT * FROM bot_sessions WHERE id=%s", (session_id,)).fetchone()
        return _session(row) if row else None

    def get_active(self) -> BotSession | None:
        row = self._execute(
            """SELECT * FROM bot_sessions WHERE status IN
               ('queued','joining','waiting_admission','recording','finalizing') LIMIT 1"""
        ).fetchone()
        return _session(row) if row else None

    def get_recent_interrupted(self, project_ids: list[str] | None = None) -> BotSession | None:
        row = self._execute(
            """SELECT * FROM bot_sessions WHERE status='interrupted'
               AND (%s::text[] IS NULL OR project_id = ANY(%s))
               ORDER BY ended_at DESC, created_at DESC LIMIT 1""", (project_ids, project_ids)
        ).fetchone()
        return _session(row) if row else None

    def parts(self, session_id: str) -> list[BotPart]:
        rows = self._execute(
            "SELECT * FROM bot_session_parts WHERE session_id=%s ORDER BY ordinal", (session_id,)
        ).fetchall()
        return [BotPart(**dict(row)) for row in rows]

    def create(self, url: str, title: str | None, key: str | None, project_id: str, started_by_account_id: str) -> BotSession:
        try:
            with self.connection.transaction():
                if key:
                    row = self._execute("SELECT * FROM bot_sessions WHERE idempotency_key=%s", (key,)).fetchone()
                    if row:
                        if (row["url_hash"], row["project_id"], row["started_by_account_id"]) != (hashlib.sha256(url.encode()).hexdigest(), project_id, started_by_account_id):
                            raise BotStateConflict("Idempotency key belongs to another link")
                        return _session(row)
                if self.get_active():
                    raise BotStateConflict("A bot session is already active")
                session_id = uuid.uuid4().hex
                self._execute(
                    """INSERT INTO bot_sessions (id, url, url_hash, title, idempotency_key, status, created_at, project_id, started_by_account_id)
                       VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)""",
                    (session_id, url, hashlib.sha256(url.encode()).hexdigest(), title, key, _now(), project_id, started_by_account_id),
                )
                return self.get(session_id)  # type: ignore[return-value]

        except psycopg.errors.UniqueViolation:
            if key:
                row = self._execute("SELECT * FROM bot_sessions WHERE idempotency_key=%s", (key,)).fetchone()
                if row and (row['url_hash'], row['project_id'], row['started_by_account_id']) == (hashlib.sha256(url.encode()).hexdigest(), project_id, started_by_account_id):
                    return _session(row)
            raise BotStateConflict("A bot session or idempotency key already exists") from None

    def claim_next(self) -> BotSession | None:
        with self.connection.transaction():
            row = self._execute(
                "SELECT id FROM bot_sessions WHERE status='queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if row is None:
                return None
            self._execute(
                "UPDATE bot_sessions SET status='joining', started_at=%s WHERE id=%s", (_now(), row["id"])
            )
            return self.get(row["id"])

    def request_stop(self, session_id: str) -> BotSession | None:
        with self.connection.transaction():
            self._execute(
                """UPDATE bot_sessions SET status='cancelled', stop_requested=true,
                   ended_at=%s, url=NULL WHERE id=%s AND status='queued'""",
                (_now(), session_id),
            )
            self._execute(
                """UPDATE bot_sessions SET stop_requested=true WHERE id=%s AND status IN
                   ('joining','waiting_admission','recording')""", (session_id,)
            )
        return self.get(session_id)

    def set_status(self, session_id: str, status: str, failure_code: str | None = None) -> BotSession:
        if status not in {"joining", "waiting_admission", "recording", "finalizing", "interrupted", "cancelled"}:
            raise ValueError("Invalid bot status")
        with self.connection.transaction():
            current = self._locked(session_id)
            if current is None or current.status in {"completed", "cancelled"}:
                raise BotStateConflict("Bot session is no longer active")
            ended_at = _now() if status in {"interrupted", "cancelled"} else None
            self._execute(
                """UPDATE bot_sessions SET status=%s, failure_code=%s, ended_at=COALESCE(%s, ended_at),
                   url=CASE WHEN %s IN ('cancelled','interrupted') THEN NULL ELSE url END WHERE id=%s""",
                (status, failure_code, ended_at, status, session_id),
            )
            return self.get(session_id)  # type: ignore[return-value]

    def add_part(self, session_id: str, storage_key: str, start_seconds: float, duration_seconds: float) -> BotPart:
        if start_seconds < 0 or not 0 < duration_seconds <= 1800:
            raise ValueError("Invalid recorded part timing")
        with self.connection.transaction():
            session = self._locked(session_id)
            if session is None or session.status != "recording":
                raise BotStateConflict("Bot is not recording")
            ordinal = len(self.parts(session_id))
            self._execute(
                "INSERT INTO bot_session_parts VALUES (%s, %s, %s, %s, %s)",
                (session_id, ordinal, storage_key, start_seconds, duration_seconds),
            )
            return BotPart(session_id, ordinal, storage_key, start_seconds, duration_seconds)

    def _checksum(self, parts: list[BotPart]) -> str:
        data_dir = self.data_dir
        digest = hashlib.sha256()
        for part in parts:
            path = audio_path(data_dir, part.storage_key)
            if len(parts) > 1:
                digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def finalize(self, session_id: str, partial: bool = False) -> MeetingRecord:
        current = self.get(session_id)
        if current and current.meeting_id:
            return MeetingRepository(self.connection).get_meeting(current.meeting_id)
        snapshot = self.parts(session_id)
        checksum = self._checksum(snapshot)
        with self.connection.transaction():
            session = self._locked(session_id)
            if session is None:
                raise BotStateConflict("Bot session not found")
            if session.meeting_id:
                return MeetingRepository(self.connection).get_meeting(session.meeting_id)  # type: ignore[return-value]
            if session.status not in ({"interrupted"} if partial else {"recording", "finalizing"}):
                raise BotStateConflict("Bot session cannot be finalized")
            parts = self.parts(session_id)
            if not parts:
                raise BotStateConflict("Bot session has no recorded audio")
            if parts != snapshot:
                raise BotStateConflict("Bot parts changed during finalization")
            meeting_id = uuid.uuid4().hex
            now = _now()
            first_key = parts[0].storage_key
            self._execute(
                """INSERT INTO meetings
                   (id, title, original_filename, storage_key, checksum, created_at, updated_at, status, source, project_id)
                   VALUES (%s, %s, 'recording.wav', %s, %s, %s, %s, 'queued', 'google_meet_bot', %s)""",
                (meeting_id, session.title, first_key, checksum, now, now, session.project_id),
            )
            for part in parts:
                self._execute(
                    """INSERT INTO meeting_audio_files
                       (meeting_id, ordinal, original_filename, storage_key, duration_seconds, start_seconds)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (meeting_id, part.ordinal, f"part-{part.ordinal + 1}.wav", part.storage_key,
                     part.duration_seconds, part.start_seconds),
                )
            self._execute("DELETE FROM bot_session_parts WHERE session_id=%s", (session_id,))
            self._execute(
                """UPDATE bot_sessions SET status='completed', url=NULL, meeting_id=%s, ended_at=%s,
                   capture_incomplete=%s, failure_code=NULL WHERE id=%s""",
                (meeting_id, now, partial, session_id),
            )
            return MeetingRepository(self.connection).get_meeting(meeting_id)  # type: ignore[return-value]

    def discard(self, session_id: str) -> tuple[str, ...]:
        with self.connection.transaction():
            session = self._locked(session_id)
            if session is None or session.meeting_id or session.status not in {"queued", "interrupted", "cancelled"}:
                raise BotStateConflict("Bot session cannot be discarded")
            keys = tuple(part.storage_key for part in self.parts(session_id))
            self._execute("DELETE FROM bot_session_parts WHERE session_id=%s", (session_id,))
            self._execute(
                "UPDATE bot_sessions SET status='cancelled', url=NULL, ended_at=%s WHERE id=%s", (_now(), session_id)
            )
            return keys

    def recover_stale(self) -> int:
        with self.connection.transaction():
            cursor = self._execute(
                """UPDATE bot_sessions SET status='interrupted', failure_code='runner_interrupted',
                   ended_at=%s, url=NULL
                   WHERE status IN ('joining','waiting_admission','recording','finalizing')""", (_now(),)
            )
        return cursor.rowcount
