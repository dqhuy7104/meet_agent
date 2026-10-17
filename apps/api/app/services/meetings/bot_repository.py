"""Durable local Google Meet bot sessions and finalized audio parts."""

from __future__ import annotations

import hashlib
import sqlite3
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


@dataclass(frozen=True)
class BotPart:
    session_id: str
    ordinal: int
    storage_key: str
    start_seconds: float
    duration_seconds: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(row: sqlite3.Row) -> BotSession:
    return BotSession(**dict(row))


class BotSessionRepository:
    """Serialize all bot state changes through SQLite write transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, session_id: str) -> BotSession | None:
        row = self.connection.execute("SELECT * FROM bot_sessions WHERE id=?", (session_id,)).fetchone()
        return _session(row) if row else None

    def get_active(self) -> BotSession | None:
        row = self.connection.execute(
            """SELECT * FROM bot_sessions WHERE status IN
               ('queued','joining','waiting_admission','recording','finalizing') LIMIT 1"""
        ).fetchone()
        return _session(row) if row else None

    def get_recent_interrupted(self) -> BotSession | None:
        row = self.connection.execute(
            """SELECT * FROM bot_sessions WHERE status='interrupted'
               ORDER BY ended_at DESC, created_at DESC LIMIT 1"""
        ).fetchone()
        return _session(row) if row else None

    def parts(self, session_id: str) -> list[BotPart]:
        rows = self.connection.execute(
            "SELECT * FROM bot_session_parts WHERE session_id=? ORDER BY ordinal", (session_id,)
        ).fetchall()
        return [BotPart(**dict(row)) for row in rows]

    def create(self, url: str, title: str | None, key: str | None) -> BotSession:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if key:
                row = self.connection.execute("SELECT * FROM bot_sessions WHERE idempotency_key=?", (key,)).fetchone()
                if row:
                    if row["url_hash"] != hashlib.sha256(url.encode()).hexdigest():
                        raise BotStateConflict("Idempotency key belongs to another link")
                    self.connection.commit()
                    return _session(row)
            if self.get_active():
                raise BotStateConflict("A bot session is already active")
            session_id = uuid.uuid4().hex
            self.connection.execute(
                """INSERT INTO bot_sessions (id, url, url_hash, title, idempotency_key, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?)""",
                (session_id, url, hashlib.sha256(url.encode()).hexdigest(), title, key, _now()),
            )
            self.connection.commit()
            return self.get(session_id)  # type: ignore[return-value]
        except BaseException:
            self.connection.rollback()
            raise

    def claim_next(self) -> BotSession | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT id FROM bot_sessions WHERE status='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE bot_sessions SET status='joining', started_at=? WHERE id=?", (_now(), row["id"])
            )
            self.connection.commit()
            return self.get(row["id"])
        except BaseException:
            self.connection.rollback()
            raise

    def request_stop(self, session_id: str) -> BotSession | None:
        with self.connection:
            self.connection.execute(
                """UPDATE bot_sessions SET stop_requested=1 WHERE id=? AND status IN
                   ('queued','joining','waiting_admission','recording')""", (session_id,)
            )
        return self.get(session_id)

    def set_status(self, session_id: str, status: str, failure_code: str | None = None) -> BotSession:
        if status not in {"joining", "waiting_admission", "recording", "finalizing", "interrupted", "cancelled"}:
            raise ValueError("Invalid bot status")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(session_id)
            if current is None or current.status in {"completed", "cancelled"}:
                raise BotStateConflict("Bot session is no longer active")
            ended_at = _now() if status in {"interrupted", "cancelled"} else None
            self.connection.execute(
                """UPDATE bot_sessions SET status=?, failure_code=?, ended_at=COALESCE(?, ended_at),
                   url=CASE WHEN ?='cancelled' THEN NULL ELSE url END WHERE id=?""",
                (status, failure_code, ended_at, status, session_id),
            )
            self.connection.commit()
            return self.get(session_id)  # type: ignore[return-value]
        except BaseException:
            self.connection.rollback()
            raise

    def add_part(self, session_id: str, storage_key: str, start_seconds: float, duration_seconds: float) -> BotPart:
        if start_seconds < 0 or not 0 < duration_seconds <= 1800:
            raise ValueError("Invalid recorded part timing")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            session = self.get(session_id)
            if session is None or session.status != "recording":
                raise BotStateConflict("Bot is not recording")
            ordinal = len(self.parts(session_id))
            self.connection.execute(
                "INSERT INTO bot_session_parts VALUES (?, ?, ?, ?, ?)",
                (session_id, ordinal, storage_key, start_seconds, duration_seconds),
            )
            self.connection.commit()
            return BotPart(session_id, ordinal, storage_key, start_seconds, duration_seconds)
        except BaseException:
            self.connection.rollback()
            raise

    def _checksum(self, parts: list[BotPart]) -> str:
        data_dir = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).parent
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
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            session = self.get(session_id)
            if session is None:
                raise BotStateConflict("Bot session not found")
            if session.meeting_id:
                self.connection.commit()
                return MeetingRepository(self.connection).get_meeting(session.meeting_id)  # type: ignore[return-value]
            if session.status not in ({"interrupted"} if partial else {"recording", "finalizing"}):
                raise BotStateConflict("Bot session cannot be finalized")
            parts = self.parts(session_id)
            if not parts:
                raise BotStateConflict("Bot session has no recorded audio")
            checksum = self._checksum(parts)
            meeting_id = uuid.uuid4().hex
            now = _now()
            first_key = parts[0].storage_key
            self.connection.execute(
                """INSERT INTO meetings
                   (id, title, original_filename, storage_key, checksum, created_at, updated_at, status, source)
                   VALUES (?, ?, 'recording.wav', ?, ?, ?, ?, 'queued', 'google_meet_bot')""",
                (meeting_id, session.title, first_key, checksum, now, now),
            )
            for part in parts:
                self.connection.execute(
                    """INSERT INTO meeting_audio_files
                       (meeting_id, ordinal, original_filename, storage_key, duration_seconds, start_seconds)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (meeting_id, part.ordinal, f"part-{part.ordinal + 1}.wav", part.storage_key,
                     part.duration_seconds, part.start_seconds),
                )
            self.connection.execute("DELETE FROM bot_session_parts WHERE session_id=?", (session_id,))
            self.connection.execute(
                """UPDATE bot_sessions SET status='completed', url=NULL, meeting_id=?, ended_at=?,
                   capture_incomplete=?, failure_code=NULL WHERE id=?""",
                (meeting_id, now, int(partial), session_id),
            )
            self.connection.commit()
            return MeetingRepository(self.connection).get_meeting(meeting_id)  # type: ignore[return-value]
        except BaseException:
            self.connection.rollback()
            raise

    def discard(self, session_id: str) -> tuple[str, ...]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            session = self.get(session_id)
            if session is None or session.meeting_id or session.status not in {"queued", "interrupted", "cancelled"}:
                raise BotStateConflict("Bot session cannot be discarded")
            keys = tuple(part.storage_key for part in self.parts(session_id))
            self.connection.execute("DELETE FROM bot_session_parts WHERE session_id=?", (session_id,))
            self.connection.execute(
                "UPDATE bot_sessions SET status='cancelled', url=NULL, ended_at=? WHERE id=?", (_now(), session_id)
            )
            self.connection.commit()
            return keys
        except BaseException:
            self.connection.rollback()
            raise

    def recover_stale(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE bot_sessions SET status='interrupted', failure_code='runner_interrupted', ended_at=?
                   WHERE status IN ('joining','waiting_admission','recording','finalizing')""", (_now(),)
            )
        return cursor.rowcount
