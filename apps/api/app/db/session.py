"""SQLite setup for the local meeting library."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _create_meetings_table(connection: sqlite3.Connection, table_name: str) -> None:
    connection.execute(
        f"""CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            title TEXT,
            original_filename TEXT NOT NULL,
            storage_key TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL,
            failure_code TEXT,
            transcript TEXT,
            pipeline_pid INTEGER,
            pipeline_start_ticks INTEGER,
            cancelled_at TEXT
            ,source TEXT NOT NULL DEFAULT 'upload'
        )"""
    )


def _migrate_meetings_table(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings)")}
    required_columns = {"pipeline_pid", "pipeline_start_ticks", "cancelled_at"}
    if required_columns.issubset(columns):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            _create_meetings_table(connection, "meetings_migrated")
            connection.execute(
                """INSERT INTO meetings_migrated
                (id, title, original_filename, storage_key, checksum, idempotency_key,
                 created_at, updated_at, status, attempt_count, duration_seconds, failure_code, transcript)
                SELECT id, title, original_filename, storage_key, checksum, idempotency_key,
                       created_at, updated_at, status, attempt_count, duration_seconds, failure_code, transcript
                FROM meetings"""
            )
            connection.execute("DROP TABLE meetings")
            connection.execute("ALTER TABLE meetings_migrated RENAME TO meetings")
            connection.execute("CREATE INDEX IF NOT EXISTS meetings_created ON meetings(created_at DESC, id DESC)")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def open_meeting_db(data_dir: Path) -> sqlite3.Connection:
    """Open one configured connection to the local meeting database."""
    connection = sqlite3.connect(data_dir / "meetings.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_meeting_store(data_dir: Path) -> None:
    """Create private storage directories and the SQLite schema."""
    for directory in (data_dir, data_dir / "audio", data_dir / "tmp"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    connection = open_meeting_db(data_dir)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT,
                original_filename TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL,
                failure_code TEXT,
                transcript TEXT,
                pipeline_pid INTEGER,
                pipeline_start_ticks INTEGER,
                cancelled_at TEXT
                ,source TEXT NOT NULL DEFAULT 'upload'
            );
            CREATE TABLE IF NOT EXISTS vad_segments (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                PRIMARY KEY (meeting_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS diarization_segments (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                speaker TEXT NOT NULL,
                PRIMARY KEY (meeting_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                UNIQUE (meeting_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS meeting_audio_files (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                duration_seconds REAL,
                start_seconds REAL,
                PRIMARY KEY (meeting_id, ordinal)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                meeting_id UNINDEXED, segment_id UNINDEXED, text, tokenize='unicode61'
            );
            CREATE INDEX IF NOT EXISTS meetings_created ON meetings(created_at DESC, id DESC);
            """
        )
        _migrate_meetings_table(connection)
        meeting_columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings)")}
        if "source" not in meeting_columns:
            connection.execute("ALTER TABLE meetings ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'")
        audio_columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_audio_files)")}
        if "start_seconds" not in audio_columns:
            connection.execute("ALTER TABLE meeting_audio_files ADD COLUMN start_seconds REAL")
        connection.execute(
            """INSERT OR IGNORE INTO meeting_audio_files (meeting_id, ordinal, original_filename, storage_key)
               SELECT id, 0, original_filename, storage_key FROM meetings WHERE status != 'cancelled'"""
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(data_dir / "meetings.sqlite3", 0o600)
